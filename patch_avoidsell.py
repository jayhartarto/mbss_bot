import sys

path = "commands/scan.py"
with open(path, encoding="utf-8") as f:
    content = f.read()

old = '''def _gptpick_candidate_filter(scoring: dict) -> bool:
    if not scoring:
        return False
    if scoring.get("is_financial_distress_flag"):
        return False
    if scoring.get("chart_pattern") == "lower_highs_bearish":
        return False
    if scoring.get("is_near_price_floor"):
        return False
    if _gptpick_num(scoring.get("value_traded"), 0.0) < GPTPICK_MIN_VALUE_TRADED_IDR:
        return False
    return True'''

new = '''def _gptpick_candidate_filter(scoring: dict) -> bool:
    if not scoring:
        return False
    if scoring.get("is_financial_distress_flag"):
        return False
    if scoring.get("chart_pattern") == "lower_highs_bearish":
        return False
    if scoring.get("is_near_price_floor"):
        return False
    # MBSS v2 Sprint 2 (Tier 1.4, lanjutan - user request, ditemukan lewat
    # kasus nyata COCO): AVOID_SELL adalah rank TERENDAH di seluruh sistem
    # (ACTION_RANK = {"AVOID_SELL": 0, ...}, "HINDARI / JUAL") - sudah
    # diperlakukan sebagai sinyal exit/disqualifying di tempat lain (lihat
    # EXIT_CANDIDATE priority classification). Sebelumnya cuma kena penalti
    # kecil (-1.5) di _gptpick_penalty, yang bisa "kalah" dari faktor lain
    # yang kuat - terbukti nyata: COCO tetap masuk top-3 dengan MED-HIGH
    # walau statusnya sendiri HINDARI/JUAL. Sekarang dikeluarkan total dari
    # kandidat, bukan sekadar dikurangi skornya.
    if scoring.get("action_id") == "AVOID_SELL":
        return False
    if _gptpick_num(scoring.get("value_traded"), 0.0) < GPTPICK_MIN_VALUE_TRADED_IDR:
        return False
    return True'''

count = content.count(old)
if count != 1:
    print(f"GAGAL: blok ditemukan {count}x (harus 1x) - file Anda beda dari yang diharapkan. Patch DIBATALKAN.")
    sys.exit(1)

content = content.replace(old, new)
with open(path, "w", encoding="utf-8") as f:
    f.write(content)
print("Patch berhasil diterapkan ke commands/scan.py")
