"""
commands/portfolio.py — Command Layer: portfolio group (MBSS v2 Sprint 1, Phase 5c)

Telegram handlers for /buy, /sell, /addcash, /withdrawcash, /resetportfolio,
/setentrydate, /watchlist, /summary, /order, /myportfolio (+ /portofolio
alias), plus the order-clear and screendaytrade-brokersum-select inline
button callbacks.

All thin handlers — the actual portfolio state (portfolio.json read/write),
scoring, lifecycle classification, and reasoning synthesis all stay in
engine/legacy_core.py, accessed via `core.xxx`. Same circular-import rule
as every other Command Layer module in this refactor — see
engine/nightly.py's docstring for the full explanation.
"""
from __future__ import annotations

import asyncio
import copy
import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import engine.legacy_core as core
import engine.broker as broker_engine
import engine.scoring as scoring_engine

MAX_BROKERSUM_PER_RUN = 5  # confirmed hard daily cap on Index Alpha's free tier, 1 call/ticker


def build_ticker_shortcut_keyboard(tickers: list, columns: int = 4) -> InlineKeyboardMarkup:
    """
    Grid tombol shortcut per ticker — dipakai di /summary untuk cek cepat tanpa
    ketik manual. callback_data dibatasi 64 byte oleh Telegram, format
    "qchk_TICKER" jauh di bawah itu jadi aman.
    """
    buttons = [InlineKeyboardButton(t, callback_data=f"qchk_{t}") for t in tickers]
    rows = [buttons[i:i + columns] for i in range(0, len(buttons), columns)]
    return InlineKeyboardMarkup(rows)


async def order_clear_callback(update, context):
    query = update.callback_query
    await query.answer()
    ticker = query.data.replace("orderclear_", "")
    orders = core.load_pending_orders()
    orders = [o for o in orders if o["ticker"] != ticker]
    core.save_pending_orders(orders)
    await query.message.reply_text(f"✅ Order {ticker} dihapus dari pemantauan.")


async def select_screendaytrade_brokersum(update, context):
    """
    Callback tombol Broker Summary dari /screendaytrade.
    Setelah user tap ticker, foto berikutnya akan dibaca sebagai Broker Summary ALL 3 hari.
    """
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    if not data.startswith("bsdt_"):
        return

    ticker = data.replace("bsdt_", "").upper().strip()
    if not ticker:
        await query.edit_message_text("⚠️ Ticker tidak valid.")
        return

    core.PENDING_BROKERSUM_CHECKS[query.message.chat_id] = {
        "ticker": ticker,
        "expires_at": datetime.datetime.now(core.WIB) + datetime.timedelta(minutes=core.PENDING_BROKERSUM_TIMEOUT_MINUTES),
        "source": "screendaytrade",
        "mode": "all_3d_net",
    }

    await query.edit_message_text(
        f"📸 Kirim screenshot Broker Summary untuk {ticker}\\n\\n"
        f"Format yang diminta:\\n"
        f"• Tab: ALL\\n"
        f"• Periode: 3 hari bursa\\n"
        f"• Mode: Net aktif\\n"
        f"• Pastikan kode broker, buy/sell, lot/value, dan average price terlihat.\\n\\n"
        f"Bot akan membaca arah akumulasi/distribusi dan memberi Smart Money Confirmation."
    )


async def batch_buy_position(update, context):
    """
    /batchbuy — MBSS v2 (user request, real case: entry 4 pick sekaligus
    hari yang sama — AADI/CPIN/RAJA/SMGR — satu-satu lewat /buy kelamaan).
    Satu posisi per baris, format PERSIS sama dengan /buy (TICKER HARGA
    LOT), cuma dikirim sekaligus lewat newline, bukan argumen command.
    REUSE core.add_position() langsung, TIDAK ada logic baru — cuma loop
    tipis di atas fungsi yang sudah ada, per baris independen (satu baris
    gagal tidak menggagalkan baris lain).
    """
    text = update.message.text or ""
    lines = [l.strip() for l in text.split("\n")[1:] if l.strip()]
    if not lines:
        await core.safe_reply(update.message,
            "Cara pakai (satu posisi per baris):\n"
            "/batchbuy\n"
            "TICKER HARGA LOT\n"
            "TICKER HARGA LOT\n\n"
            "Contoh:\n"
            "/batchbuy\n"
            "AADI 7200 10\n"
            "CPIN 5450 5\n"
            "RAJA 850 20\n"
            "SMGR 3200 8"
        )
        return

    results = []
    for line in lines:
        parts = line.split()
        if len(parts) != 3:
            results.append(f"⚠️ \"{line}\" — format salah, butuh TICKER HARGA LOT")
            continue
        ticker = parts[0].upper().strip()
        try:
            price = float(parts[1])
            lots = int(parts[2])
            if price <= 0 or lots <= 0:
                raise ValueError
        except ValueError:
            results.append(f"⚠️ {ticker} — harga/lot harus angka positif")
            continue
        success, error_message, position = core.add_position(ticker, price, lots)
        if success:
            results.append(f"✅ {ticker}: {position['lots']} lot @ avg Rp{position['avg_price']:,.0f}")
        else:
            results.append(f"⚠️ {ticker}: {error_message}")

    results.append(f"\nCash tersisa: Rp{core.get_cash_balance():,.0f}")
    await core.safe_reply(update.message, "\n".join(results))


