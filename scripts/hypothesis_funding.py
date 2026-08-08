"""H2: ファンディング時刻の持ち高調整。事前指定どおりに測る。"""

from __future__ import annotations

import csv
import statistics
from collections import defaultdict
from datetime import UTC, datetime


def load(path):
    out = []
    with open(path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            out.append(
                (
                    datetime.fromisoformat(row["timestamp"]).astimezone(UTC),
                    float(row["open"]),
                    float(row["close"]),
                )
            )
    return sorted(out)


def tstat(xs):
    if len(xs) < 3:
        return 0.0
    sd = statistics.stdev(xs)
    return statistics.mean(xs) / (sd / len(xs) ** 0.5) if sd else 0.0


def by_hour(bars):
    h = defaultdict(list)
    for ts, o, c in bars:
        if o > 0:
            h[ts.hour].append(c / o - 1)
    return h


FUND_PRE, FUND_POST = [23, 7, 15], [0, 8, 16]
PLAC_PRE, PLAC_POST = [3, 11, 19], [4, 12, 20]


def show(name, hours, h):
    xs = [r for hr in hours for r in h[hr]]
    print(
        f"    {name:22s} n={len(xs):6d} "
        f"平均 {statistics.mean(xs) * 10000:+7.2f}bp  t={tstat(xs):+6.2f}"
    )
    return xs


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
        print(f"\n=== {label}  {bars[0][0]:%Y-%m}〜{bars[-1][0]:%Y-%m} ===")
        mid = bars[len(bars) // 2][0]
        for period, sub in (
            ("前半", [b for b in bars if b[0] < mid]),
            ("後半", [b for b in bars if b[0] >= mid]),
            ("全体", bars),
        ):
            h = by_hour(sub)
            print(f"  [{period} {sub[0][0]:%Y-%m}〜{sub[-1][0]:%Y-%m}]")
            pre = show("H2 清算直前 23/07/15", FUND_PRE, h)
            post = show("H2 清算直後 00/08/16", FUND_POST, h)
            show("  プラセボ前 03/11/19", PLAC_PRE, h)
            show("  プラセボ後 04/12/20", PLAC_POST, h)
            diff = statistics.mean(post) - statistics.mean(pre)
            print(f"    → 直後-直前 の差 {diff * 10000:+.2f}bp")


if __name__ == "__main__":
    main()
