"""
engine/broksum_read.py — MBSS v2 (2026-10-21).

Reads Brights/Stockbit-style "Broker Sum" screenshots (Net mode, Domestic and Foreign tabs) and tells whether the
broker flow STRENGTHENS or WEAKENS a pick. Deliberately a flag, never a score: the only measured effect so far is
that heavy XL (Stockbit) net BUYING predicts a worse next week (research/broker_flow_history.sqlite backtest,
18 weeks: XL net >= +1% of value -> -1.02%/week vs rest, t -3.9; XL net <= -1% -> +0.56%, t +1.5, not significant).
Institutional net (CC/AK/YU) showed no effect, so it is shown as context only.

Screenshot caveats built into the maths:
  * Brights' displayed lot columns are NOT additive with the header values (observed 2.9-3.5x too large vs the API),
    so per-broker values (lot x avg x 100) are rescaled per tab so the visible rows add up to the tab's header net.
  * Only the top ~7 rows per side are visible; a broker outside them is treated as ~0 (noted in the output).
  * Window is whatever the user selected; the backtest used 5 trading days. Shares of traded value are length-free
    but the evidence is for ~5-day windows.
"""
from __future__ import annotations

import hashlib
import json
import re

import engine.broker_roles as roles
import engine.legacy_core as core

FALLBACK_LOT_SCALE = 0.34  # 1 / ~2.9, used only when the header cannot calibrate a tab


def _prompt(ticker: str) -> str:
    return f"""
You are reading a screenshot of an Indonesian trading app's "Broker Sum" screen for ticker {ticker}.
Return ONLY valid JSON, no markdown, in exactly this shape:
{{
  "success": true or false,
  "reason_if_failed": null or "short reason",
  "ticker_visible": "ticker shown" or null,
  "tab": "All" or "Domestic" or "Foreign" or null,
  "net_mode": true or false or null,
  "date_range_text": "text next to the calendar icon" or null,
  "header": {{"buy_value_idr": number or null, "sell_value_idr": number or null, "net_value_idr": number or null}},
  "buy_column": [{{"code": "XX", "lot": number, "avg": number or null}}, ...],
  "sell_column": [{{"code": "XX", "lot": number, "avg": number or null}}, ...]
}}

Rules:
- "tab" is whichever of All / Domestic / Foreign is highlighted. "net_mode" is true when the Net toggle is ON.
- header = the Total Value box (Buy / Sell / Net, in Rupiah, NOT the volume box). Suffix K=1e3, M=1e6, B=1e9, T=1e12
  ("16.23 B" = 16230000000, "647.19 M" = 647190000). A leading "-" is a negative number.
- Each table row has: left = Vol(Lot) and Avg and the BUY broker code; right = the SELL broker code, Avg, Vol(Lot).
  buy_column = the left side rows top to bottom, sell_column = the right side rows top to bottom.
- "lot" = the volume number printed for that row converted with the same suffix rule ("1.19 M" = 1190000, "249.76 K" =
  249760). KEEP A MINUS SIGN if the app prints one (e.g. "-193.14 K" = -193140); never add or remove signs yourself.
- Read every visible row, including partially cut-off ones if the code and number are legible; otherwise skip them.
- "avg" = the Avg price printed in that row (a plain number), null if unreadable.
- success=false ONLY if this is not a Broker Sum screen, the ticker is not {ticker}, or the rows are illegible.
Do not guess numbers.
{roles.prompt_reference()}
"""


def extract_tab(image_bytes: bytes, mime_type: str, ticker: str) -> dict:
    """One screenshot -> raw dict (see _prompt). Never raises."""
    try:
        raw = core._gemini_image_text(image_bytes, mime_type, _prompt(ticker)).strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        out = json.loads(raw)
        out["image_sha1"] = hashlib.sha1(image_bytes).hexdigest()
        return out
    except json.JSONDecodeError:
        return {"success": False, "reason_if_failed": "respons AI bukan JSON valid, coba screenshot yang lebih jelas"}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "reason_if_failed": f"error teknis: {str(e)[:100]}"}


