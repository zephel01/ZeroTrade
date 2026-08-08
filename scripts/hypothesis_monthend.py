"""H3: 月末リバランス。事前登録した以上、優先度が低くても測る。"""

from __future__ import annotations

import csv
import statistics
from collections import defaultdict
from datetime import UTC, datetime


def load(path):
    days = defaultdict(list)
    with open(path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            ts = datetime.fromisoformat(row["timestamp"]).astimezone(UTC)
            days[ts.date()].append((float(row["open"]), float(row["close"])))
    return {d: (v[0][0], v[-1][1]) for d, v in sorted(days.items())}


def tstat(xs):
    if len(xs) < 3:
        return 0.0
    sd = statistics.stdev(xs)
    return statistics.mean(xs) / (sd / len(xs) ** 0.5) if sd else 0.0


SOURCES = (
    ("Bitstamp現物", "data/btc_bitstamp_h1.csv"),
    ("BingX無期限", "data/btc_h1.csv"),
)


def main() -> None:
    for label, path in (
        ("Bitstamp現物", "data/btc_bitstamp_h1.csv"),
        ("BingX無期限", "data/btc_h1.csv"),
    ):
        d = load(path)
        keys = sorted(d)
        mid = keys[len(keys) // 2]
        print(f"\n=== {label} ===")
        for period, sub in (
            ("前半", [k for k in keys if k < mid]),
            ("後半", [k for k in keys if k >= mid]),
        ):
            # 月ごとに最後の3営業日 / それ以外
            bymonth = defaultdict(list)
            for k in sub:
                bymonth[(k.year, k.month)].append(k)
            last3, other = [], []
            for _, ks in bymonth.items():
                ks = sorted(ks)
                for k in ks[-3:]:
                    last3.append(d[k][1] / d[k][0] - 1)
                for k in ks[:-3]:
                    other.append(d[k][1] / d[k][0] - 1)
            if len(last3) < 20:
                continue
            print(
                f"  [{period}] 月末3日 n={len(last3):4d} "
                f"平均 {statistics.mean(last3) * 10000:+7.2f}bp "
                f"t={tstat(last3):+5.2f} | その他 n={len(other):4d} "
                f"平均 {statistics.mean(other) * 10000:+7.2f}bp t={tstat(other):+5.2f}"
            )


if __name__ == "__main__":
    main()
