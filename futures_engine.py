import csv
from datetime import datetime, timezone
import os
import requests
import time

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CSV_FILE = "futures_log.csv"

# ── TARGET ASSETS & RISK BOUNDARIES ──
PAIRS = ["SOLUSDT", "ETHUSDT", "BTCUSDT", "DOGEUSDT", "XRPUSDT", "SUIUSDT", "NEARUSDT", "AVAXUSDT"]
ACCOUNT_BALANCE_USD = 15.0     # Reference bankroll
LEVERAGE = 3                   # 3x Isolated margin
MARGIN_PER_TRADE = 5.0         # Risk $5 margin per trade
RR_RATIO_TARGET = 3.0          # 1:3.0 Target for higher realized ROI

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram secret missing: Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in GitHub Secrets.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": str(TELEGRAM_CHAT_ID).strip(),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code == 200:
            print("Telegram alert delivered successfully.")
        else:
            print(f"Telegram API Error ({res.status_code}): {res.text}")
    except Exception as e:
        print(f"Telegram dispatch error: {e}")

def get_klines_mexc(symbol: str, interval: str, limit: int = 60) -> list:
    interval_map = {"15m": "15m", "1h": "60m"}
    url = "https://api.mexc.com/api/v3/klines"
    params = {
        "symbol": symbol,
        "interval": interval_map.get(interval, "15m"),
        "limit": limit
    }
    try:
        res = requests.get(url, params=params, headers=HEADERS, timeout=10)
        if res.status_code == 200:
            data = res.json()
            candles = []
            for c in data:
                candles.append({
                    "open_time": int(c[0]),
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low": float(c[3]),
                    "close": float(c[4]),
                    "volume": float(c[5])
                })
            return candles
    except Exception as e:
        print(f"MEXC Kline fetch error on {symbol} ({interval}): {e}")
    return []

def get_mark_price_mexc(symbol: str) -> float:
    url = "https://api.mexc.com/api/v3/ticker/price"
    params = {"symbol": symbol}
    try:
        res = requests.get(url, params=params, headers=HEADERS, timeout=8)
        if res.status_code == 200:
            return float(res.json().get("price", 0) or 0)
    except Exception as e:
        print(f"Price fetch error for {symbol}: {e}")
    return 0.0

def calculate_ema(prices: list, period: int) -> float:
    if len(prices) < period:
        return prices[-1]
    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = (p - ema) * multiplier + ema
    return ema

def calculate_atr(candles: list, period: int = 14) -> float:
    """Calculates 14-period Average True Range for dynamic volatility stops."""
    if len(candles) < period + 1:
        return 0.0
    tr_list = []
    for i in range(1, len(candles)):
        h, l, prev_c = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        tr_list.append(tr)
    return sum(tr_list[-period:]) / period

def calculate_adx(candles: list, period: int = 14) -> float:
    """
    Wilder's Average Directional Index (ADX):
    ADX < 20: Weak chop / Sideways consolidation.
    ADX >= 22: Strong directional momentum.
    """
    if len(candles) < (period * 2) + 1:
        return 25.0

    tr_list, dm_plus_list, dm_minus_list = [], [], []

    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        prev_h = candles[i - 1]["high"]
        prev_l = candles[i - 1]["low"]
        prev_c = candles[i - 1]["close"]

        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        tr_list.append(tr)

        up_move = h - prev_h
        down_move = prev_l - l

        if up_move > down_move and up_move > 0:
            dm_plus_list.append(up_move)
        else:
            dm_plus_list.append(0.0)

        if down_move > up_move and down_move > 0:
            dm_minus_list.append(down_move)
        else:
            dm_minus_list.append(0.0)

    tr_smooth = sum(tr_list[:period])
    dm_plus_smooth = sum(dm_plus_list[:period])
    dm_minus_smooth = sum(dm_minus_list[:period])

    dx_list = []
    for i in range(period, len(tr_list)):
        tr_smooth = tr_smooth - (tr_smooth / period) + tr_list[i]
        dm_plus_smooth = dm_plus_smooth - (dm_plus_smooth / period) + dm_plus_list[i]
        dm_minus_smooth = dm_minus_smooth - (dm_minus_smooth / period) + dm_minus_list[i]

        di_plus = (dm_plus_smooth / tr_smooth) * 100 if tr_smooth > 0 else 0
        di_minus = (dm_minus_smooth / tr_smooth) * 100 if tr_smooth > 0 else 0

        denom = di_plus + di_minus
        dx = (abs(di_plus - di_minus) / denom) * 100 if denom > 0 else 0
        dx_list.append(dx)

    if len(dx_list) < period:
        return 25.0

    return sum(dx_list[-period:]) / period

