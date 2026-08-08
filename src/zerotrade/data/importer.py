"""外部で入手した OHLCV ファイルの取り込みとリサンプリング。

ブローカーのAPIが使えない状況でも検証を進められるようにするための経路。
無料で手に入るヒストリカルデータは提供元ごとに形式がばらばらで、
しかもほとんどが1分足なので、そのままでは検証に回せない。

対応形式:

``histdata``
    HistData.com の M1 ASCII。``YYYYMMDD HHMMSS;O;H;L;C;V`` のセミコロン区切り、
    ヘッダ無し。時刻は米国東部時間（EST、夏時間なし）で配布されている。
``dukascopy``
    Dukascopy のCSVエクスポート。``Gmt time,Open,High,Low,Close,Volume``。
``mt4``
    MetaTrader 4/5 のエクスポート。``YYYY.MM.DD,HH:MM,O,H,L,C,V`` でヘッダ無し、
    日付と時刻が別の列に分かれている。時刻は **ブローカーのサーバー時刻**で、
    多くの業者が EET（冬 UTC+2 / 夏 UTC+3）を使う。
``generic``
    ヘッダ付きCSV。列名から自動で対応付ける（``date``/``time``/``timestamp``、
    ``open``/``high``/``low``/``close``/``volume`` の揺れを吸収する）。

時刻は最終的にすべて UTC に正規化する。タイムゾーンの取り違えは
「実際には市場が閉じていた時間に約定している」という形で
静かにバックテストを壊すので、既定値には頼らず明示できるようにしてある。
"""

from __future__ import annotations

import csv
import itertools
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from zerotrade.errors import ConfigError
from zerotrade.log import get_logger
from zerotrade.models import Candle, to_decimal

__all__ = ["GRANULARITIES", "detect_format", "parse_granularity", "read_any", "resample"]

logger = get_logger(__name__)

#: 足種の表記 → 長さ。OANDA の表記に合わせてある。
GRANULARITIES: dict[str, timedelta] = {
    "M1": timedelta(minutes=1),
    "M5": timedelta(minutes=5),
    "M15": timedelta(minutes=15),
    "M30": timedelta(minutes=30),
    "H1": timedelta(hours=1),
    "H4": timedelta(hours=4),
    "D1": timedelta(days=1),
}

#: 列名の揺れを吸収するための対応表。
_ALIASES: dict[str, str] = {
    "date": "timestamp",
    "datetime": "timestamp",
    "time": "timestamp",
    "gmt time": "timestamp",
    "timestamp": "timestamp",
    "o": "open",
    "h": "high",
    "l": "low",
    "c": "close",
    "v": "volume",
    "vol": "volume",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "volume": "volume",
    "adj close": "adj_close",
}

#: HistData は夏時間を使わない米国東部時間で配布されている。
HISTDATA_TZ = "Etc/GMT+5"

#: MT4/MT5 サーバーの標準的なタイムゾーン。
#:
#: 実データ（USD/JPY 2025-08〜2026-06）で週末ギャップの位置から検証した。
#: FX市場は金曜17:00ニューヨーク時間（夏 21:00 UTC / 冬 22:00 UTC）に閉まる。
#: そのクローズがサーバー時刻の23時台に現れたので、夏 UTC+3 / 冬 UTC+2 と確定した。
#: 業者によっては異なるので、ずれていれば ``--tz`` で明示すること。
MT4_TZ = "EET"


#: MetaTrader の日付列（``2025.08.01``）。
_MT4_DATE = re.compile(r"\d{4}\.\d{2}\.\d{2}")


def parse_granularity(name: str) -> timedelta:
    """``M5`` のような表記を長さに変換する。

    Raises:
        ConfigError: 未知の表記の場合。
    """
    key = name.strip().upper()
    if key not in GRANULARITIES:
        raise ConfigError(f"未知の足種です: {name}（利用可能: {', '.join(GRANULARITIES)}）")
    return GRANULARITIES[key]


def detect_format(path: Path) -> str:
    """先頭行から形式を推測する。

    Returns:
        ``histdata`` / ``dukascopy`` / ``generic`` のいずれか。
    """
    with path.open(encoding="utf-8-sig", errors="replace") as handle:
        first = handle.readline().strip()

    if not first:
        raise ConfigError(f"ファイルが空です: {path}")

    lowered = first.lower()
    if "gmt time" in lowered:
        return "dukascopy"
    # ヘッダ無しでセミコロン区切りなら HistData。
    if ";" in first and not any(c.isalpha() for c in first.replace(";", "")):
        return "histdata"
    # ヘッダ無しで「YYYY.MM.DD,HH:MM,...」なら MetaTrader。
    parts = first.split(",")
    if len(parts) >= 6 and _MT4_DATE.fullmatch(parts[0].strip()):
        return "mt4"
    return "generic"