def signed_rows(tab: dict) -> list[dict]:
    """Left column keeps the printed sign; right column is the sell side, so its sign is flipped (a printed
    positive 1.19M there is a net SELL of 1.19M; a printed negative there is a net BUY)."""
    rows = []
    for side, sign in (("buy_column", 1), ("sell_column", -1)):
        for r in tab.get(side) or []:
            try:
                lot = float(r.get("lot"))
            except (TypeError, ValueError):
                continue
            code = str(r.get("code") or "").upper().strip()
            if not code:
                continue
            avg = r.get("avg")
            avg = float(avg) if isinstance(avg, (int, float)) and avg > 0 else None
            rows.append({"code": code, "signed_lot": sign * lot, "avg": avg})
    return rows


def scale_tab(tab: dict) -> dict:
    """Per-broker net value in IDR for one tab, rescaled so visible rows add up to the header net."""
    rows = signed_rows(tab)
    est = [{**r, "est": r["signed_lot"] * (r["avg"] or 0) * 100} for r in rows]
    s = sum(r["est"] for r in est)
    hdr = (tab.get("header") or {}).get("net_value_idr")
    k, how = FALLBACK_LOT_SCALE, "fallback"
    if isinstance(hdr, (int, float)) and s != 0 and (hdr > 0) == (s > 0) and 0.05 <= hdr / s <= 1.5:
        k, how = hdr / s, "header"
    for r in est:
        r["value"] = r["est"] * k
    return {"rows": est, "scale": k, "scale_source": how, "visible_sum_est": s}


def assess(tabs: dict, ticker: str, ret5: float | None = None) -> dict:
    """tabs = {"Domestic": extracted, "Foreign": extracted(optional)}. ret5 = the stock's own last-5-day return
    in percent (optional context). Returns the structured reading; use format_message() to print it."""
    dom = tabs.get("Domestic")
    if not dom:
        return {"ok": False, "reason": "tab Domestic belum ada — XL hanya terbaca di tab Domestic"}
    d = scale_tab(dom)
    f = scale_tab(tabs["Foreign"]) if tabs.get("Foreign") else None
    dh = dom.get("header") or {}
    notes = []
    if f is not None:
        fh = tabs["Foreign"].get("header") or {}
        V = (dh.get("buy_value_idr") or 0) + (fh.get("buy_value_idr") or 0)
    else:
        V = max(dh.get("buy_value_idr") or 0, dh.get("sell_value_idr") or 0)
        notes.append("tab Foreign tidak ada: nilai transaksi pasar diperkirakan dari tab Domestic (cenderung terlalu kecil → % XL terlihat lebih besar)")
    if not V:
        return {"ok": False, "reason": "nilai total (header Buy/Sell) tidak terbaca"}
    if d["scale_source"] == "fallback":
        notes.append("kalibrasi lot dari header gagal; memakai faktor tetap ~1/2.9")
    allrows = d["rows"] + (f["rows"] if f else [])
    by_code: dict[str, float] = {}
    for r in allrows:
        by_code[r["code"]] = by_code.get(r["code"], 0) + r["value"]
    xl = by_code.get("XL")
    if xl is None:
        xl_val, xl_seen = 0.0, False
        notes.append("XL tidak masuk baris yang terlihat (dianggap ~0)")
    else:
        xl_val, xl_seen = xl, True
    n_xl = xl_val / V
    role_net: dict[str, float] = {}
    for code, v in by_code.items():
        role_net[roles.role_of(code)] = role_net.get(roles.role_of(code), 0) + v
    role_pct = {k: v / V for k, v in role_net.items()}
    retail_app = sum(by_code.get(c, 0) for c in roles.RETAIL_APP_CODES) / V

    if n_xl >= roles.XL_CROWD_BUY_STRONG:
        verdict, tag = "MELEMAHKAN (kuat)", "⚠️⚠️"
    elif n_xl >= roles.XL_CROWD_BUY:
        verdict, tag = "MELEMAHKAN", "⚠️"
    elif n_xl <= roles.XL_SELL:
        verdict, tag = "SEDIKIT MENDUKUNG (bukti lemah)", "🟢"
    else:
        verdict, tag = "NETRAL", "➖"
    chasing = ret5 is not None and ret5 >= 5 and n_xl >= roles.XL_CROWD_BUY
    return {"ok": True, "ticker": ticker, "verdict": verdict, "tag": tag, "n_xl": n_xl, "xl_value": xl_val, "xl_seen": xl_seen,
            "V": V, "retail_app_net_pct": retail_app, "role_pct": role_pct, "by_code": by_code, "ret5": ret5, "chasing": chasing,
            "scale": {"Domestic": d["scale"], "Foreign": f["scale"] if f else None}, "notes": notes,
            "window": dom.get("date_range_text"), "has_foreign_tab": f is not None}


