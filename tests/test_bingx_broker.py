"""BingX アダプタのテスト。

実際の取引所には接続せず、BingX 固有の4点が守られているかを検証する。
1) 建玉照会に銘柄リストを渡す 2) positionSide=BOTH を送る
3) 片方向モードへ寄せる 4) 決済履歴から確定損益を取る
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from zerotrade.brokers.bingx import BingxBroker, _exit_price
from zerotrade.models import OrderRequest, Side

MARKETS = {
    "BTC/USDT": {
        "base": "BTC",
        "quote": "USDT",
        "settle": "USDT",
        "type": "swap",
        "precision": {"amount": 4},
        "limits": {"amount": {"min": 0.0001}},
    }
}


class FakeBingx:
    """BingX の ccxt 実装を模したもの。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.sandbox = False
        self.positions: list[dict[str, Any]] = []
        self.history: list[dict[str, Any]] = []
        self.position_mode_fails = False

    def set_sandbox_mode(self, enabled: bool) -> None:
        self.sandbox = enabled

    async def close(self) -> None: ...

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        return f"{amount:.4f}"

    async def load_markets(self) -> dict[str, Any]:
        return MARKETS

    async def set_position_mode(self, hedged: bool, symbol: str) -> dict[str, Any]:
        self.calls.append(("set_position_mode", (hedged, symbol)))
        if self.position_mode_fails:
            raise RuntimeError("position exists")
        return {}

    async def set_margin_mode(self, mode: str, symbol: str) -> dict[str, Any]:
        self.calls.append(("set_margin_mode", (mode, symbol)))
        return {}

    async def fetch_positions(self, symbols: list[str] | None = None) -> list[dict[str, Any]]:
        self.calls.append(("fetch_positions", (symbols,)))
        return self.positions

    async def fetch_positions_history(
        self, symbols: list[str] | None = None, since: int | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append(("fetch_positions_history", (symbols, since, limit)))
        return self.history

    async def create_order(
        self,
        symbol: str,
        type_: str,
        side: str,
        amount: float,
        price: float | None,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(("create_order", (symbol, type_, side, amount, price, params)))
        return {
            "id": "bx-1",
            "clientOrderId": params.get("clientOrderId"),
            "symbol": symbol,
            "side": side,
            "type": type_,
            "amount": amount,
            "filled": amount,
            "average": "60000",
            "status": "closed",
        }

    async def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        return {"bid": "59999", "ask": "60001"}

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: int | None, limit: int
    ) -> list[list[Any]]:
        self.calls.append(("fetch_ohlcv", (symbol, timeframe, since, limit)))
        if limit > 1440:
            # 実機と同じ挙動。超えると1本も返らない。
            raise RuntimeError('{"code":109400,"msg":"limit: ... less than or equal to 1440"}')
        return [[1_704_153_600_000, "60000", "60100", "59900", "60050", "12.5"]]


@pytest.fixture
def exchange() -> FakeBingx:
    return FakeBingx()


@pytest.fixture
async def broker(exchange: FakeBingx) -> BingxBroker:
    b = BingxBroker(client=exchange, sandbox=True, symbols=["BTC_USDT"])
    await b.connect()
    return b


# ------------------------------------------------------------ 接続


async def test_VSTテストネットが有効になる(exchange: FakeBingx) -> None:
    b = BingxBroker(client=exchange, sandbox=True, symbols=["BTC_USDT"])
    await b.connect()
    assert exchange.sandbox is True


async def test_接続時に片方向モードへ寄せる(broker: BingxBroker, exchange: FakeBingx) -> None:
    """ヘッジモードのままだと決済注文が反対側の新規建てになる。"""
    call = next(c for c in exchange.calls if c[0] == "set_position_mode")
    assert call[1] == (False, "BTC/USDT"), "hedged=False で呼ばれていない"


async def test_証拠金モードも設定する(broker: BingxBroker, exchange: FakeBingx) -> None:
    call = next(c for c in exchange.calls if c[0] == "set_margin_mode")
    assert call[1] == ("cross", "BTC/USDT")


async def test_モード設定に失敗しても接続は続く(exchange: FakeBingx) -> None:
    """既に建玉があるとモード変更は拒否される。それで起動できないのは困る。"""
    exchange.position_mode_fails = True
    b = BingxBroker(client=exchange, sandbox=True, symbols=["BTC_USDT"])
    await b.connect()  # 例外にならない
    assert await b.get_positions() == []


# ------------------------------------------------------------ 建玉


async def test_建玉照会に銘柄リストを渡す(broker: BingxBroker, exchange: FakeBingx) -> None:
    """渡さないと建玉が取れず「建玉が無い」と誤認して二重に建てる。"""
    await broker.get_positions()
    call = next(c for c in reversed(exchange.calls) if c[0] == "fetch_positions")
    assert call[1][0] == ["BTC/USDT"]


async def test_建玉を変換できる(broker: BingxBroker, exchange: FakeBingx) -> None:
    exchange.positions = [
        {
            "symbol": "BTC/USDT:USDT",
            "side": "long",
            "contracts": "0.05",
            "entryPrice": "60000",
            "unrealizedPnl": "12.5",
        }
    ]
    positions = await broker.get_positions()
    assert positions[0].symbol == "BTC_USDT"
    assert positions[0].side is Side.BUY
    assert positions[0].quantity == Decimal("0.05")


# ------------------------------------------------------------ 注文


async def test_positionSideにBOTHを送る(broker: BingxBroker, exchange: FakeBingx) -> None:
    """片方向モードでは BOTH。LONG/SHORT を混ぜると決済が新規建てになる。"""
    await broker.place_order(
        OrderRequest(
            symbol="BTC_USDT",
            side=Side.BUY,
            quantity=Decimal("0.01"),
            stop_loss=Decimal("58000"),
        )
    )
    params = exchange.calls[-1][1][5]
    assert params["positionSide"] == "BOTH"
    assert params["stopLoss"] == {"triggerPrice": 58000.0}
    assert params["clientOrderId"].startswith("zt-")


async def test_clientOrderIdは40文字以内(broker: BingxBroker, exchange: FakeBingx) -> None:
    """BingX の制限は1〜40文字。超えると注文が弾かれる。"""
    request = OrderRequest(symbol="BTC_USDT", side=Side.BUY, quantity=Decimal("0.01"))
    assert 1 <= len(request.client_order_id) <= 40


async def test_決済注文もBOTHで送られる(broker: BingxBroker, exchange: FakeBingx) -> None:
    exchange.positions = [
        {"symbol": "BTC/USDT", "side": "long", "contracts": "0.01", "entryPrice": "60000"}
    ]
    await broker.place_order(
        OrderRequest(
            symbol="BTC_USDT",
            side=Side.SELL,
            quantity=Decimal("0.01"),
            reduce_only=True,
        )
    )
    params = exchange.calls[-1][1][5]
    assert params["positionSide"] == "BOTH"
    assert params["reduceOnly"] is True


# ------------------------------------------------------------ 確定損益


def test_確定損益を実測で取れる() -> None:
    """推定ではなく実測。日次・週次の損失上限がより正確に効く。"""
    assert BingxBroker.supports_closed_trades is True


async def test_決済履歴を変換できる(broker: BingxBroker, exchange: FakeBingx) -> None:
    exchange.history = [
        {
            "id": "pos-1",
            "symbol": "BTC/USDT:USDT",
            "side": "long",
            "contracts": "0.5",
            "entryPrice": "60000",
            "realizedPnl": "-250",
            "timestamp": 1_704_153_600_000,
            "lastUpdateTimestamp": 1_704_157_200_000,
        },
        {"symbol": "ETH/USDT", "side": "long", "contracts": "0"},  # 空は除外
    ]
    trades = await broker.get_closed_trades()

    assert len(trades) == 1
    t = trades[0]
    assert t.symbol == "BTC_USDT"
    assert t.side is Side.BUY
    assert t.realized_pnl == Decimal("-250")
    assert t.reason == "exchange"
    assert t.closed_at > t.opened_at
    # 決済価格は損益から逆算される: 60000 + (-250 / 0.5) = 59500
    assert t.exit_price == Decimal("59500")


async def test_since以降だけ返す(broker: BingxBroker, exchange: FakeBingx) -> None:
    exchange.history = [
        {
            "symbol": "BTC/USDT",
            "side": "long",
            "contracts": "1",
            "entryPrice": "60000",
            "realizedPnl": "10",
            "timestamp": 1_704_153_600_000,
            "lastUpdateTimestamp": 1_704_153_600_000,
        }
    ]
    trades = await broker.get_closed_trades(since=datetime(2030, 1, 1, tzinfo=UTC))
    assert trades == []


async def test_履歴取得に失敗しても落ちない(broker: BingxBroker, exchange: FakeBingx) -> None:
    async def boom(*_: Any) -> list[dict[str, Any]]:
        raise RuntimeError("rate limited")

    exchange.fetch_positions_history = boom  # type: ignore[method-assign, assignment]
    assert await broker.get_closed_trades() == []


@pytest.mark.parametrize(
    ("entry", "pnl", "qty", "side", "expected"),
    [
        ("60000", "-250", "0.5", Side.BUY, "59500"),
        ("60000", "250", "0.5", Side.BUY, "60500"),
        ("60000", "250", "0.5", Side.SELL, "59500"),
    ],
)
def test_決済価格の逆算(entry: str, pnl: str, qty: str, side: Side, expected: str) -> None:
    assert _exit_price(Decimal(entry), Decimal(pnl), Decimal(qty), side) == Decimal(expected)


async def test_足の取得本数は1440に抑えられる(broker: BingxBroker, exchange: FakeBingx) -> None:
    """BingX は 1440 本を超えると code 109400 で **1本も返さない**。

    既定のチャンク幅 5000 のまま投げて実際に失敗した。
    """
    await broker.get_ohlcv("BTC_USDT", granularity="H1", count=5000)
    call = next(c for c in reversed(exchange.calls) if c[0] == "fetch_ohlcv")
    assert call[1][3] == 1440


def test_上限本数を申告している() -> None:
    assert BingxBroker.max_ohlcv_count == 1440


# ------------------------------------------------------------ 能力の実測


async def test_決済履歴が使えなければ推定へ落とす(exchange: FakeBingx) -> None:
    """**実機で踏んだ最も危険な不具合の回帰テスト。**

    ccxt の has は fetchPositionsHistory を True と申告していたが、
    実装が無く NotSupported を投げた。supports_closed_trades を True の
    まま運用すると StrategyRunner は建玉差分の推定を行わず、
    RiskManager は損益を一切知らないまま動く。
    **日次・週次の損失上限が永久に発動しない。**
    """

    async def unsupported(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("bingx fetchPositionsHistory() is not supported yet")

    exchange.fetch_positions_history = unsupported  # type: ignore[method-assign]
    b = BingxBroker(client=exchange, sandbox=True, symbols=["BTC_USDT"])
    await b.connect()

    assert b.supports_closed_trades is False, "推定へ落ちていない（損失上限が働かない）"


async def test_決済履歴が使えるならTrueのまま(exchange: FakeBingx) -> None:
    b = BingxBroker(client=exchange, sandbox=True, symbols=["BTC_USDT"])
    await b.connect()
    assert b.supports_closed_trades is True


async def test_能力の判定は申告ではなく実測で行う(exchange: FakeBingx) -> None:
    """has が True でも、実際に叩けなければ False にする。"""

    async def unsupported(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("not supported")

    exchange.fetch_positions_history = unsupported  # type: ignore[method-assign]
    exchange.has = {"fetchPositionsHistory": True}  # type: ignore[attr-defined]

    b = BingxBroker(client=exchange, sandbox=True, symbols=["BTC_USDT"])
    await b.connect()
    assert b.supports_closed_trades is False
