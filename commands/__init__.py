"""MBSS v2 Command Layer package — thin Telegram handlers grouped by theme.

Sprint 1 status: only `scan.py` (scan/screening group: /screendaytrade,
/gptpick, /executiongate, /eodscan) exists so far. The rest of the ~30
registered handlers still live in engine/legacy_core.py and get split into
their own commands/*.py files in later phases — see README_REFACTOR.md.
"""
