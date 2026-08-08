"""複数銘柄について donchian を前半/後半に分けて検証し、横並びにする。

**多重比較に注意すること。** 銘柄を増やせば、そのうちどれかは偶然よく見える。
5銘柄を独立に見れば、有意水準5%でも「どれかが有意」になる確率は約23%ある。
前半で良かった銘柄が後半でも同符号か、を判定の軸にする。
"""

from __future__ import annotations

import asyncio
import statistics
from decimal import Decimal
from pathlib import Path

from zerotrade.backtest.engine import run_backtest
from zerotrade.data.historical import load_csv
from zerotrade.settings import Settings, load_settings

#: (表示名, CSV, 実測スプレッド(価格単位), 最小数量, 数量刻み)
TARGETS = [
    ("BTC-USDT", "data/btc_h1.csv", "0.2", "0.0001", "0.0001"),
    ("SOL-USDT", "data/sol_usdt_h1.csv", "0.017", "0.03", "0.01"),
    ("1000PEPE-USDT", "data/1000pepe_usdt_h1.csv", "0.0000013", "699", "1"),
    ("TAO-USDT", "data/tao_usdt_h1.csv", "0.15", "0.01017", "0.00001"),
    ("GOLD", "data/nccogold2usd_usdt_h1.csv", "0.07", "0.0005", "0.0001"),
    ("WTI", "data/ncco1oilwti2usd_usdt_h1.csv", "0.06", "0.02594", "0.00001"),
]


def tstat(values: list[float]) -> float:
    if len(values) < 3:
        return 0.0
    sd = statistics.stdev(values)
    return statistics.mean(values) / (sd / len(values) ** 0.5) if sd else 0.0


def tune(base: Settings, symbol: str, spread: str, min_qty: str, step: str) -> Settings:
    return base.model_copy(
        update={
            "symbols": [symbol],
            "broker": base.broker.model_copy(update={"spread": Decimal(spread)}),
            "sizing": base.sizing.model_copy(
                update={"min_quantity": Decimal(min_qty), "quantity_step": Decimal(step)}
            ),
        }
    )


async def evaluate(name: str, path: str, spread: str, min_qty: str, step: str) -> None:
    base = load_settings("config/bingx.yaml")
    candles = load_csv(Path(path), symbol=name)
    settings = tune(base, name, spread, min_qty, step)
    half = len(candles) // 2

    print(
        f"\n=== {name}  {candles[0].timestamp:%Y-%m-%d}〜{candles[-1].timestamp:%Y-%m-%d} "
        f"({len(candles)}本) スプレッド {spread} ==="
    )
    move = (candles[-1].close / candles[0].close - 1) * 100
    print(f"  原資産の騰落: {move:+.1f}%")

    for label, part in (("前半", candles[:half]), ("後半", candles[half:]), ("全体", candles)):
        try:
            result = await run_backtest(settings, {name: part}, label=f"{name}-{label}")
        except Exception as exc:
            print(f"  [{label}] 検証できず: {exc}")
            continue
        pnls = [float(t.realized_pnl) for t in result.trades]
        verdict = "○" if result.beats_buy_and_hold else "×"
        print(
            f"  [{label}] {result.return_ratio * 100:+7.2f}%  "
            f"{result.summary.trades:4d}件 勝率 {result.summary.win_rate * 100:3.0f}% "
            f"PF {float(result.summary.profit_factor or 0):.2f}  "
            f"買い持ち {result.buy_and_hold_return * 100:+7.2f}% {verdict}  "
            f"t={tstat(pnls):+5.2f}"
        )


async def main() -> None:
    for target in TARGETS:
        await evaluate(*target)


if __name__ == "__main__":
    asyncio.run(main())
