"""テクニカル指標（純Python・Decimal 実装）。

pandas/numpy に依存させていないのは、リアルタイム実行時に
数百本のローソク足を毎ループ DataFrame へ変換するのが無駄だからと、
価格を Decimal のまま扱いたいため。バックテストで大量データを回すときは
``zerotrade[analysis]`` を入れてベクトル化実装に差し替えられる。

すべての関数は「計算不能なら None を返す」方針。
データ不足を 0 や直近値で埋めると、
起動直後に誤ったシグナルが出る典型的なバグにつながる。
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from zerotrade.models import Candle

__all__ = ["atr", "ema", "rsi", "sma", "true_range"]


def sma(values: Sequence[Decimal], period: int) -> Decimal | None:
    """単純移動平均。データが ``period`` 本に満たなければ None。"""
    if period <= 0:
        raise ValueError("period は正の整数である必要があります")
    if len(values) < period:
        return None
    window = values[-period:]
    return sum(window, Decimal(0)) / Decimal(period)


def ema(values: Sequence[Decimal], period: int) -> Decimal | None:
    """指数移動平均。初期値は先頭 ``period`` 本の単純平均。"""
    if period <= 0:
        raise ValueError("period は正の整数である必要があります")
    if len(values) < period:
        return None
    multiplier = Decimal(2) / Decimal(period + 1)
    current = sum(values[:period], Decimal(0)) / Decimal(period)
    for value in values[period:]:
        current = (value - current) * multiplier + current
    return current


def rsi(values: Sequence[Decimal], period: int = 14) -> Decimal | None:
    """RSI（Wilder 平滑化）。0〜100 を返す。

    ``period + 1`` 本以上の価格が必要（差分を取るため）。
    """
    if period <= 0:
        raise ValueError("period は正の整数である必要があります")
    if len(values) < period + 1:
        return None

    deltas = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [d if d > 0 else Decimal(0) for d in deltas]
    losses = [-d if d < 0 else Decimal(0) for d in deltas]

    avg_gain = sum(gains[:period], Decimal(0)) / Decimal(period)
    avg_loss = sum(losses[:period], Decimal(0)) / Decimal(period)

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * Decimal(period - 1) + gains[i]) / Decimal(period)
        avg_loss = (avg_loss * Decimal(period - 1) + losses[i]) / Decimal(period)

    if avg_loss == 0:
        # 下落が一度も無い＝完全な上昇局面。RSI は 100 に張り付く。
        return Decimal(100) if avg_gain > 0 else Decimal(50)

    rs = avg_gain / avg_loss
    return Decimal(100) - (Decimal(100) / (Decimal(1) + rs))


def true_range(current: Candle, previous: Candle | None) -> Decimal:
    """True Range。前足が無ければ単純な高安幅。"""
    if previous is None:
        return current.high - current.low
    return max(
        current.high - current.low,
        abs(current.high - previous.close),
        abs(current.low - previous.close),
    )


def atr(candles: Sequence[Candle], period: int = 14) -> Decimal | None:
    """ATR（Wilder 平滑化）。``period + 1`` 本以上のローソク足が必要。"""
    if period <= 0:
        raise ValueError("period は正の整数である必要があります")
    if len(candles) < period + 1:
        return None

    ranges = [true_range(candles[i], candles[i - 1]) for i in range(1, len(candles))]
    current = sum(ranges[:period], Decimal(0)) / Decimal(period)
    for value in ranges[period:]:
        current = (current * Decimal(period - 1) + value) / Decimal(period)
    return current
