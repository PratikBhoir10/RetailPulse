# Databricks notebook source
# MAGIC %md
# MAGIC # RetailPulse - Bronze / Silver / Gold pipeline (v2: audited, repeatable)
# MAGIC
# MAGIC **Safe by default.** Running this notebook with the default parameters does **not** modify your existing
# MAGIC `bronze_transactions`, `silver_transactions` or `gold_*` tables. It will:
# MAGIC
# MAGIC 1. Rebuild Silver/Gold *in memory* from `bronze_transactions` using the documented business rules,
# MAGIC 2. **compare** the result with your existing tables,
# MAGIC 3. write only NEW audit/analytics tables:
# MAGIC    `silver_rejected_records`, `gold_customer_summary`, `gold_cancellations_monthly`,
# MAGIC    `data_quality_metrics`, `pipeline_run_log`.
# MAGIC
# MAGIC Set `rebuild_core = yes` only if you deliberately want to overwrite the existing Silver/Gold tables.
# MAGIC
# MAGIC The same functions are imported by `tests/test_pipeline_local.py`, which runs them with local PySpark
# MAGIC against the real CSV (no Databricks needed).

# COMMAND ----------

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from pyspark.sql import DataFrame, SparkSession, Window
from pyspark.sql import functions as F
from pyspark.sql import types as T

CATALOG = "workspace"
SCHEMA = "retailpulse"

RAW_COLUMNS = [
    "InvoiceNo", "StockCode", "Description", "Quantity",
    "InvoiceDate", "UnitPrice", "CustomerID", "Country",
]

# Rejection reasons (one per rejected row, first matching rule wins)
R_DUPLICATE = "DUPLICATE_ROW"
R_MISSING = "MISSING_REQUIRED_FIELD"
R_CANCEL = "CANCELLATION_INVOICE"
R_QTY = "NON_POSITIVE_QUANTITY"
R_PRICE = "NON_POSITIVE_UNIT_PRICE"

# Row counts you recorded from the original pipeline (used as a baseline check only)
RECORDED_COUNTS = {
    "bronze_transactions": 541_909,
    "silver_transactions": 524_878,
    "gold_monthly_sales": 13,
    "gold_product_sales": 4_158,
    "gold_country_sales": 38,
}

# COMMAND ----------
# MAGIC %md
# MAGIC ## Stage 1 - Standardise (types, whitespace, identifiers)

# COMMAND ----------

def _blank_to_null(col):
    trimmed = F.trim(col.cast("string"))
    return F.when(trimmed == "", F.lit(None)).otherwise(trimmed)


def standardise(df: DataFrame) -> DataFrame:
    """Typed, trimmed version of the raw columns. Never drops rows.

    Uses try_cast / try_to_timestamp so that malformed values become NULL
    (and are rejected later with a reason) instead of crashing under ANSI mode.
    """
    missing = [c for c in RAW_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"Source is missing required column(s): {missing}. Found: {df.columns}")

    invoice_dtype = dict(df.dtypes)["InvoiceDate"]
    if invoice_dtype.startswith(("timestamp", "date")):
        invoice_ts = F.col("InvoiceDate").cast("timestamp")
    else:
        text = "trim(cast(InvoiceDate as string))"
        invoice_ts = F.coalesce(
            F.expr(f"try_to_timestamp({text}, 'M/d/yyyy H:mm')"),
            F.expr(f"try_to_timestamp({text})"),
        )

    customer = F.regexp_replace(F.trim(F.col("CustomerID").cast("string")), r"\.0+$", "")
    customer = F.when(customer.isin("", "nan", "NaN", "null"), F.lit(None)).otherwise(customer)

    return df.select(
        _blank_to_null(F.col("InvoiceNo")).alias("InvoiceNo"),
        _blank_to_null(F.col("StockCode")).alias("StockCode"),
        _blank_to_null(F.col("Description")).alias("Description"),
        F.expr("try_cast(`Quantity` AS INT)").alias("Quantity"),
        invoice_ts.alias("InvoiceDate"),
        F.expr("try_cast(`UnitPrice` AS DECIMAL(18,4))").alias("UnitPrice"),
        customer.alias("CustomerID"),
        _blank_to_null(F.col("Country")).alias("Country"),
    )


# COMMAND ----------
# MAGIC %md
# MAGIC ## Stage 2 - Classify every row (accepted or rejected-with-reason)

# COMMAND ----------

