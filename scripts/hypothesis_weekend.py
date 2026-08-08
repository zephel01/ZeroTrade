"""H1: 週末効果。docs/hypotheses.md の事前指定どおりに測る。"""

from __future__ import annotations

import csv
import statistics
from datetime import UTC, datetime


def load(path: str) -> list[tuple[datetime, float]]:
    out = []
    with open(path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            out.append(
                (datetime.fromisoformat(row["timestamp"]).astimezone(UTC), float(row["close"]))
            )
    return sorted(out)


def daily(bars):
    """UTC日ごとの (始値, 終値)。"""
    days: dict[datetime, list[float]] = {}
    for ts, close in bars:
        days.setdefault(ts.replace(hour=0, minute=0, second=0, microsecond=0), []).append(close)
    return {d: (v[0], v[-1]) for d, v in sorted(days.items())}


def tstat(xs):
    if len(xs) < 3:
        return 0.0
    sd = statistics.stdev(xs)
    return statistics.mean(xs) / (sd / len(xs) ** 0.5) if sd else 0.0


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return 0.0
    mx, my = statistics.mean(xs), statistics.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    return num / (dx * dy) if dx and dy else 0.0


def pairs(days, span_days: list[int], target_day: int):
    """span_days の曜日をまたぐ累積リターンと、target_day のリターンの組。"""
    keys = sorted(days)
    idx = {d: i for i, d in enumerate(keys)}
    out = []
    for d in keys:
        if d.weekday() != span_days[0]:
            continue
        try:
            span = [keys[idx[d] + k] for k in range(len(span_days))]
            tgt = keys[idx[d] + len(span_days)]
        except (KeyError, IndexError):
            continue
        if [s.weekday() for s in span] != span_days or tgt.weekday() != target_day:
            continue
        span_ret = days[span[-1]][1] / days[span[0]][0] - 1
        tgt_ret = days[tgt][1] / days[tgt][0] - 1
        out.append((span_ret, tgt_ret))
    return out


def report(name, data):
    if len(data) < 20:
        print(f"  {name}: サンプル不足 ({len(data)})")
        return
    xs = [a for a, _ in data]
    ys = [b for _, b in data]
    up = [b for a, b in data if a > 0]
    down = [b for a, b in data if a < 0]
    r = pearson(xs, ys)
    # 前区間が上げなら売り / 下げなら買い、という反転戦略の1回あたり損益
    rev = [(-b if a > 0 else b) for a, b in data]
    print(
        f"  {name}: n={len(data)} 相関 {r:+.3f} | "
        f"前が上→翌 {statistics.mean(up) * 100:+.3f}% (n={len(up)}) / "
        f"前が下→翌 {statistics.mean(down) * 100:+.3f}% (n={len(down)}) | "
        f"反転戦略 平均 {statistics.mean(rev) * 100:+.4f}% t={tstat(rev):+.2f}"
    )


SOURCES = (
    ("Bitstamp現物", "data/btc_bitstamp_h1.csv"),
    ("BingX無期限", "data/btc_h1.csv"),
)


def main() -> None:
    for label, path in (
        ("Bitstamp現物", "data/btc_bitstamp_h1.csv"),
        ("BingX無期限", "data/btc_h1.csv"),
    ):
        bars = load(path)
        d = daily(bars)
        print(f"\n=== {label}  {min(d):%Y-%m-%d}〜{max(d):%Y-%m-%d} ({len(d)}日) ===")
        keys = sorted(d)
        mid = keys[len(keys) // 2]
        for period, sub in (
            ("前半", {k: v for k, v in d.items() if k < mid}),
            ("後半", {k: v for k, v in d.items() if k >= mid}),
            ("全体", d),
        ):
            if len(sub) < 100:
                continue
            print(f" [{period} {min(sub):%Y-%m}〜{max(sub):%Y-%m}]")
            report("H1 週末(土日)→月", pairs(sub, [5, 6], 0))
            report("  プラセボ 火水→木", pairs(sub, [1, 2], 3))
            report("  プラセボ 水木→金", pairs(sub, [2, 3], 4))


if __name__ == "__main__":
    main()
