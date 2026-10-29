"""engine/ipot_broker.collect_days stops early when the endpoint errors for everything (IPOT answered 502 from 2026-10-23)."""
import engine.ipot_broker as ib


def _run(tmp_path, monkeypatch, results):
    calls = []

    def fake_fetch(t, d, fd, bd, attempts=5, block_wait=30):
        calls.append(t)
        return results(t)

    monkeypatch.setattr(ib, "fetch", fake_fetch)
    tickers = [f"T{i:03d}" for i in range(200)]
    out = ib.collect_days(["2026-10-28"], tickers, variants=(("all", "all"),), workers=2, db_path=str(tmp_path / "t.sqlite"))
    return out, calls


def test_dead_endpoint_stops_early(tmp_path, monkeypatch):
    out, calls = _run(tmp_path, monkeypatch, lambda t: ("error", [], [], None))
    assert len(calls) < 40                      # 200 queued, the breaker cuts the run after a handful
    assert out["ok"] == 0 and out["skipped"] > 150


def test_sporadic_errors_do_not_trip(tmp_path, monkeypatch):
    def res(t):
        return ("error", [], [], None) if int(t[1:]) % 3 == 0 else ("empty", [], [], None)
    out, calls = _run(tmp_path, monkeypatch, res)
    assert len(calls) == 200 and out["skipped"] == 0
