"""
test_zapi_foreign_flow_shape_2026_10_13.py — follow-up diagnostic.

First test (test_zapi_foreign_flow_manual_2026_10_11.py) confirmed
fetch_zapi_foreign_flow() works live from the VPS and returns
{"project", "data", "timestamp"} with each data[i] having
code/name/close/volume/value/foreignBuyShares/foreignSellShares/
netForeignShares -- exactly what's needed. This script checks the
remaining unknowns before building the real parser/backfill: how many
tickers does ONE call actually return (is pagination needed to cover
the full ~389-684 ISSI universe?), and what format is `timestamp`.

Costs 1 more Zapi quota call. Read-only, writes nothing.
"""
import json
import sys

sys.path.insert(0, ".")
import engine.broker as broker_engine

result = broker_engine.fetch_zapi_foreign_flow()
if result is None:
    print("RESULT: None -- call failed, see printed warning above.")
    sys.exit(1)

data = result.get("data", [])
print("timestamp field:", repr(result.get("timestamp")))
print("project field:", repr(result.get("project")))
print("len(data):", len(data))

# any pagination-ish metadata alongside data/timestamp/project?
other_keys = [k for k in result.keys() if k not in ("data", "timestamp", "project")]
print("other top-level keys (if any, might hint at pagination):", other_keys)
for k in other_keys:
    print(f"  {k}:", repr(result[k])[:200])

if data:
    print("\nFirst entry keys:", list(data[0].keys()))
    print("Last entry (check if it's a small/illiquid name, suggesting full coverage not just top movers):")
    print(json.dumps(data[-1], indent=2, default=str))
    print("\nMiddle entry (index", len(data) // 2, "):")
    print(json.dumps(data[len(data) // 2], indent=2, default=str))

    # does the net-foreign-shares list look sorted descending (sort="net" default)?
    nets = [d.get("netForeignShares") for d in data if d.get("netForeignShares") is not None]
    print("\nFirst 5 netForeignShares:", nets[:5])
    print("Last 5 netForeignShares:", nets[-5:])
    print("Looks sorted descending by net?", nets == sorted(nets, reverse=True))