def classify(std: DataFrame) -> DataFrame:
    """Adds `reject_reason` (NULL = accepted). Row count is unchanged."""
    window = Window.partitionBy(*RAW_COLUMNS).orderBy(F.monotonically_increasing_id())
    ranked = std.withColumn("_rn", F.row_number().over(window))

    required_missing = (
        F.col("InvoiceNo").isNull() | F.col("StockCode").isNull()
        | F.col("Country").isNull() | F.col("InvoiceDate").isNull()
    )
    reason = (
        F.when(F.col("_rn") > 1, F.lit(R_DUPLICATE))
        .when(required_missing, F.lit(R_MISSING))
        .when(F.upper(F.col("InvoiceNo")).startswith("C"), F.lit(R_CANCEL))
        .when(F.col("Quantity").isNull() | (F.col("Quantity") <= 0), F.lit(R_QTY))
        .when(F.col("UnitPrice").isNull() | (F.col("UnitPrice") <= 0), F.lit(R_PRICE))
    )
    return ranked.withColumn("reject_reason", reason).drop("_rn")


def build_silver(classified: DataFrame) -> DataFrame:
    return (
        classified.filter(F.col("reject_reason").isNull())
        .drop("reject_reason")
        .withColumn("Revenue", F.col("Quantity") * F.col("UnitPrice"))
        .withColumn("YearMonth", F.date_format("InvoiceDate", "yyyy-MM"))
    )


def build_rejected(classified: DataFrame, run_id: str) -> DataFrame:
    return (
        classified.filter(F.col("reject_reason").isNotNull())
        .withColumn("pipeline_run_id", F.lit(run_id))
        .withColumn("processed_at", F.current_timestamp())
    )


# COMMAND ----------
# MAGIC %md
# MAGIC ## Stage 3 - Gold aggregations

# COMMAND ----------

def gold_monthly(silver: DataFrame) -> DataFrame:
    return silver.groupBy("YearMonth").agg(
        F.count("*").alias("transaction_lines"),
        F.sum("Revenue").alias("total_revenue"),
        F.countDistinct("InvoiceNo").alias("invoice_count"),
        F.countDistinct("CustomerID").alias("unique_customers"),
    ).orderBy("YearMonth")


def gold_product(silver: DataFrame) -> DataFrame:
    return silver.groupBy("StockCode", "Description").agg(
        F.sum("Quantity").alias("units_sold"),
        F.sum("Revenue").alias("total_revenue"),
        F.countDistinct("InvoiceNo").alias("invoice_count"),
    )


def gold_country(silver: DataFrame) -> DataFrame:
    return silver.groupBy("Country").agg(
        F.count("*").alias("transaction_lines"),
        F.countDistinct("InvoiceNo").alias("invoice_count"),
        F.sum("Revenue").alias("total_revenue"),
        F.countDistinct("CustomerID").alias("unique_customers"),
    )


def gold_customer(silver: DataFrame) -> DataFrame:
    """One row per identified customer (guest lines with no CustomerID are excluded)."""
    return (
        silver.filter(F.col("CustomerID").isNotNull())
        .groupBy("CustomerID")
        .agg(
            F.countDistinct("InvoiceNo").alias("invoice_count"),
            F.sum("Revenue").alias("total_revenue"),
            F.sum("Quantity").alias("units_bought"),
            F.min("YearMonth").alias("first_purchase_month"),
            F.max("YearMonth").alias("last_purchase_month"),
            F.countDistinct("YearMonth").alias("active_months"),
            F.min("Country").alias("country"),
            F.countDistinct("Country").alias("country_count"),
        )
        .withColumn("is_repeat_customer", F.col("invoice_count") >= 2)
        .withColumn("avg_invoice_value", F.col("total_revenue") / F.col("invoice_count"))
    )


def gold_cancellations(classified: DataFrame) -> DataFrame:
    """Cancellation (credit-note) activity by month of the cancellation invoice."""
    return (
        classified.filter(F.col("reject_reason") == R_CANCEL)
        .withColumn("YearMonth", F.date_format("InvoiceDate", "yyyy-MM"))
        .groupBy("YearMonth")
        .agg(
            F.countDistinct("InvoiceNo").alias("cancellation_invoices"),
            F.count("*").alias("cancelled_lines"),
            F.sum(F.abs(F.col("Quantity"))).alias("cancelled_units"),
            F.sum(F.abs(F.col("Quantity") * F.col("UnitPrice"))).alias("cancelled_value"),
        )
        .orderBy("YearMonth")
    )


