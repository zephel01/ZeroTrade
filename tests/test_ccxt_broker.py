"""ccxt アダプタのテスト。

実際の取引所には接続せず、ccxt の統一APIを模した偽物を差し込んで
「ccxt の応答をコア層のモデルへ正しく翻訳できているか」だけを検証する。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from zerotrade.brokers.ccxt_broker import CcxtBroker
from zerotrade.errors import BrokerError, ConfigError, OrderRejected
from zerotrade.models import OrderRequest, OrderStatus, OrderType, Side

MARKETS = {
    "BTC/USDT": {
        "base": "BTC",
        "quote": "USDT",
        "type": "swap",
        "settle": "USDT",
        "precision": {"amount": 3},
        "limits": {"amount": {"min": 0.001}},
    }
}

#: 実際の取引所に近い形。永続契約は ``BTC/USDT:USDT``、現物は ``BTC/USDT``
#: として **別々に** 登録される。素朴な文字列変換では swap を掴めない。
CONTRACT_MARKETS = {
    "BTC/USDT": {"base": "BTC", "quote": "USDT", "type": "spot"},
    "BTC/USDT:USDT": {
        "base": "BTC",
        "quote": "USDT",
        "settle": "USDT",
        "type": "swap",
        "precision": {"amount": 3},
        "limits": {"amount": {"min": 0.001}},
    },
    "ETH/USDT:USDT": {"base": "ETH", "quote": "USDT", "settle": "USDT", "type": "swap"},
}


class FakeExchange:
    """ccxt の統一APIを最小限だけ模したもの。"""

    def __init__(self, markets: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.sandbox = False
        self.positions: list[dict[str, Any]] = []
        self.fail: str | None = None
        self.markets = markets if markets is not None else MARKETS

    def set_sandbox_mode(self, enabled: bool) -> None:
        self.sandbox = enabled

    async def close(self) -> None: ...

    def amount_to_precision(self, symbol: str, amount: float) -> str:
        return f"{amount:.3f}"

    async def load_markets(self) -> dict[str, Any]:
        return self.markets

    async def fetch_balance(self) -> dict[str, Any]:
        return {"USDT": {"total": "10000.5", "free": "9000.25", "used": "1000.25"}}

    async def fetch_positions(self, symbols: list[str] | None = None) -> list[dict[str, Any]]:
        self.calls.append(("fetch_positions", (symbols,)))
        if self.fail == "positions":
            raise RuntimeError("not supported")
        return self.positions

    async def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        return {"bid": "60000.1", "ask": "60000.9", "timestamp": 1_704_153_600_000}

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: int | None, limit: int
    ) -> list[list[Any]]:
        self.calls.append(("fetch_ohlcv", (symbol, timeframe, since, limit)))
        return [[1_704_153_600_000, "60000", "60100", "59900", "60050", "12.5"]]

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
            "id": "ex-1",
            "clientOrderId": params.get("clientOrderId"),
            "symbol": symbol,
            "side": side,
            "type": type_,
            "amount": amount,
            "filled": amount,
            "average": "60000.9",
            "status": "closed",
            "timestamp": 1_704_153_600_000,
        }

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        return {"id": order_id, "symbol": "BTC/USDT", "side": "buy", "status": "canceled"}

    async def fetch_order(self, order_id: str) -> dict[str, Any]:
        return {
            "id": order_id,
            "symbol": "BTC/USDT",
            "side": "buy",
            "amount": 1.0,
            "filled": 0.4,
            "status": "open",
        }

    async def fetch_open_orders(self, symbol: str | None) -> list[dict[str, Any]]:
        return [{"id": "ex-2", "symbol": "BTC/USDT", "side": "sell", "status": "open"}]


@pytest.fixture
def exchange() -> FakeExchange:
    return FakeExchange()


@pytest.fixture
async def broker(exchange: FakeExchange) -> CcxtBroker:
    b = CcxtBroker(exchange="binance", client=exchange, sandbox=True)
    await b.connect()
    return b


# ------------------------------------------------------------ 接続


async def test_接続でテストネットが有効になる(exchange: FakeExchange) -> None:
    """入金ゼロで発注経路を検証できることが、このアダプタの主目的。"""
    b = CcxtBroker(exchange="binance", client=exchange, sandbox=True)
    await b.connect()
    assert exchange.sandbox is True


async def test_本番指定ではサンドボックスにしない(exchange: FakeExchange) -> None:
    b = CcxtBroker(exchange="binance", client=exchange, sandbox=False)
    await b.connect()
    assert exchange.sandbox is False


def test_未知の取引所は拒否される() -> None:
    pytest.importorskip("ccxt")
    with pytest.raises(ConfigError, match="対応していない取引所"):
        CcxtBroker(exchange="存在しない取引所")


# ------------------------------------------------------------ 口座


async def test_残高を変換できる(broker: CcxtBroker) -> None:
    balance = await broker.get_balance()
    assert balance.currency == "USDT"
    assert balance.equity == Decimal("10000.5")
    assert balance.available == Decimal("9000.25")
    assert balance.used_margin == Decimal("1000.25")


async def test_建玉を変換できる(broker: CcxtBroker, exchange: FakeExchange) -> None:
    exchange.positions = [
        {
            "symbol": "BTC/USDT:USDT",
            "side": "short",
            "contracts": "0.5",
            "entryPrice": "60000",
            "unrealizedPnl": "-120.5",
            "id": "pos-1",
        },
        {"symbol": "ETH/USDT", "side": "long", "contracts": "0"},  # 空は除外
    ]
    positions = await broker.get_positions()

    assert len(positions) == 1
    assert positions[0].symbol == "BTC_USDT", "ccxt の記法が残っている"
    assert positions[0].side is Side.SELL
    assert positions[0].quantity == Decimal("0.5")
    assert positions[0].unrealized_pnl == Decimal("-120.5")


async def test_建玉照会に銘柄リストを渡せる(exchange: FakeExchange) -> None:
    """BingX のように fetch_positions が銘柄必須の取引所がある。"""
    b = CcxtBroker(exchange="binance", client=exchange, sandbox=True, symbols=["BTC_USDT"])
    await b.connect()
    await b.get_positions()

    call = next(c for c in reversed(exchange.calls) if c[0] == "fetch_positions")
    assert call[1][0] == ["BTC/USDT"], "銘柄リストが ccxt 記法で渡っていない"


async def test_銘柄未指定ならNoneを渡す(broker: CcxtBroker, exchange: FakeExchange) -> None:
    await broker.get_positions()
    call = next(c for c in reversed(exchange.calls) if c[0] == "fetch_positions")
    assert call[1][0] is None


async def test_建玉APIが無い取引所でも落ちない(broker: CcxtBroker, exchange: FakeExchange) -> None:
    """現物のみの取引所は fetch_positions を持たない。"""
    exchange.fail = "positions"
    assert await broker.get_positions() == []


# ------------------------------------------------------------ 相場


async def test_気配値を変換できる(broker: CcxtBroker) -> None:
    ticker = await broker.get_ticker("BTC_USDT")
    assert ticker.bid == Decimal("60000.1")
    assert ticker.ask == Decimal("60000.9")
    assert ticker.timestamp.year == 2024


async def test_足を変換できる(broker: CcxtBroker, exchange: FakeExchange) -> None:
    candles = await broker.get_ohlcv("BTC_USDT", granularity="H1", count=10)
    assert len(candles) == 1
    assert candles[0].high == Decimal("60100")
    # 足種が ccxt の表記へ変換されている
    assert exchange.calls[-1][1][1] == "1h"


async def test_未対応の足種は拒否される(broker: CcxtBroker) -> None:
    with pytest.raises(ConfigError, match="足種"):
        await broker.get_ohlcv("BTC_USDT", granularity="M3")


# ------------------------------------------------------------ 注文


async def test_発注が変換される(broker: CcxtBroker, exchange: FakeExchange) -> None:
    request = OrderRequest(
        symbol="BTC_USDT",
        side=Side.BUY,
        quantity=Decimal("0.25"),
        stop_loss=Decimal("58000"),
    )
    order = await broker.place_order(request)

    name, args = exchange.calls[-1]
    assert name == "create_order"
    assert args[0] == "BTC/USDT", "銘柄が ccxt 記法へ変換されていない"
    assert args[2] == "buy"
    assert args[5]["clientOrderId"] == request.client_order_id
    # 入れ子の辞書で渡す。stopLossPrice は「既存建玉を決済する注文」の意味になり、
    # BingX では position not exist（code 109420）で新規建てが弾かれた。
    assert args[5]["stopLoss"] == {"triggerPrice": 58000.0}

    assert order.status is OrderStatus.FILLED
    assert order.average_price == Decimal("60000.9")


async def test_数量は取引所の刻みに丸められる(broker: CcxtBroker, exchange: FakeExchange) -> None:
    """刻みを合わせないと注文自体が弾かれる。"""
    await broker.place_order(
        OrderRequest(symbol="BTC_USDT", side=Side.BUY, quantity=Decimal("0.123456789"))
    )
    assert exchange.calls[-1][1][3] == 0.123


async def test_最小数量を下回れば拒否される(broker: CcxtBroker) -> None:
    with pytest.raises(OrderRejected, match="最小単位"):
        await broker.place_order(
            OrderRequest(symbol="BTC_USDT", side=Side.BUY, quantity=Decimal("0.0001"))
        )


async def test_建玉が無いreduce_onlyは送られない(
    broker: CcxtBroker, exchange: FakeExchange
) -> None:
    """reduceOnly を無視する取引所では、決済のつもりが新規建てになる。"""
    exchange.positions = []
    with pytest.raises(OrderRejected, match="決済対象の建玉がありません"):
        await broker.place_order(
            OrderRequest(
                symbol="BTC_USDT",
                side=Side.SELL,
                quantity=Decimal("0.5"),
                reduce_only=True,
            )
        )
    assert not any(c[0] == "create_order" for c in exchange.calls)


async def test_建玉があればreduce_onlyが送られる(
    broker: CcxtBroker, exchange: FakeExchange
) -> None:
    exchange.positions = [
        {"symbol": "BTC/USDT", "side": "long", "contracts": "0.5", "entryPrice": "60000"}
    ]
    await broker.place_order(
        OrderRequest(symbol="BTC_USDT", side=Side.SELL, quantity=Decimal("0.5"), reduce_only=True)
    )
    assert exchange.calls[-1][1][5]["reduceOnly"] is True


async def test_部分約定を検知する(broker: CcxtBroker) -> None:
    order = await broker.get_order("ex-3")
    assert order.status is OrderStatus.PARTIALLY_FILLED
    assert order.filled_quantity == Decimal("0.4")


async def test_未約定注文を一覧できる(broker: CcxtBroker) -> None:
    orders = await broker.get_open_orders()
    assert len(orders) == 1
    assert orders[0].side is Side.SELL
    assert orders[0].order_type is OrderType.MARKET


async def test_取消を変換できる(broker: CcxtBroker) -> None:
    order = await broker.cancel_order("ex-1")
    assert order.status is OrderStatus.CANCELLED


# ------------------------------------------------------------ 例外の正規化


async def test_取引所の例外はBrokerErrorへ正規化される(
    broker: CcxtBroker, exchange: FakeExchange
) -> None:
    async def boom() -> dict[str, Any]:
        raise RuntimeError("exchange is down")

    exchange.fetch_balance = boom  # type: ignore[method-assign]
    with pytest.raises(BrokerError, match="失敗しました"):
        await broker.get_balance()


async def test_確定損益は推定に委ねる(broker: CcxtBroker) -> None:
    """取引所ごとに決済履歴の形式が違い、建玉との対応付けが信頼できない。

    False にしておくと StrategyRunner が建玉の差分から損益を推定する。
    """
    assert broker.supports_closed_trades is False


# ------------------------------------------------------------ シンボル解決


async def test_swapでは決済通貨サフィックス付きを選ぶ() -> None:
    """``BTC_USDT`` を素朴に ``BTC/USDT`` へ直すと現物を掴む。

    永続契約の統一シンボルは ``BTC/USDT:USDT``。実際に BingX へ繋いだとき
    「does not have market symbol BTC/USDT」で気配値も証拠金設定も落ちた。
    """
    exchange = FakeExchange(CONTRACT_MARKETS)
    b = CcxtBroker(
        exchange="bingx", client=exchange, sandbox=True, default_type="swap", symbols=["BTC_USDT"]
    )
    await b.connect()
    assert b._to_ccxt("BTC_USDT") == "BTC/USDT:USDT"


async def test_spot指定ではサフィックス無しを選ぶ() -> None:
    exchange = FakeExchange(CONTRACT_MARKETS)
    b = CcxtBroker(
        exchange="bingx", client=exchange, sandbox=True, default_type="spot", symbols=["BTC_USDT"]
    )
    await b.connect()
    assert b._to_ccxt("BTC_USDT") == "BTC/USDT"


async def test_存在しない銘柄は接続時に落ちる() -> None:
    """発注時に初めて気づくより、接続時に落ちるほうが安全。"""
    exchange = FakeExchange(CONTRACT_MARKETS)
    b = CcxtBroker(
        exchange="bingx", client=exchange, sandbox=True, default_type="swap", symbols=["DOGE_USDT"]
    )
    with pytest.raises(ConfigError, match="銘柄 DOGE_USDT がありません"):
        await b.connect()


async def test_銘柄が無いときは候補を示す() -> None:
    exchange = FakeExchange(CONTRACT_MARKETS)
    b = CcxtBroker(
        exchange="bingx", client=exchange, sandbox=True, default_type="swap", symbols=["ETH_BTC"]
    )
    with pytest.raises(ConfigError, match="ETH/USDT:USDT"):
        await b.connect()


async def test_残高通貨は設定した銘柄から決める() -> None:
    """市場一覧の先頭から取ると、無関係な通貨を掴む（実機では942銘柄returned）。"""
    exchange = FakeExchange(CONTRACT_MARKETS)
    b = CcxtBroker(
        exchange="bingx", client=exchange, sandbox=True, default_type="swap", symbols=["BTC_USDT"]
    )
    await b.connect()
    assert b._quote_currency() == "USDT"


# ------------------------------------------------------------ 認証情報


def test_認証情報が無ければ組み立てを拒否する() -> None:
    """鍵が無くても load_markets は通る。「繋がったのに残高だけ落ちる」を防ぐ。"""
    from zerotrade.brokers.ccxt_broker import build_from_settings
    from zerotrade.settings import BrokerSettings

    settings = BrokerSettings(name="ccxt", exchange="bingx")
    with pytest.raises(BrokerError, match="認証情報が設定されていません"):
        build_from_settings(settings, symbols=["BTC_USDT"])


def test_シークレットだけ欠けても拒否する() -> None:
    from zerotrade.brokers.ccxt_broker import build_from_settings
    from zerotrade.settings import BrokerSettings

    settings = BrokerSettings(name="ccxt", exchange="bingx", api_token="key")
    with pytest.raises(BrokerError, match="api_secret"):
        build_from_settings(settings, symbols=["BTC_USDT"])


# ------------------------------------------------------------ 権限まわり


async def test_通貨一覧が権限で弾かれても接続できる() -> None:
    """取引権限だけのキーでは fetch_currencies が 403 になる取引所がある。

    通貨一覧は入出金の情報で、発注には要らない。ここで落ちると
    **出金権限を外した安全なキーが使えなくなる**ので、諦めて読み直す。
    """

    class PickyExchange(FakeExchange):
        def __init__(self) -> None:
            super().__init__(CONTRACT_MARKETS)
            self.has = {"fetchCurrencies": True}
            self.attempts = 0

        async def load_markets(self) -> dict[str, Any]:
            self.attempts += 1
            if self.has.get("fetchCurrencies"):
                raise RuntimeError("permission denied: capital/config/getall")
            return self.markets

    exchange = PickyExchange()
    b = CcxtBroker(
        exchange="bingx", client=exchange, sandbox=False, default_type="swap", symbols=["BTC_USDT"]
    )
    await b.connect()

    assert exchange.attempts == 2, "通貨一覧を諦めて読み直していない"
    assert b._to_ccxt("BTC_USDT") == "BTC/USDT:USDT"


async def test_通貨一覧以外の理由なら落とす() -> None:
    """何でも握りつぶすと、本当の接続失敗に気づけなくなる。"""

    class BrokenExchange(FakeExchange):
        def __init__(self) -> None:
            super().__init__(CONTRACT_MARKETS)
            self.has: dict[str, Any] = {}

        async def load_markets(self) -> dict[str, Any]:
            raise RuntimeError("network unreachable")

    b = CcxtBroker(exchange="bingx", client=BrokenExchange(), sandbox=False)
    with pytest.raises(BrokerError, match="load_markets"):
        await b.connect()


# ------------------------------------------------------------ ストップの渡し方


async def test_新規建てにstopLossPriceを使わない(
    broker: CcxtBroker, exchange: FakeExchange
) -> None:
    """**本番で踏んだ不具合の回帰テスト。**

    ccxt は ``stopLossPrice`` を受け取ると ``reduceOnly`` を立てるため、
    「既存建玉を決済する注文」になる。建玉が無い状態で送ると BingX は
    ``{"code":109420,"msg":"position not exist"}`` で新規建てを弾く。
    """
    await broker.place_order(
        OrderRequest(
            symbol="BTC_USDT",
            side=Side.BUY,
            quantity=Decimal("0.01"),
            stop_loss=Decimal("58000"),
            take_profit=Decimal("62000"),
        )
    )
    params = exchange.calls[-1][1][5]
    assert "stopLossPrice" not in params, "新規建てが決済注文として送られている"
    assert "takeProfitPrice" not in params
    assert params["stopLoss"] == {"triggerPrice": 58000.0}
    assert params["takeProfit"] == {"triggerPrice": 62000.0}


async def test_決済注文にストップを添付しない(broker: CcxtBroker, exchange: FakeExchange) -> None:
    """決済にストップを付ける意味はなく、取引所によっては弾かれる。"""
    exchange.positions = [
        {"symbol": "BTC/USDT", "side": "long", "contracts": "0.01", "entryPrice": "60000"}
    ]
    await broker.place_order(
        OrderRequest(
            symbol="BTC_USDT",
            side=Side.SELL,
            quantity=Decimal("0.01"),
            reduce_only=True,
            stop_loss=Decimal("58000"),
        )
    )
    params = exchange.calls[-1][1][5]
    assert params["reduceOnly"] is True
    assert "stopLoss" not in params


async def test_建玉のストップを読み取れる(broker: CcxtBroker, exchange: FakeExchange) -> None:
    """建玉が無防備かどうかは、運用中いちばん知りたいことの1つ。"""
    exchange.positions = [
        {
            "symbol": "BTC/USDT",
            "side": "long",
            "contracts": "0.01",
            "entryPrice": "60000",
            "stopLossPrice": "58000",
        }
    ]
    position = (await broker.get_positions())[0]
    assert position.stop_loss == Decimal("58000")


async def test_ストップ未設定はNoneになる(broker: CcxtBroker, exchange: FakeExchange) -> None:
    """0 を返すと「ストップ 0 円」と誤読され、無防備な建玉を見逃す。"""
    exchange.positions = [
        {
            "symbol": "BTC/USDT",
            "side": "long",
            "contracts": "0.01",
            "entryPrice": "60000",
            "stopLossPrice": "0",
        }
    ]
    position = (await broker.get_positions())[0]
    assert position.stop_loss is None


async def test_ネストしたinfoからもストップを拾う(
    broker: CcxtBroker, exchange: FakeExchange
) -> None:
    """取引所によっては統一フィールドに出ず info にしか無い。"""
    exchange.positions = [
        {
            "symbol": "BTC/USDT",
            "side": "long",
            "contracts": "0.01",
            "entryPrice": "60000",
            "info": {"stopLoss": {"stopPrice": "57000"}},
        }
    ]
    position = (await broker.get_positions())[0]
    assert position.stop_loss == Decimal("57000")


async def test_条件注文のトリガー価格を読み取れる(
    broker: CcxtBroker, exchange: FakeExchange
) -> None:
    """**実機で踏んだ検出漏れの回帰テスト。**

    _to_order が stop_price を埋めていなかったため、取引所に条件注文が
    実在しても常に None になり、「建玉が無防備」と誤判定していた。
    """

    async def with_stop(symbol: str | None) -> list[dict[str, Any]]:
        return [
            {
                "id": "ex-9",
                "symbol": "BTC/USDT",
                "side": "sell",
                "status": "open",
                "stopPrice": "58000",
            }
        ]

    exchange.fetch_open_orders = with_stop  # type: ignore[method-assign]
    orders = await broker.get_open_orders("BTC_USDT")
    assert orders[0].stop_price == Decimal("58000"), "トリガー価格を拾えていない"


async def test_トリガー価格が無ければNone(broker: CcxtBroker) -> None:
    orders = await broker.get_open_orders("BTC_USDT")
    assert orders[0].stop_price is None


async def test_ストップ価格は取引所の刻みに丸められる(
    broker: CcxtBroker, exchange: FakeExchange
) -> None:
    """ATR から計算したストップは端数だらけになる。

    実際に 64692.01652383044993090602514 のような値が出た。
    刻みに合わない価格は取引所に丸められるか拒否されるので、先に揃える。
    """
    exchange.price_to_precision = lambda symbol, price: f"{price:.2f}"  # type: ignore[attr-defined]

    await broker.place_order(
        OrderRequest(
            symbol="BTC_USDT",
            side=Side.BUY,
            quantity=Decimal("0.01"),
            stop_loss=Decimal("64692.01652383044993090602514"),
        )
    )
    params = exchange.calls[-1][1][5]
    assert params["stopLoss"]["triggerPrice"] == 64692.02


async def test_丸めに失敗してもそのまま送る(broker: CcxtBroker, exchange: FakeExchange) -> None:
    """price_to_precision を持たない取引所でも発注できること。"""
    await broker.place_order(
        OrderRequest(
            symbol="BTC_USDT",
            side=Side.BUY,
            quantity=Decimal("0.01"),
            stop_loss=Decimal("58000.5"),
        )
    )
    params = exchange.calls[-1][1][5]
    assert params["stopLoss"]["triggerPrice"] == 58000.5
