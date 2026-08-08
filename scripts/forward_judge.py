"""前向き検証の進捗と判定。

6銘柄ぶんの記録DBを読み、**全銘柄をプールした1つの数字**で判定する。

## なぜプールするのか

銘柄別に見て「良かったものを探す」のは、これまで何度も踏んだ多重比較の罠である。
6銘柄あれば、実力が無くてもどれかは良く見える。前回それで 1000PEPE と GOLD が
残り、偶然の期待値（1.5個）とぴったり一致した。

だから**主判定はプールした1つの数字**で行う。銘柄別の内訳も出すが、
それは記述であって判定ではない。銘柄別の数字を見て「PEPEだけは良かった」と
言い出した時点で、また同じ罠に落ちる。

    python3 scripts/forward_judge.py
"""

from __future__ import annotations

import statistics
import sys
from decimal import Decimal
from pathlib import Path

from zerotrade.store import Store
from zerotrade.store.models import TradeRow

#: 判定に必要なトレード数。事前登録済み。docs/forward-test.md 参照。
TARGET_TRADES = 60

#: 合格ライン。開始前に確定。**後から緩めない。**
MIN_PROFIT_FACTOR = Decimal("1.0")
WIN_RATE_RANGE = (Decimal("0.30"), Decimal("0.50"))
MIN_PAYOFF_RATIO = Decimal("1.5")


def _load(state_dir: Path) -> dict[str, list[TradeRow]]:
    trades: dict[str, list[TradeRow]] = {}
    for path in sorted(state_dir.glob("*/zerotrade.db")):
        name = path.parent.name
        try:
            with Store.open_for_read(path) as store:
                trades[name] = store.trades(limit=100_000)
        except Exception as exc:  # 記録がまだ無い銘柄
            print(f"  {name}: 読めませんでした（{exc}）")
            trades[name] = []
    return trades


def _stats(rows: list[TradeRow]) -> dict[str, object]:
    pnls = [t.realized_pnl for t in rows]
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p < 0]

    gross_profit = sum(wins, Decimal(0))
    gross_loss = sum(losses, Decimal(0))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None
    win_rate = (Decimal(len(wins)) / Decimal(len(rows))) if rows else Decimal(0)
    avg_win = (gross_profit / len(wins)) if wins else Decimal(0)
    avg_loss = (gross_loss / len(losses)) if losses else Decimal(0)
    payoff = (avg_win / avg_loss) if avg_loss > 0 else None

    floats = [float(p) for p in pnls]
    if len(floats) >= 3 and statistics.stdev(floats) > 0:
        tstat = statistics.mean(floats) / (statistics.stdev(floats) / len(floats) ** 0.5)
    else:
        tstat = 0.0

    return {
        "trades": len(rows),
        "net": sum(pnls, Decimal(0)),
        "profit_factor": profit_factor,
        "win_rate": win_rate,
        "payoff": payoff,
        "t": tstat,
    }


def _fmt(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    return str(value)


def main() -> int:
    state_dir = Path("state/forward")
    if not state_dir.is_dir():
        print("まだ記録がありません。scripts/forward_start.sh で開始してください。")
        return 1

    by_symbol = _load(state_dir)
    pooled = [t for rows in by_symbol.values() for t in rows]

    print("\n銘柄別の内訳（**これは記述であって判定ではない**）")
    print(f"  {'銘柄':8s} {'件数':>5s} {'損益':>12s} {'PF':>6s} {'勝率':>6s}")
    for name, rows in sorted(by_symbol.items()):
        s = _stats(rows)
        rate = f"{float(s['win_rate']) * 100:.0f}%" if rows else "—"
        print(
            f"  {name:8s} {s['trades']:>5d} {float(s['net']):>12,.0f} "
            f"{_fmt(s['profit_factor']):>6s} {rate:>6s}"
        )

    s = _stats(pooled)
    count = int(s["trades"])  # type: ignore[call-overload]
    print(f"\nプール合計: {count} / {TARGET_TRADES} 件")

    if count < TARGET_TRADES:
        remaining = TARGET_TRADES - count
        print(f"  あと {remaining} 件で判定できます（1日あたり約4件のペース）。")
        print("  **この時点の成績を見て設定を変えないこと。** 変えたら検証は最初からになります。")
        return 0

    print("\n判定（事前登録した基準に照らす）")
    pf = s["profit_factor"]
    payoff = s["payoff"]
    win_rate = s["win_rate"]

    checks = [
        ("プロフィットファクター 1.0 以上", pf is not None and pf >= MIN_PROFIT_FACTOR, _fmt(pf)),
        (
            "勝率 30〜50%",
            WIN_RATE_RANGE[0] <= win_rate <= WIN_RATE_RANGE[1],  # type: ignore[operator]
            f"{float(win_rate) * 100:.0f}%",  # type: ignore[arg-type]
        ),
        (
            "平均利益÷平均損失 1.5 以上",
            payoff is not None and payoff >= MIN_PAYOFF_RATIO,
            _fmt(payoff),
        ),
    ]
    for label, passed, detail in checks:
        print(f"  [{'OK  ' if passed else 'NG  '}] {label} — {detail}")

    print(f"\n  参考: t値 {s['t']:+.2f} / 純損益 {float(s['net']):+,.0f}")

    if all(passed for _, passed, _ in checks):
        print("\n**合格。** 次にやるのは実弾ではなく、なぜ効くのかの説明です。")
        return 0

    print("\n**不合格。** 過去データで見えた優位性は再現しませんでした。")
    print("docs/hypotheses.md の未検証の機構へ進みます。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