async def batch_sell_position(update, context):
    """
    /batchsell — pasangan /batchbuy, format PERSIS /sell (TICKER LOT HARGA)
    per baris. REUSE core.reduce_position() langsung.
    """
    text = update.message.text or ""
    lines = [l.strip() for l in text.split("\n")[1:] if l.strip()]
    if not lines:
        await core.safe_reply(update.message,
            "Cara pakai (satu posisi per baris):\n"
            "/batchsell\n"
            "TICKER LOT HARGA\n"
            "TICKER LOT HARGA\n\n"
            "Contoh:\n"
            "/batchsell\n"
            "AADI 10 7250\n"
            "CPIN 5 5500"
        )
        return

    results = []
    for line in lines:
        parts = line.split()
        if len(parts) != 3:
            results.append(f"⚠️ \"{line}\" — format salah, butuh TICKER LOT HARGA")
            continue
        ticker = parts[0].upper().strip()
        try:
            lots = int(parts[1])
            sell_price = float(parts[2])
            if lots <= 0 or sell_price <= 0:
                raise ValueError
        except ValueError:
            results.append(f"⚠️ {ticker} — lot/harga harus angka positif")
            continue
        success, message = core.reduce_position(ticker, lots, sell_price)
        results.append(("✅ " if success else "⚠️ ") + f"{ticker}: {message}")

    await core.safe_reply(update.message, "\n".join(results))


async def buy_position(update, context):
    if len(context.args) < 3:
        await core.safe_reply(update.message,
            "Cara pakai: /buy TICKER HARGA LOT\nContoh: /buy TLKM 2850 10"
        )
        return
    ticker = context.args[0].upper().strip()
    try:
        price = float(context.args[1])
        lots = int(context.args[2])
        if price <= 0 or lots <= 0:
            raise ValueError
    except ValueError:
        await core.safe_reply(update.message, "⚠️ Harga dan lot harus angka positif.")
        return

    success, error_message, position = core.add_position(ticker, price, lots)
    if not success:
        await core.safe_reply(update.message, error_message)
        return
    await core.safe_reply(update.message,
        f"✅ Tercatat: {ticker}\n"
        f"Total: {position['lots']} lot @ avg Rp{position['avg_price']:,.0f}\n"
        f"Cash tersisa: Rp{core.get_cash_balance():,.0f}"
    )


async def sell_position(update, context):
    if len(context.args) < 3:
        await core.safe_reply(update.message,
            "Cara pakai: /sell TICKER LOT HARGA\nContoh: /sell TLKM 5 2850"
        )
        return
    ticker = context.args[0].upper().strip()
    try:
        lots = int(context.args[1])
        sell_price = float(context.args[2])
        if lots <= 0 or sell_price <= 0:
            raise ValueError
    except ValueError:
        await core.safe_reply(update.message, "⚠️ Lot dan harga harus angka positif.")
        return

    success, message = core.reduce_position(ticker, lots, sell_price)
    await core.safe_reply(update.message, ("✅ " if success else "⚠️ ") + message)


async def add_cash_command(update, context):
    if len(context.args) < 1:
        await core.safe_reply(update.message, "Cara pakai: /addcash JUMLAH\nContoh: /addcash 5000000")
        return
    try:
        amount = float(context.args[0])
        if amount <= 0:
            raise ValueError
    except ValueError:
        await core.safe_reply(update.message, "⚠️ Jumlah harus angka positif.")
        return
    new_balance = core.add_cash(amount)
    await core.safe_reply(update.message, f"✅ Cash ditambahkan: Rp{amount:,.0f}\nCash sekarang: Rp{new_balance:,.0f}")


