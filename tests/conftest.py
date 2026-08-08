"""テスト共通のフィクスチャ。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from zerotrade.core.risk import RiskManager
from zerotrade.models import Balance, Candle, OrderRequest, Position, Side, Ticker
from zerotrade.settings import RiskSettings, Settings, SizingSettings

START = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)  # 月曜


@pytest.fixture
def risk_settings() -> RiskSettings:
    return RiskSettings(
        max_risk_per_trade=Decimal("0.01"),
        max_daily_loss=Decimal("0.03"),
        max_weekly_loss=Decimal("0.06"),
        max_margin_usage=Decimal("0.30"),
        max_open_positions=3,
        max_positions_per_symbol=1,
        max_daily_trades=20,
        require_stop_loss=True,
        assumed_leverage=Decimal(25),
    )


@pytest.fixture
def sizing_settings() -> SizingSettings:
    return SizingSettings(
        min_quantity=Decimal(1000),
        quantity_step=Decimal(1000),
        contract_size=Decimal(1),
    )


class FakeClock:
    """テストから任意に進められる時計。"""

    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def risk(risk_settings: RiskSettings, clock: FakeClock) -> RiskManager:
    manager = RiskManager(risk_settings, clock=clock)
    manager.set_reference_equity(Decimal(1_000_000))
    return manager


@pytest.fixture
def balance() -> Balance:
    """equity 100万円、余力満額、証拠金未使用。"""
    return Balance(
        currency="JPY",
        equity=Decimal(1_000_000),
        available=Decimal(1_000_000),
        used_margin=Decimal(0),
    )


@pytest.fixture
def ticker() -> Ticker:
    return Ticker(symbol="USD_JPY", bid=Decimal("150.00"), ask=Decimal("150.02"))


def make_request(
    *,
    symbol: str = "USD_JPY",
    side: Side = Side.BUY,
    quantity: Decimal = Decimal(1000),
    stop_loss: Decimal | None = Decimal("149.00"),
    **kwargs: Any,
) -> OrderRequest:
    """テスト用の注文リクエスト。既定はリスク 1000円（equity の 0.1%）。"""
    return OrderRequest(symbol=symbol, side=side, quantity=quantity, stop_loss=stop_loss, **kwargs)


def make_position(
    *,
    symbol: str = "USD_JPY",
    side: Side = Side.BUY,
    quantity: Decimal = Decimal(1000),
    entry_price: Decimal = Decimal("150.00"),
    **kwargs: Any,
) -> Position:
    return Position(symbol=symbol, side=side, quantity=quantity, entry_price=entry_price, **kwargs)


def make_candles(
    closes: list[float], *, symbol: str = "USD_JPY", spread: float = 0.1
) -> list[Candle]:
    """終値の列から、上下に一定幅のヒゲを付けた足を作る。"""
    return [
        Candle(
            symbol=symbol,
            timestamp=START + timedelta(minutes=5 * i),
            open=Decimal(str(close)),
            high=Decimal(str(close + spread)),
            low=Decimal(str(close - spread)),
            close=Decimal(str(close)),
            volume=Decimal(1000),
        )
        for i, close in enumerate(closes)
    ]


@pytest.fixture
def paper_settings() -> Settings:
    return Settings.model_validate(
        {
            "mode": "paper",
            "symbols": ["USD_JPY"],
            "poll_interval_seconds": 0.001,
            "broker": {"name": "paper", "initial_balance": "1000000"},
            "risk": {"reset_timezone": "UTC"},
            "sizing": {
                "min_quantity": "1000",
                "quantity_step": "1000",
                "contract_size": "1",
            },
            "strategy": {"name": "sma_rsi", "params": {"fast_period": 5, "slow_period": 12}},
            "notifications": {"console": False},
        }
    )