def is_displacement_confirmed(candles: list, c_index: int) -> bool:
    """Validates that impulse candle has >= 1.3x 15-period average volume."""
    vol_recent = [c["volume"] for c in candles[-16:-1]]
    if not vol_recent:
        return True
    avg_vol = sum(vol_recent) / len(vol_recent)
    candle_vol = candles[c_index]["volume"]
    return candle_vol >= (avg_vol * 1.3)

def evaluate_market_regime(k_1h: list, k_15m: list) -> tuple[str, float, str]:
    """Filters out sideways chop using 1H EMA Ribbon separation and 15M ADX."""
    closes_1h = [c["close"] for c in k_1h]
    ema_20_1h = calculate_ema(closes_1h, 20)
    ema_50_1h = calculate_ema(closes_1h, 50)
    current_price = k_15m[-1]["close"]

    adx_val = calculate_adx(k_15m, period=14)

    # 1. Chop Filter: ADX < 20
    if adx_val < 20.0:
        return "SIDEWAYS", adx_val, f"ADX {adx_val:.1f} < 20 (Ranging/Chop)"

    # 2. EMA Compression Check
    ema_spread_pct = abs(ema_20_1h - ema_50_1h) / ema_50_1h * 100
    if ema_spread_pct < 0.20:
        return "SIDEWAYS", adx_val, f"EMA Ribbon Tangled ({ema_spread_pct:.2f}% < 0.20%)"

    # 3. Clean Directional Alignment
    if current_price > ema_20_1h and ema_20_1h > ema_50_1h:
        return "BULLISH", adx_val, f"Uptrend (1H EMA 20 > 50, ADX {adx_val:.1f})"
    elif current_price < ema_20_1h and ema_20_1h < ema_50_1h:
        return "BEARISH", adx_val, f"Downtrend (1H EMA 20 < 50, ADX {adx_val:.1f})"

    return "SIDEWAYS", adx_val, "Mixed EMA Alignment (Price between EMA 20 & 50)"

def initialize_journal():
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp", "symbol", "direction", "entry", "stop_loss",
                "take_profit", "risk_usd", "reward_usd", "rr_ratio",
                "current_price", "pnl_pct", "status"
            ])

