
import os
import time
from dotenv import load_dotenv
from databricks import sql

load_dotenv(override=True)

host = os.getenv("DATABRICKS_SERVER_HOSTNAME", "").strip()
http_path = os.getenv("DATABRICKS_HTTP_PATH", "").strip()

print("Hostname configured:", bool(host), flush=True)
print("HTTP path configured:", bool(http_path), flush=True)

if not host or not http_path:
    raise ValueError("Check DATABRICKS_SERVER_HOSTNAME and DATABRICKS_HTTP_PATH in .env")

start = time.time()

try:
    print("\n1. Starting Databricks OAuth connection...", flush=True)

    with sql.connect(
        server_hostname=host,
        http_path=http_path,
        auth_type="databricks-oauth",
        catalog="workspace",
        schema="retailpulse",
        timeout=15,
    ) as connection:

        print(
            f"2. Connection established in {time.time() - start:.1f}s",
            flush=True,
        )

        with connection.cursor() as cursor:
            print("3. Executing SQL query...", flush=True)

            cursor.execute("""
                SELECT COUNT(*)
                FROM workspace.retailpulse.gold_monthly_sales
            """)

            print("4. Fetching result...", flush=True)
            print("Monthly Gold rows:", cursor.fetchone()[0], flush=True)

    print("\nDatabricks connection successful!", flush=True)

except Exception as exc:
    print("\nConnection or query failed:", flush=True)
    print(type(exc).__name__ + ":", str(exc), flush=True)

finally:
    print(
        f"Elapsed time: {time.time() - start:.1f}s",
        flush=True,
    )