# COMMAND ----------
# MAGIC %md
# MAGIC ## Stage 4 - Data-quality checks (independent SQL cross-checks)

# COMMAND ----------

QUALITY_SCHEMA = T.StructType([
    T.StructField("check_name", T.StringType()),
    T.StructField("category", T.StringType()),
    T.StructField("expected", T.StringType()),
    T.StructField("actual", T.StringType()),
    T.StructField("status", T.StringType()),   # PASS / FAIL / INFO
    T.StructField("detail", T.StringType()),
])


def _check(name, category, expected, actual, detail="", tolerance=0.0, info=False):
    try:
        ok = abs(float(expected) - float(actual)) <= tolerance
    except (TypeError, ValueError):
        ok = str(expected) == str(actual)
    status = "INFO" if info else ("PASS" if ok else "FAIL")
    return (name, category, str(expected), str(actual), status, detail)


def _scalar(spark: SparkSession, sql: str):
    return spark.sql(sql).first()[0]


def quality_checks(spark, bronze_rows, classified, silver, g_month, g_prod, g_country, g_cust, existing=None):
    """Return a list of check tuples. All heavy work happens inside Spark."""
    silver.createOrReplaceTempView("rp_silver")
    classified.createOrReplaceTempView("rp_classified")
    g_month.createOrReplaceTempView("rp_gm")
    g_prod.createOrReplaceTempView("rp_gp")
    g_country.createOrReplaceTempView("rp_gc")
    g_cust.createOrReplaceTempView("rp_gcust")

    s = spark.sql(
        "SELECT COUNT(*) n, SUM(Revenue) rev, COUNT(DISTINCT InvoiceNo) inv, "
        "COUNT(DISTINCT CustomerID) cust, SUM(Quantity) units FROM rp_silver"
    ).first()
    rej = _scalar(spark, "SELECT COUNT(*) FROM rp_classified WHERE reject_reason IS NOT NULL")
    checks = []

    # Reconciliation / rule enforcement
    checks.append(_check("bronze = silver + rejected", "reconciliation", bronze_rows, s["n"] + rej,
                         "Every raw row is either accepted or quarantined with a reason."))
    checks.append(_check("silver: no non-positive quantity", "rules", 0,
                         _scalar(spark, "SELECT COUNT(*) FROM rp_silver WHERE Quantity <= 0")))
    checks.append(_check("silver: no non-positive unit price", "rules", 0,
                         _scalar(spark, "SELECT COUNT(*) FROM rp_silver WHERE UnitPrice <= 0")))
    checks.append(_check("silver: no cancellation invoices", "rules", 0,
                         _scalar(spark, "SELECT COUNT(*) FROM rp_silver WHERE upper(InvoiceNo) LIKE 'C%'")))
    checks.append(_check("silver: no exact duplicate rows", "rules", s["n"],
                         _scalar(spark, "SELECT COUNT(*) FROM (SELECT DISTINCT InvoiceNo, StockCode, Description, "
                                        "Quantity, InvoiceDate, UnitPrice, CustomerID, Country FROM rp_silver)")))
    checks.append(_check("silver: no missing required fields", "rules", 0, _scalar(
        spark, "SELECT COUNT(*) FROM rp_silver WHERE InvoiceNo IS NULL OR StockCode IS NULL "
               "OR Country IS NULL OR InvoiceDate IS NULL")))

    # Definition assumptions that make additive Gold metrics valid
    checks.append(_check("invoices never span two months", "assumption", 0, _scalar(
        spark, "SELECT COUNT(*) FROM (SELECT InvoiceNo FROM rp_silver GROUP BY InvoiceNo "
               "HAVING COUNT(DISTINCT YearMonth) > 1)"),
        "Needed to add monthly invoice counts safely."))
    checks.append(_check("invoices never span two countries", "assumption", 0, _scalar(
        spark, "SELECT COUNT(*) FROM (SELECT InvoiceNo FROM rp_silver GROUP BY InvoiceNo "
               "HAVING COUNT(DISTINCT Country) > 1)"),
        "Needed to add country invoice counts safely."))

    # Gold vs independent Silver SQL
    tol = 0.01
    checks.append(_check("gold_monthly revenue = silver revenue", "gold", s["rev"],
                         _scalar(spark, "SELECT SUM(total_revenue) FROM rp_gm"), tolerance=tol))
    checks.append(_check("gold_monthly invoices = silver distinct invoices", "gold", s["inv"],
                         _scalar(spark, "SELECT SUM(invoice_count) FROM rp_gm")))
    checks.append(_check("gold_monthly lines = silver rows", "gold", s["n"],
                         _scalar(spark, "SELECT SUM(transaction_lines) FROM rp_gm")))
    checks.append(_check("gold_product revenue = silver revenue", "gold", s["rev"],
                         _scalar(spark, "SELECT SUM(total_revenue) FROM rp_gp"), tolerance=tol))
    checks.append(_check("gold_product units = silver units", "gold", s["units"],
                         _scalar(spark, "SELECT SUM(units_sold) FROM rp_gp")))
    checks.append(_check("gold_country revenue = silver revenue", "gold", s["rev"],
                         _scalar(spark, "SELECT SUM(total_revenue) FROM rp_gc"), tolerance=tol))
    checks.append(_check("gold_country invoices = silver distinct invoices", "gold", s["inv"],
                         _scalar(spark, "SELECT SUM(invoice_count) FROM rp_gc")))
    checks.append(_check("gold_customer customers = silver distinct customers", "gold", s["cust"],
                         _scalar(spark, "SELECT COUNT(*) FROM rp_gcust")))

    # Informational profile
    anon = _scalar(spark, "SELECT COUNT(*) FROM rp_silver WHERE CustomerID IS NULL")
    checks.append(_check("silver lines without CustomerID", "profile", "-", anon,
                         f"{anon / s['n']:.1%} of lines are guest/unidentified; excluded from customer analytics.",
                         info=True))
    for reason, n in spark.sql(
        "SELECT reject_reason, COUNT(*) n FROM rp_classified WHERE reject_reason IS NOT NULL "
        "GROUP BY reject_reason ORDER BY n DESC").collect():
        checks.append(_check(f"rejected: {reason}", "profile", "-", n, info=True))

    # Baselines you recorded earlier, and comparison with existing tables
    checks.append(_check("recorded baseline: bronze rows", "baseline", RECORDED_COUNTS["bronze_transactions"], bronze_rows))
    checks.append(_check("recorded baseline: silver rows", "baseline", RECORDED_COUNTS["silver_transactions"], s["n"]))
    checks.append(_check("recorded baseline: gold monthly rows", "baseline", RECORDED_COUNTS["gold_monthly_sales"], g_month.count()))
    checks.append(_check("recorded baseline: gold product rows", "baseline", RECORDED_COUNTS["gold_product_sales"], g_prod.count()))
    checks.append(_check("recorded baseline: gold country rows", "baseline", RECORDED_COUNTS["gold_country_sales"], g_country.count()))
    for table, (rows, revenue) in (existing or {}).items():
        checks.append(_check(f"existing {table}: rows match rebuild", "comparison",
                             rows[0], rows[1], "existing table vs rule-based rebuild"))
        if revenue is not None:
            checks.append(_check(f"existing {table}: revenue matches rebuild", "comparison",
                                 revenue[0], revenue[1], tolerance=tol))
    return checks


