"""戦略プラグイン。

``zerotrade.strategies`` を import した時点で同梱戦略がレジストリへ登録される。
自作戦略は :func:`~zerotrade.strategies.base.register_strategy` を付けて
どこかで import されるようにすればよい。
"""

from __future__ import annotations

from zerotrade.strategies.base import (
    Strategy,
    StrategyContext,
    available_strategies,
    create_strategy,
    register_strategy,
)
from zerotrade.strategies.donchian import DonchianStrategy
from zerotrade.strategies.sma_rsi import SmaRsiStrategy
from zerotrade.strategies.tokyo_fix import TokyoFixStrategy

__all__ = [
    "DonchianStrategy",
    "SmaRsiStrategy",
    "Strategy",
    "StrategyContext",
    "TokyoFixStrategy",
    "available_strategies",
    "create_strategy",
    "register_strategy",
]
