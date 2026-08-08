"""バックテスト基盤。

エンジンは **本番と同じ** RiskManager / OrderManager / StrategyRunner を通す。
バックテスト専用のロジックを別に書くと、そこに紛れ込んだ差異が
「検証では勝てたのに実弾では負ける」の温床になるため。

.. code-block:: bash

    zerotrade fetch --symbol USD_JPY --granularity M5 --days 365
    zerotrade backtest --csv data/USD_JPY_M5.csv
    zerotrade optimize --csv data/USD_JPY_M5.csv --param fast_period=5,10,20
"""

from __future__ import annotations

from zerotrade.backtest.engine import (
    BacktestResult,
    SimulationClock,
    default_database,
    run_backtest,
    split_candles,
)
from zerotrade.backtest.optimize import OptimizationResult, ParameterGrid, optimize
from zerotrade.backtest.robustness import RobustnessReport, bootstrap, required_trades

__all__ = [
    "BacktestResult",
    "OptimizationResult",
    "ParameterGrid",
    "RobustnessReport",
    "SimulationClock",
    "bootstrap",
    "default_database",
    "optimize",
    "required_trades",
    "run_backtest",
    "split_candles",
]
