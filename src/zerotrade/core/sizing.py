"""ポジションサイズの決定。

基本は fixed fractional 方式:

    数量 = (口座equity × 1トレードリスク率) ÷ (ストップまでの距離 × contract_size)

丸めは必ず **切り捨て**。切り上げると計算上のリスク率を超えてしまい、
RiskManager に弾かれるか、最悪の場合そのまま許容超過のポジションを持つことになる。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from zerotrade.models import to_decimal
from zerotrade.settings import RiskSettings, SizingSettings

__all__ = ["PositionSizer", "SizingResult"]


@dataclass(frozen=True, slots=True)
class SizingResult:
    """サイズ計算の結果。

    ``quantity`` が 0 の場合は「発注しない」を意味する。
    その理由は ``reason`` に入る。
    """

    quantity: Decimal
    risk_amount: Decimal
    """このサイズで損切りに掛かった場合の想定損失額（口座通貨建て）。"""

    reason: str = ""

    def __bool__(self) -> bool:
        return self.quantity > 0


class PositionSizer:
    """設定に基づいて発注数量を決める。"""

    def __init__(self, sizing: SizingSettings, risk: RiskSettings) -> None:
        self._sizing = sizing
        self._risk = risk

    def calculate(
        self,
        *,
        equity: Decimal,
        entry_price: Decimal,
        stop_loss: Decimal | None,
        max_quantity: Decimal | None = None,
    ) -> SizingResult:
        """発注数量を計算する。

        Args:
            equity: 口座の有効証拠金。
            entry_price: 想定約定価格。
            stop_loss: 損切り価格。``fixed_fractional`` では必須。
            max_quantity: 呼び出し側が課す追加の上限（余力など）。

        Returns:
            数量と想定損失額。発注すべきでない場合 ``quantity`` は 0。
        """
        if equity <= 0:
            return SizingResult(Decimal(0), Decimal(0), "equity が 0 以下です")

        if self._sizing.method == "fixed_quantity":
            raw = self._sizing.fixed_quantity
        else:
            if stop_loss is None:
                return SizingResult(
                    Decimal(0), Decimal(0), "fixed_fractional にはストップ価格が必要です"
                )
            stop_distance = abs(entry_price - stop_loss)
            if stop_distance <= 0:
                return SizingResult(
                    Decimal(0), Decimal(0), "エントリー価格とストップ価格が同一です"
                )
            budget = equity * self._risk.max_risk_per_trade
            raw = budget / (stop_distance * self._sizing.contract_size)

        quantity = self._clamp_and_round(raw, max_quantity)

        if quantity <= 0:
            return SizingResult(
                Decimal(0),
                Decimal(0),
                f"計算サイズ {raw} が最小単位 {self._sizing.min_quantity} を下回りました",
            )

        risk_amount = (
            abs(entry_price - stop_loss) * quantity * self._sizing.contract_size
            if stop_loss is not None
            else Decimal(0)
        )
        return SizingResult(quantity, risk_amount)

    # ------------------------------------------------------------------

    def _clamp_and_round(self, raw: Decimal, max_quantity: Decimal | None) -> Decimal:
        """上限で切り、発注単位に切り捨てで丸める。"""
        limits = [q for q in (self._sizing.max_quantity, max_quantity) if q is not None]
        for limit in limits:
            raw = min(raw, to_decimal(limit))

        step = self._sizing.quantity_step
        # 切り捨て: step の整数倍のうち raw を超えない最大値。
        quantity = (raw / step).to_integral_value(rounding="ROUND_FLOOR") * step

        if quantity < self._sizing.min_quantity:
            return Decimal(0)
        return quantity
