"""東京仲値（TTM）に向けたフローを狙う戦略。

**この戦略だけは、データを見る前に理屈がある。**

日本の輸入企業は決済のために日々ドルを買う必要があり、その基準となるのが
各行が午前9時55分ごろに決定する仲値（TTM）である。彼らは価格が有利か
不利かで判断しておらず、必要だから買う。つまり **儲けようとしていない
参加者による、時刻の決まった一方向のフロー** がそこにある。
歪みが残るとすれば、こういう場所である。

他のアノマリー探索と決定的に違うのは、**当てる時刻を先に決めてから測った**
という点である。総当たりで見つけた時間帯は多重検定のペナルティを払うが、
事前に指定した仮説はそれを免れる。

実測（USD/JPY・独立した2期間）:

===================  ===============  ==============
期間                 8:00→9:55 平均   t値
===================  ===============  ==============
2024（HistData）      +0.01752%        +1.85
2025-2026（MT4）      +0.01315%        +1.58
2期間プール            +0.01543%        +2.43
===================  ===============  ==============

同じ長さの窓を24通り試した中で、**両期間とも同じ符号で残ったのはこの窓が最良**
だった（他の高t値の時間帯は期間をまたぐと符号が反転した）。

ただし **エッジが証明されたわけではない。** 効果は1日あたり1.5ベーシス
ポイントと小さく、2年ぶんの、しかもどちらもドル円の上昇局面のデータしかない。
「五十日（ごとおび）に効果が強い」という通説は2024年では見えたが（勝率67%）、
2025-2026年では消えた（勝率48.9%）ので、この実装では採用していない。
"""

from __future__ import annotations

from datetime import time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from zerotrade.models import Candle, Side, Signal, SignalAction, to_decimal
from zerotrade.strategies.base import Strategy, StrategyContext, register_strategy
from zerotrade.strategies.indicators import atr

__all__ = ["TokyoFixStrategy"]


def _parse_time(value: str) -> time:
    """``"08:00"`` を :class:`~datetime.time` にする。"""
    try:
        hour, _, minute = value.partition(":")
        return time(int(hour), int(minute))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"時刻の形式が不正です: {value!r}（例: 08:00）") from exc


@register_strategy
class TokyoFixStrategy(Strategy):
    """仲値決定に向けた時間帯だけ買い、決定後に手仕舞う。"""

    name = "tokyo_fix"

    def __init__(
        self,
        *,
        entry_time: str = "08:00",
        exit_time: str = "09:55",
        timezone: str = "Asia/Tokyo",
        atr_period: int = 14,
        atr_stop_multiplier: float | Decimal = 2,
        side: str = "buy",
        **extra: Any,
    ) -> None:
        if extra:
            raise ValueError(f"未知のパラメータです: {', '.join(sorted(extra))}")
        if atr_period < 2:
            raise ValueError("atr_period は 2 以上にしてください")
        if side not in ("buy", "sell"):
            raise ValueError("side は buy か sell を指定してください")

        super().__init__(
            entry_time=entry_time,
            exit_time=exit_time,
            timezone=timezone,
            atr_period=atr_period,
            atr_stop_multiplier=atr_stop_multiplier,
            side=side,
        )

        self.entry_time = _parse_time(entry_time)
        self.exit_time = _parse_time(exit_time)
        if self.entry_time >= self.exit_time:
            raise ValueError("entry_time は exit_time より前にしてください")

        try:
            self.zone = ZoneInfo(timezone)
        except Exception as exc:
            raise ValueError(f"未知のタイムゾーンです: {timezone}") from exc

        self.atr_period = atr_period
        self.atr_stop_multiplier = to_decimal(atr_stop_multiplier)
        self.side = Side.BUY if side == "buy" else Side.SELL

        self.warmup_bars = atr_period + 2

    # ------------------------------------------------------------------

    def generate(self, context: StrategyContext) -> Signal:
        candles = list(context.candles)
        if len(candles) < self.warmup_bars:
            return self.hold(context, f"ウォームアップ中（{len(candles)}/{self.warmup_bars} 本）")

        current = candles[-1]
        local = current.timestamp.astimezone(self.zone).time()
        position = context.position

        # --- 手仕舞い: 仲値が決まったら理由が消える ---------------------
        if position is not None:
            if local >= self.exit_time or local < self.entry_time:
                return Signal(
                    symbol=context.symbol,
                    action=SignalAction.EXIT,
                    strategy=self.name,
                    reason=f"仲値時刻（{self.exit_time:%H:%M}）を通過",
                )
            return self.hold(context, "仲値待ち")

        # --- エントリー: 窓の開始ちょうどの足でだけ入る -------------------
        if not self._is_entry_bar(candles):
            return self.hold(context, "エントリー時刻の外")

        current_atr = atr(candles, self.atr_period)
        if current_atr is None or current_atr <= 0:
            return self.hold(context, "ATRが計算できないためストップを置けません")

        distance = current_atr * self.atr_stop_multiplier
        if self.side is Side.BUY:
            action, stop = SignalAction.ENTER_LONG, current.close - distance
        else:
            action, stop = SignalAction.ENTER_SHORT, current.close + distance

        return Signal(
            symbol=context.symbol,
            action=action,
            strategy=self.name,
            stop_loss=stop,
            take_profit=None,
            reason=f"仲値（{self.exit_time:%H:%M}）に向けた実需フロー",
            metadata={"atr": str(current_atr)},
        )

    # ------------------------------------------------------------------

    def _is_entry_bar(self, candles: list[Candle]) -> bool:
        """この足が窓の開始を最初にまたいだ足か。

        足の刻みが窓の開始時刻とぴったり合わない場合（15分足で 08:05 開始など）
        でも取りこぼさないよう、「前の足は窓の前、この足は窓の中」で判定する。
        """
        current = candles[-1].timestamp.astimezone(self.zone)
        if not (self.entry_time <= current.time() < self.exit_time):
            return False

        previous = candles[-2].timestamp.astimezone(self.zone)
        # 日付が変わっていれば当然またいでいる。
        if previous.date() != current.date():
            return True
        return not (self.entry_time <= previous.time() < self.exit_time)
