"""複数銘柄の時間軸アラインメントのテスト。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from zerotrade.data.align import align_candles
from zerotrade.errors import ConfigError
from zerotrade.models import Candle

BASE = datetime(2024, 1, 2, tzinfo=UTC)


def _series(symbol: str, offsets: list[int]) -> list[Candle]:
    return [
        Candle(
            symbol=symbol,
            timestamp=BASE + timedelta(hours=i),
            open=Decimal(150),
            high=Decimal(151),
            low=Decimal(149),
            close=Decimal(150),
        )
        for i in offsets
    ]


def test_単一銘柄はそのまま返る() -> None:
    series = _series("USD_JPY", list(range(10)))
    assert align_candles({"USD_JPY": series})["USD_JPY"] == series


def test_共通しない時刻は落とされる() -> None:
    """銘柄ごとに歯抜けの位置が違うと、時計が別々に進んでしまう。"""
    a = _series("USD_JPY", [0, 1, 2, 3, 4, *range(5, 60)])
    b = _series("EUR_JPY", [0, 2, 4, *range(5, 60)])  # 1 と 3 が欠けている

    aligned = align_candles({"USD_JPY": a, "EUR_JPY": b})

    assert [c.timestamp for c in aligned["USD_JPY"]] == [c.timestamp for c in aligned["EUR_JPY"]]
    assert len(aligned["USD_JPY"]) == len(aligned["EUR_JPY"])
    stamps = {c.timestamp for c in aligned["USD_JPY"]}
    assert BASE + timedelta(hours=1) not in stamps


def test_全銘柄が同じ本数になる() -> None:
    aligned = align_candles(
        {
            "A": _series("A", list(range(200))),
            "B": _series("B", list(range(50, 250))),
            "C": _series("C", list(range(60, 300))),
        }
    )
    lengths = {len(v) for v in aligned.values()}
    assert len(lengths) == 1


def test_共通時刻が少なすぎれば拒否される() -> None:
    """気づかないまま無意味な検証が走るのを防ぐ。"""
    with pytest.raises(ConfigError, match="共通する時刻"):
        align_candles(
            {"A": _series("A", list(range(100))), "B": _series("B", list(range(200, 300)))}
        )


def test_銘柄が空なら拒否される() -> None:
    with pytest.raises(ConfigError, match="銘柄が指定されていません"):
        align_candles({})