def _idr(v: float) -> str:
    a = abs(v)
    s = f"{a/1e12:.2f}T" if a >= 1e12 else f"{a/1e9:.2f}B" if a >= 1e9 else f"{a/1e6:.0f}M"
    return ("-" if v < 0 else "+") + "Rp" + s


def format_message(a: dict) -> str:
    if not a.get("ok"):
        return f"⚠️ Belum bisa menilai: {a.get('reason')}"
    L = [f"🕵️ BROKSUM READ — {a['ticker']}  ({a.get('window') or 'window tidak terbaca'})",
         f"{a['tag']} Putusan: {a['verdict']}",
         f"XL net: {_idr(a['xl_value'])} = {a['n_xl']*100:+.1f}% dari nilai transaksi"
         + ("" if a["xl_seen"] else " (tidak terlihat di top baris)")]
    if a["chasing"]:
        L.append(f"⚠️ Harga sudah naik {a['ret5']:+.1f}% dalam 5 hari DAN XL ikut mengejar — kombinasi yang paling lemah di backtest (IC −0,12).")
    L.append("")
    L.append("Dasar (backtest 18 pekan, ~270 saham/pekan, return 5 hari dikoreksi pasar):")
    L.append("• XL net beli ≥ +1% nilai → −1,0%/pekan vs sisanya (t −3,9, negatif di 13/18 pekan)")
    L.append("• XL net jual ≤ −1% → +0,6%/pekan (t +1,5, BELUM signifikan)")
    L.append("Ini peringatan, bukan skor: tidak mengubah ranking pick.")
    fam = a["retail_app_net_pct"]
    if fam >= 0.02 and not a["verdict"].startswith("MELEMAHKAN"):
        L.append(f"ℹ️ Ritel gabungan XL+XC+PD net beli {fam*100:+.1f}% nilai. Gabungan ini BELUM teruji (XC dan PD tidak ada di data backtest), "
                 "jadi tidak dipakai dalam putusan; dicatat untuk diuji setelah data harian terkumpul.")
    L.append("")
    L.append("Konteks (belum terbukti punya efek, hanya info):")
    order = ["gov_inst", "foreign_inst", "foreign_retail_base", "retail_app", "scalper", "local_other"]
    for r in order:
        if r in a["role_pct"]:
            L.append(f"• {roles.ROLE_LABEL_ID[r]}: {a['role_pct'][r]*100:+.1f}% nilai")
    big = sorted(a["by_code"].items(), key=lambda kv: -abs(kv[1]))[:4]
    L.append("• Terbesar: " + ", ".join(f"{c} {_idr(v)}" for c, v in big))
    for n in a["notes"]:
        L.append(f"ℹ️ {n}")
    if not a["has_foreign_tab"]:
        L.append("ℹ️ Kirim juga tab Foreign untuk perhitungan yang lebih akurat.")
    L.append("Window ideal: 5 hari bursa (sama dengan backtest), mode Net.")
    return "\n".join(L)
