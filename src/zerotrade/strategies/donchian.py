"""ドンチャン・ブレイクアウト戦略。

実データ（USD/JPY 2024年・1時間足）でドリフトを差し引いて測ったところ、
わずかな順張りの偏りが見えたのが「N本高値のブレイク」だった。
SMAのクロスには何も見えなかったので、見えた方向に沿って組み直したのがこれである。

`sma_rsi` との構造的な違いは3点ある。

**利確を置かない。** 順張りは少数の大きな勝ちで多数の小さな負けを賄う構造で、
利確を置くとその「少数の大きな勝ち」を自分で切ってしまう。代わりに
シャンデリア・エグジット（高値からATRの倍数を引いた位置）でストップを追随させる。

**長期トレンドに逆らわない。** 長期移動平均より上でしか買わず、下でしか売らない。
ブレイクアウトのダマシは逆行局面に集中するため。

**エントリーと決済で期間を分ける。** 入るのは長い期間の高値更新、
出るのは短い期間の安値更新。入りは慎重に、出は素早く。

なお **この戦略にエッジがあると確認できたわけではない。** 元にした偏りは
t=2.09 で、18通り試したうちの1つである。多重検定を考えれば偶然の範囲に収まる。
構造として順張りの型を持たせただけで、有効性の判定には複数年のデータが要る。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from zerotrade.models import Candle, Side, Signal, SignalAction, to_decimal
from zerotrade.strategies.base import Strategy, StrategyContext, register_strategy
from zerotrade.strategies.indicators import atr, sma

__all__ = ["DonchianStrategy"]


@register_strategy
class DonchianStrategy(Strategy):
    """N本高安のブレイクで入り、ATRトレーリングストップで出る。"""

    name = "donchian"

    def __init__(
        self,
        *,
        entry_period: int = 20,
        exit_period: int = 10,
        trend_period: int = 100,
        atr_period: int = 14,
        atr_stop_multiplier: float | Decimal = 3,
        atr_trail_multiplier: float | Decimal = 3,
        use_trend_filter: bool = True,
        allow_short: bool = True,
        min_hour_utc: int | None = None,
        max_hour_utc: int | None = None,
        **extra: Any,
    ) -> None:
        if extra:
            raise ValueError(f"未知のパラメータです: {', '.join(sorted(extra))}")
        if entry_period < 2 or exit_period < 2 or atr_period < 2:
            raise ValueError("各期間は 2 以上にしてください")
        if trend_period < 2:
            raise ValueError("trend_period は 2 以上にしてください")

        super().__init__(
            entry_period=entry_period,
            exit_period=exit_period,
            trend_period=trend_period,
            atr_period=atr_period,
            atr_stop_multiplier=atr_stop_multiplier,
            atr_trail_multiplier=atr_trail_multiplier,
            use_trend_filter=use_trend_filter,
            allow_short=allow_short,
            min_hour_utc=min_hour_utc,
            max_hour_utc=max_hour_utc,
        )

        self.entry_period = entry_period
        self.exit_period = exit_period
        self.trend_period = trend_period
        self.atr_period = atr_period
        self.atr_stop_multiplier = to_decimal(atr_stop_multiplier)
        self.atr_trail_multiplier = to_decimal(atr_trail_multiplier)
        self.use_trend_filter = use_trend_filter
        self.allow_short = allow_short
        self.min_hour_utc = min_hour_utc
        self.max_hour_utc = max_hour_utc

        needed = [entry_period, exit_period, atr_period]
        if use_trend_filter:
            needed.append(trend_period)
        self.warmup_bars = max(needed) + 2

    # ------------------------------------------------------------------

    def generate(self, context: StrategyContext) -> Signal:
        candles = list(context.candles)
        if len(candles) < self.warmup_bars:
            return self.hold(context, f"ウォームアップ中（{len(candles)}/{self.warmup_bars} 本）")

        current = candles[-1]
        current_atr = atr(candles, self.atr_period)
        if current_atr is None or current_atr <= 0:
            return self.hold(context, "ATRが計算できないためストップを置けません")

        position = context.position
        if position is not None:
            return self._manage(context, candles, current_atr)

        if not self._within_session(current):
            return self.hold(context, "取引時間帯の外")

        # 直前の足までの高安と比べる。当日足を含めると必ず自分自身が最大になる。
        window = candles[-self.entry_period - 1 : -1]
        highest = max(c.high for c in window)
        lowest = min(c.low for c in window)

        trend = sma([c.close for c in candles], self.trend_period)
        if self.use_trend_filter and trend is None:
            return self.hold(context, "トレンド判定に必要なデータが不足しています")

        if current.close > highest:
            if self.use_trend_filter and trend is not None and current.close < trend:
                return self.hold(context, "高値ブレイクだが長期トレンドは下向き")
            return self._entry(
                context,
                SignalAction.ENTER_LONG,
                current.close,
                current_atr,
                f"{self.entry_period}本高値を上抜け",
            )

        if current.close < lowest:
            if not self.allow_short:
                return self.hold(context, "安値ブレイクだがショートは無効")
            if self.use_trend_filter and trend is not None and current.close > trend:
                return self.hold(context, "安値ブレイクだが長期トレンドは上向き")
            return self._entry(
                context,
                SignalAction.ENTER_SHORT,
                current.close,
                current_atr,
                f"{self.entry_period}本安値を下抜け",
            )

        return self.hold(context, "ブレイク無し")

    # ------------------------------------------------------------------

    def _manage(
        self, context: StrategyContext, candles: list[Candle], current_atr: Decimal
    ) -> Signal:
        """保有中の建玉を決済するか、ストップを追随させるか決める。"""
        position = context.position
        assert position is not None
        current = candles[-1]

        # 決済側のブレイク（入りより短い期間）で素早く降りる。
        window = candles[-self.exit_period - 1 : -1]
        if position.side is Side.BUY and current.close < min(c.low for c in window):
            return self._plain(context, SignalAction.EXIT, f"{self.exit_period}本安値を下抜け")
        if position.side is Side.SELL and current.close > max(c.high for c in window):
            return self._plain(context, SignalAction.EXIT, f"{self.exit_period}本高値を上抜け")

        # シャンデリア・エグジット。建玉を持ってからの極値を基準に引き上げる。
        since = [c for c in candles if c.timestamp >= position.opened_at] or [current]
        distance = current_atr * self.atr_trail_multiplier
        if position.side is Side.BUY:
            trailing = max(c.high for c in since) - distance
            improved = position.stop_loss is None or trailing > position.stop_loss
        else:
            trailing = min(c.low for c in since) + distance
            improved = position.stop_loss is None or trailing < position.stop_loss

        if improved:
            return Signal(
                symbol=context.symbol,
                action=SignalAction.UPDATE_STOP,
                strategy=self.name,
                stop_loss=trailing,
                reason="トレーリングストップを引き上げ",
            )

        return self.hold(context, "保有継続")

    def _within_session(self, candle: Candle) -> bool:
        """取引を許す時間帯か（UTC時）。

        欧米の時間帯だけに絞りたい場合に使う。東京時間は値動きが乏しく、
        ブレイクアウトのダマシが増えやすい。
        """
        if self.min_hour_utc is None and self.max_hour_utc is None:
            return True
        hour = candle.timestamp.hour
        low = self.min_hour_utc if self.min_hour_utc is not None else 0
        high = self.max_hour_utc if self.max_hour_utc is not None else 23
        if low <= high:
            return low <= hour <= high
        # 日をまたぐ指定（例: 22時〜翌6時）。
        return hour >= low or hour <= high

    def _entry(
        self,
        context: StrategyContext,
        action: SignalAction,
        price: Decimal,
        current_atr: Decimal,
        reason: str,
    ) -> Signal:
        """利確を置かないエントリー。伸びる余地を残す。"""
        distance = current_atr * self.atr_stop_multiplier
        stop = price - distance if action is SignalAction.ENTER_LONG else price + distance
        return Signal(
            symbol=context.symbol,
            action=action,
            strategy=self.name,
            stop_loss=stop,
            take_profit=None,
            reason=reason,
            metadata={"atr": str(current_atr)},
        )

    def _plain(self, context: StrategyContext, action: SignalAction, reason: str) -> Signal:
        return Signal(symbol=context.symbol, action=action, strategy=self.name, reason=reason)
