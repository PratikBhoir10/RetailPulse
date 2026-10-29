"""
Local PySpark test of the Bronze -> Silver -> Gold rules (no Databricks needed).

Run (PowerShell, from the project folder; needs Java 17+ and `pip install pyspark pytest`):

    $env:RETAILPULSE_CSV = "E:\\path\\to\\OnlineRetail.csv"
    python -m pytest tests/test_pipeline_local.py -v

The expected numbers below are the counts you recorded from your Databricks run
(524,878 Silver rows; 13 / 4,158 / 38 Gold rows). The test proves that the documented
rules reproduce them from the raw CSV. Expected revenue totals are computed
independently with pandas.
"""
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

pyspark = pytest.importorskip("pyspark")
from pyspark.sql import SparkSession, functions as F  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
import retailpulse_pipeline as rp  # noqa: E402

CSV = os.getenv("RETAILPULSE_CSV")
pytestmark = pytest.mark.skipif(not CSV or not Path(CSV).exists(),
                                reason="set RETAILPULSE_CSV to the OnlineRetail.csv path")


@pytest.fixture(scope="module")
def spark():
    session = (SparkSession.builder.master("local[2]").appName("rp-test")
               .config("spark.sql.shuffle.partitions", "4")
               .config("spark.ui.enabled", "false").getOrCreate())
    yield session
    session.stop()


@pytest.fixture(scope="module")
def built(spark):
    raw = (spark.read.option("header", True).option("encoding", "ISO-8859-1").csv(CSV))
    classified = rp.classify(rp.standardise(raw))
    silver = rp.build_silver(classified)
    return {"raw": raw, "classified": classified, "silver": silver}


@pytest.fixture(scope="module")
def pandas_silver():
    df = pd.read_csv(CSV, encoding="latin-1", dtype={"CustomerID": "string"}).drop_duplicates()
    df = df[~df.InvoiceNo.astype(str).str.startswith("C")]
    df = df[(df.Quantity > 0) & (df.UnitPrice > 0)].copy()
    df["Description"] = df.Description.str.strip()
    df["Revenue"] = df.Quantity * df.UnitPrice
    return df


def test_row_counts_match_recorded_pipeline(built):
    assert built["raw"].count() == rp.RECORDED_COUNTS["bronze_transactions"]
    assert built["classified"].count() == rp.RECORDED_COUNTS["bronze_transactions"]  # nothing silently dropped
    assert built["silver"].count() == rp.RECORDED_COUNTS["silver_transactions"]


def test_rejection_breakdown(built):
    rows = {r["reject_reason"]: r["n"] for r in
            built["classified"].filter(F.col("reject_reason").isNotNull())
            .groupBy("reject_reason").agg(F.count("*").alias("n")).collect()}
    assert rows[rp.R_DUPLICATE] == 5_268
    assert rows[rp.R_CANCEL] == 9_251
    assert rows.get(rp.R_QTY, 0) + rows.get(rp.R_PRICE, 0) == 2_512
    assert sum(rows.values()) == 541_909 - 524_878


def test_silver_revenue_matches_pandas(built, pandas_silver):
    total = built["silver"].agg(F.sum("Revenue")).first()[0]
    assert float(total) == pytest.approx(pandas_silver.Revenue.sum(), abs=0.01)
    assert built["silver"].select("InvoiceNo").distinct().count() == pandas_silver.InvoiceNo.nunique()
    assert built["silver"].select("CustomerID").distinct().count() - 1 == pandas_silver.CustomerID.nunique()  # minus NULL


def test_gold_row_counts_match_recorded(built):
    silver = built["silver"]
    assert rp.gold_monthly(silver).count() == rp.RECORDED_COUNTS["gold_monthly_sales"]
    assert rp.gold_product(silver).count() == rp.RECORDED_COUNTS["gold_product_sales"]
    assert rp.gold_country(silver).count() == rp.RECORDED_COUNTS["gold_country_sales"]


def test_gold_monthly_matches_pandas(built, pandas_silver):
    got = rp.gold_monthly(built["silver"]).toPandas().set_index("YearMonth")
    pdf = pandas_silver.copy()
    pdf["YearMonth"] = pd.to_datetime(pdf.InvoiceDate, format="%m/%d/%Y %H:%M").dt.strftime("%Y-%m")
    exp = pdf.groupby("YearMonth").agg(rev=("Revenue", "sum"), inv=("InvoiceNo", "nunique"),
                                       cust=("CustomerID", "nunique"), n=("InvoiceNo", "size"))
    assert list(got.index) == list(exp.index)
    assert (got.invoice_count.values == exp.inv.values).all()
    assert (got.unique_customers.values == exp.cust.values).all()
    assert (got.transaction_lines.values == exp.n.values).all()
    assert got.total_revenue.astype(float).values == pytest.approx(exp.rev.values, abs=0.01)


def test_customers_never_summed_across_months(built):
    monthly = rp.gold_monthly(built["silver"]).toPandas()
    distinct = built["silver"].select("CustomerID").distinct().filter(F.col("CustomerID").isNotNull()).count()
    assert monthly.unique_customers.sum() > distinct  # proves summing months would overstate customers
    assert rp.gold_customer(built["silver"]).count() == distinct == 4_338


def test_cancellations_table(built):
    canc = rp.gold_cancellations(built["classified"]).toPandas()
    assert canc.cancellation_invoices.sum() > 0
    assert (canc.cancelled_value.astype(float) > 0).all()


def test_quality_checks_all_pass(spark, built):
    silver, classified = built["silver"], built["classified"]
    checks = rp.quality_checks(
        spark, 541_909, classified, silver,
        rp.gold_monthly(silver), rp.gold_product(silver), rp.gold_country(silver), rp.gold_customer(silver))
    failed = [c for c in checks if c[4] == "FAIL"]
    assert not failed, failed
    assert len(checks) >= 25