def read_any(
    path: Path,
    symbol: str,
    *,
    fmt: str = "auto",
    timezone: str | None = None,
) -> list[Candle]:
    """任意形式の OHLCV ファイルを読み込む。

    Args:
        path: 入力ファイル。
        symbol: 付与する銘柄名。
        fmt: ``auto`` / ``histdata`` / ``dukascopy`` / ``generic``。
        timezone: タイムゾーンを持たない時刻をどのゾーンとして解釈するか。
            ``None`` なら形式ごとの既定（HistData は米国東部、他は UTC）。

    Returns:
        古い順に並んだ足。

    Raises:
        ConfigError: ファイルが無い、形式が不正、または1本も読めなかった場合。
    """
    if not path.is_file():
        raise ConfigError(f"ファイルが見つかりません: {path}")

    resolved = detect_format(path) if fmt == "auto" else fmt
    if resolved not in ("histdata", "dukascopy", "generic", "mt4"):
        raise ConfigError(f"未知の形式です: {fmt}")

    default_tz = {"histdata": HISTDATA_TZ, "mt4": MT4_TZ}.get(resolved, "UTC")
    try:
        zone = ZoneInfo(timezone or default_tz)
    except Exception as exc:  # ZoneInfoNotFoundError を含む
        raise ConfigError(f"未知のタイムゾーンです: {timezone}") from exc

    logger.info("%s を %s 形式として読み込みます（時刻は %s）", path, resolved, zone)

    readers = {
        "histdata": _read_histdata,
        "mt4": _read_mt4,
    }
    reader = readers.get(resolved, _read_delimited)
    candles = reader(path, symbol, zone)

    if not candles:
        raise ConfigError(f"{path} から足を1本も読み取れませんでした")

    candles.sort(key=lambda c: c.timestamp)
    return candles


def _read_histdata(path: Path, symbol: str, zone: ZoneInfo) -> list[Candle]:
    """``YYYYMMDD HHMMSS;O;H;L;C;V`` を読む。"""
    candles: list[Candle] = []
    with path.open(encoding="utf-8-sig", errors="replace") as handle:
        for line_no, line in enumerate(handle, start=1):
            row = line.strip()
            if not row:
                continue
            parts = row.split(";")
            if len(parts) < 5:
                raise ConfigError(f"{path}:{line_no} の列数が足りません: {row[:60]}")
            try:
                stamp = datetime.strptime(parts[0], "%Y%m%d %H%M%S").replace(tzinfo=zone)
                candles.append(
                    Candle(
                        symbol=symbol,
                        timestamp=stamp.astimezone(UTC),
                        open=to_decimal(parts[1]),
                        high=to_decimal(parts[2]),
                        low=to_decimal(parts[3]),
                        close=to_decimal(parts[4]),
                        volume=to_decimal(parts[5]) if len(parts) > 5 else to_decimal(0),
                    )
                )
            except (ValueError, ArithmeticError) as exc:
                raise ConfigError(f"{path}:{line_no} の解析に失敗しました: {exc}") from exc
    return candles


def _read_mt4(path: Path, symbol: str, zone: ZoneInfo) -> list[Candle]:
    """``YYYY.MM.DD,HH:MM,O,H,L,C,V`` を読む（ヘッダ無し）。"""
    candles: list[Candle] = []
    with path.open(encoding="utf-8-sig", errors="replace") as handle:
        for line_no, line in enumerate(handle, start=1):
            row = line.strip()
            if not row:
                continue
            parts = [p.strip() for p in row.split(",")]
            if len(parts) < 6:
                raise ConfigError(f"{path}:{line_no} の列数が足りません: {row[:60]}")
            try:
                stamp = datetime.strptime(f"{parts[0]} {parts[1]}", "%Y.%m.%d %H:%M").replace(
                    tzinfo=zone
                )
                candles.append(
                    Candle(
                        symbol=symbol,
                        timestamp=stamp.astimezone(UTC),
                        open=to_decimal(parts[2]),
                        high=to_decimal(parts[3]),
                        low=to_decimal(parts[4]),
                        close=to_decimal(parts[5]),
                        volume=to_decimal(parts[6]) if len(parts) > 6 else to_decimal(0),
                    )
                )
            except (ValueError, ArithmeticError) as exc:
                raise ConfigError(f"{path}:{line_no} の解析に失敗しました: {exc}") from exc
    return candles


