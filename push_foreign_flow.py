"""
Fetch IDX foreign-flow net_ratio_1d from a residential IP (laptop) and
commit+push the result so the VPS can pick it up via `git pull`.

Why this exists: idx.co.id's endpoint now serves a Cloudflare Managed
Challenge ("Just a moment...") to the VPS's datacenter IP, which curl_cffi
can't solve (it's a JS challenge, not a UA/fingerprint block). The laptop
isn't challenged, so this script does the fetch here instead and pushes
the result as a small JSON file that engine/nightly.py prefers over its
own live-fetch attempt (see broker.py load_pushed_foreign_flow_net_ratio).

Usage: run this once each evening, any time before you trigger /eodscan
on the VPS (order doesn't matter as long as it's pushed before that run;
if /eodscan runs first with no file, it just falls back to its own
live-fetch attempt, which will likely fail the same way it does today).

    python push_foreign_flow.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import engine.broker as broker_engine

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    target_date = broker_engine._idx_ff_target_date()
    print(f"🌊 Fetching IDX foreign flow for {target_date.isoformat()}...")
    ratios = broker_engine.fetch_idx_foreign_flow_net_ratio(date=target_date)
    if not ratios:
        print("❌ Fetch gagal/kosong -- cek koneksi atau apakah IDX juga men-challenge dari sini sekarang.")
        return 1
    print(f"✅ Dapat {len(ratios)} ticker.")

    out_dir = broker_engine.FOREIGN_FLOW_DAILY_DIR
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{target_date.isoformat()}.json")
    with open(out_path, "w") as f:
        json.dump(ratios, f)
    print(f"💾 Ditulis ke {out_path}")

    rel_path = os.path.relpath(out_path, REPO_ROOT)
    try:
        subprocess.run(["git", "add", rel_path], cwd=REPO_ROOT, check=True)
        commit = subprocess.run(
            ["git", "commit", "-m", f"Push foreign flow data {target_date.isoformat()}"],
            cwd=REPO_ROOT, check=False, capture_output=True, text=True,
        )
        if commit.returncode != 0:
            print(f"ℹ️ Tidak ada yang di-commit (mungkin file sama persis dgn sebelumnya): {commit.stdout.strip()}")
            return 0
        subprocess.run(["git", "push"], cwd=REPO_ROOT, check=True)
        print("🚀 Sudah di-push. Tinggal `git pull` di VPS sebelum /eodscan.")
    except subprocess.CalledProcessError as e:
        print(f"⚠️ Git add/commit/push gagal: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
