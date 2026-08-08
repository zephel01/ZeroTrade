"""OANDA v20 REST アダプタ。

OANDA 固有の事情はすべてこのファイルに閉じ込める:

* 数量は符号付きの ``units`` で表現される（正=買い / 負=売り）。
  ZeroTrade 側は常に「正の数量 + Side」で扱うため、境界で変換する。
* 銘柄コードは ``USD_JPY`` 形式。
* ストップ・利確は注文に ``stopLossOnFill`` / ``takeProfitOnFill`` として添付する。
* すべての数値が文字列で返る。Decimal へ変換してから外へ出す。

APIリファレンス: https://developer.oanda.com/rest-live-v20/introduction/
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from zerotrade.brokers.base import BaseBroker
from zerotrade.errors import BrokerError, OrderRejected
from zerotrade.log import get_logger
from zerotrade.models import (
    Balance,
    Candle,
    ClosedTrade,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Side,
    Ticker,
    TimeInForce,
    utcnow,
)
from zerotrade.settings import BrokerSettings

__all__ = ["OandaBroker"]

logger = get_logger(__name__)

_BASE_URLS = {
    "practice": "https://api-fxpractice.oanda.com",
    "live": "https://api-fxtrade.oanda.com",
}

# OANDA の注文状態 → ZeroTrade の OrderStatus
_STATE_MAP = {
    "PENDING": OrderStatus.OPEN,
    "FILLED": OrderStatus.FILLED,
    "TRIGGERED": OrderStatus.OPEN,
    "CANCELLED": OrderStatus.CANCELLED,
}

_ORDER_TYPE_MAP = {
    OrderType.MARKET: "MARKET",
    OrderType.LIMIT: "LIMIT",
    OrderType.STOP: "STOP",
}

_TIF_MAP = {
    TimeInForce.GTC: "GTC",
    TimeInForce.IOC: "IOC",
    TimeInForce.FOK: "FOK",
    TimeInForce.DAY: "GFD",
}


class OandaBroker(BaseBroker):
    """OANDA v20 REST API アダプタ。"""

    name = "oanda"
    supports_closed_trades = True

    def __init__(
        self,
        *,
        account_id: str,
        api_token: str,
        environment: str = "practice",
        base_url: str | None = None,
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not account_id or not api_token:
            raise BrokerError("OANDA の account_id と api_token が必要です")
        if environment not in _BASE_URLS:
            raise BrokerError(f"未知の environment です: {environment}")

        self._account_id = account_id
        self._base_url = base_url or _BASE_URLS[environment]
        self._timeout = timeout
        self._token = api_token
        self._external_client = client
        self._client: httpx.AsyncClient | None = client

    # ------------------------------------------------------------ 接続

    async def connect(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                    "Accept-Datetime-Format": "RFC3339",
                },
            )
        # 認証情報が有効かどうかは、ここで一度叩いて確かめる。
        # 発注の瞬間に 401 が出るより起動時に落ちた方が安全。
        await self._request("GET", f"/v3/accounts/{self._account_id}/summary")
        logger.info("OANDA に接続しました（account=%s）", self._account_id)

    async def disconnect(self) -> None:
        if self._client is not None and self._external_client is None:
            await self._client.aclose()
        if self._external_client is None:
            self._client = None

    # ------------------------------------------------------------ 口座

    async def get_balance(self) -> Balance:
        payload = await self._request("GET", f"/v3/accounts/{self._account_id}/summary")
        account = payload.get("account", {})
        return Balance(
            currency=str(account.get("currency", "JPY")),
            # NAV（純資産）が実質的な equity。balance は含み損益を含まない。
            equity=_dec(account.get("NAV", account.get("balance", 0))),
            available=_dec(account.get("marginAvailable", 0)),
            used_margin=_dec(account.get("marginUsed", 0)),
        )

    async def get_positions(self) -> list[Position]:
        payload = await self._request("GET", f"/v3/accounts/{self._account_id}/openPositions")
        positions: list[Position] = []

        for entry in payload.get("positions", []):
            symbol = str(entry.get("instrument", ""))
            for side, key in ((Side.BUY, "long"), (Side.SELL, "short")):
                leg = entry.get(key) or {}
                units = _dec(leg.get("units", 0))
                if units == 0:
                    continue
                positions.append(
                    Position(
                        symbol=symbol,
                        side=side,
                        quantity=abs(units),
                        entry_price=_dec(leg.get("averagePrice", 0)),
                        unrealized_pnl=_dec(leg.get("unrealizedPL", 0)),
                        broker_position_id=f"{symbol}:{key}",
                    )
                )
        return positions

    async def get_closed_trades(self, since: datetime | None = None) -> list[ClosedTrade]:
        payload = await self._request(
            "GET",
            f"/v3/accounts/{self._account_id}/trades",
            params={"state": "CLOSED", "count": 200},
        )
        trades: list[ClosedTrade] = []

        for entry in payload.get("trades", []):
            closed_at = _parse_time(entry.get("closeTime"))
            if closed_at is None or (since is not None and closed_at < since):
                continue
            initial_units = _dec(entry.get("initialUnits", 0))
            trades.append(
                ClosedTrade(
                    symbol=str(entry.get("instrument", "")),
                    side=Side.BUY if initial_units > 0 else Side.SELL,
                    quantity=abs(initial_units),
                    entry_price=_dec(entry.get("price", 0)),
                    exit_price=_dec(entry.get("averageClosePrice", 0)),
                    realized_pnl=_dec(entry.get("realizedPL", 0)),
                    opened_at=_parse_time(entry.get("openTime")) or closed_at,
                    closed_at=closed_at,
                    trade_id=str(entry.get("id", "")),
                )
            )
        return trades

    # ------------------------------------------------------------ 相場

    async def get_ticker(self, symbol: str) -> Ticker:
        payload = await self._request(
            "GET",
            f"/v3/accounts/{self._account_id}/pricing",
            params={"instruments": symbol},
        )
        prices = payload.get("prices", [])
        if not prices:
            raise BrokerError(f"{symbol} の価格を取得できませんでした")

        price = prices[0]
        bids = price.get("bids") or []
        asks = price.get("asks") or []
        if not bids or not asks:
            raise BrokerError(f"{symbol} の板情報が空です（市場が閉じている可能性があります）")

        return Ticker(
            symbol=symbol,
            bid=_dec(bids[0].get("price", 0)),
            ask=_dec(asks[0].get("price", 0)),
            timestamp=_parse_time(price.get("time")) or utcnow(),
        )

    #: v20 API が1リクエストで返せる足の上限。
    MAX_CANDLES = 5000

    async def get_ohlcv(
        self,
        symbol: str,
        *,
        granularity: str = "M5",
        count: int = 200,
        end: datetime | None = None,
    ) -> list[Candle]:
        params: dict[str, Any] = {
            "granularity": granularity,
            "count": min(count, self.MAX_CANDLES),
            # price=M は仲値。売買別の足が要る場合は "BA" を指定する。
            "price": "M",
        }
        if end is not None:
            # OANDA は to を含まない排他的な上限として扱う。
            params["to"] = end.astimezone(UTC).isoformat().replace("+00:00", "Z")

        payload = await self._request("GET", f"/v3/instruments/{symbol}/candles", params=params)
        candles: list[Candle] = []

        for entry in payload.get("candles", []):
            mid = entry.get("mid") or {}
            timestamp = _parse_time(entry.get("time"))
            if timestamp is None:
                continue
            candles.append(
                Candle(
                    symbol=symbol,
                    timestamp=timestamp,
                    open=_dec(mid.get("o", 0)),
                    high=_dec(mid.get("h", 0)),
                    low=_dec(mid.get("l", 0)),
                    close=_dec(mid.get("c", 0)),
                    volume=_dec(entry.get("volume", 0)),
                    complete=bool(entry.get("complete", True)),
                )
            )
        return candles

    # ------------------------------------------------------------ 注文

    async def place_order(self, request: OrderRequest) -> Order:
        if request.reduce_only:
            # OANDA に reduce_only は無い。反対方向の成行をそのまま出すと、
            # 建玉が既に無い場合に新規建てになってしまうため、
            # 建玉決済専用のエンドポイントへ回す。
            order = await self.close_position(request.symbol)
            if order is None:
                raise OrderRejected(
                    f"{request.symbol}: 決済対象の建玉がありません",
                    client_order_id=request.client_order_id,
                )
            return order

        body = self._build_order_body(request)
        payload = await self._request("POST", f"/v3/accounts/{self._account_id}/orders", json=body)

        # 拒否された場合、成功HTTPステータスでも reject 系トランザクションが返る。
        reject = payload.get("orderRejectTransaction") or payload.get("orderCancelTransaction")
        if reject is not None and "orderFillTransaction" not in payload:
            reason = str(reject.get("reason", "UNKNOWN"))
            raise OrderRejected(
                f"OANDA が注文を拒否しました: {reason}",
                client_order_id=request.client_order_id,
            )

        return self._order_from_create(request, payload)

    async def cancel_order(self, order_id: str) -> Order:
        await self._request("PUT", f"/v3/accounts/{self._account_id}/orders/{order_id}/cancel")
        return await self.get_order(order_id)

    async def get_order(self, order_id: str) -> Order:
        payload = await self._request("GET", f"/v3/accounts/{self._account_id}/orders/{order_id}")
        entry = payload.get("order")
        if entry is None:
            raise BrokerError(f"注文が見つかりません: {order_id}")
        return self._order_from_entry(entry)

    async def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        payload = await self._request("GET", f"/v3/accounts/{self._account_id}/pendingOrders")
        orders = [self._order_from_entry(e) for e in payload.get("orders", [])]
        if symbol is not None:
            orders = [o for o in orders if o.symbol == symbol]
        return orders

    async def update_position_stop(self, symbol: str, stop_loss: Decimal) -> Position | None:
        """建玉に紐づくトレードのストップを更新する。

        OANDA では建玉ではなく **トレード単位** でストップを持つため、
        該当銘柄の未決済トレードを引いてから個別に更新する。
        """
        payload = await self._request(
            "GET",
            f"/v3/accounts/{self._account_id}/openTrades",
        )
        trades = [t for t in payload.get("trades", []) if str(t.get("instrument", "")) == symbol]
        if not trades:
            return None

        for trade in trades:
            await self._request(
                "PUT",
                f"/v3/accounts/{self._account_id}/trades/{trade['id']}/orders",
                json={"stopLoss": {"price": _fmt(stop_loss), "timeInForce": "GTC"}},
            )

        units = _dec(trades[0].get("currentUnits", 0))
        return Position(
            symbol=symbol,
            side=Side.BUY if units > 0 else Side.SELL,
            quantity=abs(units),
            entry_price=_dec(trades[0].get("price", 0)),
            stop_loss=stop_loss,
            broker_position_id=str(trades[0].get("id", "")),
        )

    async def close_position(self, symbol: str) -> Order | None:
        """OANDA 専用の建玉一括決済エンドポイントを使う。"""
        positions = {p.symbol: p for p in await self.get_positions()}
        position = positions.get(symbol)
        if position is None:
            return None

        key = "longUnits" if position.side is Side.BUY else "shortUnits"
        payload = await self._request(
            "PUT",
            f"/v3/accounts/{self._account_id}/positions/{symbol}/close",
            json={key: "ALL"},
        )
        fill = (
            payload.get("longOrderFillTransaction")
            or payload.get("shortOrderFillTransaction")
            or {}
        )
        return Order(
            client_order_id=f"close-{symbol}",
            symbol=symbol,
            side=position.side.opposite,
            quantity=position.quantity,
            order_type=OrderType.MARKET,
            status=OrderStatus.FILLED if fill else OrderStatus.REJECTED,
            broker_order_id=str(fill.get("id", "")) or None,
            filled_quantity=abs(_dec(fill.get("units", 0))),
            average_price=_dec(fill["price"]) if "price" in fill else None,
        )

    # ------------------------------------------------------------ 変換

    def _build_order_body(self, request: OrderRequest) -> dict[str, Any]:
        """OrderRequest を OANDA の注文ボディへ変換する。"""
        # OANDA は符号付き units。売りは負値。
        units = request.quantity * request.side.sign

        order: dict[str, Any] = {
            "type": _ORDER_TYPE_MAP[request.order_type],
            "instrument": request.symbol,
            "units": _fmt(units),
            "timeInForce": _TIF_MAP[request.time_in_force],
            # client_order_id を渡しておくと、応答を取りこぼしても
            # "@クライアントID" で後から注文を引ける。
            "clientExtensions": {"id": request.client_order_id, "tag": "zerotrade"},
        }

        if request.order_type is OrderType.MARKET:
            # 成行に GTC は指定できない。
            order["timeInForce"] = "FOK"
        if request.limit_price is not None:
            order["price"] = _fmt(request.limit_price)
        if request.order_type is OrderType.STOP and request.stop_price is not None:
            order["price"] = _fmt(request.stop_price)
        if request.stop_loss is not None:
            order["stopLossOnFill"] = {"price": _fmt(request.stop_loss), "timeInForce": "GTC"}
        if request.take_profit is not None:
            order["takeProfitOnFill"] = {
                "price": _fmt(request.take_profit),
                "timeInForce": "GTC",
            }
        if request.reduce_only:
            # OANDA には reduce_only が無いため、決済は units の符号で表現する。
            # （反対方向の成行が既存建玉を相殺する仕様）
            order.pop("stopLossOnFill", None)
            order.pop("takeProfitOnFill", None)

        return {"order": order}

    def _order_from_create(self, request: OrderRequest, payload: dict[str, Any]) -> Order:
        """発注応答から Order を組み立てる。"""
        create = payload.get("orderCreateTransaction") or {}
        fill = payload.get("orderFillTransaction")

        order = Order(
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            order_type=request.order_type,
            broker_order_id=str(create.get("id", "")) or None,
            limit_price=request.limit_price,
            stop_price=request.stop_price,
            stop_loss=request.stop_loss,
            take_profit=request.take_profit,
            status=OrderStatus.OPEN,
        )

        if fill:
            order.status = OrderStatus.FILLED
            order.filled_quantity = abs(_dec(fill.get("units", request.quantity)))
            order.average_price = _dec(fill.get("price", 0))
            # 約定トランザクションIDの方が後続の照会で使いやすい。
            order.broker_order_id = str(fill.get("orderID", order.broker_order_id or ""))
        return order

    def _order_from_entry(self, entry: dict[str, Any]) -> Order:
        """注文照会のレスポンスから Order を組み立てる。"""
        units = _dec(entry.get("units", 0))
        side = Side.BUY if units >= 0 else Side.SELL
        client_id = str((entry.get("clientExtensions") or {}).get("id", "")) or str(
            entry.get("id", "")
        )
        filled = abs(_dec(entry.get("filledUnits", 0)))
        quantity = abs(units)

        status = _STATE_MAP.get(str(entry.get("state", "")), OrderStatus.OPEN)
        if status is OrderStatus.OPEN and 0 < filled < quantity:
            status = OrderStatus.PARTIALLY_FILLED

        return Order(
            client_order_id=client_id,
            symbol=str(entry.get("instrument", "")),
            side=side,
            quantity=quantity,
            order_type=_reverse_order_type(str(entry.get("type", "MARKET"))),
            status=status,
            broker_order_id=str(entry.get("id", "")) or None,
            filled_quantity=filled,
            average_price=_dec(entry["averageFillPrice"]) if "averageFillPrice" in entry else None,
            limit_price=_dec(entry["price"]) if "price" in entry else None,
            created_at=_parse_time(entry.get("createTime")) or utcnow(),
        )

    # ------------------------------------------------------------ HTTP

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """API を叩き、失敗はすべて BrokerError へ正規化する。"""
        if self._client is None:
            raise BrokerError("OANDA へ未接続です。connect() を呼んでください")

        try:
            response = await self._client.request(method, path, params=params, json=json)
        except httpx.HTTPError as exc:
            raise BrokerError(f"OANDA への通信に失敗しました: {exc}") from exc

        if response.status_code >= 400:
            raise BrokerError(
                f"OANDA API エラー {response.status_code}: {_error_message(response)}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise BrokerError(f"OANDA の応答を解釈できませんでした: {exc}") from exc

        if not isinstance(payload, dict):
            raise BrokerError(f"想定外の応答形式です: {type(payload).__name__}")
        return payload


# ---------------------------------------------------------------- ヘルパ


def _dec(value: Any) -> Decimal:
    """OANDA が返す文字列数値を Decimal にする。壊れていれば 0。"""
    if value is None:
        return Decimal(0)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        logger.warning("数値として解釈できない値です: %r", value)
        return Decimal(0)


def _fmt(value: Decimal) -> str:
    """Decimal を OANDA が受け付ける文字列にする（指数表記を避ける）。"""
    return format(value.normalize(), "f")


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    # OANDA のRFC3339はナノ秒まで返すことがあり、fromisoformat が拒否する。
    if "." in text:
        head, _, tail = text.partition(".")
        fraction = "".join(c for c in tail if c.isdigit())[:6]
        suffix = tail[len(fraction) :].lstrip("0123456789") or "+00:00"
        text = f"{head}.{fraction}{suffix}"
    text = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        logger.warning("時刻を解釈できませんでした: %r", value)
        return None


def _reverse_order_type(value: str) -> OrderType:
    for order_type, name in _ORDER_TYPE_MAP.items():
        if name == value:
            return order_type
    return OrderType.MARKET


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(payload, dict):
        return str(payload.get("errorMessage") or payload)
    return str(payload)


def build_from_settings(
    settings: BrokerSettings, *, client: httpx.AsyncClient | None = None
) -> OandaBroker:
    """設定から OandaBroker を組み立てる。"""
    if not settings.account_id or not settings.api_token:
        raise BrokerError(
            "OANDA の認証情報が設定されていません"
            "（環境変数 OANDA_ACCOUNT_ID / OANDA_API_TOKEN を設定してください）"
        )
    return OandaBroker(
        account_id=settings.account_id,
        api_token=settings.api_token,
        environment=settings.environment,
        base_url=settings.base_url,
        timeout=settings.timeout_seconds,
        client=client,
    )
