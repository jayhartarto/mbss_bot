"""
push_foreign_flow_history.py

MBSS v2 (2026-09-16, BUY ON WEAKNESS): the single-day push_foreign_flow.py
already fetches/pushes today's net_ratio_1d for ENTRY PAGI (which only
needs D-1), but BUY ON WEAKNESS needs a 10-day trailing foreign-flow sum,
which the VPS has no persistent archive for. research/foreign_flow_2y.sqlite
(this laptop's 2-year archive, already fetched from a residential IP since
IDX 403s the VPS's datacenter IP the same way) is the one source with that
history -- so it has to keep being pushed here too, same reason/same fix
as push_foreign_flow.py.

Usage: run this each evening (any order relative to push_foreign_flow.py),
any time before /eodscan on the VPS.

    python push_foreign_flow_history.py
"""
from __future__ import annotations

import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
FETCH_SCRIPT = os.path.join(REPO_ROOT, "research", "fetch_foreign_flow.py")
DB_REL_PATH = os.path.join("research", "foreign_flow_2y.sqlite")


def main() -> int:
    print("🌊 Refreshing research/foreign_flow_2y.sqlite (resumable, only fetches missing days)...")
    result = subprocess.run([sys.executable, FETCH_SCRIPT], cwd=REPO_ROOT)
    if result.returncode != 0:
        print(f"❌ fetch_foreign_flow.py gagal (exit {result.returncode}) -- cek log di atas, tidak push apa pun.")
        return 1

    try:
        subprocess.run(["git", "add", DB_REL_PATH], cwd=REPO_ROOT, check=True)
        commit = subprocess.run(
            ["git", "commit", "-m", "Push foreign flow history (10d/2y archive) for Buy on Weakness"],
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
    raise SystemExit(main())
