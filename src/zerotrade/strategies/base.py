"""Strategy 抽象とプラグインレジストリ。

戦略に許されているのは :class:`~zerotrade.models.Signal` を返すことだけ。
サイズ決定も発注もできない。これは意図的な制約で、
「今日は調子がいいからロットを上げる」類の裁量介入を
コードレベルで不可能にするためにこうしてある。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from zerotrade.errors import ConfigError
from zerotrade.models import Candle, Position, Signal, SignalAction, Ticker

__all__ = [
    "Strategy",
    "StrategyContext",
    "available_strategies",
    "create_strategy",
    "register_strategy",
]


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """戦略に渡す入力一式。"""

    symbol: str
    candles: Sequence[Candle]
    """古い順に並んだローソク足。最後の要素が最新。"""

    ticker: Ticker | None = None
    position: Position | None = None
    """その銘柄の現在の建玉。無ければ None。"""

    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def last_close(self) -> Decimal | None:
        return self.candles[-1].close if self.candles else None

    @property
    def closes(self) -> list[Decimal]:
        return [c.close for c in self.candles]


class Strategy(ABC):
    """シグナル生成の抽象基底。

    サブクラスは :meth:`generate` を実装する。副作用を持たせないこと
    （同じ入力からは常に同じシグナルが出るようにする）。
    """

    #: レジストリ登録名。``register_strategy`` が使う。
    name: str = "base"

    #: 必要な最小ローソク足本数。StrategyRunner が足りない間は呼び出さない。
    warmup_bars: int = 1

    def __init__(self, **params: Any) -> None:
        self.params = params

    @abstractmethod
    def generate(self, context: StrategyContext) -> Signal:
        """シグナルを生成する。判断材料が無ければ ``HOLD`` を返す。"""

    def hold(self, context: StrategyContext, reason: str = "") -> Signal:
        """``HOLD`` シグナルを作るヘルパ。"""
        return Signal(
            symbol=context.symbol,
            action=SignalAction.HOLD,
            strategy=self.name,
            reason=reason,
        )

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r} params={self.params!r}>"


# --------------------------------------------------------------- レジストリ

_REGISTRY: dict[str, type[Strategy]] = {}


def register_strategy(cls: type[Strategy]) -> type[Strategy]:
    """戦略クラスをレジストリへ登録するデコレータ。

    Example:
        >>> @register_strategy
        ... class MyStrategy(Strategy):
        ...     name = "my_strategy"
    """
    key = cls.name
    if key in _REGISTRY and _REGISTRY[key] is not cls:
        raise ConfigError(f"戦略名が重複しています: {key}")
    _REGISTRY[key] = cls
    return cls


def available_strategies() -> list[str]:
    """登録済み戦略名の一覧。"""
    return sorted(_REGISTRY)


def create_strategy(name: str, params: dict[str, Any] | None = None) -> Strategy:
    """名前から戦略インスタンスを生成する。

    Raises:
        ConfigError: 未登録の名前、またはパラメータが不正な場合。
    """
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ConfigError(
            f"未知の戦略です: {name}（利用可能: {', '.join(available_strategies()) or 'なし'}）"
        )
    try:
        return cls(**(params or {}))
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"戦略 {name} のパラメータが不正です: {exc}") from exc


# 型チェッカ向けの別名。デコレータとして使うことを明示する。
StrategyFactory = Callable[..., Strategy]
