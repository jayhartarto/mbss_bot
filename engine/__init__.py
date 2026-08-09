"""MBSS v2 engine package (CacheManager, NightlyEngine, MarketContextEngine, BrokerEngine, GPTPickEngine).

Sprint 1 status: only `cache.py` (CacheManager) exists as its own module so
far. The rest of the pipeline (nightly scan, market context, broker
enrichment, GPT scoring, and all Telegram command handlers) still lives in
`legacy_core.py`, imported wholesale by bot.py. Those get split into their
own files in later phases of the refactor — see Executive Summary.
"""
