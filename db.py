"""
RetailPulse - Databricks connection and data-access layer.

Design rules (these exist to prevent the repeated OAuth browser-tab loop):

1. Importing this module NEVER contacts Databricks.
2. Exactly ONE Databricks connection is opened per Streamlit server process.
   It is opened only when `DatabricksClient.connect()` is called, and the app
   calls that only when the user clicks "Connect".
3. Every SQL query reuses that single connection (serialised by a lock).
   Previous code called `sql.connect(auth_type="databricks-oauth")` once per
   query; each of those calls starts a brand-new OAuth authorisation flow.
4. A second concurrent `connect()` is refused, so two browser sessions or a
   Streamlit rerun can never start two OAuth flows at once.
5. OAuth tokens are kept in memory only (never written to disk), so if the
   connection has to be re-created the refresh token is reused silently.
6. Query RESULTS are cached with st.cache_data. Connections are NOT cached
   inside st.cache_data, so a dead connection can never be served from cache.
"""

from __future__ import annotations

import atexit
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

APP_DIR = Path(__file__).resolve().parent

# Read .env from the project folder (not from the current working directory),
# and let it win over stale Windows environment variables - the same behaviour
# as the working test_connection.py (load_dotenv(override=True)).
load_dotenv(APP_DIR / ".env", override=True)

CACHE_TTL_SECONDS = 600  # query results are reused for 10 minutes
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")

# Stock codes that are accounting / shipping entries rather than merchandise.
# They stay in every financial KPI; they can be excluded from product rankings.
ADMIN_STOCK_CODES = frozenset(
    {"POST", "DOT", "M", "AMAZONFEE", "B", "C2", "BANK CHARGES", "S", "D", "CRUK"}
)


@dataclass(frozen=True)
class Settings:
    host: str
    http_path: str
    catalog: str
    schema: str

    @property
    def missing(self) -> list[str]:
        missing = []
        if not self.host:
            missing.append("DATABRICKS_SERVER_HOSTNAME")
        if not self.http_path:
            missing.append("DATABRICKS_HTTP_PATH")
        return missing

    def table(self, name: str) -> str:
        return f"{self.catalog}.{self.schema}.{name}"


def load_settings() -> Settings:
    """Read connection settings from environment variables (no network)."""
    raw_host = os.getenv("DATABRICKS_SERVER_HOSTNAME", "").strip()
    if raw_host.startswith(("https://", "http://")):
        host = urlparse(raw_host).hostname or ""
    else:
        host = raw_host.split("/")[0].strip()

    catalog = os.getenv("DATABRICKS_CATALOG", "workspace").strip()
    schema = os.getenv("DATABRICKS_SCHEMA", "retailpulse").strip()
    for label, value in (("DATABRICKS_CATALOG", catalog), ("DATABRICKS_SCHEMA", schema)):
        if not _IDENTIFIER.match(value):
            raise ValueError(f"{label} may only contain letters, digits and underscores.")

    return Settings(
        host=host,
        http_path=os.getenv("DATABRICKS_HTTP_PATH", "").strip(),
        catalog=catalog,
        schema=schema,
    )


# --------------------------------------------------------------------------
# Errors: redaction and human-readable guidance
# --------------------------------------------------------------------------

class NotConnectedError(RuntimeError):
    """Raised when a query is attempted before a connection exists."""


class ConnectionInProgressError(RuntimeError):
    """Raised when a sign-in is already running in this server process."""


class SchemaError(RuntimeError):
    """Raised when a table does not have the columns the dashboard needs."""


_SECRET_PATTERNS = [
    re.compile(r"dapi[0-9a-zA-Z]{8,}"),
    re.compile(r"eyJ[0-9A-Za-z_\-]+\.[0-9A-Za-z_\-]+\.[0-9A-Za-z_\-]*"),
    re.compile(r"(?i)bearer\s+[0-9A-Za-z._\-]+"),
    re.compile(r"(?i)(access_token|refresh_token|client_secret|code)=[^&\s'\"]+"),
]