# COMMAND ----------
# MAGIC %md
# MAGIC ## Stage 5 - Orchestration, persistence and run log

# COMMAND ----------

def _fq(name: str) -> str:
    return f"{CATALOG}.{SCHEMA}.{name}"


def _overwrite(df: DataFrame, name: str) -> None:
    df.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(_fq(name))


def _append(df: DataFrame, name: str) -> None:
    df.write.format("delta").mode("append").option("mergeSchema", "true").saveAsTable(_fq(name))


def _existing_summary(spark, table: str, revenue_col: str | None):
    if not spark.catalog.tableExists(_fq(table)):
        return None
    sel = f"COUNT(*) n{', SUM(' + revenue_col + ') rev' if revenue_col else ''}"
    row = spark.sql(f"SELECT {sel} FROM {_fq(table)}").first()
    return row["n"], (row["rev"] if revenue_col else None)


def run_pipeline(spark: SparkSession, rebuild_core: bool = False,
                 ingest_source_path: str | None = None) -> dict:
    run_id = uuid.uuid4().hex[:12]
    log: list[tuple] = []

    def stage(name, started, status, rows_in=None, rows_out=None, message=""):
        log.append((run_id, name, started, datetime.now(timezone.utc), status, rows_in, rows_out, message))

    # --- Bronze (optional re-ingest, default: read existing) ---------------
    t0 = datetime.now(timezone.utc)
    if ingest_source_path:
        raw = (spark.read.option("header", True).option("encoding", "ISO-8859-1")
               .csv(ingest_source_path)
               .withColumn("_ingested_at", F.current_timestamp())
               .withColumn("_source_file", F.lit(ingest_source_path))
               .withColumn("_pipeline_run_id", F.lit(run_id)))
        _overwrite(raw, "bronze_transactions")
        stage("bronze_ingest", t0, "SUCCESS", None, raw.count(), f"Re-ingested from {ingest_source_path}")
    bronze = spark.table(_fq("bronze_transactions"))
    bronze_rows = bronze.count()
    stage("bronze_read", t0, "SUCCESS", None, bronze_rows, "Bronze preserved as raw source records")

    # --- Silver (in memory) ------------------------------------------------
    t1 = datetime.now(timezone.utc)
    classified = classify(standardise(bronze))
    silver = build_silver(classified)
    rejected = build_rejected(classified, run_id)

    g_month, g_prod, g_country = gold_monthly(silver), gold_product(silver), gold_country(silver)
    g_cust, g_cancel = gold_customer(silver), gold_cancellations(classified)

    existing = {}
    for table, rev_col in (("silver_transactions", "Revenue"), ("gold_monthly_sales", "total_revenue"),
                           ("gold_product_sales", "total_revenue"), ("gold_country_sales", "total_revenue")):
        summary = _existing_summary(spark, table, rev_col)
        if summary is not None:
            rebuilt = {"silver_transactions": silver, "gold_monthly_sales": g_month,
                       "gold_product_sales": g_prod, "gold_country_sales": g_country}[table]
            rebuilt_rev = rebuilt.agg(F.sum("Revenue" if table == "silver_transactions" else "total_revenue")).first()[0]
            existing[table] = ((summary[0], rebuilt.count()), (summary[1], rebuilt_rev))

    # --- Quality gate -------------------------------------------------------
    checks = quality_checks(spark, bronze_rows, classified, silver, g_month, g_prod, g_country, g_cust, existing)
    failed = [c for c in checks if c[4] == "FAIL"]
    stage("quality_checks", t1, "FAILED_CHECKS" if failed else "SUCCESS", bronze_rows, len(checks),
          f"{len(failed)} failed check(s)")

    # --- Persist ------------------------------------------------------------
    t2 = datetime.now(timezone.utc)
    _overwrite(rejected, "silver_rejected_records")
    _overwrite(g_cust, "gold_customer_summary")
    _overwrite(g_cancel, "gold_cancellations_monthly")
    if rebuild_core:
        if failed:
            raise RuntimeError("rebuild_core refused: data-quality checks failed. See data_quality_metrics.")
        _overwrite(silver, "silver_transactions")
        _overwrite(g_month, "gold_monthly_sales")
        _overwrite(g_prod, "gold_product_sales")
        _overwrite(g_country, "gold_country_sales")
        stage("rebuild_core", t2, "SUCCESS", bronze_rows, None, "Silver and Gold tables overwritten")
    stage("persist_new_tables", t2, "SUCCESS", None, None, "rejected, customer, cancellation tables written")

    quality_df = spark.createDataFrame(checks, QUALITY_SCHEMA) \
        .withColumn("pipeline_run_id", F.lit(run_id)).withColumn("checked_at", F.current_timestamp())
    _append(quality_df, "data_quality_metrics")
    log_schema = T.StructType([
        T.StructField("pipeline_run_id", T.StringType()), T.StructField("stage", T.StringType()),
        T.StructField("started_at", T.TimestampType()), T.StructField("finished_at", T.TimestampType()),
        T.StructField("status", T.StringType()), T.StructField("rows_in", T.LongType()),
        T.StructField("rows_out", T.LongType()), T.StructField("message", T.StringType()),
    ])
    _append(spark.createDataFrame(log, log_schema), "pipeline_run_log")
    return {"run_id": run_id, "failed_checks": [c[0] for c in failed], "checks": len(checks)}


# COMMAND ----------

def _widget(name: str, default: str) -> str:
    try:
        dbutils.widgets.text(name, default)          # noqa: F821  (Databricks only)
        return dbutils.widgets.get(name)             # noqa: F821
    except NameError:
        return default


if "dbutils" in globals():  # runs only inside Databricks, never on import
    _rebuild = _widget("rebuild_core", "no").strip().lower() in ("yes", "true", "1")
    _source = _widget("ingest_source_path", "").strip() or None
    print(run_pipeline(spark, rebuild_core=_rebuild, ingest_source_path=_source))  # noqa: F821
