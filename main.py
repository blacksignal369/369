#!/usr/bin/env python3
"""
Binance USDⓈ-M — BTCUSDT PVSRA (TradingView exact, volume only)
1m only · 150% / 200% · no taker
+ trade-size distribution of the alert candle (appended to the alert)
"""

from __future__ import annotations

import math
import time
import traceback
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

import requests

BOT_TOKEN = "PASTE_YOUR_NEW_TOKEN_HERE"
CHAT_ID = "6263967739"

SYMBOL = "BTCUSDT"
ENABLE_1M = True
LOG_EACH_TRADE = True  # prints every trade of the alert candle to the console

LOOKBACK = 10
HEARTBEAT_SEC = 15 * 60
REQUEST_TIMEOUT = 12
TG_RETRIES = 5
BINANCE_RETRIES = 4
JEDDAH = timezone(timedelta(hours=3))

FAPI = "https://fapi.binance.com"
PERIOD_SEC = {"1m": 60}

last_processed_ms: Dict[str, int] = {}


def active_intervals() -> List[str]:
    return ["1m"] if ENABLE_1M else []


def jeddah_str(ms: Optional[int] = None) -> str:
    if ms is None:
        ms = int(time.time() * 1000)
    dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).astimezone(JEDDAH)
    return dt.strftime("%d %b %I:%M:%S %p")


def send_tg(text: str, silent: bool = False, retries: int = TG_RETRIES) -> bool:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text[:4000],
        "disable_notification": silent,
        "disable_web_page_preview": True,
    }
    for attempt in range(retries):
        try:
            r = requests.post(url, json=payload, timeout=15)
            if r.status_code == 200 and r.json().get("ok"):
                return True
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (attempt + 1))
                continue
            print("TG fail", r.status_code, r.text[:120])
        except Exception as e:
            print("TG err", e)
        time.sleep(0.8 * (attempt + 1))
    return False


def get_json(path: str, params: dict = None):
    for attempt in range(BINANCE_RETRIES):
        try:
            r = requests.get(f"{FAPI}{path}", params=params or {}, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (418, 429, 500, 502, 503, 504):
                time.sleep(1.0 * (attempt + 1))
                continue
            print("BINANCE", r.status_code, r.text[:100])
        except Exception as e:
            print("BINANCE err", e)
        time.sleep(0.5 * (attempt + 1))
    return None


def pvsra_va(volumes: List[float], highs: List[float], lows: List[float]) -> int:
    """TV exact: sma(vol[1],10) + hl peak → 200 / 150."""
    if len(volumes) < LOOKBACK + 1:
        return 0
    prev_vols = volumes[-(LOOKBACK + 1) : -1]
    avg10 = sum(prev_vols) / LOOKBACK
    cur_vol = volumes[-1]
    cur_hl = cur_vol * (highs[-1] - lows[-1])
    prev_hl = [
        volumes[-(LOOKBACK + 1) + i]
        * (highs[-(LOOKBACK + 1) + i] - lows[-(LOOKBACK + 1) + i])
        for i in range(LOOKBACK)
    ]
    max_hl10 = max(prev_hl) if prev_hl else 0.0
    if cur_vol >= avg10 * 2.0 or cur_hl >= max_hl10:
        return 200
    if cur_vol >= avg10 * 1.5:
        return 150
    return 0


# ───────────── trade distribution of the alert candle ─────────────

def fetch_candle_trades(open_ms: int, period_ms: int) -> Optional[List[dict]]:
    """All aggTrades inside [open_ms, open_ms + period_ms - 1]."""
    end_ms = open_ms + period_ms - 1
    trades: List[dict] = []
    data = get_json(
        "/fapi/v1/aggTrades",
        {"symbol": SYMBOL, "startTime": open_ms, "endTime": end_ms, "limit": 1000},
    )
    while True:
        if not isinstance(data, list):
            return None
        if not data:
            break
        finished = False
        for t in data:
            if int(t["T"]) > end_ms:
                finished = True
                break
            trades.append(t)
        if finished or len(data) < 1000:
            break
        data = get_json(
            "/fapi/v1/aggTrades",
            {"symbol": SYMBOL, "fromId": int(data[-1]["a"]) + 1, "limit": 1000},
        )
    return trades


def log_trades(trades: List[dict]) -> None:
    for t in trades:
        ms = int(t["T"])
        dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).astimezone(JEDDAH)
        side = "SELL" if t["m"] else "BUY"  # m = buyer is maker → taker sold
        print(
            f"[TRADE] {dt.strftime('%H:%M:%S')}.{ms % 1000:03d} "
            f"id={t['a']} {side} qty={t['q']} price={t['p']}"
        )


