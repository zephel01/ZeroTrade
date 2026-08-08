"""OrderManager のテスト。

最も重要な検証は「リスク却下時にブローカーが呼ばれないこと」。
ここが破れると、システム全体の前提が崩れる。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.conftest import FakeClock, make_candles, make_position, make_request
from zerotrade.brokers.paper import PaperBroker
from zerotrade.core.orders import OrderManager
from zerotrade.core.risk import MarketContext, RiskManager
from zerotrade.errors import BrokerError
from zerotrade.models import Balance, Order, OrderRequest, OrderStatus, Side, Ticker


@pytest.fixture
async def broker() -> PaperBroker:
    broker = PaperBroker(
        ["USD_JPY"],
        candles={"USD_JPY": make_candles([150.0] * 60, spread=0.1)},
        spread=Decimal("0.02"),
        warmup_bars=30,
    )
    await broker.connect()
    return broker


class ExplodingBroker(PaperBroker):
    """place_order が必ず失敗するブローカー。呼ばれたかどうかを記録する。"""

    def __init__(self) -> None:
        super().__init__(
            ["USD_JPY"], candles={"USD_JPY": make_candles([150.0] * 40)}, warmup_bars=20
        )
        self.place_order_calls = 0

    async def place_order(self, request: OrderRequest) -> Order:
        self.place_order_calls += 1
        raise BrokerError("接続断")


async def test_承認された注文はブローカーへ渡る(
    broker: PaperBroker, risk: RiskManager, balance: Balance, ticker: Ticker
) -> None:
    manager = OrderManager(broker, risk)
    result = await manager.submit(
        make_request(quantity=Decimal(5_000)),
        balance=balance,
        positions=[],
        market=MarketContext(ticker=ticker),
    )

    assert result.submitted
    assert result.order is not None
    assert result.order.status is OrderStatus.FILLED
    assert risk.state.daily_trades == 1


async def test_リスク却下時はブローカーを呼ばない(
    risk: RiskManager, balance: Balance, ticker: Ticker
) -> None:
    broker = ExplodingBroker()
    await broker.connect()
    manager = OrderManager(broker, risk)

    result = await manager.submit(
        make_request(stop_loss=None),  # ストップ無し → require_stop_loss で却下
        balance=balance,
        positions=[],
        market=MarketContext(ticker=ticker),
    )

    assert not result.submitted
    assert result.decision.rule == "require_stop_loss"
    assert broker.place_order_calls == 0, "却下された注文がブローカーへ届いてはいけない"
    assert risk.state.daily_trades == 0


async def test_ブローカー障害は例外にせず結果で返す(
    risk: RiskManager, balance: Balance, ticker: Ticker
) -> None:
    broker = ExplodingBroker()
    await broker.connect()
    manager = OrderManager(broker, risk)

    result = await manager.submit(
        make_request(), balance=balance, positions=[], market=MarketContext(ticker=ticker)
    )

    assert not result.submitted
    assert result.decision.approved, "リスク判定自体は通っている"
    assert result.error is not None
    assert risk.state.daily_trades == 0, "送信できなかった注文は回数に数えない"


async def test_停止中でも決済は通る(
    broker: PaperBroker, risk: RiskManager, balance: Balance, ticker: Ticker
) -> None:
    manager = OrderManager(broker, risk)
    await manager.submit(
        make_request(quantity=Decimal(5_000)),
        balance=balance,
        positions=[],
        market=MarketContext(ticker=ticker),
    )

    risk.record_trade_closed("USD_JPY", Decimal(-100_000))
    assert risk.is_halted

    position = (await broker.get_positions())[0]
    result = await manager.close_position(position, balance=balance, positions=[position])
    assert result.submitted
    assert await broker.get_positions() == []


async def test_未約定注文を追跡し取消できる(
    broker: PaperBroker, risk: RiskManager, balance: Balance, ticker: Ticker
) -> None:
    from zerotrade.models import OrderType

    manager = OrderManager(broker, risk)
    result = await manager.submit(
        make_request(
            order_type=OrderType.LIMIT,
            limit_price=Decimal("140.00"),
            stop_loss=Decimal("139.00"),
        ),
        balance=balance,
        positions=[],
        market=MarketContext(ticker=ticker),
    )
    assert result.order is not None
    assert len(manager.active_orders()) == 1

    cancelled = await manager.cancel(result.order.client_order_id)
    assert cancelled is not None
    assert cancelled.status is OrderStatus.CANCELLED
    assert manager.active_orders() == []


async def test_refreshで約定を検知する(
    broker: PaperBroker, risk: RiskManager, balance: Balance, ticker: Ticker
) -> None:
    from zerotrade.models import OrderType

    # 次の1本で 149.20 まで下がる系列に差し替える（カーソルは30本目）。
    broker.inject_candles("USD_JPY", make_candles([150.0] * 30 + [149.0], spread=0.1))
    manager = OrderManager(broker, risk)

    result = await manager.submit(
        make_request(
            order_type=OrderType.LIMIT,
            limit_price=Decimal("149.20"),
            stop_loss=Decimal("148.00"),
        ),
        balance=balance,
        positions=[],
        market=MarketContext(ticker=ticker),
    )
    assert result.order is not None

    await broker.get_ohlcv("USD_JPY")  # 足を1本進める
    finished = await manager.refresh()

    assert len(finished) == 1
    assert finished[0].status is OrderStatus.FILLED
    assert manager.active_orders() == []


async def test_cancel_allが未約定注文をすべて消す(
    broker: PaperBroker, risk: RiskManager, balance: Balance, ticker: Ticker
) -> None:
    from zerotrade.models import OrderType

    manager = OrderManager(broker, risk)
    for price in ("140.00", "141.00"):
        await manager.submit(
            make_request(
                order_type=OrderType.LIMIT,
                limit_price=Decimal(price),
                stop_loss=Decimal("139.00"),
                quantity=Decimal(1_000),
            ),
            balance=balance,
            positions=[],
            market=MarketContext(ticker=ticker),
        )
    assert len(manager.active_orders()) == 2

    await manager.cancel_all()
    assert manager.active_orders() == []


async def test_ポジション上限を超える注文は届かない(
    risk: RiskManager, balance: Balance, ticker: Ticker
) -> None:
    broker = ExplodingBroker()
    await broker.connect()
    manager = OrderManager(broker, risk)

    result = await manager.submit(
        make_request(),
        balance=balance,
        positions=[make_position(symbol="USD_JPY")],
        market=MarketContext(ticker=ticker),
    )
    assert result.decision.rule == "max_positions_per_symbol"
    assert broker.place_order_calls == 0


async def test_日付が変われば取引回数がリセットされる(
    broker: PaperBroker, risk: RiskManager, clock: FakeClock, balance: Balance, ticker: Ticker
) -> None:
    manager = OrderManager(broker, risk)
    await manager.submit(
        make_request(quantity=Decimal(1_000)),
        balance=balance,
        positions=[],
        market=MarketContext(ticker=ticker),
    )
    assert risk.state.daily_trades == 1

    clock.advance(days=1)
    risk.evaluate(
        make_request(), balance=balance, positions=[], market=MarketContext(ticker=ticker)
    )
    assert risk.state.daily_trades == 0


# ------------------------------------------------------------ 決済の安全確認


async def test_建玉が既に無ければ決済注文を送らない(
    broker: PaperBroker, risk: RiskManager, balance: Balance, ticker: Ticker
) -> None:
    """呼び出し側の建玉情報は1ループ古いことがある。

    その間にストップが約定していると、決済のつもりの注文が
    そのまま反対方向の新規建てになる。
    """
    manager = OrderManager(broker, risk)
    ghost = make_position(symbol="USD_JPY", side=Side.BUY, quantity=Decimal(5_000))

    result = await manager.close_position(ghost, balance=balance, positions=[ghost])

    assert not result.submitted
    assert result.error is not None
    assert "既に存在しません" in result.error
    assert await broker.get_positions() == [], "存在しない建玉の決済で新規建てが生まれた"


async def test_残っている数量に合わせて決済する(
    broker: PaperBroker, risk: RiskManager, balance: Balance, ticker: Ticker
) -> None:
    """建玉が部分的に減っていた場合、余剰が新規建てにならないこと。"""
    manager = OrderManager(broker, risk)
    await manager.submit(
        make_request(quantity=Decimal(3_000)),
        balance=balance,
        positions=[],
        market=MarketContext(ticker=ticker),
    )

    stale = make_position(symbol="USD_JPY", side=Side.BUY, quantity=Decimal(9_999))
    result = await manager.close_position(stale, balance=balance, positions=[stale])

    assert result.submitted
    assert result.order is not None
    assert result.order.filled_quantity == Decimal(3_000)
    assert await broker.get_positions() == []
