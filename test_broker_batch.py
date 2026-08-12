"""
test_broker_batch.py — skrip mandiri (MBSS v2, user request)

Tes langsung endpoint POST /stocks/broker-summary/batch Index Alpha, TANPA
perlu bot jalan — supaya bisa dicoba cepat dari Termux dan cek dashboard/
usage Index Alpha Anda sesudahnya, buat pastikan: apakah 1 panggilan batch
(N ticker) dihitung "1x" dari kuota bulanan, atau dihitung N-kali.

SENGAJA pakai batch KECIL (5 ticker) dulu — jangan langsung coba banyak
sebelum ini terverifikasi.

Cara pakai:
    cd ~/mbss
    python3 test_broker_batch.py
"""
import os
import sys
import json
import datetime

import requests

# Baca .env manual (skrip ini berdiri sendiri, tidak import dari bot.py/engine)
def load_env():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        print(f"❌ Tidak ketemu .env di {env_path}")
        sys.exit(1)
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())

load_env()

API_TOKEN = os.environ.get("INDEXALPHA_API_TOKEN", "")
if not API_TOKEN:
    print("❌ INDEXALPHA_API_TOKEN kosong di .env — isi dulu sebelum jalankan skrip ini.")
    sys.exit(1)

BASE_URL = "https://api.indexalpha.id"
HEADERS = {"accept": "application/json", "Authorization": f"Bearer {API_TOKEN}"}

# ⚠️ SENGAJA cuma 5 ticker dulu — jangan diperbanyak sebelum tahu pasti
# apakah batch ini dihitung 1x atau N-kali dari kuota bulanan.
TEST_TICKERS = ["BBCA", "TLKM", "KOTA", "DOSS", "ADRO"]
DAYS_BACK = 7

today = datetime.date.today()
from_date = today - datetime.timedelta(days=DAYS_BACK)
from_str, to_str = from_date.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")

print(f"🔍 Tes batch broker-summary: {len(TEST_TICKERS)} ticker ({', '.join(TEST_TICKERS)})")
print(f"   Periode: {from_str} s/d {to_str}")
print(f"   Endpoint: POST {BASE_URL}/stocks/broker-summary/batch")
print()

try:
    resp = requests.post(
        f"{BASE_URL}/stocks/broker-summary/batch",
        json={"tickers": TEST_TICKERS, "from": from_str, "to": to_str, "investor": "all"},
        headers=HEADERS,
        timeout=30,
    )
    print(f"HTTP status: {resp.status_code}")

    # Cek header response — beberapa API nunjukkin sisa kuota di sini
    # (mis. X-RateLimit-Remaining), kalau Index Alpha punya, ini akan
    # kelihatan tanpa perlu buka dashboard mereka sama sekali.
    quota_headers = {k: v for k, v in resp.headers.items() if "limit" in k.lower() or "quota" in k.lower() or "remaining" in k.lower()}
    if quota_headers:
        print("📊 Header terkait kuota (kalau ada):")
        for k, v in quota_headers.items():
            print(f"   {k}: {v}")
        print()

    data = resp.json()
    print("Response 'success':", data.get("success"))

    if data.get("success"):
        result_data = data.get("data", {})
        print(f"✅ Berhasil — dapat data untuk {len(result_data)} ticker:")
        for ticker, rows in result_data.items():
            print(f"   {ticker}: {len(rows)} baris broker")
    else:
        print(f"❌ Gagal — error: {data.get('error')}")

    print()
    print("=" * 60)
    print("LANGKAH SELANJUTNYA:")
    print("1. Buka dashboard/halaman usage akun Index Alpha Anda")
    print("2. Bandingkan kuota SEBELUM vs SESUDAH menjalankan skrip ini")
    print(f"3. Kalau kuota cuma berkurang 1 -> batch dihitung 1x (bagus, bisa diperluas)")
    print(f"4. Kalau kuota berkurang {len(TEST_TICKERS)} -> batch dihitung per-ticker (tetap hemat vs single-call, tapi jangan pakai 50 ticker sekaligus tanpa sadar)")
    print("=" * 60)

except Exception as e:
    print(f"❌ Request gagal: {e}")
