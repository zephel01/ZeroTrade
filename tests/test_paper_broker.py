"""PaperBroker（約定シミュレータ）のテスト。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.conftest import make_candles
from zerotrade.brokers.paper import PaperBroker
from zerotrade.errors import BrokerError, InsufficientFundsError
from zerotrade.models import OrderRequest, OrderStatus, OrderType, Side


@pytest.fixture
async def broker() -> PaperBroker:
    """終値150円で横ばいの相場。ウォームアップ済み。"""
    candles = make_candles([150.0] * 60, spread=0.1)
    broker = PaperBroker(
        ["USD_JPY"],
        initial_balance=Decimal(1_000_000),
        spread=Decimal("0.02"),
        candles={"USD_JPY": candles},
        warmup_bars=30,
    )
    await broker.connect()
    return broker


async def test_未接続では操作できない() -> None:
    broker = PaperBroker(["USD_JPY"])
    with pytest.raises(BrokerError, match="未接続"):
        await broker.get_balance()


async def test_初期残高が返る(broker: PaperBroker) -> None:
    balance = await broker.get_balance()
    assert balance.equity == Decimal(1_000_000)
    assert balance.used_margin == 0
    assert balance.currency == "JPY"


async def test_成行買いで建玉ができる(broker: PaperBroker) -> None:
    order = await broker.place_order(
        OrderRequest(symbol="USD_JPY", side=Side.BUY, quantity=Decimal(10_000))
    )
    assert order.status is OrderStatus.FILLED
    assert order.filled_quantity == Decimal(10_000)
    # 買いは ask（終値 + スプレッド/2）で約定する。
    assert order.average_price == Decimal("150.01")

    positions = await broker.get_positions()
    assert len(positions) == 1
    assert positions[0].side is Side.BUY
    assert positions[0].quantity == Decimal(10_000)


async def test_反対売買で建玉が閉じ損益が確定する(broker: PaperBroker) -> None:
    await broker.place_order(
        OrderRequest(symbol="USD_JPY", side=Side.BUY, quantity=Decimal(10_000))
    )
    await broker.place_order(
        OrderRequest(symbol="USD_JPY", side=Side.SELL, quantity=Decimal(10_000), reduce_only=True)
    )

    assert await broker.get_positions() == []
    trades = await broker.get_closed_trades()
    assert len(trades) == 1
    # ask 150.01 で買い bid 149.99 で売り → スプレッドぶんの損失
    assert trades[0].realized_pnl == Decimal("-200.00")


async def test_部分決済で建玉が残る(broker: PaperBroker) -> None:
    await broker.place_order(
        OrderRequest(symbol="USD_JPY", side=Side.BUY, quantity=Decimal(10_000))
    )
    await broker.place_order(
        OrderRequest(symbol="USD_JPY", side=Side.SELL, quantity=Decimal(4_000), reduce_only=True)
    )

    positions = await broker.get_positions()
    assert len(positions) == 1
    assert positions[0].quantity == Decimal(6_000)
    assert positions[0].side is Side.BUY


async def test_証拠金不足なら拒否される(broker: PaperBroker) -> None:
    # 150 × 1,000,000 / 25 = 600万円の証拠金が必要
    with pytest.raises(InsufficientFundsError):
        await broker.place_order(
            OrderRequest(symbol="USD_JPY", side=Side.BUY, quantity=Decimal(1_000_000))
        )


async def test_ストップに触れると自動決済される() -> None:
    # 150 で買い、その後 148 まで下落する系列
    candles = make_candles([150.0] * 40 + [149.0, 148.0, 147.0], spread=0.1)
    broker = PaperBroker(
        ["USD_JPY"], candles={"USD_JPY": candles}, spread=Decimal("0.02"), warmup_bars=40
    )
    await broker.connect()

    await broker.place_order(
        OrderRequest(
            symbol="USD_JPY",
            side=Side.BUY,
            quantity=Decimal(10_000),
            stop_loss=Decimal("149.50"),
        )
    )
    assert len(await broker.get_positions()) == 1

    # 足を進めるとストップに掛かる。
    await broker.get_ohlcv("USD_JPY")
    assert await broker.get_positions() == []

    trades = await broker.get_closed_trades()
    assert trades[-1].reason == "stop_loss"
    assert trades[-1].exit_price == Decimal("149.50")
    assert trades[-1].realized_pnl < 0


async def test_利確に触れると自動決済される() -> None:
    candles = make_candles([150.0] * 40 + [151.0, 152.0], spread=0.1)
    broker = PaperBroker(
        ["USD_JPY"], candles={"USD_JPY": candles}, spread=Decimal("0.02"), warmup_bars=40
    )
    await broker.connect()

    await broker.place_order(
        OrderRequest(
            symbol="USD_JPY",
            side=Side.BUY,
            quantity=Decimal(10_000),
            take_profit=Decimal("150.50"),
        )
    )
    await broker.get_ohlcv("USD_JPY")

    assert await broker.get_positions() == []
    trades = await broker.get_closed_trades()
    assert trades[-1].reason == "take_profit"
    assert trades[-1].realized_pnl > 0


async def test_同足で両方に触れたらストップが優先される() -> None:
    """楽観的なバックテスト結果を避けるための保守的な仕様。"""
    # 大陰線: 高値152 / 安値148 で、ストップと利確の両方に触れる
    candles = make_candles([150.0] * 40, spread=0.1)
    candles.append(
        candles[-1].__class__(
            symbol="USD_JPY",
            timestamp=candles[-1].timestamp,
            open=Decimal("150"),
            high=Decimal("152"),
            low=Decimal("148"),
            close=Decimal("150"),
        )
    )
    broker = PaperBroker(
        ["USD_JPY"], candles={"USD_JPY": candles}, spread=Decimal("0.02"), warmup_bars=40
    )
    await broker.connect()

    await broker.place_order(
        OrderRequest(
            symbol="USD_JPY",
            side=Side.BUY,
            quantity=Decimal(1_000),
            stop_loss=Decimal("149.00"),
            take_profit=Decimal("151.00"),
        )
    )
    await broker.get_ohlcv("USD_JPY")

    trades = await broker.get_closed_trades()
    assert trades[-1].reason == "stop_loss"


async def test_指値は価格が届いたときに約定する() -> None:
    candles = make_candles([150.0] * 40 + [149.0], spread=0.1)
    broker = PaperBroker(
        ["USD_JPY"], candles={"USD_JPY": candles}, spread=Decimal("0.02"), warmup_bars=40
    )
    await broker.connect()

    order = await broker.place_order(
        OrderRequest(
            symbol="USD_JPY",
            side=Side.BUY,
            quantity=Decimal(1_000),
            order_type=OrderType.LIMIT,
            limit_price=Decimal("149.20"),
        )
    )
    assert order.status is OrderStatus.OPEN

    await broker.get_ohlcv("USD_JPY")
    updated = await broker.get_order(order.client_order_id)
    assert updated.status is OrderStatus.FILLED
    assert updated.average_price == Decimal("149.20")


async def test_注文の取消(broker: PaperBroker) -> None:
    order = await broker.place_order(
        OrderRequest(
            symbol="USD_JPY",
            side=Side.BUY,
            quantity=Decimal(1_000),
            order_type=OrderType.LIMIT,
            limit_price=Decimal("140.00"),
        )
    )
    assert order in await broker.get_open_orders()

    cancelled = await broker.cancel_order(order.client_order_id)
    assert cancelled.status is OrderStatus.CANCELLED
    assert await broker.get_open_orders() == []


async def test_未知の銘柄は拒否される(broker: PaperBroker) -> None:
    from zerotrade.errors import OrderRejected

    with pytest.raises(OrderRejected):
        await broker.place_order(
            OrderRequest(symbol="EUR_USD", side=Side.BUY, quantity=Decimal(1_000))
        )


async def test_close_positionで建玉が閉じる(broker: PaperBroker) -> None:
    await broker.place_order(OrderRequest(symbol="USD_JPY", side=Side.BUY, quantity=Decimal(5_000)))
    order = await broker.close_position("USD_JPY")
    assert order is not None
    assert await broker.get_positions() == []


# ------------------------------------------------------------ reduce_only


async def test_建玉が無いreduce_onlyは新規建てにならない(broker: PaperBroker) -> None:
    """決済のつもりの注文が反対方向の新規建てになる事故を防ぐ。

    ストップに約定した直後など、呼び出し側が1ループ古い建玉情報を
    持っている場面で実際に起きる。
    """
    from zerotrade.errors import OrderRejected

    with pytest.raises(OrderRejected, match="決済対象の建玉がありません"):
        await broker.place_order(
            OrderRequest(symbol="USD_JPY", side=Side.SELL, quantity=Decimal(1000), reduce_only=True)
        )
    assert await broker.get_positions() == []


async def test_同方向のreduce_onlyも拒否される(broker: PaperBroker) -> None:
    """買い建玉に対する「買いの決済注文」は積み増しになってしまう。"""
    from zerotrade.errors import OrderRejected

    await broker.place_order(OrderRequest(symbol="USD_JPY", side=Side.BUY, quantity=Decimal(5_000)))
    with pytest.raises(OrderRejected):
        await broker.place_order(
            OrderRequest(symbol="USD_JPY", side=Side.BUY, quantity=Decimal(5_000), reduce_only=True)
        )
    positions = await broker.get_positions()
    assert positions[0].quantity == Decimal(5_000), "積み増しされている"


async def test_建玉より多いreduce_onlyは切り詰められる(broker: PaperBroker) -> None:
    """余剰ぶんが反対方向の新規建てになってはいけない。"""
    await broker.place_order(OrderRequest(symbol="USD_JPY", side=Side.BUY, quantity=Decimal(3_000)))
    order = await broker.place_order(
        OrderRequest(symbol="USD_JPY", side=Side.SELL, quantity=Decimal(10_000), reduce_only=True)
    )

    assert order.filled_quantity == Decimal(3_000)
    assert await broker.get_positions() == []


# ------------------------------------------------------------ トレーリングストップ


async def test_ストップを引き上げられる(broker: PaperBroker) -> None:
    await broker.place_order(
        OrderRequest(
            symbol="USD_JPY",
            side=Side.BUY,
            quantity=Decimal(5_000),
            stop_loss=Decimal("149.00"),
        )
    )
    updated = await broker.update_position_stop("USD_JPY", Decimal("149.80"))

    assert updated is not None
    assert updated.stop_loss == Decimal("149.80")


async def test_ストップの引き下げは無視される(broker: PaperBroker) -> None:
    """損失許容量を後から広げる操作。リスク管理の前提が崩れる。"""
    await broker.place_order(
        OrderRequest(
            symbol="USD_JPY",
            side=Side.BUY,
            quantity=Decimal(5_000),
            stop_loss=Decimal("149.50"),
        )
    )
    updated = await broker.update_position_stop("USD_JPY", Decimal("148.00"))

    assert updated is not None
    assert updated.stop_loss == Decimal("149.50"), "ストップが引き下げられている"


async def test_売り建玉では逆向きに判定する(broker: PaperBroker) -> None:
    await broker.place_order(
        OrderRequest(
            symbol="USD_JPY",
            side=Side.SELL,
            quantity=Decimal(5_000),
            stop_loss=Decimal("151.00"),
        )
    )
    tightened = await broker.update_position_stop("USD_JPY", Decimal("150.50"))
    assert tightened is not None
    assert tightened.stop_loss == Decimal("150.50")

    loosened = await broker.update_position_stop("USD_JPY", Decimal("152.00"))
    assert loosened is not None
    assert loosened.stop_loss == Decimal("150.50")


async def test_建玉が無ければNoneを返す(broker: PaperBroker) -> None:
    assert await broker.update_position_stop("USD_JPY", Decimal("149")) is None