def _read_delimited(path: Path, symbol: str, zone: ZoneInfo) -> list[Candle]:
    """ヘッダ付きの区切りファイルを読む。区切り文字は自動判定する。"""
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        try:
            dialect: type[csv.Dialect] | csv.Dialect = csv.Sniffer().sniff(
                sample, delimiters=",;\t"
            )
        except csv.Error:
            dialect = csv.excel

        reader = csv.DictReader(handle, dialect=dialect)
        fields = reader.fieldnames or []
        mapping = {
            _ALIASES[name.strip().lower()]: name
            for name in fields
            if name and name.strip().lower() in _ALIASES
        }

        missing = [c for c in ("timestamp", "open", "high", "low", "close") if c not in mapping]
        if missing:
            raise ConfigError(
                f"{path} に必要な列がありません: {', '.join(missing)}"
                f"（見つかった列: {', '.join(fields)}）"
            )

        candles: list[Candle] = []
        for line_no, row in enumerate(reader, start=2):
            raw = (row.get(mapping["timestamp"]) or "").strip()
            if not raw:
                continue
            try:
                stamp = _parse_timestamp(raw, zone)
                candles.append(
                    Candle(
                        symbol=symbol,
                        timestamp=stamp,
                        open=to_decimal(row[mapping["open"]]),
                        high=to_decimal(row[mapping["high"]]),
                        low=to_decimal(row[mapping["low"]]),
                        close=to_decimal(row[mapping["close"]]),
                        volume=to_decimal(row.get(mapping.get("volume", ""), 0) or 0),
                    )
                )
            except (ValueError, KeyError, ArithmeticError) as exc:
                raise ConfigError(f"{path}:{line_no} の解析に失敗しました: {exc}") from exc
    return candles


_TIME_FORMATS = (
    "%d.%m.%Y %H:%M:%S.%f",  # Dukascopy
    "%d.%m.%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y%m%d %H%M%S",
    "%Y-%m-%d",
    "%Y/%m/%d",
)


def _try_parse(text: str) -> datetime | None:
    """ISO 8601 とよくある表記を順に試す。どれも合わなければ None。"""
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for pattern in _TIME_FORMATS:
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


def _parse_timestamp(raw: str, zone: ZoneInfo) -> datetime:
    """よくある時刻表記を UTC の datetime にする。"""
    text = raw.strip().replace("Z", "+00:00")
    parsed = _try_parse(text)
    if parsed is None:
        raise ValueError(f"時刻として解釈できません: {raw!r}")

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(UTC)


def resample(candles: Sequence[Candle], granularity: str) -> list[Candle]:
    """足をより長い足種へまとめ直す。

    無料のヒストリカルデータはほとんどが1分足なので、
    実際に検証したい足種へここで変換する。

    始値は区間の最初、終値は最後、高値安値は区間内の極値、
    出来高は合計。**区間の途中で終わっている最後の足は捨てる**。
    未確定の足を残すと、実際には存在しなかった高値安値で
    ストップ判定が動いてしまう。

    Raises:
        ConfigError: 未知の足種、または元の足より短い足種を指定した場合。
    """
    if not candles:
        return []

    interval = parse_granularity(granularity)
    source = _infer_interval(candles)
    if source is not None and interval < source:
        raise ConfigError(f"元データ（約 {source}）より短い足種 {granularity} は作れません")

    buckets: dict[datetime, list[Candle]] = {}
    for candle in candles:
        key = _floor(candle.timestamp, interval)
        buckets.setdefault(key, []).append(candle)

    result: list[Candle] = []
    for key in sorted(buckets):
        group = buckets[key]
        result.append(
            Candle(
                symbol=group[0].symbol,
                timestamp=key,
                open=group[0].open,
                high=max(c.high for c in group),
                low=min(c.low for c in group),
                close=group[-1].close,
                volume=sum((c.volume for c in group), start=to_decimal(0)),
            )
        )

    # 最後の区間が埋まりきっていなければ落とす。
    if result and source is not None:
        expected = int(interval / source)
        if expected > 1 and len(buckets[sorted(buckets)[-1]]) < expected:
            result.pop()

    logger.info("%d本 → %s %d本にまとめました", len(candles), granularity, len(result))
    return result


def _floor(stamp: datetime, interval: timedelta) -> datetime:
    """時刻を足種の刻みへ切り下げる（UTCのエポック基準）。"""
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    elapsed = (stamp.astimezone(UTC) - epoch) // interval
    return epoch + elapsed * interval


def _infer_interval(candles: Iterable[Candle]) -> timedelta | None:
    """元データの足の長さを推定する。連続する差分の最小値を採用する。"""
    times = sorted({c.timestamp for c in candles})
    if len(times) < 2:
        return None
    gaps = [b - a for a, b in itertools.pairwise(times) if b > a]
    return min(gaps) if gaps else None
