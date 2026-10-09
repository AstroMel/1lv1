"""Deterministic dealer visit prioritization. Plain math, no AI judgment, no randomness.

Same workbook + same as-of date + same owner-signed rules = identical ranking, always.
"""
import re

import numpy as np
import pandas as pd

VALUE_DAYS = 730      # "value" = spend over the last 24 months
RECENT_DAYS = 90      # "recent" = last 90 days
BASELINE_FROM, BASELINE_TO = 90, 455  # "usual" = the 365 days before the recent 90

DEALER_COLS = {
    "dealer_id": ["dealer_id", "id", "dealer_no", "dealer_number", "dealer"],
    "dealer_name": ["dealer_name", "name", "company", "business_name"],
    "region": ["region", "territory", "area", "state", "rep"],
    "last_visit": ["last_visit", "last_visit_date", "last_visited", "last_visit_on"],
}
PURCHASE_COLS = {
    "dealer_id": ["dealer_id", "id", "dealer_no", "dealer_number", "dealer", "buyer_id"],
    "date": ["purchase_date", "date", "sale_date", "sold_date", "auction_date"],
    "amount": ["amount", "total", "price", "hammer_price", "sale_price", "spend"],
}
OPTIONAL = {"dealer_name", "region", "last_visit"}


class InputError(Exception):
    pass


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "_", str(s).strip().lower()).strip("_")


def _shape(df, spec, label):
    cols = {_norm(c): c for c in df.columns}
    out, missing = {}, []
    for role, aliases in spec.items():
        hit = next((cols[a] for a in aliases if a in cols), None)
        if hit is None:
            if role not in OPTIONAL:
                missing.append(role)
            out[role] = pd.Series([None] * len(df), index=df.index)
        else:
            out[role] = df[hit]
    if missing:
        raise InputError(f"'{label}' sheet is missing column(s): {', '.join(missing)}. "
                         f"Found: {', '.join(map(str, df.columns))}")
    return pd.DataFrame(out)


def _clean_id(v):
    if pd.isna(v):
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _find_sheet(sheets, names, label):
    for k in sheets:
        if _norm(k) in names:
            return sheets[k]
    raise InputError(f"No '{label}' sheet. Sheets found: {', '.join(sheets)}")


