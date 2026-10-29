"""BSJP Lane1 message: gate closed hides picks (logged); gate open shows picks + guide."""
import engine.bsjp2 as bsjp2
import engine.ihsg_gate as gate

PICK = {"ticker": "AAA", "tier": "TIER3", "rung": "MIDDLE", "rocket": None, "close_d1": 1000, "ret_2d_cum": 6.0,
        "tp1": 1050, "tp2": 1100, "sl": 950}


def _setup(monkeypatch, tmp_path, is_open):
    monkeypatch.setattr(bsjp2, "_load_json_state", lambda *_a, **_k: {"picks": [dict(PICK)]})
    monkeypatch.setattr(bsjp2, "LADDER_TIERS", ("TIER3",), raising=False)
    monkeypatch.setattr(gate, "get_gate_today", lambda: {"open": is_open, "reason": "tes", "date": "2026-10-29"})
    log = tmp_path / "d.jsonl"
    monkeypatch.setattr(gate.log_dropped, "__defaults__", (str(log),))
    return log


def test_closed_hides_and_logs(monkeypatch, tmp_path):
    log = _setup(monkeypatch, tmp_path, False)
    msg = bsjp2.build_bsjp2_tp_message()
    assert "Gate IHSG tertutup" in msg and "AAA" not in msg
    assert "AAA" in log.read_text()


def test_open_shows_picks_and_guide(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, True)
    msg = bsjp2.build_bsjp2_tp_message()
    assert "AAA" in msg and "📖 Entry" in msg and "Tahan: 2 hari" in msg
