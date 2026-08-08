"""ヒストリカルデータの分割取得と CSV 保存。

ブローカーの足取得APIには1リクエストあたりの上限がある
（OANDA v20 なら5000本）。数か月〜数年ぶんを取るには
時刻を遡りながら分割して呼ぶ必要があるので、その面倒をここに閉じ込める。

遡り方向に取るのは、ほとんどのAPIが「``to`` より前の N 本」という
指定しか受け付けないため。最後に古い順へ並べ替えて返す。
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from zerotrade.brokers.base import BaseBroker
from zerotrade.errors import BrokerError
from zerotrade.log import get_logger
from zerotrade.models import Candle

__all__ = ["fetch_candles", "save_csv"]

logger = get_logger(__name__)

#: 1リクエストあたりの既定の取得本数。
DEFAULT_CHUNK = 5000

#: 取りこぼしや無限ループを避けるための安全弁。
MAX_REQUESTS = 500


async def fetch_candles(
    broker: BaseBroker,
    symbol: str,
    *,
    granularity: str = "M5",
    start: datetime | None = None,
    end: datetime | None = None,
    chunk: int = DEFAULT_CHUNK,
    max_requests: int = MAX_REQUESTS,
) -> list[Candle]:
    """``start`` 以降 ``end`` 以前の足を、分割取得してまとめて返す。

    Args:
        broker: 接続済みのブローカー。
        symbol: 銘柄。
        granularity: 足種。
        start: この時刻以降の足だけ残す。``None`` なら取れるだけ遡る。
        end: この時刻より前から遡り始める。``None`` なら最新から。
        chunk: 1リクエストあたりの本数。
        max_requests: リクエスト回数の上限（安全弁）。

    Returns:
        古い順に並び、重複を除いた足のリスト。

    Raises:
        BrokerError: 1本も取得できなかった場合。
    """
    collected: dict[datetime, Candle] = {}
    cursor = end
    requests = 0

    # 1リクエストの上限は取引所ごとに違う（BingX 1440 / OANDA 5000）。
    # 超えるとエラーで1本も取れないので、ブローカーの申告に合わせて縮める。
    limit = min(chunk, broker.max_ohlcv_count)
    if limit < chunk:
        logger.info("1リクエストの上限に合わせ、取得本数を %d → %d に縮めます", chunk, limit)

    while requests < max_requests:
        requests += 1
        batch = await broker.get_ohlcv(symbol, granularity=granularity, count=limit, end=cursor)
        if not batch:
            break

        fresh = [c for c in batch if c.timestamp not in collected]
        for candle in batch:
            collected[candle.timestamp] = candle

        oldest = min(c.timestamp for c in batch)
        logger.info(
            "%s: %d本取得（最古 %s / 累計 %d本）",
            symbol,
            len(batch),
            oldest.isoformat(),
            len(collected),
        )

        # 進捗が無い＝これ以上遡れない。無限ループを断つ。
        if not fresh:
            break
        if start is not None and oldest <= start:
            break
        # 次は今回の最古より前を取りに行く。
        cursor = oldest

    if not collected:
        raise BrokerError(f"{symbol} の足を1本も取得できませんでした")

    candles = sorted(collected.values(), key=lambda c: c.timestamp)
    if start is not None:
        candles = [c for c in candles if c.timestamp >= start]
    if end is not None:
        candles = [c for c in candles if c.timestamp < end]

    # 未確定の足を混ぜると、実際には存在しなかった高値・安値で
    # バックテストの約定判定が動いてしまう。
    return [c for c in candles if c.complete]


def save_csv(candles: Sequence[Candle], path: Path) -> Path:
    """足を CSV に保存する。:func:`~zerotrade.data.historical.load_csv` で読み戻せる。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for c in candles:
            writer.writerow(
                [
                    c.timestamp.astimezone(UTC).isoformat(),
                    c.open,
                    c.high,
                    c.low,
                    c.close,
                    c.volume,
                ]
            )
    return path
