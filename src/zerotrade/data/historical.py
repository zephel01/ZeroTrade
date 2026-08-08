"""ヒストリカルデータの取得。

初期実装は CSV 読み込みと合成データ生成のみ。
実運用のヒストリカル取得はブローカーの ``get_ohlcv`` を使う。
"""

from __future__ import annotations

import csv
import math
import random
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from zerotrade.errors import ConfigError
from zerotrade.models import Candle, to_decimal

__all__ = ["load_csv", "synthetic_candles"]

_REQUIRED_COLUMNS = ("timestamp", "open", "high", "low", "close")


def load_csv(path: str | Path, symbol: str) -> list[Candle]:
    """OHLCV の CSV を読み込む。

    必要な列は ``timestamp, open, high, low, close``（``volume`` は任意）。
    timestamp は ISO 8601 形式。タイムゾーンが無い場合は UTC とみなす。

    Raises:
        ConfigError: ファイルが無い、または必要な列が欠けている場合。
    """
    csv_path = Path(path)
    if not csv_path.is_file():
        raise ConfigError(f"CSVが見つかりません: {csv_path}")

    candles: list[Candle] = []
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in _REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise ConfigError(f"CSVに必要な列がありません: {', '.join(missing)}")

        for line_no, row in enumerate(reader, start=2):
            try:
                timestamp = datetime.fromisoformat(row["timestamp"])
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=UTC)
                candles.append(
                    Candle(
                        symbol=symbol,
                        timestamp=timestamp,
                        open=to_decimal(row["open"]),
                        high=to_decimal(row["high"]),
                        low=to_decimal(row["low"]),
                        close=to_decimal(row["close"]),
                        volume=to_decimal(row.get("volume") or 0),
                    )
                )
            except (ValueError, KeyError) as exc:
                raise ConfigError(f"{csv_path}:{line_no} の解析に失敗しました: {exc}") from exc

    candles.sort(key=lambda c: c.timestamp)
    return candles


def synthetic_candles(
    symbol: str,
    *,
    count: int = 500,
    start_price: float = 150.0,
    volatility: float = 0.0015,
    drift: float = 0.0,
    interval: timedelta = timedelta(minutes=5),
    seed: int = 42,
    start: datetime | None = None,
) -> list[Candle]:
    """幾何ブラウン運動による疑似ローソク足を生成する。

    ペーパートレードの動作確認用。``seed`` を固定してあるので
    同じ引数からは常に同じ系列が出る（テストの再現性のため）。
    """
    if count <= 0:
        raise ValueError("count は正の整数である必要があります")

    rng = random.Random(seed)
    begin = start or datetime.now(UTC) - interval * count
    price = start_price
    candles: list[Candle] = []

    for i in range(count):
        step = math.exp(drift + volatility * rng.gauss(0.0, 1.0))
        open_price = price
        close_price = price * step
        # 高値・安値はローソクの実体から外側へ少し広げる。
        wick = abs(close_price - open_price) + price * volatility * abs(rng.gauss(0.0, 0.6))
        high = max(open_price, close_price) + wick / 2
        low = min(open_price, close_price) - wick / 2
        price = close_price

        candles.append(
            Candle(
                symbol=symbol,
                timestamp=begin + interval * i,
                open=_q(open_price),
                high=_q(high),
                low=_q(low),
                close=_q(close_price),
                volume=Decimal(rng.randint(100, 10_000)),
            )
        )
    return candles


def _q(value: float) -> Decimal:
    """価格を小数5桁へ丸める（FXの一般的な刻み）。"""
    return to_decimal(round(value, 5))


def latest_closes(candles: Sequence[Candle], count: int) -> list[Decimal]:
    """直近 ``count`` 本の終値。"""
    return [c.close for c in candles[-count:]]
