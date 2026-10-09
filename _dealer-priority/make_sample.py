"""Builds a SYNTHETIC dealers workbook for trying the tool. Contains no real data."""
import argparse
import random
from datetime import date, timedelta

import pandas as pd


def build(n=60, as_of=date(2026, 10, 9), seed=7):
    rnd = random.Random(seed)
    regions = ["North", "South", "East", "West"]
    dealers, purchases = [], []
    for i in range(1, n + 1):
        did = f"D{i:04d}"
        kind = rnd.choice(["steady", "steady", "growing", "declining", "lapsed", "new"])
        size = rnd.choice([8_000, 20_000, 45_000, 120_000])
        gap = rnd.choice([14, 30, 45, 60])
        visit = None if rnd.random() < 0.1 else as_of - timedelta(days=rnd.randint(5, 300))
        dealers.append({"dealer_id": did, "dealer_name": f"Sample Motors {i}",
                        "region": rnd.choice(regions), "last_visit": visit})
        span = {"new": 120, "lapsed": 700}.get(kind, 700)
        d = as_of - timedelta(days=span)
        while d < as_of:
            age = (as_of - d).days
            f = {"growing": 0.5 if age > 180 else 1.6, "declining": 1.5 if age > 120 else 0.3}.get(kind, 1)
            if kind == "lapsed" and age < 200:
                break
            purchases.append({"dealer_id": did, "purchase_date": d,
                              "amount": round(size * f * rnd.uniform(0.4, 1.6) / 3, 2)})
            d += timedelta(days=max(1, int(gap * rnd.uniform(0.6, 1.4))))
    p = pd.DataFrame(purchases)
    # a few deliberate data problems so the 'Data checks' sheet has something to show
    p.loc[len(p)] = ["D9999", as_of - timedelta(days=10), 5000]
    p.loc[len(p)] = ["D0001", as_of - timedelta(days=5), -250]
    p.loc[len(p)] = ["D0002", "not a date", 900]
    return pd.DataFrame(dealers), p


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dealers", type=int, default=60)
    ap.add_argument("--out", default="sample/sample_dealers.xlsx")
    a = ap.parse_args()
    d, p = build(a.dealers)
    with pd.ExcelWriter(a.out) as w:
        d.to_excel(w, sheet_name="Dealers", index=False)
        p.to_excel(w, sheet_name="Purchases", index=False)
    print(f"Wrote {a.out}: {len(d)} dealers, {len(p)} purchases (synthetic)")
