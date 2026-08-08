"""マーケットデータの供給元。

戦略と StrategyRunner はこの抽象にだけ依存する。
ライブ実行ではブローカーAPIから、バックテストではCSVから、
同じインターフェースで足を受け取る。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from zerotrade.brokers.base import BaseBroker
from zerotrade.models import Candle, Ticker

__all__ = ["BrokerFeed", "MarketDataFeed", "StaticFeed"]


class MarketDataFeed(ABC):
    """ローソク足と気配値を供給する抽象。"""

    @abstractmethod
    async def get_candles(
        self, symbol: str, *, granularity: str = "M5", count: int = 200
    ) -> list[Candle]:
        """古い順に並んだローソク足を返す。"""

    @abstractmethod
    async def get_ticker(self, symbol: str) -> Ticker:
        """現在の気配値を返す。"""


class BrokerFeed(MarketDataFeed):
    """ブローカーAPIをそのままデータ源として使う。"""

    def __init__(self, broker: BaseBroker) -> None:
        self._broker = broker

    async def get_candles(
        self, symbol: str, *, granularity: str = "M5", count: int = 200
    ) -> list[Candle]:
        candles = await self._broker.get_ohlcv(symbol, granularity=granularity, count=count)
        # 形成中の足は戦略に渡さない。バックテストでは確定足しか見えないため、
        # ここを揃えないとライブだけ挙動が変わる（未確定の高値安値で
        # ブレイクを判定してしまい、足が閉じると消える「幻のシグナル」が出る）。
        return [c for c in candles if c.complete]

    async def get_ticker(self, symbol: str) -> Ticker:
        return await self._broker.get_ticker(symbol)


class StaticFeed(MarketDataFeed):
    """あらかじめ読み込んだ足を順に吐き出すフィード。

    バックテストやリプレイ用。:meth:`advance` を呼ぶたびに1本進む。
    """

    def __init__(self, candles_by_symbol: dict[str, list[Candle]], *, spread: float = 0.0) -> None:
        from decimal import Decimal

        self._all = candles_by_symbol
        self._index = 0
        self._spread = Decimal(str(spread))

    @property
    def index(self) -> int:
        return self._index

    @property
    def exhausted(self) -> bool:
        return all(self._index >= len(c) for c in self._all.values())

    def advance(self, steps: int = 1) -> None:
        self._index += steps

    async def get_candles(
        self, symbol: str, *, granularity: str = "M5", count: int = 200
    ) -> list[Candle]:
        candles = self._all.get(symbol, [])
        end = min(self._index, len(candles))
        return list(candles[max(0, end - count) : end])

    async def get_ticker(self, symbol: str) -> Ticker:
        from zerotrade.errors import BrokerError
        from zerotrade.models import to_decimal

        candles = await self.get_candles(symbol, count=1)
        if not candles:
            raise BrokerError(f"{symbol} の価格データがありません")
        close = candles[-1].close
        half = to_decimal(self._spread) / 2
        return Ticker(
            symbol=symbol,
            bid=close - half,
            ask=close + half,
            timestamp=candles[-1].timestamp,
        )