def compute(sheets, as_of, cfg):
    """sheets: {name: DataFrame} from the workbook. Returns (ranked, issues)."""
    as_of = pd.Timestamp(as_of).normalize()
    issues = []
    d = _shape(_find_sheet(sheets, {"dealers", "dealer"}, "Dealers"), DEALER_COLS, "Dealers")
    p = _shape(_find_sheet(sheets, {"purchases", "purchase", "auction_purchases", "sales"},
                           "Purchases"), PURCHASE_COLS, "Purchases")

    d["dealer_id"] = d["dealer_id"].map(_clean_id)
    d["row"] = d.index + 2
    for _, r in d[(d.dealer_id == "")].iterrows():
        issues.append(("Dealers", r.row, "", "Blank dealer ID; row skipped"))
    d = d[d.dealer_id != ""]
    for _, r in d[d.duplicated("dealer_id", keep="first")].iterrows():
        issues.append(("Dealers", r.row, r.dealer_id, "Duplicate dealer ID; first row kept"))
    d = d.drop_duplicates("dealer_id", keep="first").copy()
    d["last_visit"] = pd.to_datetime(d["last_visit"], errors="coerce")
    for _, r in d[d.last_visit > as_of].iterrows():
        issues.append(("Dealers", r.row, r.dealer_id,
                       "Last visit is after the as-of date; treated as no visit on record"))
    d.loc[d.last_visit > as_of, "last_visit"] = pd.NaT

    p["row"] = p.index + 2
    p["dealer_id"] = p["dealer_id"].map(_clean_id)
    p["date"] = pd.to_datetime(p["date"], errors="coerce").dt.normalize()
    p["amount"] = pd.to_numeric(
        p["amount"].astype(str).str.replace(r"[$,\s]", "", regex=True), errors="coerce")
    known = set(d.dealer_id)
    keep = pd.Series(True, index=p.index)
    for i, r in p.iterrows():
        why = None
        if r.dealer_id not in known:
            why = "Dealer ID not found on Dealers sheet"
        elif pd.isna(r.date):
            why = "Missing or unreadable date"
        elif r.date > as_of:
            why = "Purchase dated after the as-of date"
        elif pd.isna(r.amount) or r.amount <= 0:
            why = "Missing, zero, or negative amount"
        if why:
            keep[i] = False
            issues.append(("Purchases", r.row, r.dealer_id, why + "; excluded from scoring"))
    p = p[keep]
    by_dealer = {k: v for k, v in p.groupby("dealer_id")}

    rows = []
    for _, dl in d.iterrows():
        pp = by_dealer.get(dl.dealer_id)
        rec = {"dealer_id": dl.dealer_id, "dealer_name": dl.dealer_name, "region": dl.region,
               "last_visit": dl.last_visit, "spend_24m": 0.0, "recent_90d": 0.0,
               "usual_90d": 0.0, "last_purchase": pd.NaT, "days_since_purchase": np.nan,
               "buy_rhythm_days": np.nan, "has_history": pp is not None}
        if pp is not None:
            age = (as_of - pp.date).dt.days
            rec["spend_24m"] = float(pp.amount[age < VALUE_DAYS].sum())
            rec["recent_90d"] = float(pp.amount[age < RECENT_DAYS].sum())
            base = pp.amount[(age >= BASELINE_FROM) & (age < BASELINE_TO)].sum()
            rec["usual_90d"] = float(base) * RECENT_DAYS / (BASELINE_TO - BASELINE_FROM)
            rec["last_purchase"] = pp.date.max()
            rec["days_since_purchase"] = int((as_of - pp.date.max()).days)
            days = sorted(pp.date[age < VALUE_DAYS].unique())
            gaps = np.diff(days).astype("timedelta64[D]").astype(int) if len(days) >= 3 else []
            rec["buy_rhythm_days"] = float(np.median(gaps)) if len(gaps) else float(
                cfg["default_buy_interval_days"])
        rows.append(rec)
    df = pd.DataFrame(rows)
    if df.empty:
        raise InputError("No dealers found on the Dealers sheet")

    n = len(df)
    df["value"] = 100.0 if n == 1 and df.spend_24m.iloc[0] > 0 else (
        (df.spend_24m.rank(method="min") - 1) / max(n - 1, 1) * 100)
    df.loc[df.spend_24m <= 0, "value"] = 0.0

    ratio = (1 - df.recent_90d / df.usual_90d.where(df.usual_90d > 0)).clip(0, 1)
    df["decline"] = ratio.fillna(0) * 100

    lapse_ratio = df.days_since_purchase / df.buy_rhythm_days
    df["lapse"] = ((lapse_ratio - 1) / 2).clip(0, 1).fillna(0) * 100

    days_visit = (as_of - df.last_visit).dt.days
    df["days_since_visit"] = days_visit
    df["visit_gap"] = (days_visit / (2 * cfg["visit_interval_days"])).clip(0, 1).fillna(1) * 100

    w = cfg["weights"]
    df["priority"] = (w["value"] * df.value + w["decline"] * df.decline
                      + w["lapse"] * df.lapse + w["visit_gap"] * df.visit_gap).round(1)
    df = df.sort_values(["priority", "spend_24m", "dealer_id"],
                        ascending=[False, False, True], kind="mergesort").reset_index(drop=True)
    df["rank"] = df.index + 1
    n_top = cfg["top_n"]
    df["visit_list"] = np.where(df["rank"] <= n_top, "Visit now",
                                np.where(df["rank"] <= 2 * n_top, "Next", "Maintain"))
    df["why"] = df.apply(_why, axis=1)
    return df, issues


def _why(r):
    out = []
    if not r.has_history:
        out.append("No purchases on record")
    if r.spend_24m > 0 and r.value >= 75:
        out.append(f"Top {max(1, round(100 - r.value))}% buyer (${r.spend_24m:,.0f} over 24 months)")
    if r.decline >= 25:
        out.append(f"Buying {r.decline:.0f}% below their usual pace (last 90 days)")
    if r.lapse >= 25:
        out.append(f"{int(r.days_since_purchase)} days since last purchase "
                   f"(usually buys every {r.buy_rhythm_days:.0f})")
    if r.visit_gap >= 50:
        out.append("No visit on record" if pd.isna(r.last_visit)
                   else f"Last visit {int(r.days_since_visit)} days ago")
    return "; ".join(out) or "On track"
