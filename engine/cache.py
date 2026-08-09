"""
engine/cache.py — CacheManager

Central, common cache layer for MBSS v2. Goal (per Executive Summary):
all EOD commands read from ONE shared cache instead of each command doing
its own ad-hoc fetch/save. This is the foundation every other engine module
(NightlyEngine, MarketContextEngine, BrokerEngine, GPTPickEngine, and the
Command Layer) will read from and write to.

Design:
- Primary format: pickle (binary protocol 4). Chosen because scoring dicts
  routinely contain numpy scalars (np.bool_, np.int64), pandas Timestamps,
  and DataFrames — none of which JSON can represent without lossy manual
  conversion.
- Backward-compat: if a pickle file is missing/corrupt, falls back to a
  same-named .json file (for reading old caches written before this
  refactor, or for callers who deliberately want a human-readable format).
- Partitioned by name: each logical cache lives in its own file under
  cache/<name>.pkl (e.g. "eod", "market", "gpt") so consumers only load
  what they need instead of one giant blob.
- Atomic writes: writes to a temp file then os.replace() into place, so a
  crash or interrupted write can never leave a half-written, corrupt cache
  behind (the old cache stays intact until the new one is fully written).
- Never crashes the caller: every read/write failure is logged and returns
  a safe default (None / empty dict) rather than raising, per the
  Executive Summary's error-handling principle ("don't crash the pipeline,
  keep the old cache intact").
"""
from __future__ import annotations

import json
import logging
import os
import pickle
import tempfile
import time
from typing import Any

logger = logging.getLogger("mbss.cache")


class CacheManager:
    """Load/save named caches under a base directory, pickle-first with JSON fallback."""

    def __init__(self, cache_dir: str):
        self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)

    # ---- path helpers -----------------------------------------------
    def _pkl_path(self, name: str) -> str:
        return os.path.join(self.cache_dir, f"{name}.pkl")

    def _json_path(self, name: str) -> str:
        return os.path.join(self.cache_dir, f"{name}.json")

    # ---- core API ------------------------------------------------------
    def set(self, name: str, data: Any, meta: dict | None = None) -> bool:
        """
        Save `data` under `name`. Wraps it with a small envelope (saved_at,
        meta) so consumers can check freshness without re-deriving it.
        Returns True on success, False on failure (never raises).
        """
        envelope = {
            "saved_at": time.time(),
            "meta": meta or {},
            "data": data,
        }
        path = self._pkl_path(name)
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=self.cache_dir, prefix=f".{name}.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "wb") as f:
                    pickle.dump(envelope, f, protocol=pickle.HIGHEST_PROTOCOL)
                os.replace(tmp_path, path)
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
            logger.info("cache '%s' saved (%s)", name, path)
            return True
        except Exception as e:
            logger.error("cache '%s' save failed: %s", name, e)
            return False

    def get(self, name: str, default: Any = None) -> Any:
        """Return only the payload previously passed to set(), or `default`."""
        envelope = self._get_envelope(name)
        if envelope is None:
            return default
        return envelope.get("data", default)

    def get_meta(self, name: str) -> dict:
        """Return the meta dict saved alongside the data, or {} if none."""
        envelope = self._get_envelope(name)
        if envelope is None:
            return {}
        return envelope.get("meta", {})

    def get_saved_at(self, name: str) -> float | None:
        envelope = self._get_envelope(name)
        if envelope is None:
            return None
        return envelope.get("saved_at")

    def exists(self, name: str) -> bool:
        return os.path.exists(self._pkl_path(name)) or os.path.exists(self._json_path(name))

    def delete(self, name: str) -> None:
        for path in (self._pkl_path(name), self._json_path(name)):
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError as e:
                    logger.warning("cache '%s' delete failed for %s: %s", name, path, e)

    # ---- internal --------------------------------------------------
    def _get_envelope(self, name: str) -> dict | None:
        pkl_path = self._pkl_path(name)
        if os.path.exists(pkl_path):
            try:
                with open(pkl_path, "rb") as f:
                    envelope = pickle.load(f)
                if isinstance(envelope, dict) and "data" in envelope:
                    return envelope
                # Pickle file exists but isn't in our envelope shape (e.g. a
                # raw dict saved by older code) — treat the whole thing as
                # the payload so old caches keep working during migration.
                return {"saved_at": None, "meta": {}, "data": envelope}
            except Exception as e:
                logger.error("cache '%s' pickle read failed, trying JSON fallback: %s", name, e)
        return self._get_envelope_json_fallback(name)

    def _get_envelope_json_fallback(self, name: str) -> dict | None:
        json_path = self._json_path(name)
        if not os.path.exists(json_path):
            return None
        try:
            with open(json_path, encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict) and "data" in raw and "saved_at" in raw:
                return raw
            return {"saved_at": None, "meta": {}, "data": raw}
        except Exception as e:
            logger.error("cache '%s' JSON fallback read failed: %s", name, e)
            return None


# ---------------------------------------------------------------------
# Module-level singleton — every engine/command module should import and
# reuse this instance instead of constructing its own CacheManager, so
# there's really only ever one shared cache directory for the whole app.
# ---------------------------------------------------------------------
_DEFAULT_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cache"
)
cache_manager = CacheManager(_DEFAULT_CACHE_DIR)
