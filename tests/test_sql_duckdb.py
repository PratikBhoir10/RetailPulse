"""
Tests the dashboard's SQL + Python aggregation logic (db.py) WITHOUT Databricks.

The real OnlineRetail.csv is cleaned with the documented Silver rules into an in-memory DuckDB
database laid out like workspace.retailpulse.*, and each db.py query runs against it.
Results are compared with independent pandas calculations.

Limits: DuckDB is not Spark. Only the portable SQL subset used by db.py is exercised
(INSTR, SUBSTRING, CAST AS STRING, COUNT(DISTINCT), CASE, CTE/JOIN). Authentication,
warehouse behaviour and Unity Catalog permissions are NOT covered.

    $env:RETAILPULSE_CSV = "E:\\path\\to\\OnlineRetail.csv"
    python -m pytest tests/test_sql_duckdb.py -v
"""
import os
import re
import sys
from pathlib import Path

import pandas as pd
import pytest

duckdb = pytest.importorskip("duckdb")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import db  # noqa: E402

CSV = os.getenv("RETAILPULSE_CSV")
pytestmark = pytest.mark.skipif(not CSV or not Path(CSV).exists(),
                                reason="set RETAILPULSE_CSV to the OnlineRetail.csv path")


class DuckClient:
    """Same .query(sql, params) interface as DatabricksClient, backed by DuckDB."""

    def __init__(self, con):
        self.con = con
        self.calls = 0

    def query(self, statement, params=None):
        self.calls += 1
        sql = re.sub(r":([A-Za-z_]\w*)", r"$\1", statement)
        return self.con.execute(sql, params or {}).df()


@pytest.fixture(scope="module")
def silver_pdf():
    df = pd.read_csv(CSV, encoding="latin-1", dtype={"CustomerID": "string"}).drop_duplicates()
    df = df[~df.InvoiceNo.astype(str).str.startswith("C")]
    df = df[(df.Quantity > 0) & (df.UnitPrice > 0)].copy()
    df["Description"] = df.Description.str.strip()
    df["InvoiceDate"] = pd.to_datetime(df.InvoiceDate, format="%m/%d/%Y %H:%M")
    df["YearMonth"] = df.InvoiceDate.dt.strftime("%Y-%m")
    df["Revenue"] = df.Quantity * df.UnitPrice
    df["CustomerID"] = df.CustomerID.str.replace(r"\.0$", "", regex=True)
    return df.reset_index(drop=True)


@pytest.fixture(scope="module")
def client(silver_pdf):
    con = duckdb.connect()
    con.execute("ATTACH ':memory:' AS workspace")
    con.execute("CREATE SCHEMA workspace.retailpulse")
    con.register("s", silver_pdf)
    con.execute("CREATE TABLE workspace.retailpulse.silver_transactions AS SELECT * FROM s")
    con.execute("""CREATE TABLE workspace.retailpulse.gold_monthly_sales AS SELECT YearMonth, COUNT(*) transaction_lines,
        SUM(Revenue) total_revenue, COUNT(DISTINCT InvoiceNo) invoice_count, COUNT(DISTINCT CustomerID) unique_customers
        FROM s GROUP BY YearMonth""")
    con.execute("""CREATE TABLE workspace.retailpulse.gold_product_sales AS SELECT StockCode, Description,
        SUM(Quantity) units_sold, SUM(Revenue) total_revenue, COUNT(DISTINCT InvoiceNo) invoice_count
        FROM s GROUP BY StockCode, Description""")
    con.execute("""CREATE TABLE workspace.retailpulse.gold_country_sales AS SELECT Country, COUNT(*) transaction_lines,
        COUNT(DISTINCT InvoiceNo) invoice_count, SUM(Revenue) total_revenue, COUNT(DISTINCT CustomerID) unique_customers
        FROM s GROUP BY Country""")
    con.execute("CREATE TABLE workspace.retailpulse.bronze_transactions AS SELECT * FROM s LIMIT 10")
    return DuckClient(con)


def expected(pdf, months=None, countries=None, term=""):
    out = pdf
    if months:
        out = out[out.YearMonth.isin(months)]
    if countries:
        out = out[out.Country.isin(countries)]
    if term:
        t = term.lower()
        out = out[out.Description.str.lower().str.contains(t, regex=False, na=False)
                  | out.StockCode.str.lower().str.contains(t, regex=False, na=False)]
    return out


CASES = [
    (None, None, ""),
    (("2011-01", "2011-02", "2011-03"), None, ""),
    (None, ("France", "Germany"), ""),
    (None, None, "heart"),
    (("2011-10", "2011-11"), ("United Kingdom",), "bag"),
    (None, None, "o'brien; DROP TABLE x"),      # hostile input must be harmless (no matches)
    (None, None, "100%_"),                       # LIKE wildcards must be literal (no matches)
]


@pytest.mark.parametrize("months,countries,term", CASES)
def test_kpis_match_pandas(client, silver_pdf, months, countries, term):
    got = db.query_kpis(client, months, countries, term)
    exp = expected(silver_pdf, months, countries, term)
    assert got["revenue"] == pytest.approx(float(exp.Revenue.sum()), abs=0.01)
    assert got["invoices"] == exp.InvoiceNo.nunique()
    assert got["customers"] == exp.CustomerID.nunique()
    assert got["units"] == pytest.approx(float(exp.Quantity.sum()))
    assert got["transaction_lines"] == len(exp)


