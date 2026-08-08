"""外部データの取り込みとリサンプリングのテスト。

タイムゾーンの取り違えは「市場が閉じていた時間に約定している」という形で
静かにバックテストを壊すので、UTC への正規化を重点的に確認する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from zerotrade.data.importer import (
    detect_format,
    parse_granularity,
    read_any,
    resample,
)
from zerotrade.errors import ConfigError
from zerotrade.models import Candle

HISTDATA = """20240102 000000;140.100;140.200;140.000;140.150;0
20240102 000100;140.150;140.300;140.100;140.250;0
20240102 000200;140.250;140.400;140.200;140.350;0
"""

DUKASCOPY = """Gmt time,Open,High,Low,Close,Volume
02.01.2024 00:00:00.000,140.100,140.200,140.000,140.150,120.5
02.01.2024 00:01:00.000,140.150,140.300,140.100,140.250,98.0
"""

GENERIC = """timestamp,open,high,low,close,volume
2024-01-02T00:00:00Z,140.100,140.200,140.000,140.150,120
2024-01-02T00:01:00Z,140.150,140.300,140.100,140.250,98
"""


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------------------ 形式判定


@pytest.mark.parametrize(
    ("name", "text", "expected"),
    [
        ("h.csv", HISTDATA, "histdata"),
        ("d.csv", DUKASCOPY, "dukascopy"),
        ("g.csv", GENERIC, "generic"),
    ],
)
def test_形式を自動判定できる(tmp_path: Path, name: str, text: str, expected: str) -> None:
    assert detect_format(_write(tmp_path, name, text)) == expected


def test_空ファイルは拒否される(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="空です"):
        detect_format(_write(tmp_path, "empty.csv", ""))


def test_存在しないファイルは拒否される(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="見つかりません"):
        read_any(tmp_path / "nope.csv", "USD_JPY")


def test_未知の形式指定は拒否される(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="未知の形式"):
        read_any(_write(tmp_path, "g.csv", GENERIC), "USD_JPY", fmt="mt4")


# ------------------------------------------------------------ 読み込み


def test_HistDataは米国東部時間としてUTCへ直す(tmp_path: Path) -> None:
    """HistData は夏時間なしの EST で配布されている。

    UTC と誤解すると、足が5時間ずれたまま検証が走る。
    """
    candles = read_any(_write(tmp_path, "h.csv", HISTDATA), "USD_JPY")

    assert len(candles) == 3
    # EST(UTC-5) の 00:00 は UTC の 05:00。
    assert candles[0].timestamp == datetime(2024, 1, 2, 5, 0, tzinfo=UTC)
    assert candles[0].open == Decimal("140.100")
    assert candles[0].high == Decimal("140.200")


def test_タイムゾーンを明示できる(tmp_path: Path) -> None:
    candles = read_any(_write(tmp_path, "h.csv", HISTDATA), "USD_JPY", timezone="Asia/Tokyo")
    # JST(UTC+9) の 00:00 は前日の UTC 15:00。
    assert candles[0].timestamp == datetime(2024, 1, 1, 15, 0, tzinfo=UTC)


def test_未知のタイムゾーンは拒否される(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="タイムゾーン"):
        read_any(_write(tmp_path, "h.csv", HISTDATA), "USD_JPY", timezone="Asia/Nowhere")


def test_Dukascopy形式を読める(tmp_path: Path) -> None:
    candles = read_any(_write(tmp_path, "d.csv", DUKASCOPY), "USD_JPY")

    assert len(candles) == 2
    assert candles[0].timestamp == datetime(2024, 1, 2, 0, 0, tzinfo=UTC)
    assert candles[0].volume == Decimal("120.5")


def test_汎用CSVを読める(tmp_path: Path) -> None:
    candles = read_any(_write(tmp_path, "g.csv", GENERIC), "USD_JPY")

    assert len(candles) == 2
    assert candles[0].timestamp == datetime(2024, 1, 2, 0, 0, tzinfo=UTC)
    assert candles[-1].close == Decimal("140.250")


def test_列名の揺れを吸収する(tmp_path: Path) -> None:
    text = "Date,Open,High,Low,Close\n2024-01-02,140.1,140.9,139.8,140.5\n"
    candles = read_any(_write(tmp_path, "y.csv", text), "USD_JPY")
    assert candles[0].close == Decimal("140.5")


def test_必要な列が無ければ拒否される(tmp_path: Path) -> None:
    text = "timestamp,price\n2024-01-02,140.1\n"
    with pytest.raises(ConfigError, match="必要な列がありません"):
        read_any(_write(tmp_path, "bad.csv", text), "USD_JPY")


def test_壊れた行は行番号つきで報告される(tmp_path: Path) -> None:
    text = GENERIC + "こわれた行,x,y,z,w,v\n"
    with pytest.raises(ConfigError, match=r":4 "):
        read_any(_write(tmp_path, "broken.csv", text), "USD_JPY")


def test_古い順に並び替えられる(tmp_path: Path) -> None:
    text = (
        "timestamp,open,high,low,close\n"
        "2024-01-02T00:05:00Z,3,3,3,3\n"
        "2024-01-02T00:00:00Z,1,1,1,1\n"
    )
    candles = read_any(_write(tmp_path, "unsorted.csv", text), "USD_JPY")
    assert candles[0].close == Decimal(1)
    assert candles[-1].close == Decimal(3)


# ------------------------------------------------------------ 足種


@pytest.mark.parametrize(
    ("name", "expected"),
    [("M1", timedelta(minutes=1)), ("m5", timedelta(minutes=5)), ("H1", timedelta(hours=1))],
)
def test_足種を解釈できる(name: str, expected: timedelta) -> None:
    assert parse_granularity(name) == expected


def test_未知の足種は拒否される() -> None:
    with pytest.raises(ConfigError, match="未知の足種"):
        parse_granularity("M7")


# ------------------------------------------------------------ リサンプリング


def _minutes(count: int, *, start: datetime | None = None) -> list[Candle]:
    base = start or datetime(2024, 1, 2, 0, 0, tzinfo=UTC)
    return [
        Candle(
            symbol="USD_JPY",
            timestamp=base + timedelta(minutes=i),
            open=Decimal(100 + i),
            high=Decimal(100 + i) + Decimal(2),
            low=Decimal(100 + i) - Decimal(2),
            close=Decimal(100 + i) + Decimal(1),
            volume=Decimal(10),
        )
        for i in range(count)
    ]


def test_1分足を5分足へまとめる() -> None:
    result = resample(_minutes(15), "M5")

    assert len(result) == 3
    first = result[0]
    assert first.timestamp == datetime(2024, 1, 2, 0, 0, tzinfo=UTC)
    assert first.open == Decimal(100), "始値は区間の最初"
    assert first.close == Decimal(105), "終値は区間の最後"
    assert first.high == Decimal(106), "高値は区間内の最大"
    assert first.low == Decimal(98), "安値は区間内の最小"
    assert first.volume == Decimal(50), "出来高は合計"


def test_埋まりきっていない最後の足は捨てる() -> None:
    """未確定の足を残すと、実在しなかった高値安値で約定判定が動く。"""
    result = resample(_minutes(12), "M5")
    assert len(result) == 2, "12本 → 完全な5分足は2本だけ"


def test_区切りに揃えて丸められる() -> None:
    """00:03 開始でも、足の頭は 00:00 と 00:05 に揃う。"""
    start = datetime(2024, 1, 2, 0, 3, tzinfo=UTC)
    result = resample(_minutes(20, start=start), "M5")
    assert all(c.timestamp.minute % 5 == 0 for c in result)


def test_同じ足種への変換はそのまま通る() -> None:
    result = resample(_minutes(30), "M1")
    assert len(result) == 30


def test_元より短い足種は拒否される() -> None:
    hourly = [
        Candle(
            symbol="USD_JPY",
            timestamp=datetime(2024, 1, 2, tzinfo=UTC) + timedelta(hours=i),
            open=Decimal(100),
            high=Decimal(101),
            low=Decimal(99),
            close=Decimal(100),
        )
        for i in range(10)
    ]
    with pytest.raises(ConfigError, match="短い足種"):
        resample(hourly, "M5")


def test_空の入力は空を返す() -> None:
    assert resample([], "M5") == []


def test_H1へのまとめも動く() -> None:
    result = resample(_minutes(180), "H1")
    assert len(result) == 3
    assert all(c.timestamp.minute == 0 for c in result)
