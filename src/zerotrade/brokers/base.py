"""ブローカー共通インターフェース。

戦略・リスク管理・注文管理はこの抽象にのみ依存する。
OANDA の ``units`` が符号付きだとか、日本株が単元株制だとか、
そういうブローカー固有の事情はすべて各 Adapter の内側に閉じ込める。

新しいブローカーを追加するときに実装が必要なのは、
:class:`BaseBroker` の抽象メソッド8個だけ。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from types import TracebackType
from typing import Self

from zerotrade.errors import BrokerError
from zerotrade.models import (
    Balance,
    Candle,
    ClosedTrade,
    Order,
    OrderRequest,
    Position,
    Ticker,
)

__all__ = ["BaseBroker"]


class BaseBroker(ABC):
    """すべてのブローカーアダプタの基底クラス。

    実装上の約束:

    * すべてのメソッドは失敗時に :class:`~zerotrade.errors.BrokerError`
      （またはその派生）を送出する。ブローカー固有の例外を外へ漏らさない。
    * 金額・数量・価格は :class:`~decimal.Decimal` で受け渡す。
    * :meth:`place_order` は冪等キーとして
      ``request.client_order_id`` をブローカーへ渡す努力をする。
    """

    #: 表示・ログ用の識別子。サブクラスで上書きする。
    name: str = "base"

    #: :meth:`get_closed_trades` で正確な確定損益を返せるか。
    #: False の場合、StrategyRunner はポジションの差分から損益を推定する。
    supports_closed_trades: bool = False

    #: 約定を手元で模擬するブローカーか。
    #: False のものは**実在の取引所へ注文を送る**。``mode`` との組み合わせを
    #: 起動時に検査するために使う（:func:`zerotrade.app.build_application`）。
    is_simulated: bool = False

    #: :meth:`get_ohlcv` の1リクエストあたり上限本数。
    #: 上限は取引所ごとに違い、超えるとエラーになる（BingX は 1440、
    #: OANDA v20 は 5000）。分割取得はこの値を見てページ幅を決める。
    max_ohlcv_count: int = 5000

    # ---------------------------------------------------------- 接続管理

    @abstractmethod
    async def connect(self) -> None:
        """接続を確立し、認証情報が有効であることを確認する。"""

    @abstractmethod
    async def disconnect(self) -> None:
        """接続を閉じる。冪等でなければならない。"""

    # ---------------------------------------------------------- 口座照会

    @abstractmethod
    async def get_balance(self) -> Balance:
        """口座残高（有効証拠金・余力・使用証拠金）を返す。"""

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """保有中のポジション一覧を返す。無ければ空リスト。"""

    # ---------------------------------------------------------- 注文

    @abstractmethod
    async def place_order(self, request: OrderRequest) -> Order:
        """発注する。

        Warning:
            このメソッドを直接呼んではならない。必ず
            :class:`~zerotrade.core.orders.OrderManager` 経由で呼ぶこと。
            OrderManager が RiskManager の判定を通してから委譲する。
        """

    @abstractmethod
    async def cancel_order(self, order_id: str) -> Order:
        """注文を取り消し、取消後の状態を返す。"""

    @abstractmethod
    async def get_order(self, order_id: str) -> Order:
        """単一注文の最新状態を返す。"""

    @abstractmethod
    async def get_open_orders(self, symbol: str | None = None) -> list[Order]:
        """未約定注文の一覧を返す。"""

    # ---------------------------------------------------------- 相場

    @abstractmethod
    async def get_ticker(self, symbol: str) -> Ticker:
        """現在の気配値を返す。"""

    # ---------------------------------------------------------- 任意実装

    async def get_ohlcv(
        self,
        symbol: str,
        *,
        granularity: str = "M5",
        count: int = 200,
        end: datetime | None = None,
    ) -> list[Candle]:
        """ローソク足を古い順に取得する（任意実装）。

        Args:
            symbol: 銘柄。
            granularity: 足種（``M5`` / ``H1`` など。表記はブローカー準拠）。
            count: 取得本数。ブローカー側の上限を超える指定は切り詰められる。
            end: この時刻より前の足を返す。``None`` なら最新まで。
                ヒストリカルデータを遡って分割取得するときに使う。

        Raises:
            BrokerError: このブローカーが対応していない場合。
        """
        raise BrokerError(f"{self.name} は get_ohlcv に対応していません")

    async def get_closed_trades(self, since: datetime | None = None) -> list[ClosedTrade]:
        """``since`` 以降に決済されたトレードを返す（任意実装）。

        リスク管理の日次・週次損失はこの確定損益を基準にするため、
        対応できるブローカーは必ず実装し
        :attr:`supports_closed_trades` を True にすること。
        """
        return []

    async def update_position_stop(self, symbol: str, stop_loss: Decimal) -> Position | None:
        """建玉のストップを更新する（任意実装）。

        トレーリングストップに必要。対象の建玉が無ければ ``None`` を返す。

        Raises:
            BrokerError: このブローカーが対応していない場合。
        """
        raise BrokerError(f"{self.name} は update_position_stop に対応していません")

    async def set_leverage(self, symbol: str, leverage: Decimal) -> None:
        """レバレッジを設定する（任意実装）。"""
        raise BrokerError(f"{self.name} は set_leverage に対応していません")

    async def close_position(self, symbol: str) -> Order | None:
        """建玉を成行で決済する（任意実装）。

        既定実装は :meth:`get_positions` と :meth:`place_order` の組み合わせ。
        専用APIを持つブローカーは上書きする。
        """
        for position in await self.get_positions():
            if position.symbol != symbol:
                continue
            return await self.place_order(
                OrderRequest(
                    symbol=symbol,
                    side=position.side.opposite,
                    quantity=position.quantity,
                    reduce_only=True,
                )
            )
        return None

    # ---------------------------------------------------- コンテキスト管理

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.disconnect()

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"
