"""
test_zapi_foreign_flow_shape2_2026_10_13.py — follow-up #2.

Discovered result["data"] is a DICT with 9 keys, not a list of ticker
records as first assumed (len(data)==9, data[0] raised KeyError). This
prints the dict's keys and a safe preview of each value's type/shape so
we can find where the actual per-ticker array lives (likely nested one
level deeper, e.g. data["list"] or similar).

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

data = result.get("data", {})
print("type(data):", type(data).__name__)
print("data keys:", list(data.keys()) if isinstance(data, dict) else "N/A (not a dict)")
print()

if isinstance(data, dict):
    for k, v in data.items():
        if isinstance(v, list):
            print(f"data['{k}']: list, len={len(v)}")
            if v:
                print(f"   first item: {json.dumps(v[0], default=str)[:300]}")
        elif isinstance(v, dict):
            print(f"data['{k}']: dict, keys={list(v.keys())}")
        else:
            print(f"data['{k}']: {type(v).__name__} = {repr(v)[:200]}")