def redact(text: str) -> str:
    """Strip anything that looks like a credential before showing an error."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


# (regex, headline, what to do) - first match wins.
_ERROR_GUIDE = [
    (r"Address already in use|Errno 98|Errno 10048|WinError 10048|port.*8020|8020.*in use",
     "The sign-in callback port is busy",
     "Another process (often a previous Streamlit run or test_connection.py) is holding "
     "localhost port 8020-8024. Close other RetailPulse/Streamlit terminals, then click Connect again."),
    (r"nodename nor servname|Name or service not known|getaddrinfo failed|NameResolution|Failed to resolve",
     "The Databricks hostname could not be reached",
     "Check DATABRICKS_SERVER_HOSTNAME in .env: hostname only, for example "
     "dbc-xxxx.cloud.databricks.com (no https:// and no path). Also check your internet connection."),
    (r"PERMISSION_DENIED|INSUFFICIENT_PERMISSIONS|does not have .*(privilege|permission)|USE CATALOG|USE SCHEMA|Permission denied",
     "Your Databricks user is not allowed to read these tables",
     "In Databricks, open Catalog > workspace > retailpulse and confirm your user has USE CATALOG, "
     "USE SCHEMA and SELECT on the tables."),
    (r"TABLE_OR_VIEW_NOT_FOUND|SCHEMA_NOT_FOUND|NO_SUCH_CATALOG|Table or view not found|cannot be found",
     "A table, schema or catalog was not found",
     "Check that workspace.retailpulse.gold_monthly_sales, gold_product_sales, gold_country_sales "
     "and silver_transactions exist, and that DATABRICKS_CATALOG / DATABRICKS_SCHEMA are correct."),
    (r"UNRESOLVED_COLUMN|cannot be resolved|Column .* does not exist",
     "A column the dashboard expects does not exist",
     "A table schema differs from what the dashboard expects. Expand the technical details to see the column name."),
    (r"STOPPED|STARTING|TEMPORARILY_UNAVAILABLE|\b503\b|not running|warehouse.*(start|stopp)",
     "The SQL Warehouse is not ready",
     "Open Databricks > SQL Warehouses, start the warehouse, wait until it shows Running, then retry."),
    (r"RESOURCE_DOES_NOT_EXIST|No such (warehouse|endpoint)|\b404\b|Invalid HTTP path|http_path",
     "The SQL Warehouse HTTP path looks wrong",
     "Re-copy DATABRICKS_HTTP_PATH from SQL Warehouses > your warehouse > Connection details."),
    (r"(?i)invalid_grant|invalid_client|oauth|unauthori[sz]ed|\b401\b|\b403\b|token",
     "Databricks sign-in was rejected or has expired",
     "Click Reconnect and complete the browser sign-in once. If it keeps failing, close all "
     "Databricks tabs, restart Streamlit and try again."),
    (r"(?i)InvalidSessionHandle|invalid session|session.*(expired|closed|not found)|connection.*(closed|reset|aborted)|Broken pipe",
     "The Databricks session ended",
     "The warehouse probably stopped after being idle. Click Reconnect (usually silent; no new tab needed)."),
    (r"(?i)timed out|timeout|Max retries exceeded",
     "Databricks did not answer in time",
     "The warehouse may be starting up or your network is slow. Wait a minute and retry."),
]

_DEAD_CONNECTION = re.compile(
    r"(?i)InvalidSessionHandle|invalid session|session.*(expired|closed|not found)|"
    r"connection.*(closed|reset|aborted|refused)|Broken pipe|\b401\b|unauthori[sz]ed|"
    r"Max retries exceeded|RequestError|CursorAlreadyClosed"
)


def describe_error(exc: BaseException) -> tuple[str, str, str]:
    """Return (headline, advice, redacted technical detail) for display."""
    detail = redact(f"{type(exc).__name__}: {exc}")
    for pattern, headline, advice in _ERROR_GUIDE:
        if re.search(pattern, detail):
            return headline, advice, detail
    return (
        "Something went wrong while talking to Databricks",
        "See the technical details below. If the connection was lost, click Reconnect.",
        detail,
    )


# --------------------------------------------------------------------------
# The client: one connection, explicit connect, serialised queries
# --------------------------------------------------------------------------

def _safe_close(obj) -> None:
    try:
        obj.close()
    except Exception:
        pass


class DatabricksClient:
    """Owns the single Databricks SQL connection for this server process."""

    def __init__(self) -> None:
        self._lock = threading.RLock()            # guards the connection + queries
        self._connect_lock = threading.Lock()     # allows only one sign-in at a time
        self._conn = None
        self._token_cache = None                  # in-memory OAuth tokens only
        self.connected_at: datetime | None = None
        self.last_success_at: datetime | None = None

    # ---- state -----------------------------------------------------------
    @property
    def is_connected(self) -> bool:
        return self._conn is not None

    @property
    def is_connecting(self) -> bool:
        return self._connect_lock.locked()

    # ---- connect / disconnect -------------------------------------------
    def connect(self, settings: Settings) -> None:
        """Open the connection. May open ONE browser tab for OAuth sign-in."""
        if self.is_connected:
            return
        if not self._connect_lock.acquire(blocking=False):
            raise ConnectionInProgressError(
                "A Databricks sign-in is already in progress. Finish it in the browser tab "
                "that opened, then refresh this page."
            )
        conn = None
        try:
            if self.is_connected:
                return
            if settings.missing:
                raise ValueError("Missing settings in .env: " + ", ".join(settings.missing))

            # Imported here so that importing this module has no side effects.
            from databricks import sql
            from databricks.sql.experimental.oauth_persistence import OAuthPersistenceCache

            if self._token_cache is None:
                self._token_cache = OAuthPersistenceCache()  # memory only

            conn = sql.connect(
                server_hostname=settings.host,
                http_path=settings.http_path,
                auth_type="databricks-oauth",
                catalog=settings.catalog,
                schema=settings.schema,
                experimental_oauth_persistence=self._token_cache,
            )
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchall()

            with self._lock:
                self._conn = conn
                self.connected_at = datetime.now()
                self.last_success_at = self.connected_at
            conn = None  # ownership transferred to self._conn
        finally:
            if conn is not None:
                _safe_close(conn)
            self._connect_lock.release()

    def disconnect(self, forget_login: bool = False) -> None:
        """Close the connection. Keeps tokens unless forget_login=True."""
        with self._lock:
            conn, self._conn = self._conn, None
            self.connected_at = None
        if conn is not None:
            _safe_close(conn)
        if forget_login:
            self._token_cache = None

    close = disconnect

    # ---- queries ---------------------------------------------------------
    def query(self, statement: str, params: dict | None = None) -> pd.DataFrame:
        """Run one read-only statement and return a DataFrame."""
        with self._lock:
            conn = self._conn
            if conn is None:
                raise NotConnectedError("Not connected to Databricks.")
            try:
                with conn.cursor() as cursor:
                    if params:
                        cursor.execute(statement, params)
                    else:
                        cursor.execute(statement)
                    frame = cursor.fetchall_arrow().to_pandas()
            except Exception as exc:
                if _DEAD_CONNECTION.search(str(exc)):
                    # Drop the dead connection so the UI offers "Reconnect".
                    # No automatic retry: a retry could start another sign-in.
                    self.disconnect()
                raise
            self.last_success_at = datetime.now()
            return frame


@st.cache_resource(show_spinner=False)
def get_client() -> DatabricksClient:
    """One client per server process. Creating it does NOT connect."""
    client = DatabricksClient()
    atexit.register(client.disconnect)
    return client


# --------------------------------------------------------------------------
# Schema contracts (verified at runtime, never assumed)
# --------------------------------------------------------------------------

GOLD_MONTHLY_COLUMNS = {"YearMonth", "transaction_lines", "total_revenue", "invoice_count", "unique_customers"}
GOLD_PRODUCT_COLUMNS = {"StockCode", "Description", "units_sold", "total_revenue", "invoice_count"}
GOLD_COUNTRY_COLUMNS = {"Country", "transaction_lines", "invoice_count", "total_revenue", "unique_customers"}
SILVER_REQUIRED = {
    "InvoiceNo", "StockCode", "Description", "Quantity",
    "CustomerID", "Country", "YearMonth", "Revenue",
}


def _require(columns, required: set[str], table: str) -> None:
    missing = sorted(required - set(columns))
    if missing:
        raise SchemaError(
            f"{table} is missing column(s): {', '.join(missing)}. "
            f"Columns found: {', '.join(map(str, columns))}."
        )


def _numeric(frame: pd.DataFrame, columns) -> pd.DataFrame:
    for column in columns:
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0)
    return frame


def _month_key(series: pd.Series) -> pd.Series:
    """'2011-03' from either '2011-03' or '2011-03-01' style values."""
    return series.astype(str).str.slice(0, 7)


def _clean_products(frame: pd.DataFrame) -> pd.DataFrame:
    frame = _numeric(frame, ["units_sold", "total_revenue", "invoice_count"])
    frame["StockCode"] = frame["StockCode"].astype(str)
    frame["Description"] = frame["Description"].fillna("(no description)").astype(str)
    frame["is_admin"] = frame["StockCode"].str.upper().isin(ADMIN_STOCK_CODES)
    return frame.reset_index(drop=True)


def _clean_countries(frame: pd.DataFrame) -> pd.DataFrame:
    frame = _numeric(frame, ["transaction_lines", "invoice_count", "total_revenue", "unique_customers"])
    frame["Country"] = frame["Country"].astype(str)
    return frame.reset_index(drop=True)


def _clean_monthly(frame: pd.DataFrame) -> pd.DataFrame:
    frame = _numeric(frame, ["transaction_lines", "total_revenue", "invoice_count", "unique_customers"])
    return frame.sort_values("month_key").reset_index(drop=True)


# --------------------------------------------------------------------------
# Cached queries. `_client` starts with an underscore so Streamlit does not try
# to hash it; the cache key is the remaining arguments.
# --------------------------------------------------------------------------

def _tables() -> Settings:
    return load_settings()


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def load_silver_columns(_client: DatabricksClient) -> list[str]:
    """Column names of the Silver table (reads a single row)."""
    frame = _client.query(f"SELECT * FROM {_tables().table('silver_transactions')} LIMIT 1")
    return [str(c) for c in frame.columns]


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def load_gold_monthly(_client: DatabricksClient) -> pd.DataFrame:
    frame = _client.query(f"SELECT * FROM {_tables().table('gold_monthly_sales')}")
    _require(frame.columns, GOLD_MONTHLY_COLUMNS, "gold_monthly_sales")
    frame = frame.copy()
    frame["month_key"] = _month_key(frame["YearMonth"])
    return _clean_monthly(frame)


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def load_gold_products(_client: DatabricksClient) -> pd.DataFrame:
    frame = _client.query(f"SELECT * FROM {_tables().table('gold_product_sales')}")
    _require(frame.columns, GOLD_PRODUCT_COLUMNS, "gold_product_sales")
    return _clean_products(frame.copy())


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def load_gold_countries(_client: DatabricksClient) -> pd.DataFrame:
    frame = _client.query(f"SELECT * FROM {_tables().table('gold_country_sales')}")
    _require(frame.columns, GOLD_COUNTRY_COLUMNS, "gold_country_sales")
    return _clean_countries(frame.copy())


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def load_last_invoice_date(_client: DatabricksClient, has_invoice_date: bool):
    """Latest invoice timestamp, used only to flag a partially-covered month."""
    if not has_invoice_date:
        return None
    frame = _client.query(
        f"SELECT MAX(InvoiceDate) AS last_date FROM {_tables().table('silver_transactions')}"
    )
    value = frame["last_date"].iloc[0] if not frame.empty else None
    # Only trust real date/timestamp values (a text column would sort wrongly).
    if value is None or pd.isna(value) or not isinstance(value, (pd.Timestamp, datetime)):
        return None
    return pd.Timestamp(value)


# ---- Filtered (Silver) aggregates -----------------------------------------
# Only aggregated rows ever leave Databricks. All user input is bound as a
# named parameter (:name); only generated placeholder names are put in the SQL.

def _where(months, countries, term) -> tuple[str, dict]:
    clauses: list[str] = []
    params: dict = {}

    if months:
        names = []
        for i, value in enumerate(months):
            params[f"m{i}"] = str(value)
            names.append(f":m{i}")
        clauses.append(f"SUBSTRING(CAST(YearMonth AS STRING), 1, 7) IN ({', '.join(names)})")

    if countries:
        names = []
        for i, value in enumerate(countries):
            params[f"c{i}"] = str(value)
            names.append(f":c{i}")
        clauses.append(f"Country IN ({', '.join(names)})")

    if term:
        params["term"] = term
        clauses.append(
            "(INSTR(LOWER(CAST(Description AS STRING)), :term) > 0 "
            "OR INSTR(LOWER(CAST(StockCode AS STRING)), :term) > 0)"
        )

    return ("WHERE " + " AND ".join(clauses)) if clauses else "", params


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False, max_entries=64)
def query_kpis(_client: DatabricksClient, months, countries, term) -> dict:
    where, params = _where(months, countries, term)
    frame = _client.query(
        f"""
        SELECT
            COALESCE(SUM(Revenue), 0)       AS revenue,
            COUNT(DISTINCT InvoiceNo)       AS invoices,
            COUNT(DISTINCT CustomerID)      AS customers,
            COALESCE(SUM(Quantity), 0)      AS units,
            COUNT(*)                        AS transaction_lines
        FROM {_tables().table('silver_transactions')}
        {where}
        """,
        params,
    )
    row = frame.iloc[0]
    return {
        "revenue": float(pd.to_numeric(row["revenue"], errors="coerce") or 0),
        "invoices": int(pd.to_numeric(row["invoices"], errors="coerce") or 0),
        "customers": int(pd.to_numeric(row["customers"], errors="coerce") or 0),
        "units": float(pd.to_numeric(row["units"], errors="coerce") or 0),
        "transaction_lines": int(pd.to_numeric(row["transaction_lines"], errors="coerce") or 0),
    }


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False, max_entries=64)
def query_monthly(_client: DatabricksClient, months, countries, term) -> pd.DataFrame:
    where, params = _where(months, countries, term)
    frame = _client.query(
        f"""
        SELECT
            SUBSTRING(CAST(YearMonth AS STRING), 1, 7) AS month_key,
            COUNT(*)                    AS transaction_lines,
            SUM(Revenue)                AS total_revenue,
            COUNT(DISTINCT InvoiceNo)   AS invoice_count,
            COUNT(DISTINCT CustomerID)  AS unique_customers
        FROM {_tables().table('silver_transactions')}
        {where}
        GROUP BY SUBSTRING(CAST(YearMonth AS STRING), 1, 7)
        ORDER BY month_key
        """,
        params,
    )
    return _clean_monthly(frame)


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False, max_entries=64)
def query_products(_client: DatabricksClient, months, countries, term) -> pd.DataFrame:
    where, params = _where(months, countries, term)
    frame = _client.query(
        f"""
        SELECT
            StockCode,
            Description,
            SUM(Quantity)               AS units_sold,
            SUM(Revenue)                AS total_revenue,
            COUNT(DISTINCT InvoiceNo)   AS invoice_count
        FROM {_tables().table('silver_transactions')}
        {where}
        GROUP BY StockCode, Description
        """,
        params,
    )
    return _clean_products(frame)


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False, max_entries=64)
def query_countries(_client: DatabricksClient, months, countries, term) -> pd.DataFrame:
    where, params = _where(months, countries, term)
    frame = _client.query(
        f"""
        SELECT
            Country,
            COUNT(*)                    AS transaction_lines,
            COUNT(DISTINCT InvoiceNo)   AS invoice_count,
            SUM(Revenue)                AS total_revenue,
            COUNT(DISTINCT CustomerID)  AS unique_customers
        FROM {_tables().table('silver_transactions')}
        {where}
        GROUP BY Country
        """,
        params,
    )
    return _clean_countries(frame)


# --------------------------------------------------------------------------
# Orchestration: choose Gold or Silver for the current filters
# --------------------------------------------------------------------------

@dataclass
class Reference:
    """Unfiltered Gold data used to build the filter controls."""
    monthly: pd.DataFrame
    products: pd.DataFrame
    countries: pd.DataFrame
    silver_columns: list[str]
    last_invoice_date: pd.Timestamp | None

    @property
    def months(self) -> list[str]:
        return self.monthly["month_key"].tolist()

    @property
    def country_names(self) -> list[str]:
        return sorted(self.countries["Country"].tolist())

    @property
    def partial_month(self) -> str | None:
        """Month key whose data stops before month-end, if known."""
        last = self.last_invoice_date
        if last is None:
            return None
        month_end = (last + pd.offsets.MonthEnd(0)).normalize()
        return last.strftime("%Y-%m") if last.normalize() < month_end else None


def load_reference(client: DatabricksClient) -> Reference:
    """Load + validate the unfiltered Gold tables and the Silver column list."""
    silver_columns = load_silver_columns(client)
    _require(silver_columns, SILVER_REQUIRED, "silver_transactions")
    return Reference(
        monthly=load_gold_monthly(client),
        products=load_gold_products(client),
        countries=load_gold_countries(client),
        silver_columns=silver_columns,
        last_invoice_date=load_last_invoice_date(client, "InvoiceDate" in silver_columns),
    )


@dataclass
class Dashboard:
    kpis: dict
    monthly: pd.DataFrame
    products: pd.DataFrame
    countries: pd.DataFrame
    sources: dict = field(default_factory=dict)
    is_default: bool = True
    restrict: tuple = (None, None, "")


def load_dashboard(
    client: DatabricksClient,
    ref: Reference,
    selected_months: list[str],
    selected_countries: list[str],
    term: str,
) -> Dashboard:
    """
    Source selection
      * No filter at all .............. Gold tables (pre-aggregated).
      * Month range only .............. Gold monthly trend; Silver for the rest.
      * Country and/or product filter . Silver, aggregated in SQL.
      * KPIs ......................... always Silver (exact distinct counts).
    Unique customers are never added up across months or countries.
    """
    months = tuple(sorted(selected_months))
    months_restrict = None if set(months) == set(ref.months) else months

    countries = tuple(sorted(selected_countries))
    countries_restrict = None if (not countries or set(countries) == set(ref.country_names)) else countries

    term = (term or "").strip().lower()[:80]
    is_default = months_restrict is None and countries_restrict is None and not term

    kpis = query_kpis(client, months_restrict, countries_restrict, term)

    if countries_restrict is None and not term:
        monthly = ref.monthly[ref.monthly["month_key"].isin(months)].reset_index(drop=True)
        monthly_source = "Gold"
    else:
        monthly = query_monthly(client, months_restrict, countries_restrict, term)
        monthly_source = "Silver"

    if is_default:
        products, countries_df = ref.products.copy(), ref.countries.copy()
        detail_source = "Gold"
    else:
        products = query_products(client, months_restrict, countries_restrict, term)
        countries_df = query_countries(client, months_restrict, countries_restrict, term)
        detail_source = "Silver"

    # The richer dashboard expects a consistent month_key field and admin-product flag.
    if "month_key" not in monthly.columns and "YearMonth" in monthly.columns:
        monthly = monthly.rename(columns={"YearMonth": "month_key"})
    if "is_admin" not in products.columns:
        products = products.copy()
        products["is_admin"] = products["StockCode"].astype(str).str.upper().isin(ADMIN_STOCK_CODES)
    if "is_admin" not in ref.products.columns:
        ref.products["is_admin"] = ref.products["StockCode"].astype(str).str.upper().isin(ADMIN_STOCK_CODES)
    return Dashboard(
        kpis=kpis, monthly=monthly, products=products, countries=countries_df,
        sources={"kpis": "Silver", "monthly": monthly_source,
                 "products": detail_source, "countries": detail_source},
        is_default=is_default,
        restrict=(months_restrict, countries_restrict, term),
    )


# --------------------------------------------------------------------------
# Extended dashboard APIs: customer analytics and pipeline-quality views
# --------------------------------------------------------------------------

_OPTIONAL_TABLES = {
    "gold_customer_summary", "gold_cancellations_monthly", "data_quality_metrics",
    "pipeline_run_log", "silver_rejected_records",
}
_RECORDED_COUNTS = {
    "bronze_transactions": 541909, "silver_transactions": 524878,
    "gold_monthly_sales": 13, "gold_product_sales": 4158, "gold_country_sales": 38,
}

def _silver_table() -> str:
    return _tables().table("silver_transactions")

def _customer_where(months, countries, term):
    return _where(months, countries, term)

@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False, max_entries=64)
def query_customer_frequency(_client, months, countries, term):
    """Return a frequency distribution for repeat-customer analytics.

    One row per invoice-count bucket, with columns:
      - invoices_per_customer: number of distinct invoices placed by a customer
      - customers: number of customers in that bucket

    analytics.repeat_stats() and analytics.bucket_frequency() expect this shape.
    """
    where, params = _customer_where(months, countries, term)
    if where:
        customer_filter = f"{where} AND CustomerID IS NOT NULL"
    else:
        customer_filter = "WHERE CustomerID IS NOT NULL"

    sql = f"""
        WITH customer_orders AS (
            SELECT
                CustomerID,
                COUNT(DISTINCT InvoiceNo) AS invoices_per_customer
            FROM {_silver_table()}
            {customer_filter}
            GROUP BY CustomerID
        )
        SELECT
            invoices_per_customer,
            COUNT(*) AS customers
        FROM customer_orders
        GROUP BY invoices_per_customer
        ORDER BY invoices_per_customer
    """
    return _client.query(sql, params)

@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False, max_entries=64)
def query_identification(_client, months, countries, term):
    where, params = _customer_where(months, countries, term)
    sql = f"""SELECT
      COALESCE(SUM(CASE WHEN CustomerID IS NOT NULL THEN Revenue ELSE 0 END),0) identified_revenue,
      COALESCE(SUM(CASE WHEN CustomerID IS NULL THEN Revenue ELSE 0 END),0) anonymous_revenue,
      COALESCE(SUM(CASE WHEN CustomerID IS NULL THEN 1 ELSE 0 END),0) anonymous_lines,
      COUNT(DISTINCT CASE WHEN CustomerID IS NOT NULL THEN CustomerID END) customers
      FROM {_silver_table()} {where}"""
    row = _client.query(sql, params).iloc[0].to_dict()
    return {k: (float(v or 0) if k.endswith("revenue") else int(v or 0)) for k,v in row.items()}

@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False, max_entries=64)
def query_top_customers(_client, months, countries, term):
    where, params = _customer_where(months, countries, term)
    sql = f"""SELECT CustomerID, MAX(Country) AS Country, COUNT(DISTINCT InvoiceNo) AS invoice_count,
      SUM(Quantity) AS units_bought, SUM(Revenue) AS total_revenue
      FROM {_silver_table()} {where} AND CustomerID IS NOT NULL
      GROUP BY CustomerID ORDER BY total_revenue DESC LIMIT 200""" if where else f"""SELECT CustomerID, MAX(Country) AS Country,
      COUNT(DISTINCT InvoiceNo) AS invoice_count, SUM(Quantity) AS units_bought, SUM(Revenue) AS total_revenue
      FROM {_silver_table()} WHERE CustomerID IS NOT NULL GROUP BY CustomerID ORDER BY total_revenue DESC LIMIT 200"""
    return _client.query(sql, params)

@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False, max_entries=64)
def query_customer_flow(_client, months, countries, term):
    # First purchase is measured across the whole dataset; displayed months are filtered afterward.
    where, params = _customer_where(months, countries, term)
    month_expr = "SUBSTRING(CAST(YearMonth AS STRING),1,7)"
    sql = f"""WITH scoped AS (SELECT CustomerID, InvoiceNo, Country, Description, StockCode, Revenue, {month_expr} month_key
      FROM {_silver_table()} {where}), first_purchase AS (
      SELECT CustomerID, MIN({month_expr}) first_month FROM {_silver_table()} WHERE CustomerID IS NOT NULL GROUP BY CustomerID)
      SELECT s.month_key, COUNT(DISTINCT CASE WHEN s.CustomerID IS NOT NULL AND s.month_key=f.first_month THEN s.CustomerID END) new_customers,
      COUNT(DISTINCT CASE WHEN s.CustomerID IS NOT NULL AND s.month_key<>f.first_month THEN s.CustomerID END) returning_customers
      FROM scoped s LEFT JOIN first_purchase f ON s.CustomerID=f.CustomerID GROUP BY s.month_key ORDER BY s.month_key"""
    return _client.query(sql, params)

@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False, max_entries=16)
def load_table_counts(_client):
    rows=[]
    for name, baseline in _RECORDED_COUNTS.items():
        frame=_client.query(f"SELECT COUNT(*) AS n FROM {_tables().table(name)}")
        live=int(frame.iloc[0]["n"])
        rows.append({"table_name":name,"live_rows":live,"recorded_rows":baseline,"matches":live==baseline})
    return pd.DataFrame(rows)

@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False, max_entries=8)
def load_silver_profile(_client):
    frame=_client.query(f"""SELECT COUNT(*) rows_total,
      SUM(CASE WHEN Quantity<=0 THEN 1 ELSE 0 END) non_positive_quantity,
      SUM(CASE WHEN Revenue<=0 THEN 1 ELSE 0 END) non_positive_revenue,
      SUM(CASE WHEN CustomerID IS NULL THEN 1 ELSE 0 END) lines_no_customer,
      SUM(CASE WHEN Description IS NULL OR TRIM(CAST(Description AS STRING))='' THEN 1 ELSE 0 END) lines_no_description
      FROM {_silver_table()}""")
    out={k:int(v or 0) for k,v in frame.iloc[0].to_dict().items()}
    try:
        spans=_client.query(f"SELECT COUNT(*) n FROM (SELECT InvoiceNo FROM {_silver_table()} GROUP BY InvoiceNo HAVING COUNT(DISTINCT YearMonth)>1) x")
        out["invoices_spanning_months"]=int(spans.iloc[0]["n"] or 0)
    except Exception:
        out["invoices_spanning_months"]=0
    return out

@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False, max_entries=16)
def load_optional_table(_client, name):
    if name not in _OPTIONAL_TABLES:
        raise ValueError("Optional table is not on the allowlist.")
    try:
        return _client.query(f"SELECT * FROM {_tables().table(name)}")
    except Exception as exc:
        message=str(exc).lower()
        if "not found" in message or "does not exist" in message or "table_or_view_not_found" in message:
            return None
        raise

@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False, max_entries=8)
def load_rejected_summary(_client):
    frame=load_optional_table(_client,"silver_rejected_records")
    if frame is None or frame.empty:
        return frame
    if "reject_reason" not in frame.columns:
        return pd.DataFrame(columns=["reject_reason","records"])
    return frame.groupby("reject_reason",dropna=False).size().reset_index(name="records").sort_values("records",ascending=False)