async def withdraw_cash_command(update, context):
    if len(context.args) < 1:
        await core.safe_reply(update.message, "Cara pakai: /withdrawcash JUMLAH\nContoh: /withdrawcash 1000000")
        return
    try:
        amount = float(context.args[0])
        if amount <= 0:
            raise ValueError
    except ValueError:
        await core.safe_reply(update.message, "⚠️ Jumlah harus angka positif.")
        return
    success, message, _ = core.withdraw_cash(amount)
    await core.safe_reply(update.message, ("✅ " if success else "⚠️ ") + message)


async def reset_portfolio_command(update, context):
    """
    Wipes ALL positions, cash, and realized P&L history back to empty — a safety
    net for a botched manual entry. Requires explicit confirmation since this is
    destructive and not reversible (no undo, no backup kept).
    """
    if not context.args or context.args[0].lower() != "confirm":
        portfolio = core.load_portfolio()
        num_positions = len(portfolio.get("positions", {}))
        cash = portfolio.get("cash", 0.0)
        await core.safe_reply(
            update.message,
            f"⚠️ Ini akan MENGHAPUS SEMUA data portofolio:\n"
            f"- {num_positions} posisi saham\n"
            f"- Cash: Rp{cash:,.0f}\n"
            f"- Seluruh riwayat realized P&L\n\n"
            f"Tindakan ini TIDAK BISA DIBATALKAN.\n\n"
            f"Jika yakin, ketik: /resetportfolio confirm"
        )
        return

    core.save_portfolio(copy.deepcopy(core.PORTFOLIO_SCHEMA_DEFAULT))
    await core.safe_reply(update.message, "✅ Portofolio telah direset total — posisi, cash, dan riwayat P&L kosong kembali.")


async def set_entry_date_command(update, context):
    if len(context.args) < 2:
        await core.safe_reply(update.message,
            "Cara pakai: /setentrydate TICKER YYYY-MM-DD\nContoh: /setentrydate TLKM 2026-07-08"
        )
        return
    ticker = context.args[0].upper().strip()
    date_str = context.args[1].strip()
    success, message = core.set_entry_date(ticker, date_str)
    await core.safe_reply(update.message, ("✅ " if success else "⚠️ ") + message)


async def watchlist_command(update, context):
    """
    /watchlist — view current watchlist
    /watchlist add TICKER — add (max 3)
    /watchlist remove TICKER — remove
    """
    portfolio = core.load_portfolio()
    watchlist = portfolio.get("watchlist", [])

    if not context.args:
        if not watchlist:
            await core.safe_reply(update.message, f"📋 Watchlist kosong (maks {core.WATCHLIST_MAX_SIZE} saham).\nTambah dengan: /watchlist add TICKER")
        else:
            await core.safe_reply(update.message, f"📋 Watchlist ({len(watchlist)}/{core.WATCHLIST_MAX_SIZE}): {', '.join(watchlist)}")
        return

    action = context.args[0].lower()
    if action == "add" and len(context.args) >= 2:
        ticker = context.args[1].upper().strip()
        success, message = core.add_to_watchlist(ticker)
        await core.safe_reply(update.message, ("✅ " if success else "⚠️ ") + message)
    elif action == "remove" and len(context.args) >= 2:
        ticker = context.args[1].upper().strip()
        success, message = core.remove_from_watchlist(ticker)
        await core.safe_reply(update.message, ("✅ " if success else "⚠️ ") + message)
    else:
        await core.safe_reply(update.message,
            "Cara pakai:\n/watchlist — lihat watchlist\n/watchlist add TICKER — tambah\n/watchlist remove TICKER — hapus"
        )


