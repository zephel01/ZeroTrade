"""SMAクロス + RSIフィルタ + ATRストップの基本戦略。

初期実装として意図的に単純にしてある。狙いは「稼ぐこと」より
**システム全体（シグナル → サイズ決定 → リスク検査 → 発注 → 決済）が
一周することを確認できる最小の戦略** を用意すること。

ロジック:

* 短期SMAが長期SMAを上抜け、かつRSIが買われすぎでない → ロング
* 短期SMAが長期SMAを下抜け、かつRSIが売られすぎでない → ショート
* 保有中に逆方向のクロスが出たら決済
* ストップ・利確は ATR の倍数で置く（値幅ベースなので銘柄に依存しにくい）
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from zerotrade.models import Side, Signal, SignalAction, to_decimal
from zerotrade.strategies.base import Strategy, StrategyContext, register_strategy
from zerotrade.strategies.indicators import atr, rsi, sma

__all__ = ["SmaRsiStrategy"]


@register_strategy
class SmaRsiStrategy(Strategy):
    """短期・長期SMAのクロスをRSIとATRで補強した戦略。"""

    name = "sma_rsi"

    def __init__(
        self,
        *,
        fast_period: int = 20,
        slow_period: int = 50,
        rsi_period: int = 14,
        rsi_overbought: float | Decimal = 70,
        rsi_oversold: float | Decimal = 30,
        atr_period: int = 14,
        atr_stop_multiplier: float | Decimal = 2,
        atr_target_multiplier: float | Decimal = 3,
        allow_short: bool = True,
        **extra: Any,
    ) -> None:
        if extra:
            raise ValueError(f"未知のパラメータです: {', '.join(sorted(extra))}")
        if fast_period >= slow_period:
            raise ValueError("fast_period は slow_period より小さくしてください")
        if min(fast_period, slow_period, rsi_period, atr_period) < 2:
            raise ValueError("各期間は 2 以上にしてください")

        super().__init__(
            fast_period=fast_period,
            slow_period=slow_period,
            rsi_period=rsi_period,
            rsi_overbought=rsi_overbought,
            rsi_oversold=rsi_oversold,
            atr_period=atr_period,
            atr_stop_multiplier=atr_stop_multiplier,
            atr_target_multiplier=atr_target_multiplier,
            allow_short=allow_short,
        )

        self.fast_period = fast_period
        self.slow_period = slow_period
        self.rsi_period = rsi_period
        self.rsi_overbought = to_decimal(rsi_overbought)
        self.rsi_oversold = to_decimal(rsi_oversold)
        self.atr_period = atr_period
        self.atr_stop_multiplier = to_decimal(atr_stop_multiplier)
        self.atr_target_multiplier = to_decimal(atr_target_multiplier)
        self.allow_short = allow_short

        # クロス判定に1本前のSMAが要るので +1、RSI/ATRは差分計算で +1。
        self.warmup_bars = max(slow_period, rsi_period, atr_period) + 2

    # ------------------------------------------------------------------

    def generate(self, context: StrategyContext) -> Signal:
        closes = context.closes
        if len(closes) < self.warmup_bars:
            return self.hold(context, f"ウォームアップ中（{len(closes)}/{self.warmup_bars} 本）")

        fast_now = sma(closes, self.fast_period)
        slow_now = sma(closes, self.slow_period)
        fast_prev = sma(closes[:-1], self.fast_period)
        slow_prev = sma(closes[:-1], self.slow_period)
        current_rsi = rsi(closes, self.rsi_period)

        if None in (fast_now, slow_now, fast_prev, slow_prev, current_rsi):
            return self.hold(context, "指標の計算に必要なデータが不足しています")

        # mypy 向けの絞り込み。上の None チェックで実質保証されている。
        assert fast_now is not None and slow_now is not None
        assert fast_prev is not None and slow_prev is not None
        assert current_rsi is not None

        crossed_up = fast_prev <= slow_prev and fast_now > slow_now
        crossed_down = fast_prev >= slow_prev and fast_now < slow_now

        # --- 決済判定を先に行う（ドテンではなく一度フラットに戻す） ---
        position = context.position
        if position is not None:
            if position.side is Side.BUY and crossed_down:
                return self._signal(context, SignalAction.EXIT, "上昇トレンド終了（デッドクロス）")
            if position.side is Side.SELL and crossed_up:
                return self._signal(
                    context, SignalAction.EXIT, "下降トレンド終了（ゴールデンクロス）"
                )
            return self.hold(context, "保有継続")

        # --- 新規エントリー ---
        current_atr = atr(context.candles, self.atr_period)
        if current_atr is None or current_atr <= 0:
            return self.hold(context, "ATRが計算できないためストップを置けません")

        if crossed_up and current_rsi < self.rsi_overbought:
            return self._entry(
                context,
                SignalAction.ENTER_LONG,
                current_atr,
                f"ゴールデンクロス（RSI {current_rsi:.1f}）",
            )

        if crossed_down and self.allow_short and current_rsi > self.rsi_oversold:
            return self._entry(
                context,
                SignalAction.ENTER_SHORT,
                current_atr,
                f"デッドクロス（RSI {current_rsi:.1f}）",
            )

        if crossed_up:
            return self.hold(context, f"ゴールデンクロスだがRSIが買われすぎ（{current_rsi:.1f}）")
        if crossed_down and not self.allow_short:
            return self.hold(context, "デッドクロスだがショートは無効")
        if crossed_down:
            return self.hold(context, f"デッドクロスだがRSIが売られすぎ（{current_rsi:.1f}）")

        return self.hold(context, "クロス無し")

    # ------------------------------------------------------------------

    def _entry(
        self,
        context: StrategyContext,
        action: SignalAction,
        current_atr: Decimal,
        reason: str,
    ) -> Signal:
        """ATR ベースのストップ・利確を付けたエントリーシグナルを作る。"""
        price = context.last_close
        assert price is not None  # warmup チェック済み
        stop_distance = current_atr * self.atr_stop_multiplier
        target_distance = current_atr * self.atr_target_multiplier

        if action is SignalAction.ENTER_LONG:
            stop_loss = price - stop_distance
            take_profit = price + target_distance
        else:
            stop_loss = price + stop_distance
            take_profit = price - target_distance

        return Signal(
            symbol=context.symbol,
            action=action,
            strategy=self.name,
            stop_loss=stop_loss,
            take_profit=take_profit,
            reason=reason,
            metadata={"atr": str(current_atr)},
        )

    def _signal(self, context: StrategyContext, action: SignalAction, reason: str) -> Signal:
        return Signal(
            symbol=context.symbol,
            action=action,
            strategy=self.name,
            reason=reason,
        )