def update_active_paper_trades():
    """Audits open positions with dual mathematical validation."""
    if not os.path.exists(CSV_FILE):
        return

    rows = []
    with open(CSV_FILE, mode="r", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    updated = False
    for row in rows:
        if row.get("status") in ["TP HIT", "SL HIT", "CLOSED"]:
            continue

        symbol = row.get("symbol")
        direction = row.get("direction")
        entry = float(row.get("entry") or 0)
        sl = float(row.get("stop_loss") or 0)
        tp = float(row.get("take_profit") or 0)
        risk_usd = float(row.get("risk_usd") or 0)
        reward_usd = float(row.get("reward_usd") or 0)

        if entry <= 0:
            continue

        mark_price = get_mark_price_mexc(symbol)
        if mark_price <= 0:
            continue

        row["current_price"] = f"{mark_price:.4f}"

        # ── LONG AUDIT ──
        if direction == "LONG":
            raw_pnl_pct = ((mark_price - entry) / entry) * 100
            leveraged_pnl_pct = raw_pnl_pct * LEVERAGE
            row["pnl_pct"] = f"{leveraged_pnl_pct:+.2f}%"

            # TP: Price reaches target AND return is positive
            if mark_price >= tp and leveraged_pnl_pct > 0:
                row["status"] = "TP HIT"
                updated = True
                msg = (
                    f"🟢 <b>FUTURES TAKE PROFIT HIT: {symbol}</b>\n\n"
                    f"• <b>Direction:</b> LONG\n"
                    f"• <b>Entry:</b> ${entry:,.4f}\n"
                    f"• <b>Exit Price:</b> ${mark_price:,.4f}\n"
                    f"• <b>Target:</b> ${tp:,.4f}\n\n"
                    f"💰 <b>Paper Return:</b> +{leveraged_pnl_pct:.2f}% (+${reward_usd:.2f})"
                )
                send_telegram(msg)

            # SL: Price hits stop-loss AND return is negative
            elif mark_price <= sl and leveraged_pnl_pct < 0:
                row["status"] = "SL HIT"
                updated = True
                msg = (
                    f"🛑 <b>FUTURES STOP LOSS HIT: {symbol}</b>\n\n"
                    f"• <b>Direction:</b> LONG\n"
                    f"• <b>Entry:</b> ${entry:,.4f}\n"
                    f"• <b>Exit Price:</b> ${mark_price:,.4f}\n"
                    f"• <b>Stop Loss:</b> ${sl:,.4f}\n\n"
                    f"⚠️ <b>Paper Loss:</b> {leveraged_pnl_pct:.2f}% (-${risk_usd:.2f})"
                )
                send_telegram(msg)

        # ── SHORT AUDIT (FIXED INVERTED OPERATORS) ──
        elif direction == "SHORT":
            raw_pnl_pct = ((entry - mark_price) / entry) * 100
            leveraged_pnl_pct = raw_pnl_pct * LEVERAGE
            row["pnl_pct"] = f"{leveraged_pnl_pct:+.2f}%"

            # TP: Price drops to target AND return is positive
            if mark_price <= tp and leveraged_pnl_pct > 0:
                row["status"] = "TP HIT"
                updated = True
                msg = (
                    f"🟢 <b>FUTURES TAKE PROFIT HIT: {symbol}</b>\n\n"
                    f"• <b>Direction:</b> SHORT\n"
                    f"• <b>Entry:</b> ${entry:,.4f}\n"
                    f"• <b>Exit Price:</b> ${mark_price:,.4f}\n"
                    f"• <b>Target:</b> ${tp:,.4f}\n\n"
                    f"💰 <b>Paper Return:</b> +{leveraged_pnl_pct:.2f}% (+${reward_usd:.2f})"
                )
                send_telegram(msg)

            # SL: Price rises to stop-loss AND return is negative
            elif mark_price >= sl and leveraged_pnl_pct < 0:
                row["status"] = "SL HIT"
                updated = True
                msg = (
                    f"🛑 <b>FUTURES STOP LOSS HIT: {symbol}</b>\n\n"
                    f"• <b>Direction:</b> SHORT\n"
                    f"• <b>Entry:</b> ${entry:,.4f}\n"
                    f"• <b>Exit Price:</b> ${mark_price:,.4f}\n"
                    f"• <b>Stop Loss:</b> ${sl:,.4f}\n\n"
                    f"⚠️ <b>Paper Loss:</b> {leveraged_pnl_pct:.2f}% (-${risk_usd:.2f})"
                )
                send_telegram(msg)

    if updated:
        with open(CSV_FILE, mode="w", newline="", encoding="utf-8") as f:
            fieldnames = [
                "timestamp", "symbol", "direction", "entry", "stop_loss",
                "take_profit", "risk_usd", "reward_usd", "rr_ratio",
                "current_price", "pnl_pct", "status"
            ]
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

def is_recent_setup_logged(symbol: str, direction: str) -> bool:
    if not os.path.exists(CSV_FILE):
        return False
    with open(CSV_FILE, mode="r", newline="", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))
        for row in reader[-15:]:
            if row.get("symbol") == symbol and row.get("direction") == direction and row.get("status") == "ACTIVE":
                return True
    return False