async def order_command(update, context):
    """
    /order buy TICKER LOT HARGA — catat order beli yang DIPANTAU (bukan posisi
    yang sudah terisi — untuk itu pakai /buy). /order sell TICKER LOT HARGA —
    sama untuk order jual. /order (tanpa argumen) — lihat semua order yang
    dipantau. /order clear TICKER — hapus dari pemantauan (setelah dicek
    manual di app broker bahwa order sudah match/dibatalkan).
    """
    if not context.args:
        orders = core.load_pending_orders()
        if not orders:
            await core.safe_reply(update.message, "📋 Tidak ada order yang dipantau.\nTambah dengan: /order buy TICKER LOT HARGA")
            return
        lines = ["📋 ORDER YANG DIPANTAU\n"]
        for o in orders:
            side_label = "BELI" if o["side"] == "buy" else "JUAL"
            lines.append(f"{o['ticker']} {side_label} {o['lot']} lot @ {o['price']} (ditambahkan {o['date_added']})")
        await core.safe_reply(update.message, "\n".join(lines))
        return

    subcommand = context.args[0].lower()

    if subcommand == "clear":
        if len(context.args) < 2:
            await core.safe_reply(update.message, "Cara pakai: /order clear TICKER")
            return
        ticker = context.args[1].upper()
        orders = core.load_pending_orders()
        before = len(orders)
        orders = [o for o in orders if o["ticker"] != ticker]
        core.save_pending_orders(orders)
        removed = before - len(orders)
        await core.safe_reply(update.message, f"✅ {removed} order {ticker} dihapus dari pemantauan." if removed else f"Tidak ada order {ticker} yang dipantau.")
        return

    if subcommand not in ("buy", "sell"):
        await core.safe_reply(update.message, "Cara pakai:\n/order buy TICKER LOT HARGA\n/order sell TICKER LOT HARGA\n/order clear TICKER\n/order (lihat semua)")
        return

    if len(context.args) < 4:
        await core.safe_reply(update.message, f"Cara pakai: /order {subcommand} TICKER LOT HARGA\nContoh: /order {subcommand} ANTM 10 3050")
        return

    ticker = context.args[1].upper()
    try:
        lot = int(context.args[2])
        price = float(context.args[3])
    except ValueError:
        await core.safe_reply(update.message, "Lot dan harga harus berupa angka.")
        return

    orders = core.load_pending_orders()
    orders.append({
        "ticker": ticker, "side": subcommand, "lot": lot, "price": price,
        "date_added": core.get_current_trading_day_close_marker(),
    })
    core.save_pending_orders(orders)
    side_label = "BELI" if subcommand == "buy" else "JUAL"
    await core.safe_reply(update.message, f"✅ Order {side_label} {ticker} {lot} lot @ {price} ditambahkan ke pemantauan.")


