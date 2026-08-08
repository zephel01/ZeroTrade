"""記録層。

取引プロセスが書き、ダッシュボードとレポートが **別プロセスから** 読む。
この分離により、表示側が落ちても取引は続き、取引を止めていても履歴は見られる。
"""

from __future__ import annotations

from zerotrade.store.models import (
    EquityPoint,
    EventRow,
    PerformanceSummary,
    RejectionRow,
    SignalRow,
    TradeRow,
)
from zerotrade.store.sqlite import Store, summarize

__all__ = [
    "EquityPoint",
    "EventRow",
    "PerformanceSummary",
    "RejectionRow",
    "SignalRow",
    "Store",
    "TradeRow",
    "summarize",
]