@pytest.mark.parametrize("months,countries,term", CASES)
def test_monthly_products_countries_match_pandas(client, silver_pdf, months, countries, term):
    exp = expected(silver_pdf, months, countries, term)
    m = db.query_monthly(client, months, countries, term).set_index("month_key")
    em = exp.groupby("YearMonth").agg(rev=("Revenue", "sum"), inv=("InvoiceNo", "nunique"), cust=("CustomerID", "nunique"))
    assert list(m.index) == list(em.index)
    if len(em):
        assert m.total_revenue.values == pytest.approx(em.rev.values, abs=0.01)
        assert (m.invoice_count.values == em.inv.values).all()
        assert (m.unique_customers.values == em.cust.values).all()

    p = db.query_products(client, months, countries, term)
    assert len(p) == exp.groupby(["StockCode", "Description"]).ngroups
    assert p.total_revenue.sum() == pytest.approx(float(exp.Revenue.sum()), abs=0.01)

    c = db.query_countries(client, months, countries, term).set_index("Country")
    ec = exp.groupby("Country").agg(rev=("Revenue", "sum"), inv=("InvoiceNo", "nunique"))
    assert sorted(c.index) == sorted(ec.index)
    if len(ec):
        assert c.loc[ec.index, "total_revenue"].values == pytest.approx(ec.rev.values, abs=0.01)


def test_headline_numbers_and_distinct_customer_rule(client):
    k = db.query_kpis(client, None, None, "")
    assert k["revenue"] == pytest.approx(10_642_110.80, abs=0.01)
    assert k["invoices"] == 19_960
    assert k["customers"] == 4_338
    assert k["revenue"] / k["invoices"] == pytest.approx(533.17, abs=0.01)
    monthly = db.query_monthly(client, None, None, "")
    assert monthly.unique_customers.sum() > k["customers"]   # summing months would be wrong
    assert len(monthly) == 13


def test_hostile_input_returns_nothing_and_table_survives(client):
    k = db.query_kpis(client, None, None, "o'brien; DROP TABLE x")
    assert k["transaction_lines"] == 0
    assert db.query_kpis(client, None, None, "")["transaction_lines"] == 524_878


def test_customer_queries(client, silver_pdf):
    exp = silver_pdf[silver_pdf.CustomerID.notna()]
    freq = db.query_customer_frequency(client, None, None, "")
    assert freq.customers.sum() == exp.CustomerID.nunique()
    per = exp.groupby("CustomerID").InvoiceNo.nunique()
    assert int((freq.invoices_per_customer * freq.customers).sum()) == int(per.sum())

    top = db.query_top_customers(client, None, None, "")
    assert top.total_revenue.iloc[0] == pytest.approx(exp.groupby("CustomerID").Revenue.sum().max(), abs=0.01)

    ident = db.query_identification(client, None, None, "")
    assert ident["anonymous_lines"] == silver_pdf.CustomerID.isna().sum()
    assert ident["anonymous_revenue"] + ident["identified_revenue"] == pytest.approx(float(silver_pdf.Revenue.sum()), abs=0.01)

    flow = db.query_customer_flow(client, None, None, "").set_index("month_key")
    first = exp.groupby("CustomerID").YearMonth.min()
    month = exp.assign(first=exp.CustomerID.map(first))
    new = month[month.YearMonth == month["first"]].groupby("YearMonth").CustomerID.nunique()
    assert (flow.new_customers.reindex(new.index).values == new.values).all()
    # every Dec-2010 customer is "new": dataset history starts there
    assert flow.loc["2010-12", "returning_customers"] == 0


def test_table_counts_and_profile(client):
    counts = db.load_table_counts(client).set_index("table_name")
    assert counts.loc["silver_transactions", "live_rows"] == 524_878
    assert counts.loc["silver_transactions", "matches"]
    assert counts.loc["gold_monthly_sales", "matches"] and counts.loc["gold_country_sales", "matches"]
    assert counts.loc["gold_product_sales", "live_rows"] == 4_158 and counts.loc["gold_product_sales", "matches"]
    profile = db.load_silver_profile(client)
    assert profile["non_positive_quantity"] == 0 and profile["non_positive_revenue"] == 0
    assert profile["invoices_spanning_months"] == 0


def test_gold_loaders_validate_schema(client):
    assert len(db.load_gold_monthly(client)) == 13
    assert len(db.load_gold_countries(client)) == 38
    assert len(db.load_gold_products(client)) == 4_158
    assert set(db.load_silver_columns(client)) >= db.SILVER_REQUIRED


def test_dashboard_routing_and_query_budget(client):
    ref = db.load_reference(client)
    before = client.calls
    d = db.load_dashboard(client, ref, ref.months, [], "")
    assert d.is_default and d.sources["products"] == "Gold" and d.sources["monthly"] == "Gold"
    d2 = db.load_dashboard(client, ref, ref.months[:3], [], "")
    assert not d2.is_default and d2.sources["monthly"] == "Gold" and d2.sources["products"] == "Silver"
    assert len(d2.monthly) == 3
    d3 = db.load_dashboard(client, ref, ref.months, ["France"], "")
    assert d3.sources["monthly"] == "Silver"
    # repeated identical call must be served from cache: zero extra queries
    mark = client.calls
    db.load_dashboard(client, ref, ref.months[:3], [], "")
    assert client.calls == mark
    assert client.calls - before <= 9