async def portfolio_summary(update, context):
    """
    Fast, lightweight summary — live price only (no full 500-bar scoring, no Gemini
    call), just current value + P&L. Meant for quick check-ins, not analysis.
    """
    portfolio = core.load_portfolio()
    positions = portfolio.get("positions", {})
    cash = portfolio.get("cash", 0.0)

    if not positions:
        await core.safe_reply(update.message, f"Portofolio masih kosong.\nCash tersedia: Rp{cash:,.0f}")
        return

    await core.safe_reply(update.message, "🔎 Mengambil harga terkini...")

    total_stock_value = 0.0
    total_cost_basis = 0.0
    lines = []

    for ticker, pos in positions.items():
        try:
            quote = await asyncio.to_thread(core.itick_get_quote, ticker)
        except Exception as e:
            print(f"Error fetching quote for {ticker} summary: {e}")
            quote = None

        avg_price = pos["avg_price"]
        lots = pos["lots"]
        shares = lots * core.BOARD_LOT_SIZE
        cost_basis = avg_price * shares
        total_cost_basis += cost_basis

        if quote and quote.get("ld"):
            current_price = quote["ld"]
            market_value = current_price * shares
            pnl_pct = ((current_price - avg_price) / avg_price) * 100
            total_stock_value += market_value
            lines.append(f"{ticker}: {lots} lot, Rp{market_value:,.0f} ({pnl_pct:+.1f}%)")
        else:
            # Fall back to cost basis if live quote fails, clearly marked as such
            total_stock_value += cost_basis
            lines.append(f"{ticker}: {lots} lot, Rp{cost_basis:,.0f} (harga live gagal, pakai avg cost)")

    total_unrealized_pnl = total_stock_value - total_cost_basis
    total_unrealized_pct = (total_unrealized_pnl / total_cost_basis * 100) if total_cost_basis > 0 else 0
    total_wealth = total_stock_value + cash

    realized_log = portfolio.get("realized_pnl_log", [])
    total_realized = sum(entry["pnl_idr"] for entry in realized_log)

    position_lines = "\n".join(lines)
    watchlist = portfolio.get("watchlist", [])
    all_tickers_for_buttons = list(positions.keys()) + [t for t in watchlist if t not in positions]

    keyboard = build_ticker_shortcut_keyboard(all_tickers_for_buttons) if all_tickers_for_buttons else None
    await core.safe_reply(
        update.message,
        f"📊 RINGKASAN PORTOFOLIO\n\n"
        f"{position_lines}\n\n"
        f"Total Saham: Rp{total_stock_value:,.0f}\n"
        f"Cash: Rp{cash:,.0f}\n"
        f"─────────────────\n"
        f"Total Kekayaan: Rp{total_wealth:,.0f}\n\n"
        f"Unrealized P&L: Rp{total_unrealized_pnl:,.0f} ({total_unrealized_pct:+.1f}%)\n"
        f"Realized P&L (semua waktu): Rp{total_realized:,.0f}"
    )
    if keyboard:
        await update.message.reply_text("🔍 Cek cepat:", reply_markup=keyboard)

    # Segmen order tracking — pelengkap kedua di /summary.
    pending_orders = core.load_pending_orders()
    if not pending_orders:
        await update.message.reply_text(
            "Ada order untuk saham Anda?\n"
            "Tambahkan dengan /order buy TICKER LOT HARGA atau /order sell TICKER LOT HARGA"
        )
    else:
        order_lines = ["📋 ORDER TRACKING (perkiraan, cek app broker untuk pastikan)\n"]
        order_buttons = []
        for o in pending_orders:
            try:
                quote = await asyncio.to_thread(core.itick_get_quote, o["ticker"])
            except Exception:
                quote = None
            status = core.check_order_touch_status(o, quote)
            side_label = "BELI" if o["side"] == "buy" else "JUAL"
            if status["touched"] is True:
                status_str = f"🟡 Kemungkinan sudah match — {status['note']}"
            elif status["touched"] is False:
                status_str = f"⚪ Belum tersentuh — {status['note']}"
            else:
                status_str = f"❔ {status['note']}"
            order_lines.append(f"{o['ticker']} {side_label} {o['lot']} lot @ {o['price']}\n{status_str}")
            order_buttons.append(InlineKeyboardButton(f"✅ Clear {o['ticker']}", callback_data=f"orderclear_{o['ticker']}"))

        order_keyboard = InlineKeyboardMarkup([order_buttons[i:i + 2] for i in range(0, len(order_buttons), 2)])
        await update.message.reply_text("\n\n".join(order_lines), reply_markup=order_keyboard)


