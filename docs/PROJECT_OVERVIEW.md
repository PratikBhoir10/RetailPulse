# RetailPulse project overview

## Objective
Transform historical online retail transaction records into business summaries that help explore revenue trends, product performance, geographic markets, customer purchase behavior and data quality.

## Technologies
- Databricks: managed data and SQL workspace
- Apache Spark / PySpark: distributed cleaning, transformation and aggregation
- Delta tables: Bronze, Silver and Gold storage layers
- Databricks SQL Connector with OAuth: dashboard data access
- Streamlit: interactive dashboard application
- Plotly: charts
- Pandas/NumPy: presentation-level calculations on aggregated query results

## Layer responsibilities
- Bronze: source records retained for traceability.
- Silver: standardized accepted sales records with calculated revenue; rejected rows are tracked separately when the enhanced pipeline runs.
- Gold: aggregated monthly, product and country tables, plus optional customer and cancellation summaries.

## Metric definitions
- Revenue: Quantity × UnitPrice over accepted sales lines, as defined by the current Silver pipeline.
- Invoices: distinct InvoiceNo values.
- Unique customers: distinct CustomerID values in the current selection; do not sum distinct counts across months/countries.
- Units sold: sum of Quantity for accepted lines.
- Average invoice value: revenue divided by distinct invoices.

## Validation before submission
1. Run the pipeline in a safe/default mode and inspect data-quality checks.
2. Compare Gold totals with Silver aggregates using independent SQL.
3. Confirm the actual table schemas and live row counts.
4. Test all dashboard filters and CSV exports.
5. Take screenshots of the running dashboard and add them to the report/presentation.
