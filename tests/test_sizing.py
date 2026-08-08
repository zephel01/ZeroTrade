"""PositionSizer のテスト。"""

from __future__ import annotations

from decimal import Decimal

from zerotrade.core.sizing import PositionSizer
from zerotrade.settings import RiskSettings, SizingSettings


def test_リスク率から数量を逆算する(
    sizing_settings: SizingSettings, risk_settings: RiskSettings
) -> None:
    sizer = PositionSizer(sizing_settings, risk_settings)
    # equity 100万 × 1% = 10,000円 ÷ ストップ距離 1.00円 = 10,000通貨
    result = sizer.calculate(
        equity=Decimal(1_000_000),
        entry_price=Decimal("150.00"),
        stop_loss=Decimal("149.00"),
    )
    assert result.quantity == Decimal(10_000)
    assert result.risk_amount == Decimal(10_000)


def test_ストップが近いほど数量は増える(
    sizing_settings: SizingSettings, risk_settings: RiskSettings
) -> None:
    sizer = PositionSizer(sizing_settings, risk_settings)
    tight = sizer.calculate(
        equity=Decimal(1_000_000),
        entry_price=Decimal("150.00"),
        stop_loss=Decimal("149.50"),
    )
    wide = sizer.calculate(
        equity=Decimal(1_000_000),
        entry_price=Decimal("150.00"),
        stop_loss=Decimal("148.00"),
    )
    assert tight.quantity > wide.quantity
    # どちらもリスク額は上限（10,000円）を超えない。
    assert tight.risk_amount <= Decimal(10_000)
    assert wide.risk_amount <= Decimal(10_000)


def test_発注単位へ切り捨てられる(risk_settings: RiskSettings) -> None:
    settings = SizingSettings(
        min_quantity=Decimal(1000), quantity_step=Decimal(1000), contract_size=Decimal(1)
    )
    sizer = PositionSizer(settings, risk_settings)
    # 10,000 / 1.30 = 7,692.3... → 7,000 へ切り捨て
    result = sizer.calculate(
        equity=Decimal(1_000_000),
        entry_price=Decimal("150.00"),
        stop_loss=Decimal("148.70"),
    )
    assert result.quantity == Decimal(7_000)
    assert result.risk_amount < Decimal(10_000), "切り捨てによりリスクは上限未満になる"


def test_最小単位に満たなければ発注しない(
    sizing_settings: SizingSettings, risk_settings: RiskSettings
) -> None:
    sizer = PositionSizer(sizing_settings, risk_settings)
    # equity 5万 × 1% = 500円 ÷ 1.00円 = 500通貨 < 最小1000通貨
    result = sizer.calculate(
        equity=Decimal(50_000),
        entry_price=Decimal("150.00"),
        stop_loss=Decimal("149.00"),
    )
    assert result.quantity == 0
    assert not result
    assert "最小単位" in result.reason


def test_ストップ無しは計算できない(
    sizing_settings: SizingSettings, risk_settings: RiskSettings
) -> None:
    sizer = PositionSizer(sizing_settings, risk_settings)
    result = sizer.calculate(
        equity=Decimal(1_000_000), entry_price=Decimal("150.00"), stop_loss=None
    )
    assert result.quantity == 0


def test_エントリーとストップが同値なら発注しない(
    sizing_settings: SizingSettings, risk_settings: RiskSettings
) -> None:
    sizer = PositionSizer(sizing_settings, risk_settings)
    result = sizer.calculate(
        equity=Decimal(1_000_000),
        entry_price=Decimal("150.00"),
        stop_loss=Decimal("150.00"),
    )
    assert result.quantity == 0


def test_呼び出し側の上限が優先される(
    sizing_settings: SizingSettings, risk_settings: RiskSettings
) -> None:
    sizer = PositionSizer(sizing_settings, risk_settings)
    result = sizer.calculate(
        equity=Decimal(1_000_000),
        entry_price=Decimal("150.00"),
        stop_loss=Decimal("149.00"),
        max_quantity=Decimal(3_000),
    )
    assert result.quantity == Decimal(3_000)


def test_固定ロット方式(risk_settings: RiskSettings) -> None:
    settings = SizingSettings(
        method="fixed_quantity",
        fixed_quantity=Decimal(5_000),
        min_quantity=Decimal(1000),
        quantity_step=Decimal(1000),
    )
    sizer = PositionSizer(settings, risk_settings)
    result = sizer.calculate(
        equity=Decimal(1_000_000), entry_price=Decimal("150.00"), stop_loss=None
    )
    assert result.quantity == Decimal(5_000)


def test_equityがゼロなら発注しない(
    sizing_settings: SizingSettings, risk_settings: RiskSettings
) -> None:
    sizer = PositionSizer(sizing_settings, risk_settings)
    result = sizer.calculate(
        equity=Decimal(0), entry_price=Decimal("150.00"), stop_loss=Decimal("149.00")
    )
    assert result.quantity == 0