async def my_portfolio(update, context):
    portfolio = core.load_portfolio()
    positions = portfolio.get("positions", {})
    cash = portfolio.get("cash", 0.0)
    watchlist = portfolio.get("watchlist", [])
    include_brokersum = len(context.args) > 0 and context.args[0].lower() == "brokersum"
    use_zapi_brokersum = (
        include_brokersum and len(context.args) > 1 and context.args[1].lower() == "zapi"
    )
    explicit_brokersum_tickers = (
        {t.upper() for t in context.args[1:]}
        if include_brokersum and len(context.args) > 1 and not use_zapi_brokersum
        else None
    )

    if not positions and not watchlist:
        await core.safe_reply(update.message,
            "Portofolio masih kosong. Tambahkan posisi dengan:\n/buy TICKER HARGA LOT\n"
            "Atau tambahkan watchlist dengan:\n/watchlist add TICKER\n\n"
            f"Cash tersedia: Rp{cash:,.0f}"
        )
        return

    total_tickers = len(positions) + len(watchlist)
    await core.safe_reply(update.message, f"🔎 Menganalisa {total_tickers} posisi/watchlist, mohon tunggu...")

    positions_data = []
    watchlist_data = []
    sector_value = {}
    total_stock_value = 0.0
    missing_entry_date_tickers = []

    # PASS 1: full technical scoring for everyone. Chunked dengan cooldown —
    # compute_factor_scoring di sini pakai include_quote_check=True (default),
    # artinya 2 request iTick per ticker (kline+quote), BUKAN 1 seperti bulk
    # scan. Chunk size dibagi setengah dari ITICK_CHUNK_SIZE supaya total
    # request per chunk (ticker x 2) tetap di bawah ~12 rolling-window limit
    # yang sudah dikonfirmasi — sebelumnya loop ini TIDAK PUNYA pacing sama
    # sekali, menyebabkan semua ticker setelah ~6 gagal rate-limit begitu
    # portofolio+watchlist melebihi itu (ditemukan lewat log nyata: 13 ticker,
    # semua gagal "code=None, msg=None" setelah beberapa yang pertama berhasil).
    PORTFOLIO_SCORING_CHUNK_SIZE = max(1, core.ITICK_CHUNK_SIZE // 2)
    position_items = list(positions.items())

    for chunk_start in range(0, len(position_items), PORTFOLIO_SCORING_CHUNK_SIZE):
        chunk = position_items[chunk_start:chunk_start + PORTFOLIO_SCORING_CHUNK_SIZE]
        for ticker, pos in chunk:
            try:
                scoring = await asyncio.wait_for(asyncio.to_thread(scoring_engine.compute_factor_scoring, ticker), timeout=1800)
            except asyncio.TimeoutError:
                print(f"⏱️ Timed out fetching {ticker} for portfolio, skipping.")
                continue
            except Exception as e:
                print(f"Error scoring {ticker} for portfolio: {e}")
                continue
            if not scoring:
                continue

            current_price = scoring["price"]
            avg_price = pos["avg_price"]
            lots = pos["lots"]
            shares = lots * core.BOARD_LOT_SIZE
            market_value = current_price * shares
            unrealized_pnl_pct = ((current_price - avg_price) / avg_price) * 100
            unrealized_pnl_idr = (current_price - avg_price) * shares

            total_stock_value += market_value
            sector = scoring.get("sector", "N/A")
            sector_value[sector] = sector_value.get(sector, 0) + market_value

            if not pos.get("entry_date"):
                missing_entry_date_tickers.append(ticker)

            positions_data.append({
                **scoring,
                "avg_buy_price": avg_price,
                "lots": lots,
                "market_value_idr": int(market_value),
                "unrealized_pnl_pct": round(unrealized_pnl_pct, 2),
                "unrealized_pnl_idr": int(unrealized_pnl_idr),
                "_entry_date": pos.get("entry_date"),
            })
        is_last_chunk = (chunk_start + PORTFOLIO_SCORING_CHUNK_SIZE) >= len(position_items)
        if not is_last_chunk:
            print(f"⏳ /myportfolio: cooling down {core.ITICK_COOLDOWN_SECONDS}s sebelum chunk berikutnya "
                  f"({chunk_start + len(chunk)}/{len(position_items)} posisi selesai)...")
            await asyncio.sleep(core.ITICK_COOLDOWN_SECONDS)

    # Cooldown sebelum mulai watchlist — window rate-limit mungkin masih "panas"
    # dari chunk TERAKHIR loop positions di atas (yang sengaja tidak diberi
    # cooldown karena dikira sudah selesai), jadi perlu jeda dulu sebelum
    # menyambung ke request baru untuk watchlist.
    if position_items:
        print(f"⏳ /myportfolio: cooling down {core.ITICK_COOLDOWN_SECONDS}s sebelum mulai watchlist...")
        await asyncio.sleep(core.ITICK_COOLDOWN_SECONDS)

    for ticker in watchlist:
        try:
            scoring = await asyncio.wait_for(asyncio.to_thread(scoring_engine.compute_factor_scoring, ticker), timeout=1800)
        except asyncio.TimeoutError:
            print(f"⏱️ Timed out fetching watchlist {ticker}, skipping.")
            continue
        except Exception as e:
            print(f"Error scoring watchlist {ticker}: {e}")
            continue
        if not scoring:
            continue
        watchlist_data.append(scoring)

    if not positions_data and not watchlist_data:
        await core.safe_reply(update.message, "⚠️ Gagal mengambil data. Coba lagi nanti.")
        return

    # PASS 2: brokersum — before lifecycle classification, since it can adjust
    # final_score, which lifecycle classification depends on.
    if include_brokersum:
        combined = positions_data + watchlist_data

        if use_zapi_brokersum:
            # Mode Zapi: budget jauh lebih longgar (100/menit, ~10/hari rata2 aman
            # dari 300/bulan) — cakup SEMUA holdings+watchlist, bukan top-5 seperti
            # Index Alpha. Tetap ada batas pengaman kalau portofolio berkembang
            # jauh melebihi kebutuhan saat ini, supaya tidak habiskan kuota bulanan
            # dalam sehari tanpa sadar.
            MAX_ZAPI_BROKERSUM_SAFE = 15
            if len(combined) > MAX_ZAPI_BROKERSUM_SAFE:
                ranked = sorted(combined, key=lambda s: scoring_engine.compute_brokersum_priority(s, total_stock_value), reverse=True)
                selected = ranked[:MAX_ZAPI_BROKERSUM_SAFE]
                selection_note = (
                    f"⚠️ {len(combined)} ticker melebihi batas aman {MAX_ZAPI_BROKERSUM_SAFE} — "
                    f"auto-pilih prioritas tertinggi: {', '.join(s['ticker'] for s in selected)}"
                )
            else:
                selected = combined
                selection_note = f"SEMUA {len(selected)} posisi/watchlist tercover: {', '.join(s['ticker'] for s in selected)}"

            await core.safe_reply(update.message, f"📡 Brokersum via Zapi ({selection_note})...")

            for scoring in selected:
                ticker = scoring["ticker"]
                try:
                    brokersum = await asyncio.to_thread(
                        broker_engine.compute_brokersum_metrics_zapi, ticker, scoring.get("cmf"), scoring.get("obv_divergence")
                    )
                    if brokersum:
                        scoring["brokersum"] = brokersum
                        scoring_engine.apply_brokersum_adjustment(scoring, brokersum)
                        # Simpan ke cache harian yang sama — supaya /check TICKER
                        # bisa pakai ulang gratis. CATATAN: cache per-ticker, bukan
                        # per-sumber — kalau hari yang sama juga pernah pakai
                        # Index Alpha untuk ticker yang sama, yang terakhir dipanggil
                        # akan menimpa. Bukan masalah besar, tapi perlu disadari.
                        cache = broker_engine._load_brokersum_cache()
                        cache[ticker] = {"date": broker_engine.get_last_published_trading_day(), "data": brokersum}
                        broker_engine._save_brokersum_cache(cache)
                except Exception as e:
                    print(f"⚠️ Zapi brokersum fetch failed for {ticker}: {e}")
                await asyncio.sleep(0.5)  # jeda ringan, sopan santun — 100/menit sangat longgar

        elif explicit_brokersum_tickers:
            selected = [s for s in combined if s["ticker"] in explicit_brokersum_tickers]
            selection_note = f"dipilih manual: {', '.join(s['ticker'] for s in selected)}"

            await core.safe_reply(update.message, f"📡 Brokersum ({selection_note}) — pakai kuota Index Alpha terbatas...")

            for scoring in selected:
                ticker = scoring["ticker"]
                try:
                    brokersum = await asyncio.to_thread(
                        broker_engine.get_cached_or_fetch_brokersum, ticker, scoring.get("cmf"), scoring.get("obv_divergence")
                    )
                    if brokersum:
                        scoring["brokersum"] = brokersum
                        scoring_engine.apply_brokersum_adjustment(scoring, brokersum)
                except Exception as e:
                    print(f"⚠️ Brokersum fetch failed for {ticker}: {e}")
        else:
            ranked = sorted(combined, key=lambda s: scoring_engine.compute_brokersum_priority(s, total_stock_value), reverse=True)
            selected = ranked[:MAX_BROKERSUM_PER_RUN]
            selection_note = f"auto-pilih {len(selected)} prioritas tertinggi: {', '.join(s['ticker'] for s in selected)}"

            await core.safe_reply(update.message, f"📡 Brokersum ({selection_note}) — pakai kuota Index Alpha terbatas...")

            for scoring in selected:
                ticker = scoring["ticker"]
                try:
                    brokersum = await asyncio.to_thread(
                        broker_engine.get_cached_or_fetch_brokersum, ticker, scoring.get("cmf"), scoring.get("obv_divergence")
                    )
                    if brokersum:
                        scoring["brokersum"] = brokersum
                        scoring_engine.apply_brokersum_adjustment(scoring, brokersum)
                except Exception as e:
                    print(f"⚠️ Brokersum fetch failed for {ticker}: {e}")

    # PASS 3: lifecycle category + TP horizon — computed AFTER brokersum, so they
    # reflect the real, possibly-adjusted final score, not the pre-adjustment one.
    for scoring in positions_data:
        days_held = core.compute_trading_days_held(scoring.get("_entry_date"))
        scoring["_days_held"] = days_held
        scoring["_lifecycle"] = core.classify_lifecycle_category(days_held, scoring)
        scoring["_tp_horizon"] = core.estimate_tp_horizon(scoring)

    for scoring in watchlist_data:
        scoring["_tp_horizon"] = core.estimate_tp_horizon(scoring)

    # PASS 3b: live active breakout for held positions/watchlist during market hours.
    # This makes /myportfolio actionable for session-2 decisions: take profit, hold,
    # wait for breakout, or tighten stop if price loses VWAP/invalidation.
    if core.get_current_idx_session() is not None:
        for scoring in positions_data + watchlist_data:
            try:
                scoring["active_breakout"] = await asyncio.to_thread(
                    core.compute_active_breakout_score, scoring["ticker"], scoring, True
                )
            except Exception as e:
                scoring["active_breakout"] = {"available": False, "reason": str(e)[:120]}
            await asyncio.sleep(0.3)

    # Sector concentration
    total_value = total_stock_value + cash
    sector_concentration = {
        sector: round((value / total_stock_value) * 100, 1)
        for sector, value in sector_value.items()
    } if total_stock_value > 0 else {}
    concentrated_sectors = {s: pct for s, pct in sector_concentration.items() if pct >= 40}

    realized_log = portfolio.get("realized_pnl_log", [])
    total_realized = sum(entry["pnl_idr"] for entry in realized_log)

    portfolio_context = (
        f"Cash tersedia: Rp{cash:,.0f}\n"
        f"Total nilai saham: Rp{total_stock_value:,.0f}\n"
        f"Total kekayaan (saham + cash): Rp{total_value:,.0f}\n"
        f"Total realized P&L (semua waktu): Rp{total_realized:,.0f}\n"
        f"Konsentrasi sektor (% dari nilai saham): {sector_concentration}\n"
        + (f"⚠️ SEKTOR TERKONSENTRASI (>=40% dari portofolio saham): {concentrated_sectors}\n"
           if concentrated_sectors else "Tidak ada sektor yang terlalu terkonsentrasi (semua di bawah 40%).\n")
        + (f"\nWATCHLIST ({len(watchlist_data)} saham, belum dimiliki — evaluasi untuk timing entry, "
           f"bukan add/trim/hold/sell):\n{[w['ticker'] for w in watchlist_data]}\n" if watchlist_data else "")
    )

    combined_data = positions_data + watchlist_data
    reasoning_result = await asyncio.to_thread(core.get_portfolio_reasoning_and_synthesis, combined_data, portfolio_context)
    per_stock_reasoning = reasoning_result.get("per_stock_reasoning", {})
    weekly_synthesis = reasoning_result.get("weekly_synthesis", "")

    # Assemble the final message deterministically in Python — every number is
    # exactly what was computed, Gemini only ever fills in the reasoning text.
    header = f"📊 RINGKASAN PORTOFOLIO\nTotal Kekayaan: Rp{total_value:,.0f} (Saham Rp{total_stock_value:,.0f} + Cash Rp{cash:,.0f})"
    if concentrated_sectors:
        header += f"\n⚠️ Konsentrasi Sektor: {', '.join(f'{s} {p}%' for s, p in concentrated_sectors.items())}"
    if missing_entry_date_tickers:
        header += (
            f"\n\n⚠️ {len(missing_entry_date_tickers)} posisi belum punya tanggal beli: {', '.join(missing_entry_date_tickers)}\n"
            f"Lengkapi dengan: /setentrydate TICKER YYYY-MM-DD\n"
            f"Kategori siklus untuk posisi ini disembunyikan sampai tanggal diisi."
        )

    blocks = [header]
    for scoring in positions_data:
        weight_pct = (scoring["market_value_idr"] / total_stock_value * 100) if total_stock_value > 0 else 0
        block = core.format_position_block(
            scoring, is_holding=True, weight_pct=weight_pct,
            unrealized_pnl_pct=scoring["unrealized_pnl_pct"], days_held=scoring.get("_days_held"),
            reasoning_text=per_stock_reasoning.get(scoring["ticker"], ""),
        )
        blocks.append(block)

    if watchlist_data:
        blocks.append("─────────────────────────────────────\nWATCHLIST")
        for scoring in watchlist_data:
            reasoning = per_stock_reasoning.get(scoring["ticker"], "")
            th = scoring.get("_tp_horizon", {})
            horizon_str = f" ({th['horizon_days_low']}-{th['horizon_days_high']} hari, Confidence: {th['confidence']})" if th.get("horizon_days_low") else ""
            blocks.append(f"{scoring['ticker']} — {scoring.get('action_label_id', '?')}{horizon_str}\n{reasoning}")

    blocks.append(f"─────────────────────────────────────\nSINTESIS MINGGUAN:\n{weekly_synthesis}")

    full_message = "\n\n".join(blocks)
    await core.safe_reply(update.message, full_message)
