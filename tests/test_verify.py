"""配管テスト（発注経路の検証）のテスト。

**このモジュールが検証する対象は、実弾を動かすコードである。** だから
「失敗しても建玉を残さないこと」を最優先で確かめる。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from zerotrade.brokers.base import BaseBroker
from zerotrade.errors import BrokerError, OrderRejected
from zerotrade.models import (
    Balance,
    Candle,
    ClosedTrade,
    Order,
    OrderRequest,
    OrderStatus,
    Position,
    Side,
    Ticker,
    utcnow,
)
from zerotrade.settings import Settings
from zerotrade.verify import run_verification

SYMBOL = "BTC_USDT"


class FakeExchange(BaseBroker):
    """発注経路の各段階を差し替えられる偽の取引所。"""

    name = "fake"
    supports_closed_trades = True

    def __init__(self) -> None:
        self.position: Position | None = None
        self.orders: list[OrderRequest] = []
        self.fail_on: set[str] = set()
        self.close_leaves_position = False
        self.drop_stop = False
        self.closed: list[ClosedTrade] = []

    async def connect(self) -> None:
        if "connect" in self.fail_on:
            raise BrokerError("接続できません")

    async def disconnect(self) -> None: ...

    async def get_balance(self) -> Balance:
        if "balance" in self.fail_on:
            raise BrokerError("残高が取れません")
        return Balance(
            currency="USDT",
            equity=Decimal(100),
            available=Decimal(100),
            used_margin=Decimal(0),
        )

    async def get_positions(self) -> list[Position]:
        if "positions" in self.fail_on:
            raise BrokerError("建玉が取れません")
        return [self.position] if self.position else []

    async def place_order(self, request: OrderRequest) -> Order:
        self.orders.append(request)
        if "place" in self.fail_on:
            raise OrderRejected("拒否されました")

        if request.reduce_only:
            if not self.close_leaves_position:
                self.position = None
        else:
            self.position = Position(
                symbol=request.symbol,
                side=request.side,
                quantity=request.quantity,
                entry_price=Decimal(60000),
                stop_loss=None if self.drop_stop else request.stop_loss,
            )
        return Order(
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            order_type=request.order_type,
            broker_order_id="ex-1",
            status=OrderStatus.FILLED,
            filled_quantity=request.quantity,
            average_price=Decimal(60000),
        )

    async def cancel_order(self, order_id: str) -> Order:  # pragma: no cover
        raise BrokerError("未対応")

    async def get_order(self, order_id: str) -> Order:  # pragma: no cover
        raise BrokerError("未対応")

    async def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        return []

    async def get_ticker(self, symbol: str) -> Ticker:
        if "ticker" in self.fail_on:
            raise BrokerError("気配値が取れません")
        return Ticker(symbol=symbol, bid=Decimal(60000), ask=Decimal(60001), timestamp=utcnow())

    async def get_ohlcv(
        self,
        symbol: str,
        *,
        granularity: str = "M5",
        count: int = 200,
        end: datetime | None = None,
    ) -> list[Candle]:
        return [
            Candle(
                symbol=symbol,
                timestamp=utcnow(),
                open=Decimal(60000),
                high=Decimal(60100),
                low=Decimal(59900),
                close=Decimal(60000),
                volume=Decimal(1),
            )
            for _ in range(50)
        ]

    async def get_closed_trades(self, since: datetime | None = None) -> list[ClosedTrade]:
        return self.closed


@pytest.fixture
def exchange() -> FakeExchange:
    return FakeExchange()


@pytest.fixture
def settings(exchange: FakeExchange, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setattr("zerotrade.verify.create_broker", lambda _s: exchange)
    return Settings.model_validate({"symbols": [SYMBOL], "broker": {"name": "paper"}})


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """待ち時間を潰してテストを速くする。"""
    monkeypatch.setattr("zerotrade.verify.SETTLE_WAIT_SECONDS", 0)
    monkeypatch.setattr("zerotrade.verify.SETTLE_RETRIES", 2)


def _check(report: object, name: str) -> object:
    return next(c for c in report.checks if c.name == name)  # type: ignore[attr-defined]


# ------------------------------------------------------------ 正常系


async def test_一往復して合格する(settings: Settings, exchange: FakeExchange) -> None:
    exchange.closed = [
        ClosedTrade(
            symbol=SYMBOL,
            side=Side.BUY,
            quantity=Decimal("0.0001"),
            entry_price=Decimal(60001),
            exit_price=Decimal(60000),
            realized_pnl=Decimal("-0.0001"),
            opened_at=utcnow(),
            closed_at=utcnow(),
        )
    ]
    report = await run_verification(settings, SYMBOL)

    assert report.ok, [c.line() for c in report.checks if not c.passed]
    assert not report.position_left_open


async def test_決済はreduce_onlyで送られる(settings: Settings, exchange: FakeExchange) -> None:
    """ここが False だと、決済注文が反対側の新規建てになる。"""
    await run_verification(settings, SYMBOL)
    closing = [o for o in exchange.orders if o.reduce_only]
    assert closing, "reduce_only の注文が送られていない"


async def test_新規注文にストップが付く(settings: Settings, exchange: FakeExchange) -> None:
    await run_verification(settings, SYMBOL)
    opening = next(o for o in exchange.orders if not o.reduce_only)
    assert opening.stop_loss is not None
    assert opening.stop_loss < Decimal(60000), "ストップが買値より上にある"


async def test_dry_runは発注しない(settings: Settings, exchange: FakeExchange) -> None:
    report = await run_verification(settings, SYMBOL, dry_run=True)
    assert exchange.orders == [], "--dry-run なのに発注している"
    assert report.ok


# ------------------------------------------------------------ 異常系


async def test_接続に失敗したら以降を行わない(settings: Settings, exchange: FakeExchange) -> None:
    exchange.fail_on.add("connect")
    report = await run_verification(settings, SYMBOL)

    assert not report.ok
    assert exchange.orders == [], "接続できていないのに発注している"


async def test_発注に失敗しても落ちない(settings: Settings, exchange: FakeExchange) -> None:
    exchange.fail_on.add("place")
    report = await run_verification(settings, SYMBOL)

    assert not report.ok
    assert not _check(report, "新規注文").passed  # type: ignore[attr-defined]


async def test_決済できなければ建玉残りとして報告する(
    settings: Settings, exchange: FakeExchange
) -> None:
    """**最重要。** 建玉を残したまま「終わりました」と言ってはいけない。"""
    exchange.close_leaves_position = True
    report = await run_verification(settings, SYMBOL)

    assert report.position_left_open, "建玉が残っているのに報告されていない"
    assert not report.ok


async def test_例外が出ても決済を試みる(
    settings: Settings, exchange: FakeExchange, monkeypatch: pytest.MonkeyPatch
) -> None:
    """途中で想定外の例外が出ても、建玉を置き去りにしない。"""
    original = exchange.get_closed_trades

    async def boom(*_args: object, **_kwargs: object) -> list[ClosedTrade]:
        raise RuntimeError("想定外")

    monkeypatch.setattr(exchange, "get_closed_trades", boom)
    with pytest.raises(RuntimeError):
        await run_verification(settings, SYMBOL)

    # 建玉が残っていないこと（_ensure_flat が finally で走る）
    assert exchange.position is None
    monkeypatch.setattr(exchange, "get_closed_trades", original)


async def test_既に建玉があれば警告する(settings: Settings, exchange: FakeExchange) -> None:
    exchange.position = Position(
        symbol=SYMBOL,
        side=Side.BUY,
        quantity=Decimal("0.01"),
        entry_price=Decimal(60000),
    )
    report = await run_verification(settings, SYMBOL, dry_run=True)
    assert not _check(report, "建玉照会").passed  # type: ignore[attr-defined]


async def test_残高ゼロは不合格(settings: Settings, exchange: FakeExchange) -> None:
    """残高が無ければ発注は通らない。先に気づけるようにする。"""

    async def empty() -> Balance:
        return Balance(
            currency="USDT",
            equity=Decimal(0),
            available=Decimal(0),
            used_margin=Decimal(0),
        )

    exchange.get_balance = empty  # type: ignore[method-assign]
    report = await run_verification(settings, SYMBOL, dry_run=True)
    assert not _check(report, "残高照会").passed  # type: ignore[attr-defined]


async def test_ストップが付いていれば合格する(settings: Settings, exchange: FakeExchange) -> None:
    report = await run_verification(settings, SYMBOL)
    assert _check(report, "ストップの添付").passed  # type: ignore[attr-defined]


async def test_ストップが付かなければ不合格(settings: Settings, exchange: FakeExchange) -> None:
    """**無防備な建玉を「成功」と報告してはいけない。**

    取引所側にストップが入らないと、プロセスが落ちた瞬間に誰も見ていない
    建玉が残る。StrategyRunner の強制決済は動いている間しか働かない。
    """
    exchange.drop_stop = True
    report = await run_verification(settings, SYMBOL)

    check = _check(report, "ストップの添付")
    assert not check.passed  # type: ignore[attr-defined]
    assert "無防備" in check.detail  # type: ignore[attr-defined]
    assert not report.ok
