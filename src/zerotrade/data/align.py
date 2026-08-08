"""複数銘柄の時間軸を揃える。

銘柄ごとに歯抜けの位置が違う。祝日、流動性の薄い時間帯、
データ提供元の欠損——理由はさまざまだが、揃っていないまま
バックテストへ渡すと **銘柄ごとに時計が別々に進む**。

PaperBroker は銘柄ごとに独立してカーソルを進めるため、
本数がずれた瞬間から「A社は3月、B社は5月」を同時に見ながら
取引する、という現実には起こりえない状態になる。
そのずれは損益に現れないので気づけない。だから入口で揃える。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime

from zerotrade.errors import ConfigError
from zerotrade.log import get_logger
from zerotrade.models import Candle

__all__ = ["align_candles"]

logger = get_logger(__name__)


def align_candles(
    candles: Mapping[str, Sequence[Candle]], *, min_bars: int = 50
) -> dict[str, list[Candle]]:
    """すべての銘柄に存在する時刻だけを残して揃える。

    Args:
        candles: 銘柄ごとの足（古い順）。
        min_bars: 揃えた結果がこの本数を下回ったら異常とみなす。

    Returns:
        同じ時刻列を持つ銘柄ごとの足。

    Raises:
        ConfigError: 銘柄が空、または共通の時刻がほとんど残らなかった場合。
    """
    if not candles:
        raise ConfigError("銘柄が指定されていません")

    if len(candles) == 1:
        symbol, series = next(iter(candles.items()))
        return {symbol: list(series)}

    common: set[datetime] | None = None
    for series in candles.values():
        stamps = {c.timestamp for c in series}
        common = stamps if common is None else (common & stamps)
    assert common is not None

    if len(common) < min_bars:
        detail = " / ".join(f"{s}:{len(v)}本" for s, v in candles.items())
        raise ConfigError(
            f"銘柄間で共通する時刻が {len(common)} 本しかありません（{detail}）。"
            "足種や期間が揃っているか確認してください"
        )

    aligned = {
        symbol: [c for c in series if c.timestamp in common] for symbol, series in candles.items()
    }

    dropped = {symbol: len(series) - len(aligned[symbol]) for symbol, series in candles.items()}
    if any(dropped.values()):
        logger.info(
            "時間軸を揃えました（共通 %d 本 / 除外 %s）",
            len(common),
            ", ".join(f"{s}:{n}本" for s, n in dropped.items() if n),
        )
    return aligned