def build_trade_report(trades: List[dict]) -> str:
    qtys = [float(t["q"]) for t in trades]
    n = len(qtys)
    if n == 0:
        return ""
    s = sorted(qtys)
    total = sum(s)
    if total <= 0:
        return ""

    # 75th percentile (nearest-rank → an actual trade size)
    p75 = s[max(0, math.ceil(0.75 * n) - 1)]

    in_range = [q for q in s if q <= p75]
    out_range = [q for q in s if q > p75]
    range_vol = sum(in_range)
    out_vol = sum(out_range)

    return (
        f"\nTrades: {n} · Total: {total:.3f}\n"
        f"Range: {s[0]:.3f} → {p75:.3f} (min → P75)\n"
        f"Max in range: {max(in_range):.3f}\n"
        f"Range vol: {range_vol:.3f} · {range_vol / total * 100:.2f}%\n"
        f"Right side (>P75): {out_vol:.3f} · {out_vol / total * 100:.2f}%\n"
        f"Max trade (candle): {s[-1]:.3f}"
    )


def check_interval(interval: str) -> None:
    data = get_json(
        "/fapi/v1/klines",
        {"symbol": SYMBOL, "interval": interval, "limit": LOOKBACK + 3},
    )
    if not isinstance(data, list) or len(data) < LOOKBACK + 2:
        return

    bars = data[-(LOOKBACK + 2) : -1]
    if len(bars) < LOOKBACK + 1:
        return

    try:
        open_t = int(bars[-1][0])
        o = float(bars[-1][1])
        c = float(bars[-1][4])
    except (TypeError, ValueError, IndexError):
        return

    if last_processed_ms.get(interval) == open_t:
        return

    volumes = [float(b[5]) for b in bars]
    highs = [float(b[2]) for b in bars]
    lows = [float(b[3]) for b in bars]

    va = pvsra_va(volumes, highs, lows)
    if va == 0:
        last_processed_ms[interval] = open_t
        return

    # alert candle → pull every trade, log it, build distribution
    extra = ""
    try:
        trades = fetch_candle_trades(open_t, PERIOD_SEC[interval] * 1000)
        if trades:
            if LOG_EACH_TRADE:
                log_trades(trades)
            extra = build_trade_report(trades)
        else:
            print(f"[WARN] no trades fetched for {interval} candle {open_t}")
    except Exception:
        traceback.print_exc()

    msg = f"{'🟢' if c > o else '🔴'} {interval} · {va}%{extra}"
    if send_tg(msg):
        last_processed_ms[interval] = open_t
        print(f"[ALERT] {msg}")
    else:
        print(f"[HOLD] TG failed — retry {interval}")


def near_close_window(interval: str, now: float) -> bool:
    period = PERIOD_SEC[interval]
    return 2.0 <= (now % period) <= 120.0


def startup() -> bool:
    data = get_json("/fapi/v1/klines", {"symbol": SYMBOL, "interval": "1m", "limit": 3})
    if not isinstance(data, list) or len(data) < 2:
        send_tg(f"❌ STARTUP FAILED\n{SYMBOL}")
        return False
    tfs = ", ".join(active_intervals())
    return send_tg(
        f"✅ READY\n"
        f"{SYMBOL} · Futures\n"
        f"TFs: {tfs}\n"
        f"PVSRA 150% / 200% (TV volume only)\n"
        f"{jeddah_str()}"
    )


def main() -> None:
    if not startup():
        time.sleep(5)
    last_hb = time.time()
    last_poll: Dict[str, float] = {iv: 0.0 for iv in active_intervals()}
    while True:
        try:
            now = time.time()
            for iv in active_intervals():
                if near_close_window(iv, now) and (now - last_poll.get(iv, 0) > 15):
                    last_poll[iv] = now
                    try:
                        check_interval(iv)
                    except Exception:
                        traceback.print_exc()
            if now - last_hb >= HEARTBEAT_SEC:
                if send_tg("❤️", silent=True):
                    last_hb = now
            time.sleep(3)
        except Exception:
            traceback.print_exc()
            time.sleep(10)


if __name__ == "__main__":
    main()
