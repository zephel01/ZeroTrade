"""ccxt 経由の汎用アダプタ（100以上の仮想通貨取引所）。

ccxt の統一APIは :class:`~zerotrade.brokers.base.BaseBroker` とほぼ1対1で
対応する。``fetch_balance`` / ``fetch_positions`` / ``create_order`` /
``cancel_order`` / ``fetch_order`` / ``fetch_open_orders`` / ``fetch_ticker`` /
``fetch_ohlcv``。この1ファイルで対応取引所すべてが使えるようになる。

**ただし ccxt が統一するのはインターフェースであって意味ではない。**
取引所ごとに違うまま残り、実際に事故になるのは次のあたりである。

* **注文の刻みと最小数量。** ここを合わせないと注文自体が弾かれる。
  ``market['precision']`` と ``market['limits']`` から取って丸める。
* **ポジションモード。** 一方向（one-way）と両建て（hedge）で
  決済注文の意味が変わる。ZeroTrade は一方向を前提にしている。
* **``reduceOnly`` の対応可否。** 対応していない取引所では、決済のつもりの
  注文が新規建てになりうる。:meth:`place_order` で建玉を確認してから送る。
* **ストップ注文の指定方法。** ``stopLossPrice`` を受ける取引所もあれば、
  別注文として出す必要がある取引所もある。ここは取引所差が大きいため、
  添付を試みつつ StrategyRunner の強制決済を最後の砦として残す。

また仮想通貨は24時間365日動き、週末も休場もない。スワップではなく
**ファンディングレート**（多くは8時間ごと）が損益に効く。
``risk.reset_timezone`` の日次境界は便宜的なものになる点に注意。

テストネットに対応している取引所なら ``environment: practice`` で
``set_sandbox_mode`` が有効になる。**入金ゼロ・即時発行のAPIキーで
ライブ発注の経路を通しで検証できる**のが、このアダプタの最大の利点である。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from zerotrade.brokers.base import BaseBroker
from zerotrade.errors import BrokerError, ConfigError, OrderRejected
from zerotrade.log import get_logger
from zerotrade.models import (
    Balance,
    Candle,
    Order,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Side,
    Ticker,
    to_decimal,
    utcnow,
)
from zerotrade.settings import BrokerSettings

__all__ = ["CcxtBroker", "build_from_settings", "require_credentials"]

logger = get_logger(__name__)

_STATUS_MAP = {
    "open": OrderStatus.OPEN,
    "closed": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELLED,
    "cancelled": OrderStatus.CANCELLED,
    "expired": OrderStatus.EXPIRED,
    "rejected": OrderStatus.REJECTED,
}

_TYPE_MAP = {
    OrderType.MARKET: "market",
    OrderType.LIMIT: "limit",
    OrderType.STOP: "stop",
}

#: ccxt の足種表記。ZeroTrade の表記から変換する。
_TIMEFRAMES = {
    "M1": "1m",
    "M5": "5m",
    "M15": "15m",
    "M30": "30m",
    "H1": "1h",
    "H4": "4h",
    "D1": "1d",
}


def _optional_dec(entry: dict[str, Any], *keys: str) -> Decimal | None:
    """建玉のストップ等、**入っていないこともある**値を読む。

    取引所ごとにキー名が違ううえ、ネストした ``info`` にしか無いこともある。
    見つからなければ ``None``（＝設定されていない）を返す。
    0 を返すと「ストップ 0 円」と誤読されるので、必ず None にする。
    """
    raw_info = entry.get("info")
    info: dict[str, Any] = raw_info if isinstance(raw_info, dict) else {}
    for key in keys:
        for source in (entry, info):
            value = source.get(key)
            if value in (None, "", 0, "0"):
                continue
            if isinstance(value, dict):
                value = value.get("triggerPrice") or value.get("stopPrice")
                if value in (None, "", 0, "0"):
                    continue
            parsed = _dec(value)
            if parsed > 0:
                return parsed
    return None


def _dec(value: Any, default: Decimal = Decimal(0)) -> Decimal:
    if value is None:
        return default
    try:
        return to_decimal(str(value))
    except (ArithmeticError, ValueError):
        return default


class CcxtBroker(BaseBroker):
    """ccxt が対応する取引所への汎用アダプタ。"""

    name = "ccxt"

    # 決済履歴の形式が取引所ごとに違い、建玉との対応付けが信頼できない。
    # StrategyRunner が建玉の差分から損益を推定する経路へ委ねる。
    supports_closed_trades = False

    # 多くの取引所が 1000 本を上限にしている。取引所ごとに違うため、
    # 正確な値が分かるものはサブクラスで上書きする。
    max_ohlcv_count = 1000

    def __init__(
        self,
        *,
        exchange: str,
        api_key: str | None = None,
        secret: str | None = None,
        password: str | None = None,
        sandbox: bool = True,
        default_type: str = "swap",
        timeout: float = 30.0,
        symbols: list[str] | None = None,
        client: Any = None,
    ) -> None:
        self._exchange_id = exchange
        self._sandbox = sandbox
        self._exchange = client
        self._markets: dict[str, Any] = {}
        self._default_type = default_type
        # 統一シンボルの解決結果を覚える（swap なら BTC/USDT:USDT）。
        self._symbol_cache: dict[str, str] = {}
        # 一部の取引所（BingX など）は fetch_positions に銘柄リストを要求する。
        self._symbols = list(symbols or [])

        if client is None:
            try:
                import ccxt.async_support as ccxt
            except ImportError as exc:  # pragma: no cover - 依存が無い環境
                raise ConfigError(
                    "ccxt がインストールされていません。"
                    '`pip install "zerotrade[ccxt]"` を実行してください'
                ) from exc

            if exchange not in ccxt.exchanges:
                raise ConfigError(
                    f"ccxt が対応していない取引所です: {exchange}"
                    f"（例: binance, bybit, okx, bitflyer）"
                )
            factory = getattr(ccxt, exchange)
            self._exchange = factory(
                {
                    "apiKey": api_key,
                    "secret": secret,
                    "password": password,
                    "timeout": int(timeout * 1000),
                    # 取引所のレート制限に自動で従う。無効にすると BAN されうる。
                    "enableRateLimit": True,
                    "options": {"defaultType": default_type},
                }
            )

        self.name = f"ccxt:{exchange}"

    # ------------------------------------------------------------ 接続

    async def connect(self) -> None:
        if self._sandbox:
            try:
                self._exchange.set_sandbox_mode(True)
            except Exception as exc:  # 取引所によっては未対応
                raise BrokerError(
                    f"{self._exchange_id} はテストネットに対応していません: {exc}"
                ) from exc

        # 銘柄ごとの刻みと最小数量はここでしか取れない。
        # 読み込まずに発注すると精度エラーで弾かれる。
        self._markets = await self._load_markets()
        # 市場情報が入ったので、素朴な変換で埋まったかもしれない解決結果を捨てる。
        self._symbol_cache.clear()
        logger.info(
            "%s に接続しました（%s / 銘柄 %d）",
            self.name,
            "テストネット" if self._sandbox else "本番",
            len(self._markets),
        )

        # 設定した銘柄をここで解決しておく。発注時に初めて
        # 「そんな銘柄は無い」と分かるより、接続時に落ちるほうが安全。
        for symbol in self._symbols:
            logger.debug("%s → %s", symbol, self._to_ccxt(symbol))

        # ストップを添付できない取引所では、建玉が無防備になりうる。
        # 強制決済という保険はあるが、気づかないまま運用するのが最悪なので言う。
        has = getattr(self._exchange, "has", None)
        if isinstance(has, dict) and not has.get("createOrderWithTakeProfitAndStopLoss"):
            logger.warning(
                "%s は新規建てへのストップ添付に対応していない可能性があります。"
                "取引所側にストップが入らない場合、StrategyRunner の強制決済が"
                "唯一の保険になります",
                self._exchange_id,
            )

    async def _load_markets(self) -> dict[str, Any]:
        """銘柄情報を読み込む。通貨一覧が権限で弾かれたら、それを諦めて再試行する。

        ccxt の ``load_markets`` は内部で ``fetch_currencies`` も呼ぶ。
        取引所によってはこれが**資産・ウォレット系の私設エンドポイント**で、
        取引権限しか無いAPIキーでは 403 になる。

        通貨一覧は入出金の情報であって、**発注には要らない**。
        ここで落ちると取引権限だけの安全なキーが使えなくなるので、
        1度だけ通貨一覧を諦めて読み直す。取引に必要な
        ``precision`` / ``limits`` は銘柄情報の側に入っている。
        """
        try:
            return await self._call("load_markets")  # type: ignore[no-any-return]
        except BrokerError as exc:
            has = getattr(self._exchange, "has", None)
            if not isinstance(has, dict) or not has.get("fetchCurrencies"):
                raise
            logger.warning(
                "通貨一覧の取得に失敗しました（出金・送金権限が無いキーでは通常のことです）。"
                "通貨一覧を諦めて銘柄情報だけ読み直します: %s",
                exc,
            )
            has["fetchCurrencies"] = False
            return await self._call("load_markets")  # type: ignore[no-any-return]

    async def disconnect(self) -> None:
        close = getattr(self._exchange, "close", None)
        if close is not None:
            try:
                await close()
            except Exception as exc:  # pragma: no cover
                logger.debug("接続の終了に失敗しました: %s", exc)

    # ------------------------------------------------------------ 口座

    async def get_balance(self) -> Balance:
        raw = await self._call("fetch_balance")
        currency = self._quote_currency()
        entry = (raw.get(currency) or {}) if isinstance(raw, dict) else {}

        total = _dec(entry.get("total"))
        free = _dec(entry.get("free"))
        used = _dec(entry.get("used"))
        return Balance(
            currency=currency,
            equity=total,
            available=free,
            used_margin=used,
        )

    async def get_positions(self) -> list[Position]:
        try:
            raw = await self._call("fetch_positions", self._position_symbols())
        except BrokerError:
            # 現物のみの取引所は fetch_positions を持たない。
            return []

        logger.debug("fetch_positions の生応答: %s", raw)
        positions = []
        for entry in raw or []:
            contracts = _dec(entry.get("contracts") or entry.get("contractSize"))
            if contracts == 0:
                continue
            side = Side.BUY if str(entry.get("side", "long")) == "long" else Side.SELL
            positions.append(
                Position(
                    symbol=self._to_zerotrade(str(entry.get("symbol", ""))),
                    side=side,
                    quantity=abs(contracts),
                    entry_price=_dec(entry.get("entryPrice")),
                    unrealized_pnl=_dec(entry.get("unrealizedPnl")),
                    # ストップが取引所側に入っているかを確かめられるようにする。
                    # 建玉が無防備かどうかは、運用中いちばん知りたいことの1つ。
                    stop_loss=_optional_dec(entry, "stopLossPrice", "stopLoss"),
                    take_profit=_optional_dec(entry, "takeProfitPrice", "takeProfit"),
                    broker_position_id=str(entry.get("id") or entry.get("symbol") or ""),
                )
            )
        return positions

    # ------------------------------------------------------------ 相場

    async def get_ticker(self, symbol: str) -> Ticker:
        raw = await self._call("fetch_ticker", self._to_ccxt(symbol))
        bid, ask = _dec(raw.get("bid")), _dec(raw.get("ask"))
        if bid <= 0 or ask <= 0:
            # 板が薄い銘柄では bid/ask が返らないことがある。
            last = _dec(raw.get("last") or raw.get("close"))
            if last <= 0:
                raise BrokerError(f"{symbol} の気配値を取得できませんでした")
            bid = ask = last

        stamp = raw.get("timestamp")
        return Ticker(
            symbol=symbol,
            bid=bid,
            ask=ask,
            timestamp=(datetime.fromtimestamp(int(stamp) / 1000, tz=UTC) if stamp else utcnow()),
        )

    async def get_ohlcv(
        self,
        symbol: str,
        *,
        granularity: str = "M5",
        count: int = 200,
        end: datetime | None = None,
    ) -> list[Candle]:
        timeframe = _TIMEFRAMES.get(granularity.upper())
        if timeframe is None:
            raise ConfigError(
                f"ccxt は足種 {granularity} に対応していません"
                f"（利用可能: {', '.join(_TIMEFRAMES)}）"
            )

        # 上限を超えると取引所がエラーを返し、1本も取れない。
        # 呼び出し側が上限を知らなくても済むよう、ここでも抑える。
        count = min(count, self.max_ohlcv_count)

        # ccxt は since（開始時刻）指定なので、遡り取得は開始側から逆算する。
        since = None
        if end is not None:
            span = _timeframe_seconds(timeframe) * count
            since = int((end.timestamp() - span) * 1000)

        raw = await self._call("fetch_ohlcv", self._to_ccxt(symbol), timeframe, since, count)
        candles = [
            Candle(
                symbol=symbol,
                timestamp=datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC),
                open=_dec(row[1]),
                high=_dec(row[2]),
                low=_dec(row[3]),
                close=_dec(row[4]),
                volume=_dec(row[5] if len(row) > 5 else 0),
            )
            for row in raw or []
        ]
        if end is not None:
            candles = [c for c in candles if c.timestamp < end]
        candles.sort(key=lambda c: c.timestamp)

        # 最新の足はまだ形成中のことがある。ccxt はそれを区別せず返すため、
        # 時刻から判定して印を付ける。**未確定足を戦略に見せると、
        # バックテスト（確定足しか見えない）と挙動が変わる。**
        span = _timeframe_seconds(timeframe)
        now = utcnow()
        return [
            replace(candle, complete=(candle.timestamp + timedelta(seconds=span) <= now))
            for candle in candles
        ]

    # ------------------------------------------------------------ 注文

    async def place_order(self, request: OrderRequest) -> Order:
        symbol = self._to_ccxt(request.symbol)

        if request.reduce_only:
            # reduceOnly を無視する取引所があり、そこでは決済注文が
            # そのまま新規建てになる。送る前にこちらで確かめる。
            existing = {p.symbol: p for p in await self.get_positions()}
            live = existing.get(request.symbol)
            if live is None or live.side is request.side:
                raise OrderRejected(
                    f"{request.symbol}: 決済対象の建玉がありません",
                    client_order_id=request.client_order_id,
                )

        amount = self._round_amount(symbol, request.quantity)
        if amount <= 0:
            raise OrderRejected(
                f"{request.symbol}: 数量 {request.quantity} が最小単位を下回ります",
                client_order_id=request.client_order_id,
            )

        params = self._order_params(request)

        price = request.limit_price or request.stop_price
        raw = await self._call(
            "create_order",
            symbol,
            _TYPE_MAP[request.order_type],
            str(request.side),
            float(amount),
            float(price) if price is not None else None,
            params,
        )
        return self._to_order(raw, request)

    async def cancel_order(self, order_id: str) -> Order:
        raw = await self._call("cancel_order", order_id)
        return self._to_order(raw)

    async def get_order(self, order_id: str) -> Order:
        raw = await self._call("fetch_order", order_id)
        return self._to_order(raw)

    async def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        raw = await self._call("fetch_open_orders", self._to_ccxt(symbol) if symbol else None)
        # 取引所ごとにキー名が違うため、生の応答を残しておく。
        # 「条件注文が見つからない」ときに、無いのか読めていないのかを切り分ける唯一の材料。
        logger.debug("fetch_open_orders(%s) の生応答: %s", symbol, raw)
        return [self._to_order(entry) for entry in raw or []]

    async def set_leverage(self, symbol: str, leverage: Decimal) -> None:
        await self._call("set_leverage", float(leverage), self._to_ccxt(symbol))

    # ------------------------------------------------------------ 変換

    def _position_symbols(self) -> list[str] | None:
        """``fetch_positions`` へ渡す銘柄リスト。

        省略できる取引所も多いが、BingX のように必須の取引所がある。
        設定の ``symbols`` を渡しておけば両方に対応できる。
        """
        return [self._to_ccxt(s) for s in self._symbols] or None

    def _order_params(self, request: OrderRequest) -> dict[str, Any]:
        """発注時の追加パラメータ。取引所固有の指定はサブクラスで足す。

        **ストップの渡し方には2種類あり、意味がまったく違う。**

        * ``stopLossPrice`` … 「この注文自体がストップ注文である」の意。
          ccxt は ``reduceOnly`` を立てるため、**既存の建玉が要る**。
          建玉が無い状態で送ると BingX は ``position not exist``
          （code 109420）で弾く。
        * ``stopLoss: {"triggerPrice": X}`` … 「新規建てにストップを
          添付する」の意。こちらが欲しい挙動である。

        ZeroTrade の ``OrderRequest.stop_loss`` は後者を意図しているので、
        入れ子の辞書で渡す。前者を使うと**新規建てが通らない**か、
        通ったとしてもストップの無い建玉ができる。
        """
        params: dict[str, Any] = {"clientOrderId": request.client_order_id}
        if request.reduce_only:
            params["reduceOnly"] = True
            # 決済注文にストップを添付する意味はない。
            return params

        # 対応していれば約定と同時にストップが入る。無視されても
        # StrategyRunner の強制決済が最後の砦として働く。
        ccxt_symbol = self._to_ccxt(request.symbol)
        if request.stop_loss is not None:
            params["stopLoss"] = {"triggerPrice": self._round_price(ccxt_symbol, request.stop_loss)}
        if request.take_profit is not None:
            params["takeProfit"] = {
                "triggerPrice": self._round_price(ccxt_symbol, request.take_profit)
            }
        return params

    def _to_ccxt(self, symbol: str) -> str:
        """``BTC_USDT`` を ccxt の統一シンボルへ変換する。

        **単純な文字列変換では足りない。** 永続契約（swap）の統一シンボルは
        ``BTC/USDT`` ではなく ``BTC/USDT:USDT`` （決済通貨のサフィックス付き）
        であり、現物の ``BTC/USDT`` とは別の銘柄として登録されている。
        素朴に置換すると「そんな銘柄は無い」と弾かれる。

        そこで ``load_markets`` の結果に照らして解決し、
        ``market_type``（swap / spot）に一致するものを優先する。
        市場情報がまだ無い場合だけ素朴な変換にフォールバックする。
        """
        cached = self._symbol_cache.get(symbol)
        if cached is not None:
            return cached
        resolved = self._resolve_symbol(symbol)
        self._symbol_cache[symbol] = resolved
        return resolved

    def _candidates(self, symbol: str) -> list[str]:
        """統一シンボルの候補を優先順に並べる。"""
        plain = (symbol if "/" in symbol else symbol.replace("_", "/")).split(":")[0]
        quote = plain.split("/")[-1]
        # swap を先に見る。既定が swap のため、意図せず現物へ流れるのを防ぐ。
        contract_first = self._default_type in {"swap", "future"}
        ordered = [f"{plain}:{quote}", plain] if contract_first else [plain, f"{plain}:{quote}"]
        if ":" in symbol:
            ordered.insert(0, symbol)
        return ordered

    def _resolve_symbol(self, symbol: str) -> str:
        candidates = self._candidates(symbol)
        if not self._markets:
            return candidates[0]

        for candidate in candidates:
            market = self._markets.get(candidate)
            if market is not None and market.get("type") == self._default_type:
                return candidate
        for candidate in candidates:
            if candidate in self._markets:
                logger.warning(
                    "%s は %s 市場に見つからなかったため %s を使います",
                    symbol,
                    self._default_type,
                    candidate,
                )
                return candidate
        raise ConfigError(
            f"{self._exchange_id} に銘柄 {symbol} がありません"
            f"（{self._default_type} 市場で探しました）。"
            f"候補: {', '.join(self._similar_symbols(symbol)) or 'なし'}"
        )

    def _similar_symbols(self, symbol: str, limit: int = 5) -> list[str]:
        """設定ミスを直しやすくするため、近い名前の銘柄を挙げる。

        完全一致だけでは足りない。取引所によっては ``1000PEPE`` を
        ``PEPE``（契約サイズ1000）として登録するなど、名前の付け方が違う。
        部分一致まで拾って候補を出す。
        """
        base = (symbol if "/" in symbol else symbol.replace("_", "/")).split("/")[0].upper()
        # 数字の接頭辞（1000PEPE → PEPE）も候補に含める
        stripped = base.lstrip("0123456789") or base
        exact, partial = [], []
        for name, market in self._markets.items():
            if market.get("type") != self._default_type:
                continue
            market_base = str(market.get("base") or "").upper()
            if market_base == base:
                exact.append(name)
            elif market_base in {stripped, base} or stripped in market_base:
                partial.append(name)
        return sorted(exact)[:limit] or sorted(partial)[:limit]

    @staticmethod
    def _to_zerotrade(symbol: str) -> str:
        """ccxt の ``BTC/USDT:USDT`` を ``BTC_USDT`` へ。"""
        return symbol.split(":")[0].replace("/", "_")

    def _round_price(self, ccxt_symbol: str, price: Decimal) -> float:
        """取引所の刻みに合わせて価格を丸める。

        ストップは ATR から計算するため ``64692.01652383044993090602514`` のような
        端数が付く。取引所は価格の刻み（tick size）を持っており、刻みに合わない
        価格は**丸められるか、拒否される**。どちらになるかは取引所次第なので、
        こちらで揃えてから送る。

        丸めの方向は取引所任せでよい。ストップの位置が1ティック動いても
        リスク量はほとんど変わらない（数量の丸めと違い、上振れが危険側に
        効かない）ため。
        """
        try:
            return float(self._exchange.price_to_precision(ccxt_symbol, float(price)))
        except Exception as exc:
            logger.debug("価格の丸めに失敗しました（そのまま送ります）: %s", exc)
            return float(price)

    def _round_amount(self, ccxt_symbol: str, quantity: Decimal) -> Decimal:
        """取引所の刻みに合わせて数量を丸める（切り捨て）。

        切り上げるとリスク上限を超えるので、必ず切り捨てる。
        最小数量を下回った場合は 0 を返し、呼び出し側が見送る。
        """
        market = self._markets.get(ccxt_symbol)
        if market is None:
            return quantity

        try:
            rounded = to_decimal(self._exchange.amount_to_precision(ccxt_symbol, float(quantity)))
        except Exception as exc:
            logger.debug("数量の丸めに失敗しました（そのまま送ります）: %s", exc)
            return quantity

        minimum = ((market.get("limits") or {}).get("amount") or {}).get("min")
        if minimum is not None and rounded < to_decimal(str(minimum)):
            return Decimal(0)
        return rounded

    def _quote_currency(self) -> str:
        """残高を測る通貨。

        取引所は数百〜千の銘柄を返すため、市場一覧の先頭から取ると
        まったく関係ない通貨を掴む（実際 BingX では 942 銘柄が返る）。
        **設定した銘柄の決済通貨**を見るのが正しい。
        """
        for symbol in self._symbols:
            market = self._markets.get(self._to_ccxt(symbol))
            if market is None:
                continue
            currency = market.get("settle") or market.get("quote")
            if currency:
                return str(currency)
        return "USDT"

    def _to_order(self, raw: dict[str, Any], request: OrderRequest | None = None) -> Order:
        info = raw or {}
        symbol = self._to_zerotrade(str(info.get("symbol", "")))
        side = Side.SELL if str(info.get("side", "buy")) == "sell" else Side.BUY
        client_id = str(
            info.get("clientOrderId")
            or (request.client_order_id if request else "")
            or info.get("id", "")
        )

        filled = _dec(info.get("filled"))
        amount = _dec(info.get("amount"), filled)
        status = _STATUS_MAP.get(str(info.get("status", "")), OrderStatus.OPEN)
        if status is OrderStatus.OPEN and 0 < filled < amount:
            status = OrderStatus.PARTIALLY_FILLED

        stamp = info.get("timestamp")
        return Order(
            client_order_id=client_id,
            symbol=symbol or (request.symbol if request else ""),
            side=side,
            quantity=amount or (request.quantity if request else Decimal(0)),
            order_type=(
                OrderType.LIMIT if str(info.get("type", "")) == "limit" else OrderType.MARKET
            ),
            status=status,
            broker_order_id=str(info.get("id", "")) or None,
            filled_quantity=filled,
            average_price=_dec(info["average"]) if info.get("average") else None,
            limit_price=_dec(info["price"]) if info.get("price") else None,
            # 取引所から取り直した注文にもトリガー価格を載せる。
            # ここが空だと「条件注文が実在するのに検出できない」状態になり、
            # **建玉が無防備かどうかを確かめる手段が無くなる**。
            stop_price=_optional_dec(info, "stopPrice", "triggerPrice"),
            stop_loss=(
                _optional_dec(info, "stopLossPrice", "stopLoss")
                or (request.stop_loss if request else None)
            ),
            take_profit=(
                _optional_dec(info, "takeProfitPrice", "takeProfit")
                or (request.take_profit if request else None)
            ),
            reduce_only=bool(request.reduce_only) if request else False,
            created_at=(datetime.fromtimestamp(int(stamp) / 1000, tz=UTC) if stamp else utcnow()),
        )

    # ------------------------------------------------------------ 呼び出し

    async def _call(self, method: str, *args: Any) -> Any:
        """ccxt の呼び出しを包み、失敗を BrokerError へ正規化する。"""
        func = getattr(self._exchange, method, None)
        if func is None:
            raise BrokerError(f"{self.name} は {method} に対応していません")
        try:
            return await func(*args)
        except Exception as exc:
            # ccxt の例外階層は取引所ごとに派生が多く、名前で判別しない。
            raise BrokerError(f"{self.name}.{method} が失敗しました: {exc}") from exc


def _timeframe_seconds(timeframe: str) -> int:
    """``5m`` のような表記を秒に直す。"""
    units = {"m": 60, "h": 3600, "d": 86_400, "w": 604_800}
    return int(timeframe[:-1]) * units.get(timeframe[-1], 60)


def require_credentials(settings: BrokerSettings, exchange: str) -> None:
    """APIキーが無いまま組み立てるのを止める。

    ccxt の取引所オブジェクトは鍵が無くても生成でき、公開エンドポイント
    （``load_markets`` など）は通ってしまう。**接続したように見えて残高だけ
    落ちる**という分かりにくい壊れ方をするので、ここで先に落とす。
    ``fallback_to_paper`` が有効なら PaperBroker へ切り替わる。
    """
    missing = [
        name
        for name, value in (("api_token", settings.api_token), ("api_secret", settings.api_secret))
        if not value
    ]
    if missing:
        raise BrokerError(
            f"{exchange} の認証情報が設定されていません（未設定: {', '.join(missing)}）。"
            f"環境変数を export したうえで再実行してください"
        )


def build_from_settings(
    settings: BrokerSettings,
    *,
    symbols: list[str] | None = None,
    client: Any = None,
) -> CcxtBroker:
    """設定から CcxtBroker を組み立てる。"""
    if not settings.exchange:
        raise ConfigError(
            "ccxt を使うには broker.exchange を指定してください（例: binance, bybit）"
        )
    if client is None:
        require_credentials(settings, settings.exchange)
    return CcxtBroker(
        exchange=settings.exchange,
        api_key=settings.api_token,
        secret=settings.api_secret,
        password=settings.api_passphrase,
        sandbox=settings.environment == "practice",
        default_type=settings.market_type,
        timeout=settings.timeout_seconds,
        symbols=symbols,
        client=client,
    )
