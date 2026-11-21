
#!/usr/bin/env python3
"""
Binance USDⓈ-M — BTCUSDT PVSRA (TradingView exact, volume only)
1m (toggle) + 1h + 1d · 150% / 200% · no taker
"""

from __future__ import annotations

import time
import traceback
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

import requests

BOT_TOKEN = "7541584197:AAGZuuVygk54j3P6p_pcXZzplXEmQSpT7bs"
CHAT_ID = "6263967739"

SYMBOL = "BTCUSDT"
ENABLE_1M = False

LOOKBACK = 10
HEARTBEAT_SEC = 15 * 60
REQUEST_TIMEOUT = 12
TG_RETRIES = 5
BINANCE_RETRIES = 4
JEDDAH = timezone(timedelta(hours=3))

FAPI = "https://fapi.binance.com"
PERIOD_SEC = {"1m": 60, "1h": 3600, "1d": 86400}

last_processed_ms: Dict[str, int] = {}


def active_intervals() -> List[str]:
    out = []
    if ENABLE_1M:
        out.append("1m")
    out.extend(["1h", "1d"])
    return out


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

    msg = f"{'🟢' if c > o else '🔴'} {interval} · {va}%"
    if send_tg(msg):
        last_processed_ms[interval] = open_t
        print(f"[ALERT] {msg}")
    else:
        print(f"[HOLD] TG failed — retry {interval}")


def near_close_window(interval: str, now: float) -> bool:
    period = PERIOD_SEC[interval]
    return 2.0 <= (now % period) <= 120.0


def startup() -> bool:
    data = get_json("/fapi/v1/klines", {"symbol": SYMBOL, "interval": "1h", "limit": 3})
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