"""BingX アダプタ（ccxt 経由 + BingX 固有の作り込み）。

plan.md で「（将来）BingXAdapter など」として置かれていた枠を埋めるもの。
:class:`~zerotrade.brokers.ccxt_broker.CcxtBroker` を土台に、
汎用アダプタでは対応しきれない BingX 固有の事情を4点だけ足している。

**1. 建玉照会に銘柄リストが必須。** BingX の ``fetchPositions`` は
統一シンボルのリストを要求する。渡さないと建玉が取れず、その結果
「建玉が無い」と誤認して二重に建ててしまう。設定の ``symbols`` を渡す。

**2. ``positionSide`` の指定。** BingX の永続契約は片方向モード（``BOTH``）と
ヘッジモード（``LONG`` / ``SHORT``）を持つ。ZeroTrade は銘柄あたり1建玉を
前提にしているので、接続時に**片方向モードへ寄せてから** ``BOTH`` で発注する。
ヘッジモードのまま動かすと、決済注文が反対側の新規建てとして通ってしまう。

**3. ``clientOrderId`` の照会期限は2時間。** 冪等キーとして送りはするが、
注文の追跡には取引所側の注文ID（``broker_order_id``）を優先する。

**4. 確定損益が正確に取れる。** ``fetchPositionsHistory`` が決済済み建玉と
実現損益を返すため、:attr:`supports_closed_trades` を True にできる。
汎用アダプタは建玉の差分から推定するが、BingX ではその必要がない。
日次・週次の損失上限がより正確に効く。

テストネットは VST（Virtual Simulated Trading、``open-api-vst.bingx.com``）。
``environment: practice`` で自動的にそちらへ向く。**入金ゼロで発注から決済まで
一周させられる**ので、実弾の前に必ずここを通すこと。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from zerotrade.brokers.ccxt_broker import CcxtBroker, _dec, require_credentials
from zerotrade.errors import BrokerError
from zerotrade.log import get_logger
from zerotrade.models import ClosedTrade, OrderRequest, Side, utcnow
from zerotrade.settings import BrokerSettings

__all__ = ["BingxBroker", "build_from_settings"]

logger = get_logger(__name__)


class BingxBroker(CcxtBroker):
    """BingX 用に作り込んだ ccxt アダプタ。"""

    name = "bingx"

    # fetchPositionsHistory が使えれば実現損益を実測できる。
    # **ただし ccxt の has は当てにならない。** True と申告しているのに
    # 実装が無く NotSupported を投げる版があった（実機で踏んだ）。
    # 接続時に実際に叩いて確かめ、駄目なら False へ落とす。
    supports_closed_trades = True

    # BingX の足取得は1リクエスト 1440 本まで（超えると code 109400）。
    max_ohlcv_count = 1440

    def __init__(
        self,
        *,
        api_key: str | None = None,
        secret: str | None = None,
        sandbox: bool = True,
        default_type: str = "swap",
        margin_mode: str | None = "cross",
        timeout: float = 30.0,
        symbols: list[str] | None = None,
        client: Any = None,
    ) -> None:
        super().__init__(
            exchange="bingx",
            api_key=api_key,
            secret=secret,
            sandbox=sandbox,
            default_type=default_type,
            timeout=timeout,
            symbols=symbols,
            client=client,
        )
        self.name = "bingx"
        self._margin_mode = margin_mode
        self._configured: set[str] = set()

    # ------------------------------------------------------------ 接続

    async def connect(self) -> None:
        await super().connect()
        # 建玉を持ったまま切り替えられないため、接続直後に済ませる。
        for symbol in self._symbols:
            await self._ensure_one_way(symbol)
        await self._probe_closed_trades()

    async def _probe_closed_trades(self) -> None:
        """確定損益を実測で取れるかを、実際に叩いて確かめる。

        **ここを取り違えると損失上限が働かなくなる。**
        :attr:`supports_closed_trades` が True だと StrategyRunner は
        建玉差分からの推定を行わない。取引所から確定損益も取れなければ、
        RiskManager は損益を一切知らないまま動き続ける。
        日次・週次の損失上限が**永久に発動しない**状態になる。

        ccxt の ``has`` は当てにならなかった（True と申告しつつ
        ``NotSupported`` を投げた）ので、申告ではなく実測で決める。
        """
        try:
            await self._call("fetch_positions_history", self._position_symbols(), None, 1)
        except BrokerError as exc:
            self.supports_closed_trades = False
            logger.warning(
                "決済履歴APIが使えないため、確定損益は建玉の差分から推定します"
                "（損失上限は働きますが誤差が乗ります）: %s",
                exc,
            )
        else:
            logger.info("決済履歴APIが使えます。確定損益は実測値を使います")

    async def _ensure_one_way(self, symbol: str) -> None:
        """片方向モードへ寄せる。

        ヘッジモードのままだと、決済のつもりの注文が反対側の新規建てとして
        通ってしまう。ZeroTrade は銘柄あたり1建玉を前提にしているので、
        ここを揃えないと建玉が積み上がる。

        既に建玉がある場合や現物のみの銘柄では切り替えが拒否される。
        その場合は警告だけ出して続ける（発注時の ``positionSide`` で
        片方向として振る舞うため、致命的ではない）。
        """
        if symbol in self._configured:
            return
        ccxt_symbol = self._to_ccxt(symbol)

        try:
            await self._call("set_position_mode", False, ccxt_symbol)
            logger.info("%s を片方向モードに設定しました", symbol)
        except BrokerError as exc:
            # 原因は建玉の存在・権限不足・認証情報の欠落など複数ある。
            # 断定するとログが誤診の元になるので、素の理由をそのまま出す。
            logger.warning("%s の片方向モード設定に失敗しました: %s", symbol, exc)

        if self._margin_mode:
            try:
                await self._call("set_margin_mode", self._margin_mode, ccxt_symbol)
            except BrokerError as exc:
                logger.warning("%s の証拠金モード設定に失敗しました: %s", symbol, exc)

        self._configured.add(symbol)

    # ------------------------------------------------------------ 注文

    def _order_params(self, request: OrderRequest) -> dict[str, Any]:
        """BingX 固有のパラメータを足す。"""
        params = super()._order_params(request)
        # 片方向モードでは BOTH を指定する。ヘッジモードの LONG/SHORT を
        # 混ぜると、決済注文が反対側の新規建てになる。
        params["positionSide"] = "BOTH"
        return params

    # ------------------------------------------------------------ 確定損益

    async def get_closed_trades(self, since: datetime | None = None) -> list[ClosedTrade]:
        """決済済み建玉と実現損益を返す。

        ``fetchPositionsHistory`` を使う。ここが取れるおかげで、
        日次・週次の損失上限が推定ではなく実測で効く。
        """
        try:
            raw = await self._call(
                "fetch_positions_history",
                self._position_symbols(),
                int(since.timestamp() * 1000) if since else None,
                None,
            )
        except BrokerError as exc:
            logger.warning("決済履歴を取得できませんでした: %s", exc)
            return []

        trades: list[ClosedTrade] = []
        for entry in raw or []:
            realized = _dec(entry.get("realizedPnl"))
            quantity = abs(_dec(entry.get("contracts")))
            if quantity == 0:
                continue

            opened = _timestamp(entry.get("timestamp"))
            closed = _timestamp(entry.get("lastUpdateTimestamp")) or opened
            if since is not None and closed < since:
                continue

            entry_price = _dec(entry.get("entryPrice"))
            side = Side.BUY if str(entry.get("side", "long")) == "long" else Side.SELL
            # 実現損益から決済価格を逆算する（BingX は終値を返さないことがある）。
            exit_price = _exit_price(entry_price, realized, quantity, side)

            trades.append(
                ClosedTrade(
                    symbol=self._to_zerotrade(str(entry.get("symbol", ""))),
                    side=side,
                    quantity=quantity,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    realized_pnl=realized,
                    opened_at=opened,
                    closed_at=closed,
                    trade_id=str(entry.get("id") or entry.get("symbol") or ""),
                    reason="exchange",
                )
            )
        return trades


def _timestamp(value: Any) -> datetime:
    """ccxt のミリ秒タイムスタンプを datetime にする。"""
    if not value:
        return utcnow()
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except (TypeError, ValueError, OSError):
        return utcnow()


def _exit_price(entry_price: Decimal, realized: Decimal, quantity: Decimal, side: Side) -> Decimal:
    """実現損益から決済価格を逆算する。

    ``pnl = (exit - entry) * 符号 * 数量`` を解く。
    数量が 0 のときは建値をそのまま返す（呼び出し側で除外済み）。
    """
    if quantity == 0:
        return entry_price
    return entry_price + (realized / quantity) * side.sign


def build_from_settings(
    settings: BrokerSettings,
    *,
    symbols: list[str] | None = None,
    client: Any = None,
) -> BingxBroker:
    """設定から BingxBroker を組み立てる。

    ``symbols`` は必ず渡すこと。BingX の建玉照会は銘柄リストを要求するため、
    渡さないと建玉が取れず「建玉が無い」と誤認して二重に建てる。
    """
    if client is None:
        require_credentials(settings, "bingx")
    return BingxBroker(
        api_key=settings.api_token,
        secret=settings.api_secret,
        sandbox=settings.environment == "practice",
        default_type=settings.market_type,
        margin_mode=settings.margin_mode,
        timeout=settings.timeout_seconds,
        symbols=symbols,
        client=client,
    )
