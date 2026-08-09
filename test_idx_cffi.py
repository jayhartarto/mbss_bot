"""
Test IDX API dengan curl_cffi untuk bypass Cloudflare.
Install dulu: pip install curl_cffi
Jalankan di Termux (Python 3.11).
"""
import json
from curl_cffi import requests

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.idx.co.id/",
}

def test(label, url, params=None):
    print(f"\n{'='*55}")
    print(f"  {label}")
    print('='*55)
    try:
        r = requests.get(url, params=params, headers=HEADERS,
                         impersonate="chrome110", timeout=15)
        print(f"  HTTP: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list):
                print(f"  list len={len(data)}")
                if data:
                    print(f"  Keys: {list(data[0].keys()) if isinstance(data[0], dict) else '?'}")
                    print(json.dumps(data[0], indent=2)[:400])
            elif isinstance(data, dict):
                print(f"  dict keys={list(data.keys())[:8]}")
                print(json.dumps(data, indent=2)[:400])
        else:
            print(f"  Raw: {r.text[:200]}")
    except Exception as e:
        print(f"  Error: {e}")

today = "20260727"

test("Historical SS ANTM (pengganti iTick kline)",
     "https://www.idx.co.id/primary/TradingData/GetTradingInfoSS",
     {"InstrumentID": "ANTM", "start": 0, "length": 5})

test("Daily trading ANTM (pengganti iTick quote)",
     "https://www.idx.co.id/primary/TradingData/GetTradingInfoDaily",
     {"InstrumentID": "ANTM"})

test("Broker Summary ANTM (pengganti Index Alpha!)",
     "https://www.idx.co.id/primary/TradingData/GetBrokerSummary",
     {"StartDate": today, "EndDate": today,
      "InstrumentID": "ANTM", "start": 0, "length": 10})

test("Stock Summary semua saham",
     "https://www.idx.co.id/primary/TradingData/GetStockSummary",
     {"date": today, "start": 0, "length": 5})
