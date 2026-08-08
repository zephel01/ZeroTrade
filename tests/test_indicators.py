"""テクニカル指標のテスト。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.conftest import make_candles
from zerotrade.strategies.indicators import atr, ema, rsi, sma, true_range


def test_SMAは単純平均を返す() -> None:
    values = [Decimal(x) for x in (1, 2, 3, 4, 5)]
    assert sma(values, 5) == Decimal(3)
    assert sma(values, 2) == Decimal("4.5")


def test_データ不足ならNoneを返す() -> None:
    values = [Decimal(1), Decimal(2)]
    assert sma(values, 5) is None
    assert ema(values, 5) is None
    assert rsi(values, 14) is None
    assert atr(make_candles([1.0, 2.0]), 14) is None


def test_期間が不正なら例外() -> None:
    with pytest.raises(ValueError, match="正の整数"):
        sma([Decimal(1)], 0)


def test_一貫した上昇ではRSIが100に近づく() -> None:
    values = [Decimal(100 + i) for i in range(30)]
    result = rsi(values, 14)
    assert result is not None
    assert result == Decimal(100)


def test_一貫した下落ではRSIが0に近づく() -> None:
    values = [Decimal(200 - i) for i in range(30)]
    result = rsi(values, 14)
    assert result is not None
    assert result == Decimal(0)


def test_RSIは0から100の範囲に収まる() -> None:
    raw = (10, 12, 11, 15, 14, 18, 16, 20, 19, 22, 21, 25, 24, 28, 27, 30)
    values = [Decimal(str(v)) for v in raw]
    result = rsi(values, 14)
    assert result is not None
    assert Decimal(0) <= result <= Decimal(100)


def test_TrueRangeは前足の終値を考慮する() -> None:
    candles = make_candles([100.0, 105.0], spread=0.5)
    # 2本目: high-low=1.0, |high - prev_close|=|105.5-100|=5.5 が最大
    assert true_range(candles[1], candles[0]) == Decimal("5.5")
    # 前足が無ければ単純な高安幅
    assert true_range(candles[0], None) == Decimal("1.0")


def test_ATRは平均的な値幅を返す() -> None:
    # 終値が一定なら値幅は毎回 spread*2 = 1.0
    candles = make_candles([100.0] * 30, spread=0.5)
    result = atr(candles, 14)
    assert result is not None
    assert result == Decimal("1.0")


def test_EMAは直近の値に重みを置く() -> None:
    rising = [Decimal(i) for i in range(1, 21)]
    fast = ema(rising, 5)
    slow = ema(rising, 15)
    assert fast is not None and slow is not None
    assert fast > slow, "上昇局面では短期EMAが長期EMAを上回る"
