"""
test_zapi_foreign_flow_manual_2026_10_11.py — ONE-OFF manual diagnostic.

engine/broker.py's fetch_zapi_foreign_flow() has NEVER been called live
(its own docstring/comment says so) -- hits Zapi's `finance:idx/foreign-
flow` endpoint, a third-party API gateway (api.zpi.web.id), NOT a direct
idx.co.id scrape, so it's a candidate to sidestep the Cloudflare-blocks-
datacenter-IP problem that forces the current laptop-only foreign-flow
update workflow.

Run this ONCE on the VPS (where the real ZAPI_API_KEY lives -- this
laptop's .env has it empty, can't test from here) to see the RAW response
shape before building any backfill/parsing logic around it:

    python test_zapi_foreign_flow_manual_2026_10_11.py

Costs exactly 1 Zapi quota call (shared 600/month pool with every other
Zapi endpoint -- orderbook, stock-summary, etc.). Does NOT write to any
file or database -- read-only diagnostic, prints the response and exits.
"""
import json
import sys

sys.path.insert(0, ".")
import engine.broker as broker_engine

print("Calling fetch_zapi_foreign_flow() for the latest trading day (no date_str = API default)...")
print("(costs 1 Zapi quota call)\n")

result = broker_engine.fetch_zapi_foreign_flow()

if result is None:
    print("RESULT: None -- call failed (quota exhausted, rate-limited, non-200 HTTP, or network error).")
    print("Check the printed warning above (if any) for the specific reason.")
    sys.exit(1)

print("RESULT: call succeeded. Raw response below:\n")
print(json.dumps(result, indent=2, default=str)[:4000])
if len(json.dumps(result, default=str)) > 4000:
    print("\n... (truncated, response is longer than 4000 chars)")

print("\n--- Quick shape check ---")
print("Top-level type:", type(result).__name__)
if isinstance(result, dict):
    print("Top-level keys:", list(result.keys())[:20])
elif isinstance(result, list):
    print(f"List of {len(result)} items. First item keys (if dict):",
          list(result[0].keys()) if result and isinstance(result[0], dict) else result[:1])
