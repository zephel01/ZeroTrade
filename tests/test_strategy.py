"""戦略とレジストリのテスト。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.conftest import make_candles, make_position
from zerotrade.errors import ConfigError
from zerotrade.models import Side, SignalAction
from zerotrade.strategies import available_strategies, create_strategy
from zerotrade.strategies.base import Strategy, StrategyContext
from zerotrade.strategies.sma_rsi import SmaRsiStrategy


def _context(closes: list[float], **kwargs: object) -> StrategyContext:
    return StrategyContext(symbol="USD_JPY", candles=make_candles(closes), **kwargs)  # type: ignore[arg-type]


def _reversal_up() -> list[float]:
    """20本の下降のあと2本上昇し、**最終足でちょうどゴールデンクロスする** 系列。

    クロスの成立は「1本前は下、今は上」の遷移で判定するため、
    反転から何本も経過した系列ではシグナルが出ない。
    fast=3 / slow=8 で最終足がクロス足になるよう本数を合わせてある。
    """
    return [150 - i * 0.1 for i in range(20)] + [148.1 + (i + 1) * 0.5 for i in range(2)]


def _reversal_down() -> list[float]:
    """:func:`_reversal_up` の逆。最終足でデッドクロスする系列。"""
    return [150 + i * 0.1 for i in range(20)] + [151.9 - (i + 1) * 0.5 for i in range(2)]


# ------------------------------------------------------------ レジストリ


def test_同梱戦略が登録されている() -> None:
    assert "sma_rsi" in available_strategies()


def test_未知の戦略名はConfigError() -> None:
    with pytest.raises(ConfigError, match="未知の戦略"):
        create_strategy("存在しない戦略")


def test_不正なパラメータはConfigError() -> None:
    with pytest.raises(ConfigError, match="パラメータが不正"):
        create_strategy("sma_rsi", {"fast_period": 50, "slow_period": 20})

    with pytest.raises(ConfigError, match="パラメータが不正"):
        create_strategy("sma_rsi", {"存在しないパラメータ": 1})


def test_戦略はレジストリから生成できる() -> None:
    strategy = create_strategy("sma_rsi", {"fast_period": 3, "slow_period": 8})
    assert isinstance(strategy, SmaRsiStrategy)
    assert strategy.fast_period == 3


# ------------------------------------------------------------ シグナル生成


def test_ウォームアップ中はHOLD() -> None:
    strategy = SmaRsiStrategy(fast_period=5, slow_period=20)
    signal = strategy.generate(_context([150.0] * 5))
    assert signal.action is SignalAction.HOLD
    assert "ウォームアップ" in signal.reason


def test_ゴールデンクロスでロングシグナル() -> None:
    strategy = SmaRsiStrategy(
        fast_period=3, slow_period=8, rsi_period=3, atr_period=3, rsi_overbought=95
    )
    # 下降 → 反転上昇でクロスさせる
    closes = _reversal_up()
    signal = strategy.generate(_context(closes))

    assert signal.action is SignalAction.ENTER_LONG
    assert signal.side is Side.BUY
    assert signal.stop_loss is not None
    assert signal.take_profit is not None
    assert signal.stop_loss < Decimal(str(closes[-1]))
    assert signal.take_profit > Decimal(str(closes[-1]))


def test_デッドクロスでショートシグナル() -> None:
    strategy = SmaRsiStrategy(
        fast_period=3, slow_period=8, rsi_period=3, atr_period=3, rsi_oversold=5
    )
    closes = _reversal_down()
    signal = strategy.generate(_context(closes))

    assert signal.action is SignalAction.ENTER_SHORT
    assert signal.side is Side.SELL
    assert signal.stop_loss is not None
    assert signal.stop_loss > Decimal(str(closes[-1]))


def test_ショート無効ならエントリーしない() -> None:
    strategy = SmaRsiStrategy(
        fast_period=3, slow_period=8, rsi_period=3, atr_period=3, allow_short=False
    )
    closes = _reversal_down()
    signal = strategy.generate(_context(closes))

    assert signal.action is SignalAction.HOLD
    assert "ショートは無効" in signal.reason


def test_RSIが買われすぎならエントリーを見送る() -> None:
    strategy = SmaRsiStrategy(
        fast_period=3, slow_period=8, rsi_period=3, atr_period=3, rsi_overbought=10
    )
    closes = _reversal_up()
    signal = strategy.generate(_context(closes))

    assert signal.action is SignalAction.HOLD
    assert "買われすぎ" in signal.reason


def test_保有中の逆クロスでEXIT() -> None:
    strategy = SmaRsiStrategy(fast_period=3, slow_period=8, rsi_period=3, atr_period=3)
    closes = _reversal_down()
    signal = strategy.generate(_context(closes, position=make_position(side=Side.BUY)))
    assert signal.action is SignalAction.EXIT


def test_保有中でクロスが無ければHOLD() -> None:
    strategy = SmaRsiStrategy(fast_period=3, slow_period=8, rsi_period=3, atr_period=3)
    closes = [150 + i * 0.1 for i in range(30)]
    signal = strategy.generate(_context(closes, position=make_position(side=Side.BUY)))
    assert signal.action is SignalAction.HOLD
    assert signal.reason == "保有継続"


def test_横ばい相場ではエントリーしない() -> None:
    strategy = SmaRsiStrategy(fast_period=3, slow_period=8, rsi_period=3, atr_period=3)
    signal = strategy.generate(_context([150.0] * 40))
    assert signal.action is SignalAction.HOLD


def test_戦略は発注機能を持たない() -> None:
    """設計上の保証: 戦略からブローカーへ到達する経路が存在しない。"""
    strategy = SmaRsiStrategy()
    forbidden = {"place_order", "submit", "broker", "risk"}
    assert not (forbidden & set(dir(strategy)))


def test_Strategyは抽象クラス() -> None:
    with pytest.raises(TypeError):
        Strategy()  # type: ignore[abstract]
