"""ヒストリカルデータの分割取得のテスト。

検証したいのは「1リクエストの上限をまたいで正しく遡れるか」と
「遡れなくなったときに無限ループしないか」の2点。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from zerotrade.brokers.base import BaseBroker
from zerotrade.data.fetcher import fetch_candles, save_csv
from zerotrade.data.historical import load_csv
from zerotrade.errors import BrokerError
from zerotrade.models import Balance, Candle, Order, OrderRequest, Position, Ticker

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _series(
    count: int, *, start: datetime = BASE, step: timedelta = timedelta(minutes=5)
) -> list[Candle]:
    return [
        Candle(
            symbol="USD_JPY",
            timestamp=start + step * i,
            open=Decimal("150.0"),
            high=Decimal("150.5"),
            low=Decimal("149.5"),
            close=Decimal("150.2"),
            volume=Decimal(100),
        )
        for i in range(count)
    ]


class FakeBroker(BaseBroker):
    """``end`` より前の足を最大 ``count`` 本返すだけのブローカー。"""

    name = "fake"

    def __init__(self, candles: list[Candle], *, limit: int = 100) -> None:
        self.candles = candles
        self.limit = limit
        self.calls: list[datetime | None] = []
        self.counts: list[int] = []

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...

    async def get_balance(self) -> Balance:  # pragma: no cover - 未使用
        raise BrokerError("未対応")

    async def get_positions(self) -> list[Position]:  # pragma: no cover
        return []

    async def place_order(self, request: OrderRequest) -> Order:  # pragma: no cover
        raise BrokerError("未対応")

    async def cancel_order(self, order_id: str) -> Order:  # pragma: no cover
        raise BrokerError("未対応")

    async def get_order(self, order_id: str) -> Order:  # pragma: no cover
        raise BrokerError("未対応")

    async def get_open_orders(self, symbol: str | None = None) -> list[Order]:  # pragma: no cover
        return []

    async def get_ticker(self, symbol: str) -> Ticker:  # pragma: no cover
        raise BrokerError("未対応")

    async def get_ohlcv(
        self,
        symbol: str,
        *,
        granularity: str = "M5",
        count: int = 200,
        end: datetime | None = None,
    ) -> list[Candle]:
        self.calls.append(end)
        self.counts.append(count)
        available = [c for c in self.candles if end is None or c.timestamp < end]
        return available[-min(count, self.limit) :]


# ------------------------------------------------------------ 分割取得


async def test_上限をまたいで遡って集める() -> None:
    broker = FakeBroker(_series(250), limit=100)
    candles = await fetch_candles(broker, "USD_JPY", chunk=100)

    assert len(candles) == 250
    assert candles == sorted(candles, key=lambda c: c.timestamp), "古い順に並んでいない"
    assert len(broker.calls) >= 3, "1リクエストで済ませようとしている"
    # 2回目以降は必ず時刻を指定して遡っている。
    assert broker.calls[0] is None
    assert all(c is not None for c in broker.calls[1:])


async def test_重複した足は1本にまとまる() -> None:
    """遡り取得ではチャンクの境界が重なる。"""
    broker = FakeBroker(_series(120), limit=50)
    candles = await fetch_candles(broker, "USD_JPY", chunk=50)

    timestamps = [c.timestamp for c in candles]
    assert len(timestamps) == len(set(timestamps))


async def test_startより前は切り落とされる() -> None:
    broker = FakeBroker(_series(200), limit=100)
    cutoff = BASE + timedelta(minutes=5 * 150)
    candles = await fetch_candles(broker, "USD_JPY", chunk=100, start=cutoff)

    assert candles
    assert all(c.timestamp >= cutoff for c in candles)


async def test_endより後は切り落とされる() -> None:
    broker = FakeBroker(_series(200), limit=100)
    cutoff = BASE + timedelta(minutes=5 * 50)
    candles = await fetch_candles(broker, "USD_JPY", chunk=100, end=cutoff)

    assert candles
    assert all(c.timestamp < cutoff for c in candles)


async def test_これ以上遡れなければ止まる() -> None:
    """同じ足しか返らなくなったとき、無限ループしないこと。"""
    broker = FakeBroker(_series(10), limit=100)
    candles = await fetch_candles(broker, "USD_JPY", chunk=100)

    assert len(candles) == 10
    assert len(broker.calls) <= 3, "進捗が無いのに呼び続けている"


async def test_リクエスト上限で打ち切られる() -> None:
    broker = FakeBroker(_series(10_000), limit=10)
    await fetch_candles(broker, "USD_JPY", chunk=10, max_requests=5)
    assert len(broker.calls) == 5


async def test_1本も取れなければ例外() -> None:
    broker = FakeBroker([], limit=100)
    with pytest.raises(BrokerError, match="1本も取得できませんでした"):
        await fetch_candles(broker, "USD_JPY")


async def test_未確定の足は除外される() -> None:
    """未確定の足を混ぜると、実在しなかった高値安値で約定判定が動く。"""
    series = _series(20)
    series[-1] = Candle(
        symbol="USD_JPY",
        timestamp=series[-1].timestamp,
        open=series[-1].open,
        high=series[-1].high,
        low=series[-1].low,
        close=series[-1].close,
        complete=False,
    )
    broker = FakeBroker(series, limit=100)
    candles = await fetch_candles(broker, "USD_JPY")

    assert len(candles) == 19
    assert all(c.complete for c in candles)


# ------------------------------------------------------------ CSV 往復


async def test_CSVに保存して読み戻せる(tmp_path: Path) -> None:
    original = _series(50)
    path = save_csv(original, tmp_path / "nested" / "USD_JPY_M5.csv")

    restored = load_csv(path, "USD_JPY")
    assert len(restored) == 50
    assert restored[0].timestamp == original[0].timestamp
    assert restored[0].close == original[0].close
    assert restored[-1].high == original[-1].high


async def test_取得本数はブローカーの上限に抑えられる() -> None:
    """上限を超えると取引所は **1本も返さない**（BingX は code 109400）。

    既定のチャンク幅 5000 を BingX（上限1440）へそのまま投げて実際に失敗した。
    """
    broker = FakeBroker(_series(300), limit=100)
    broker.max_ohlcv_count = 120

    await fetch_candles(broker, "USD_JPY", granularity="M5", chunk=5000)

    assert broker.counts, "1回も呼ばれていない"
    assert max(broker.counts) == 120, "ブローカーの上限を超えて要求している"


async def test_上限が広ければ指定どおり要求する() -> None:
    broker = FakeBroker(_series(300), limit=100)
    broker.max_ohlcv_count = 5000

    await fetch_candles(broker, "USD_JPY", granularity="M5", chunk=200)

    assert max(broker.counts) == 200
