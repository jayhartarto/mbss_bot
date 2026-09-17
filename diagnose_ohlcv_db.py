import sqlite3
conn = sqlite3.connect("mbss_ohlcv.db")
cur = conn.cursor()
cur.execute("SELECT MAX(date), COUNT(DISTINCT ticker) FROM ohlcv_daily")
print("DB overall (max date, ticker count):", cur.fetchall())
cur.execute("SELECT date FROM ohlcv_daily WHERE ticker='BBCA' ORDER BY date DESC LIMIT 10")
print("BBCA last 10 dates:", cur.fetchall())
conn.close()
