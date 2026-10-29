"""
RetailPulse - analytics helpers (pure pandas/numpy, no Streamlit, no Databricks).

Everything here works on the already-aggregated frames returned by db.py, so it is
cheap and fully unit-testable (see tests/test_analytics.py).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# --------------------------------------------------------------------------
# Month helpers ("YYYY-MM" keys)
# --------------------------------------------------------------------------

def month_label(key: str) -> str:
    """'2011-03' -> 'Mar 2011' (falls back to the raw text)."""
    try:
        year, month = str(key)[:7].split("-")
        return f"{MONTH_NAMES[int(month) - 1]} {year}"
    except (ValueError, IndexError):
        return str(key)


def _ordinal(key: str) -> int:
    year, month = str(key)[:7].split("-")
    return int(year) * 12 + int(month) - 1


def are_consecutive(earlier: str, later: str) -> bool:
    try:
        return _ordinal(later) - _ordinal(earlier) == 1
    except (ValueError, IndexError):
        return False


def pct_change(current, previous) -> float | None:
    """Percentage change, or None when it is not meaningful (missing/zero base)."""
    try:
        current, previous = float(current), float(previous)
    except (TypeError, ValueError):
        return None
    if np.isnan(current) or np.isnan(previous) or previous == 0:
        return None
    return (current - previous) / abs(previous) * 100.0


# --------------------------------------------------------------------------
# Month-over-month
# --------------------------------------------------------------------------

def add_average_invoice_value(monthly: pd.DataFrame) -> pd.DataFrame:
    out = monthly.copy()
    out["avg_invoice_value"] = np.where(out["invoice_count"] > 0,
                                        out["total_revenue"] / out["invoice_count"].replace(0, np.nan), np.nan)
    return out


def mom_changes(monthly: pd.DataFrame, partial_month: str | None = None) -> dict:
    """
    Compare the latest COMPLETE month with the month before it.
    Returns {"ok": False, "reason": ...} when no meaningful comparison exists.
    """
    if monthly is None or monthly.empty:
        return {"ok": False, "reason": "No monthly data for the current filters."}

    frame = add_average_invoice_value(monthly.sort_values("month_key").reset_index(drop=True))
    note = ""
    if partial_month and frame["month_key"].iloc[-1] == partial_month:
        frame = frame.iloc[:-1]
        note = f"{month_label(partial_month)} is a partial month and was left out of the comparison."
    if len(frame) < 2:
        return {"ok": False, "reason": "Month-over-month needs at least two months in the selected range."
                + (f" {note}" if note else "")}

    cur, prev = frame.iloc[-1], frame.iloc[-2]
    if not are_consecutive(prev["month_key"], cur["month_key"]):
        return {"ok": False, "reason": f"{month_label(prev['month_key'])} and {month_label(cur['month_key'])} "
                                        "are not consecutive months, so no comparison is shown."}
    metrics = {
        "Revenue": ("total_revenue", "money"),
        "Invoices": ("invoice_count", "number"),
        "Average invoice value": ("avg_invoice_value", "money"),
        "Unique customers": ("unique_customers", "number"),
    }
    result = {
        label: {"current": float(cur[col]), "previous": float(prev[col]),
                "pct": pct_change(cur[col], prev[col]), "kind": kind}
        for label, (col, kind) in metrics.items()
    }
    return {"ok": True, "current": cur["month_key"], "previous": prev["month_key"],
            "metrics": result, "note": note}


def mom_series(monthly: pd.DataFrame, column: str, partial_month: str | None = None) -> pd.Series:
    """
    Month-over-month % change for every month, aligned with `monthly`'s index.
    NaN for the first month, for non-consecutive months, for a zero base, and for the partial month.
    """
    frame = add_average_invoice_value(monthly) if column == "avg_invoice_value" else monthly
    order = frame.sort_values("month_key")
    out = pd.Series(np.nan, index=order.index, dtype=float)
    keys, values = order["month_key"].tolist(), order[column].tolist()
    for i in range(1, len(keys)):
        if keys[i] == partial_month or not are_consecutive(keys[i - 1], keys[i]):
            continue
        change = pct_change(values[i], values[i - 1])
        if change is not None:
            out.iloc[i] = change
    return out.reindex(monthly.index)


# --------------------------------------------------------------------------
# Anomaly detection (robust, transparent, deliberately simple)
# --------------------------------------------------------------------------

def modified_zscores(values) -> np.ndarray:
    """Iglewicz-Hoaglin modified z-score: 0.6745 * (x - median) / MAD. Zeros if MAD == 0."""
    x = np.asarray(values, dtype=float)
    median = np.nanmedian(x)
    mad = np.nanmedian(np.abs(x - median))
    if mad == 0 or np.isnan(mad):
        return np.zeros_like(x)
    return 0.6745 * (x - median) / mad


def flag_anomalies(monthly: pd.DataFrame, column: str, partial_month: str | None = None,
                   threshold: float = 3.5, min_months: int = 6) -> dict:
    """
    Flag months whose value is unusual relative to the other months (|modified z| > threshold).
    This is a descriptive screen on a handful of monthly points - NOT a forecast and NOT
    a statistical test. Partial months are excluded because they are incomplete by construction.
    """
    frame = monthly.sort_values("month_key").copy()
    if partial_month:
        frame = frame[frame["month_key"] != partial_month]
    if len(frame) < min_months:
        return {"ok": False, "reason": f"At least {min_months} complete months are needed; "
                                       f"the current selection has {len(frame)}.", "frame": frame}
    frame["z_score"] = modified_zscores(frame[column].to_numpy())
    frame["is_anomaly"] = frame["z_score"].abs() > threshold
    return {"ok": True, "frame": frame, "threshold": threshold, "count": int(frame["is_anomaly"].sum())}


# --------------------------------------------------------------------------
# Products
# --------------------------------------------------------------------------

def pareto(products: pd.DataFrame, column: str = "total_revenue") -> pd.DataFrame:
    """Cumulative share of `column` against the share of products (largest first)."""
    positive = products[products[column] > 0].sort_values(column, ascending=False)
    total = positive[column].sum()
    if positive.empty or total == 0:
        return pd.DataFrame(columns=["rank", "product_share", "cumulative_share"])
    out = pd.DataFrame({"rank": np.arange(1, len(positive) + 1)})
    out["product_share"] = out["rank"] / len(positive) * 100
    out["cumulative_share"] = positive[column].cumsum().to_numpy() / total * 100
    return out


def share_held_by_top(products: pd.DataFrame, fraction: float = 0.10, column: str = "total_revenue") -> float | None:
    curve = pareto(products, column)
    if curve.empty:
        return None
    n = max(1, int(round(len(curve) * fraction)))
    return float(curve["cumulative_share"].iloc[n - 1])


# --------------------------------------------------------------------------
# Customers
# --------------------------------------------------------------------------

def repeat_stats(freq: pd.DataFrame) -> dict:
    """From the (invoices_per_customer, customers) distribution."""
    if freq is None or freq.empty or freq["customers"].sum() == 0:
        return {"customers": 0, "repeat_customers": 0, "repeat_rate": None, "avg_invoices": None}
    customers = float(freq["customers"].sum())
    repeat = float(freq.loc[freq["invoices_per_customer"] >= 2, "customers"].sum())
    invoices = float((freq["invoices_per_customer"] * freq["customers"]).sum())
    return {"customers": int(customers), "repeat_customers": int(repeat),
            "repeat_rate": repeat / customers * 100, "avg_invoices": invoices / customers}


def bucket_frequency(freq: pd.DataFrame, cap: int = 10) -> pd.DataFrame:
    """Collapse the long tail into a 'cap+' bucket for a readable chart."""
    if freq is None or freq.empty:
        return pd.DataFrame(columns=["bucket", "customers"])
    f = freq.copy()
    f["bucket_n"] = f["invoices_per_customer"].clip(upper=cap).astype(int)
    out = f.groupby("bucket_n", as_index=False)["customers"].sum()
    out["bucket"] = out["bucket_n"].astype(str)
    out.loc[out["bucket_n"] == cap, "bucket"] = f"{cap}+"
    return out[["bucket", "customers"]]


# --------------------------------------------------------------------------
# Cancellations
# --------------------------------------------------------------------------

def cancellation_view(monthly: pd.DataFrame, cancellations: pd.DataFrame | None, months) -> pd.DataFrame | None:
    """
    Merge cancellation activity with sales revenue for the chosen months.
    Cancellation value is dated by the CANCELLATION invoice month (not the original sale month).
    """
    if cancellations is None or cancellations.empty:
        return None
    c = cancellations.copy()
    c["month_key"] = c["YearMonth"].astype(str).str.slice(0, 7)
    for col in ["cancellation_invoices", "cancelled_lines", "cancelled_units", "cancelled_value"]:
        c[col] = pd.to_numeric(c[col], errors="coerce").fillna(0)
    c = c[c["month_key"].isin(list(months))]
    merged = c.merge(monthly[["month_key", "total_revenue"]], on="month_key", how="left")
    merged["cancelled_pct_of_sales"] = np.where(merged["total_revenue"] > 0,
                                                merged["cancelled_value"] / merged["total_revenue"] * 100, np.nan)
    return merged.sort_values("month_key").reset_index(drop=True)


# --------------------------------------------------------------------------
# Reconciliation (Gold vs Silver) for the monitoring page
# --------------------------------------------------------------------------

def reconcile(kpis: dict, gold_monthly: pd.DataFrame, gold_products: pd.DataFrame,
              gold_countries: pd.DataFrame, tolerance: float = 0.01) -> pd.DataFrame:
    """Compare Silver-derived totals with the sums held in each Gold table."""
    rows = [
        ("Revenue: gold_monthly_sales vs Silver", kpis["revenue"], gold_monthly["total_revenue"].sum(), tolerance),
        ("Revenue: gold_product_sales vs Silver", kpis["revenue"], gold_products["total_revenue"].sum(), tolerance),
        ("Revenue: gold_country_sales vs Silver", kpis["revenue"], gold_countries["total_revenue"].sum(), tolerance),
        ("Invoices: gold_monthly_sales vs Silver", kpis["invoices"], gold_monthly["invoice_count"].sum(), 0),
        ("Invoices: gold_country_sales vs Silver", kpis["invoices"], gold_countries["invoice_count"].sum(), 0),
        ("Units: gold_product_sales vs Silver", kpis["units"], gold_products["units_sold"].sum(), 0),
        ("Transaction lines: gold_monthly_sales vs Silver", kpis["transaction_lines"],
         gold_monthly["transaction_lines"].sum(), 0),
    ]
    out = pd.DataFrame(rows, columns=["check", "silver", "gold", "tolerance"])
    out["difference"] = out["gold"] - out["silver"]
    out["status"] = np.where(out["difference"].abs() <= out["tolerance"], "PASS", "FAIL")
    return out
