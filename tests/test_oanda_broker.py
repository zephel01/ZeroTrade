"""OANDA アダプタのテスト（respx で HTTP をモック）。

実際のAPIキーは不要。検証したいのは
「OANDA固有の表現をコア層のモデルへ正しく翻訳できているか」だけ。
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
import respx

from zerotrade.brokers.oanda import OandaBroker
from zerotrade.errors import BrokerError, OrderRejected
from zerotrade.models import OrderRequest, OrderStatus, OrderType, Side

ACCOUNT = "101-000-0000000-000"
BASE = "https://api-fxpractice.oanda.com"


@pytest.fixture
def broker() -> OandaBroker:
    return OandaBroker(account_id=ACCOUNT, api_token="dummy-token")


def _summary() -> dict[str, object]:
    return {
        "account": {
            "currency": "JPY",
            "balance": "1000000.0",
            "NAV": "1012345.6",
            "marginUsed": "60000.0",
            "marginAvailable": "952345.6",
        }
    }


async def test_認証情報が無ければ構築できない() -> None:
    with pytest.raises(BrokerError, match="account_id"):
        OandaBroker(account_id="", api_token="x")


async def test_未知の環境は拒否される() -> None:
    with pytest.raises(BrokerError, match="environment"):
        OandaBroker(account_id=ACCOUNT, api_token="x", environment="staging")


@respx.mock
async def test_接続時に認証を確認する(broker: OandaBroker) -> None:
    route = respx.get(f"{BASE}/v3/accounts/{ACCOUNT}/summary").mock(
        return_value=httpx.Response(200, json=_summary())
    )
    await broker.connect()
    assert route.called
    await broker.disconnect()


@respx.mock
async def test_認証失敗は起動時に落ちる(broker: OandaBroker) -> None:
    respx.get(f"{BASE}/v3/accounts/{ACCOUNT}/summary").mock(
        return_value=httpx.Response(401, json={"errorMessage": "Insufficient authorization"})
    )
    with pytest.raises(BrokerError, match="401"):
        await broker.connect()


@respx.mock
async def test_残高はNAVをequityとして扱う(broker: OandaBroker) -> None:
    respx.get(f"{BASE}/v3/accounts/{ACCOUNT}/summary").mock(
        return_value=httpx.Response(200, json=_summary())
    )
    await broker.connect()
    balance = await broker.get_balance()

    # balance ではなく NAV（含み損益込み）を equity とする。
    assert balance.equity == Decimal("1012345.6")
    assert balance.used_margin == Decimal("60000.0")
    assert balance.currency == "JPY"


@respx.mock
async def test_符号付きunitsをSideと数量へ変換する(broker: OandaBroker) -> None:
    respx.get(f"{BASE}/v3/accounts/{ACCOUNT}/summary").mock(
        return_value=httpx.Response(200, json=_summary())
    )
    respx.get(f"{BASE}/v3/accounts/{ACCOUNT}/openPositions").mock(
        return_value=httpx.Response(
            200,
            json={
                "positions": [
                    {
                        "instrument": "USD_JPY",
                        "long": {"units": "0", "averagePrice": "0", "unrealizedPL": "0"},
                        "short": {
                            "units": "-10000",
                            "averagePrice": "150.250",
                            "unrealizedPL": "-1200.0",
                        },
                    }
                ]
            },
        )
    )
    await broker.connect()
    positions = await broker.get_positions()

    assert len(positions) == 1
    assert positions[0].side is Side.SELL
    assert positions[0].quantity == Decimal(10_000), "数量は常に正の値で扱う"
    assert positions[0].unrealized_pnl == Decimal("-1200.0")


@respx.mock
async def test_気配値を取得する(broker: OandaBroker) -> None:
    respx.get(f"{BASE}/v3/accounts/{ACCOUNT}/summary").mock(
        return_value=httpx.Response(200, json=_summary())
    )
    respx.get(f"{BASE}/v3/accounts/{ACCOUNT}/pricing").mock(
        return_value=httpx.Response(
            200,
            json={
                "prices": [
                    {
                        "instrument": "USD_JPY",
                        "time": "2026-01-05T12:00:00.123456789Z",
                        "bids": [{"price": "150.000"}],
                        "asks": [{"price": "150.020"}],
                    }
                ]
            },
        )
    )
    await broker.connect()
    ticker = await broker.get_ticker("USD_JPY")

    assert ticker.bid == Decimal("150.000")
    assert ticker.spread == Decimal("0.020")
    # ナノ秒付きRFC3339を落とさずに解釈できること。
    assert ticker.timestamp is not None
    assert ticker.timestamp.year == 2026


@respx.mock
async def test_板が空なら例外(broker: OandaBroker) -> None:
    respx.get(f"{BASE}/v3/accounts/{ACCOUNT}/summary").mock(
        return_value=httpx.Response(200, json=_summary())
    )
    respx.get(f"{BASE}/v3/accounts/{ACCOUNT}/pricing").mock(
        return_value=httpx.Response(
            200, json={"prices": [{"instrument": "USD_JPY", "bids": [], "asks": []}]}
        )
    )
    await broker.connect()
    with pytest.raises(BrokerError, match="板情報"):
        await broker.get_ticker("USD_JPY")


@respx.mock
async def test_売り注文はunitsが負になる(broker: OandaBroker) -> None:
    respx.get(f"{BASE}/v3/accounts/{ACCOUNT}/summary").mock(
        return_value=httpx.Response(200, json=_summary())
    )
    route = respx.post(f"{BASE}/v3/accounts/{ACCOUNT}/orders").mock(
        return_value=httpx.Response(
            201,
            json={
                "orderCreateTransaction": {"id": "1001"},
                "orderFillTransaction": {
                    "id": "1002",
                    "orderID": "1001",
                    "units": "-5000",
                    "price": "150.000",
                },
            },
        )
    )
    await broker.connect()
    order = await broker.place_order(
        OrderRequest(
            symbol="USD_JPY",
            side=Side.SELL,
            quantity=Decimal(5_000),
            stop_loss=Decimal("151.000"),
        )
    )

    body = respx.calls.last.request.content.decode()
    assert '"units":"-5000"' in body
    assert '"stopLossOnFill"' in body
    assert '"timeInForce":"FOK"' in body, "成行に GTC は指定できない"
    assert route.called

    assert order.status is OrderStatus.FILLED
    assert order.filled_quantity == Decimal(5_000)
    assert order.average_price == Decimal("150.000")


@respx.mock
async def test_クライアント注文IDが送信される(broker: OandaBroker) -> None:
    respx.get(f"{BASE}/v3/accounts/{ACCOUNT}/summary").mock(
        return_value=httpx.Response(200, json=_summary())
    )
    respx.post(f"{BASE}/v3/accounts/{ACCOUNT}/orders").mock(
        return_value=httpx.Response(201, json={"orderCreateTransaction": {"id": "1"}})
    )
    await broker.connect()
    request = OrderRequest(
        symbol="USD_JPY", side=Side.BUY, quantity=Decimal(1_000), stop_loss=Decimal("149")
    )
    await broker.place_order(request)

    body = respx.calls.last.request.content.decode()
    assert request.client_order_id in body, "冪等キーが送られていないと二重発注を検知できない"


@respx.mock
async def test_拒否トランザクションはOrderRejected(broker: OandaBroker) -> None:
    respx.get(f"{BASE}/v3/accounts/{ACCOUNT}/summary").mock(
        return_value=httpx.Response(200, json=_summary())
    )
    respx.post(f"{BASE}/v3/accounts/{ACCOUNT}/orders").mock(
        return_value=httpx.Response(
            201,
            json={
                "orderCreateTransaction": {"id": "1"},
                "orderRejectTransaction": {"reason": "INSUFFICIENT_MARGIN"},
            },
        )
    )
    await broker.connect()
    with pytest.raises(OrderRejected, match="INSUFFICIENT_MARGIN"):
        await broker.place_order(
            OrderRequest(
                symbol="USD_JPY",
                side=Side.BUY,
                quantity=Decimal(1_000),
                stop_loss=Decimal("149"),
            )
        )


@respx.mock
async def test_ローソク足を変換する(broker: OandaBroker) -> None:
    respx.get(f"{BASE}/v3/accounts/{ACCOUNT}/summary").mock(
        return_value=httpx.Response(200, json=_summary())
    )
    respx.get(f"{BASE}/v3/instruments/USD_JPY/candles").mock(
        return_value=httpx.Response(
            200,
            json={
                "candles": [
                    {
                        "time": "2026-01-05T12:00:00.000000000Z",
                        "volume": 120,
                        "complete": True,
                        "mid": {"o": "150.0", "h": "150.5", "l": "149.8", "c": "150.2"},
                    }
                ]
            },
        )
    )
    await broker.connect()
    candles = await broker.get_ohlcv("USD_JPY", granularity="M5", count=1)

    assert len(candles) == 1
    assert candles[0].high == Decimal("150.5")
    assert candles[0].complete is True


@respx.mock
async def test_決済済みトレードを変換する(broker: OandaBroker) -> None:
    respx.get(f"{BASE}/v3/accounts/{ACCOUNT}/summary").mock(
        return_value=httpx.Response(200, json=_summary())
    )
    respx.get(f"{BASE}/v3/accounts/{ACCOUNT}/trades").mock(
        return_value=httpx.Response(
            200,
            json={
                "trades": [
                    {
                        "id": "77",
                        "instrument": "USD_JPY",
                        "initialUnits": "-10000",
                        "price": "150.500",
                        "averageClosePrice": "150.000",
                        "realizedPL": "5000.0",
                        "openTime": "2026-01-05T10:00:00.000000000Z",
                        "closeTime": "2026-01-05T11:00:00.000000000Z",
                    }
                ]
            },
        )
    )
    await broker.connect()
    trades = await broker.get_closed_trades()

    assert len(trades) == 1
    assert trades[0].side is Side.SELL
    assert trades[0].quantity == Decimal(10_000)
    assert trades[0].realized_pnl == Decimal("5000.0")


@respx.mock
async def test_未約定注文の一覧(broker: OandaBroker) -> None:
    respx.get(f"{BASE}/v3/accounts/{ACCOUNT}/summary").mock(
        return_value=httpx.Response(200, json=_summary())
    )
    respx.get(f"{BASE}/v3/accounts/{ACCOUNT}/pendingOrders").mock(
        return_value=httpx.Response(
            200,
            json={
                "orders": [
                    {
                        "id": "55",
                        "instrument": "USD_JPY",
                        "units": "3000",
                        "type": "LIMIT",
                        "price": "149.000",
                        "state": "PENDING",
                        "filledUnits": "0",
                        "clientExtensions": {"id": "zt-abc"},
                        "createTime": "2026-01-05T09:00:00.000000000Z",
                    }
                ]
            },
        )
    )
    await broker.connect()
    orders = await broker.get_open_orders()

    assert len(orders) == 1
    assert orders[0].client_order_id == "zt-abc"
    assert orders[0].order_type is OrderType.LIMIT
    assert orders[0].status is OrderStatus.OPEN
    assert orders[0].side is Side.BUY


@respx.mock
async def test_通信エラーはBrokerErrorへ正規化される(broker: OandaBroker) -> None:
    respx.get(f"{BASE}/v3/accounts/{ACCOUNT}/summary").mock(
        side_effect=httpx.ConnectError("network down")
    )
    with pytest.raises(BrokerError, match="通信"):
        await broker.connect()


async def test_未接続で操作すると例外(broker: OandaBroker) -> None:
    with pytest.raises(BrokerError, match="未接続"):
        await broker.get_balance()


@respx.mock
async def test_reduce_onlyは建玉決済エンドポイントを使う(broker: OandaBroker) -> None:
    """反対方向の成行をそのまま出すと、建玉が既に無い場合に新規建てになる。"""
    respx.get(f"{BASE}/v3/accounts/{ACCOUNT}/summary").mock(
        return_value=httpx.Response(200, json=_summary())
    )
    respx.get(f"{BASE}/v3/accounts/{ACCOUNT}/openPositions").mock(
        return_value=httpx.Response(
            200,
            json={
                "positions": [
                    {
                        "instrument": "USD_JPY",
                        "long": {
                            "units": "10000",
                            "averagePrice": "150.000",
                            "unrealizedPL": "0",
                        },
                        "short": {"units": "0", "averagePrice": "0", "unrealizedPL": "0"},
                    }
                ]
            },
        )
    )
    close_route = respx.put(f"{BASE}/v3/accounts/{ACCOUNT}/positions/USD_JPY/close").mock(
        return_value=httpx.Response(
            200,
            json={
                "longOrderFillTransaction": {
                    "id": "900",
                    "units": "-10000",
                    "price": "150.100",
                }
            },
        )
    )
    orders_route = respx.post(f"{BASE}/v3/accounts/{ACCOUNT}/orders").mock(
        return_value=httpx.Response(201, json={"orderCreateTransaction": {"id": "1"}})
    )

    await broker.connect()
    order = await broker.place_order(
        OrderRequest(symbol="USD_JPY", side=Side.SELL, quantity=Decimal(10_000), reduce_only=True)
    )

    assert close_route.called
    assert not orders_route.called, "通常の発注エンドポイントを使ってはいけない"
    assert order.status is OrderStatus.FILLED
    assert order.filled_quantity == Decimal(10_000)


@respx.mock
async def test_建玉が無いreduce_onlyは拒否される(broker: OandaBroker) -> None:
    respx.get(f"{BASE}/v3/accounts/{ACCOUNT}/summary").mock(
        return_value=httpx.Response(200, json=_summary())
    )
    respx.get(f"{BASE}/v3/accounts/{ACCOUNT}/openPositions").mock(
        return_value=httpx.Response(200, json={"positions": []})
    )
    orders_route = respx.post(f"{BASE}/v3/accounts/{ACCOUNT}/orders").mock(
        return_value=httpx.Response(201, json={"orderCreateTransaction": {"id": "1"}})
    )

    await broker.connect()
    with pytest.raises(OrderRejected, match="決済対象の建玉がありません"):
        await broker.place_order(
            OrderRequest(
                symbol="USD_JPY", side=Side.SELL, quantity=Decimal(1_000), reduce_only=True
            )
        )
    assert not orders_route.called, "建玉が無いのに新規注文を送っている"
