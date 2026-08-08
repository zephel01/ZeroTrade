"""パラメータ掃引。

**この機能は自分を騙すための道具になりやすい。** 全期間で最も成績の良い
パラメータを選べば、その数字は必ず良く見える。過去にいちばん都合よく
当てはまる組み合わせを選んだのだから当然で、それが将来も効くかは
まったく別の話である。

そこでこの実装は、探索と評価を最初から分けている。足を前半（in-sample）と
後半（out-of-sample）に割り、**探索は前半だけで行い、選んだパラメータを
後半で答え合わせする**。両方の成績を並べて出すので、前半だけ突出して
後半で崩れる組み合わせは一目で分かる。

順位付けの既定は素の損益ではなく、ドローダウンで割った値にしてある。
「最終的にいくら増えたか」だけを見ると、途中で口座の半分を溶かす経路も
高く評価されてしまうため。
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from zerotrade.backtest.engine import BacktestResult, run_backtest, split_candles
from zerotrade.errors import ConfigError
from zerotrade.log import get_logger
from zerotrade.models import Candle
from zerotrade.settings import Settings

__all__ = [
    "OptimizationResult",
    "ParameterGrid",
    "optimize",
    "parse_param_spec",
    "score_by_recovery_factor",
]

logger = get_logger(__name__)

ParameterGrid = Mapping[str, Sequence[Any]]
Scorer = Callable[[BacktestResult], Decimal]


def score_by_recovery_factor(result: BacktestResult) -> Decimal:
    """既定のスコア。純損益 ÷ 最大ドローダウン。

    素の損益で並べると、途中で口座を半分溶かしてから戻した経路が
    上位に来てしまう。「どれだけ痛い目に遭って稼いだか」で割る。

    トレードが少なすぎる組み合わせは統計として意味を持たないので
    強く減点する。
    """
    if result.summary.trades < 5:
        return Decimal(-1_000_000)
    if result.summary.max_drawdown <= 0:
        # 一度も落ち込まなかった。損益そのもので評価する。
        return result.summary.net_pnl
    return result.summary.net_pnl / result.summary.max_drawdown


@dataclass(frozen=True, slots=True)
class OptimizationResult:
    """掃引1件ぶんの結果。"""

    params: dict[str, Any]
    in_sample: BacktestResult
    out_of_sample: BacktestResult | None
    score: Decimal

    @property
    def is_robust(self) -> bool:
        """後半でも **単純保有を上回れた** か。

        単にプラスであることを条件にすると、上昇相場では
        何もしなくても満たせてしまう。ベンチマーク超えを条件にする。
        """
        if self.out_of_sample is None:
            return False
        return self.out_of_sample.summary.net_pnl > 0 and self.out_of_sample.beats_buy_and_hold

    def describe(self) -> str:
        params = " ".join(f"{k}={v}" for k, v in sorted(self.params.items()))
        line = f"{params}\n    in : {self.in_sample.describe_vs_benchmark()}"
        if self.out_of_sample is not None:
            mark = "○" if self.is_robust else "×"
            line += f"\n    out: {self.out_of_sample.describe_vs_benchmark()}  {mark}"
        return line


def parse_param_spec(specs: Sequence[str]) -> dict[str, list[Any]]:
    """``fast_period=5,10,20`` 形式の指定を辞書に変換する。

    値は int → float → 文字列の順に解釈を試みる。

    Raises:
        ConfigError: ``=`` が無い、または値が空の場合。
    """
    grid: dict[str, list[Any]] = {}
    for spec in specs:
        name, sep, raw = spec.partition("=")
        if not sep or not name.strip() or not raw.strip():
            raise ConfigError(
                f"パラメータ指定の形式が不正です: {spec!r}（例: fast_period=5,10,20）"
            )
        grid[name.strip()] = [_coerce(v.strip()) for v in raw.split(",") if v.strip()]
    return grid


def _coerce(value: str) -> Any:
    for caster in (int, float):
        try:
            return caster(value)
        except ValueError:
            continue
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    return value


async def optimize(
    settings: Settings,
    candles: Mapping[str, Sequence[Candle]],
    grid: ParameterGrid,
    *,
    split_ratio: float = 0.7,
    scorer: Scorer = score_by_recovery_factor,
    top: int = 10,
) -> list[OptimizationResult]:
    """グリッド探索し、上位を out-of-sample で検証する。

    Args:
        settings: 基準となる設定。
        candles: 銘柄ごとの足（古い順）。
        grid: パラメータ名 → 試す値のリスト。
        split_ratio: in-sample に使う割合。既定は前半70%。
        scorer: 並べ替えに使うスコア関数。
        top: out-of-sample で検証する上位件数。

    Returns:
        スコアの高い順に並んだ結果。上位 ``top`` 件だけ
        ``out_of_sample`` が埋まる。

    Raises:
        ConfigError: グリッドが空の場合。
    """
    if not grid:
        raise ConfigError("掃引するパラメータが指定されていません")

    names = list(grid)
    combinations = [
        dict(zip(names, values, strict=True)) for values in itertools.product(*grid.values())
    ]
    logger.info("%d 通りの組み合わせを探索します", len(combinations))

    train: dict[str, list[Candle]] = {}
    test: dict[str, list[Candle]] = {}
    for symbol in settings.symbols:
        head, tail = split_candles(candles[symbol], split_ratio)
        train[symbol], test[symbol] = head, tail

    scored: list[OptimizationResult] = []
    for i, params in enumerate(combinations, start=1):
        try:
            result = await run_backtest(
                settings, train, strategy_params=params, label=f"探索 {i}/{len(combinations)}"
            )
        except ConfigError as exc:
            # 期間の組み合わせによってはウォームアップが足りない。
            # そのパラメータを飛ばすだけで、掃引全体は続ける。
            logger.warning("%s をスキップしました: %s", params, exc)
            continue
        scored.append(
            OptimizationResult(
                params=params, in_sample=result, out_of_sample=None, score=scorer(result)
            )
        )

    scored.sort(key=lambda r: r.score, reverse=True)

    verified: list[OptimizationResult] = []
    for entry in scored[:top]:
        out = await run_backtest(settings, test, strategy_params=entry.params, label="検証")
        verified.append(
            OptimizationResult(
                params=entry.params,
                in_sample=entry.in_sample,
                out_of_sample=out,
                score=entry.score,
            )
        )

    return verified + scored[top:]