def log_signal(symbol, direction, entry, sl, tp, risk_usd, reward_usd):
    with open(CSV_FILE, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        writer.writerow([
            now_str, symbol, direction, f"{entry:.4f}", f"{sl:.4f}",
            f"{tp:.4f}", f"{risk_usd:.2f}", f"{reward_usd:.2f}", f"1:{RR_RATIO_TARGET}",
            f"{entry:.4f}", "0.00%", "ACTIVE"
        ])

def check_institutional_fvg(candles: list) -> tuple[bool, bool, float, str]:
    """
    Audits 15M candles for an Institutional FVG with Displacement & Liquidity Sweeps.
    Returns (bullish_fvg, bearish_fvg, entry_target, note).
    """
    for offset in [-4, -3]:
        c1 = candles[offset - 1]
        c2 = candles[offset]
        c3 = candles[offset + 1]

        if not is_displacement_confirmed(candles, offset):
            continue

        # Bullish: Low of C3 > High of C1
        if c3["low"] > c1["high"] and c2["close"] > c2["open"]:
            prior_lows = [c["low"] for c in candles[offset - 8:offset - 1]]
            sweep_confirmed = prior_lows and min(c1["low"], c2["low"]) <= min(prior_lows)
            note = "Displacement + Liquidity Sweep Confirmed" if sweep_confirmed else "Displacement Volume Confirmed"
            return True, False, c3["low"], note

        # Bearish: High of C3 < Low of C1
        if c3["high"] < c1["low"] and c2["close"] < c2["open"]:
            prior_highs = [c["high"] for c in candles[offset - 8:offset - 1]]
            sweep_confirmed = prior_highs and max(c1["high"], c2["high"]) >= max(prior_highs)
            note = "Displacement + Liquidity Sweep Confirmed" if sweep_confirmed else "Displacement Volume Confirmed"
            return False, True, c3["high"], note

    return False, False, 0.0, "NONE"

def analyze_market(symbol: str):
    k_15m = get_klines_mexc(symbol, "15m", limit=60)
    k_1h = get_klines_mexc(symbol, "1h", limit=60)
    if len(k_15m) < 35 or len(k_1h) < 50:
        print(f"[{symbol}] Insufficient candle history.")
        return

    # 1. Market Regime & Chop Audit
    regime, adx_val, regime_reason = evaluate_market_regime(k_1h, k_15m)
    current_price = k_15m[-1]["close"]

    if regime == "SIDEWAYS":
        print(f"[{symbol}] ⏸️ SKIP: {regime_reason}")
        return

    # 2. Institutional Imbalance Verification
    bullish_fvg, bearish_fvg, fvg_entry, edge_note = check_institutional_fvg(k_15m)

    atr_15m = calculate_atr(k_15m, period=14)
    atr_buffer = atr_15m * 0.40  # 40% ATR buffer stops premature wick stop-outs

    swing_low = min(c["low"] for c in k_15m[-10:])
    swing_high = max(c["high"] for c in k_15m[-10:])

    status_fvg = "BULL" if bullish_fvg else "BEAR" if bearish_fvg else "NONE"
    print(f"[{symbol}] Price: ${current_price:,.4f} | Regime: {regime} (ADX: {adx_val:.1f}) | FVG: {status_fvg} | ATR: ${atr_15m:.4f}")

    if regime == "BULLISH" and bullish_fvg:
        entry_price = fvg_entry if fvg_entry > 0 else current_price
        stop_loss = swing_low - atr_buffer
        risk_per_unit = entry_price - stop_loss

        if risk_per_unit > 0:
            target_price = entry_price + (risk_per_unit * RR_RATIO_TARGET)
            risk_pct = (risk_per_unit / entry_price) * 100

            if 0.3 <= risk_pct <= 3.8 and not is_recent_setup_logged(symbol, "LONG"):
                notional_position = MARGIN_PER_TRADE * LEVERAGE
                dollar_risk = (risk_pct / 100) * notional_position
                dollar_reward = dollar_risk * RR_RATIO_TARGET

                msg = (
                    f"📊 <b>HIGH-CONFLUENCE TREND TRADE: {symbol}</b>\n\n"
                    f"<b>Direction:</b> 🟢 <b>LONG</b>\n"
                    f"• <b>Regime:</b> Confirmed Trend (ADX {adx_val:.1f})\n"
                    f"• <b>Leverage:</b> {LEVERAGE}x (Isolated)\n"
                    f"• <b>Margin:</b> ${MARGIN_PER_TRADE:.2f} (Notional: ${notional_position:.2f})\n\n"
                    f"🎯 <b>Entry:</b> ${entry_price:,.4f}\n"
                    f"🛑 <b>Stop Loss:</b> ${stop_loss:,.4f} (Buffered ATR)\n"
                    f"🏁 <b>Take Profit:</b> ${target_price:,.4f} (1:{RR_RATIO_TARGET} R:R Target)\n\n"
                    f"• <b>Risk:</b> -${dollar_risk:.2f} | <b>Reward:</b> +${dollar_reward:.2f}\n"
                    f"• <b>Setup Edge:</b> {edge_note} + 1H EMA 20/50 Ribbon"
                )
                send_telegram(msg)
                log_signal(symbol, "LONG", entry_price, stop_loss, target_price, dollar_risk, dollar_reward)

    elif regime == "BEARISH" and bearish_fvg:
        entry_price = fvg_entry if fvg_entry > 0 else current_price
        stop_loss = swing_high + atr_buffer
        risk_per_unit = stop_loss - entry_price

        if risk_per_unit > 0:
            target_price = entry_price - (risk_per_unit * RR_RATIO_TARGET)
            risk_pct = (risk_per_unit / entry_price) * 100

            if 0.3 <= risk_pct <= 3.8 and not is_recent_setup_logged(symbol, "SHORT"):
                notional_position = MARGIN_PER_TRADE * LEVERAGE
                dollar_risk = (risk_pct / 100) * notional_position
                dollar_reward = dollar_risk * RR_RATIO_TARGET

                msg = (
                    f"📊 <b>HIGH-CONFLUENCE TREND TRADE: {symbol}</b>\n\n"
                    f"<b>Direction:</b> 🔴 <b>SHORT</b>\n"
                    f"• <b>Regime:</b> Confirmed Trend (ADX {adx_val:.1f})\n"
                    f"• <b>Leverage:</b> {LEVERAGE}x (Isolated)\n"
                    f"• <b>Margin:</b> ${MARGIN_PER_TRADE:.2f} (Notional: ${notional_position:.2f})\n\n"
                    f"🎯 <b>Entry:</b> ${entry_price:,.4f}\n"
                    f"🛑 <b>Stop Loss:</b> ${stop_loss:,.4f} (Buffered ATR)\n"
                    f"🏁 <b>Take Profit:</b> ${target_price:,.4f} (1:{RR_RATIO_TARGET} R:R Target)\n\n"
                    f"• <b>Risk:</b> -${dollar_risk:.2f} | <b>Reward:</b> +${dollar_reward:.2f}\n"
                    f"• <b>Setup Edge:</b> {edge_note} + 1H EMA 20/50 Ribbon"
                )
                send_telegram(msg)
                log_signal(symbol, "SHORT", entry_price, stop_loss, target_price, dollar_risk, dollar_reward)

if __name__ == "__main__":
    initialize_journal()
    print("Auditing active paper trades...")
    update_active_paper_trades()

    print("Scanning target perpetuals with Trend & Anti-Chop Engine...")
    for pair in PAIRS:
        analyze_market(pair)
        time.sleep(0.1)
    print("Execution complete.")
