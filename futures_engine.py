import csv
from datetime import datetime, timezone
import os
import requests

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CSV_FILE = "futures_log.csv"

# ── TARGET ASSETS & RISK BOUNDARIES ──
PAIRS = ["SOLUSDT", "ETHUSDT"]
ACCOUNT_BALANCE_USD = 15.0     # Reference bankroll
LEVERAGE = 3                   # 3x Isolated margin
MARGIN_PER_TRADE = 5.0         # Risk only $5 margin per trade
RR_RATIO_TARGET = 2.5          # Minimum 1:2.5 Risk-to-Reward

def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID.strip(),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

def get_klines(symbol: str, interval: str, limit: int = 60) -> list:
    """Fetches public candlestick data from Binance Futures REST API."""
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    try:
        res = requests.get(url, params=params, timeout=10)
        if res.status_code == 200:
            raw = res.json()
            candles = []
            for c in raw:
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
        print(f"Kline fetch error on {symbol} ({interval}): {e}")
    return []

def get_current_mark_price(symbol: str) -> float:
    """Fetches real-time mark price for futures evaluation."""
    url = "https://fapi.binance.com/fapi/v1/premiumIndex"
    params = {"symbol": symbol}
    try:
        res = requests.get(url, params=params, timeout=8)
        if res.status_code == 200:
            return float(res.json().get("markPrice", 0) or 0)
    except Exception as e:
        print(f"Mark price fetch error for {symbol}: {e}")
    return 0.0

def calculate_ema(prices: list, period: int) -> float:
    if len(prices) < period:
        return prices[-1]
    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = (p - ema) * multiplier + ema
    return ema

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
    """Audits open trades, updates unrealized PnL, and triggers TP/SL closures."""
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

        mark_price = get_current_mark_price(symbol)
        if mark_price <= 0:
            continue

        row["current_price"] = f"{mark_price:.2f}"

        # Calculate PnL based on position direction and leverage
        if direction == "LONG":
            raw_pnl_pct = ((mark_price - entry) / entry) * 100
            leveraged_pnl_pct = raw_pnl_pct * LEVERAGE
            row["pnl_pct"] = f"{leveraged_pnl_pct:+.2f}%"

            # Check Take Profit
            if mark_price >= tp:
                row["status"] = "TP HIT"
                updated = True
                msg = (
                    f"🟢 <b>FUTURES TAKE PROFIT HIT: {symbol}</b>\n\n"
                    f"• <b>Direction:</b> LONG\n"
                    f"• <b>Entry:</b> ${entry:,.2f}\n"
                    f"• <b>Exit Price:</b> ${mark_price:,.2f}\n"
                    f"• <b>Target:</b> ${tp:,.2f}\n\n"
                    f"💰 <b>Paper Return:</b> +{leveraged_pnl_pct:.2f}% (+${reward_usd:.2f})"
                )
                send_telegram(msg)

            # Check Stop Loss
            elif mark_price <= sl:
                row["status"] = "SL HIT"
                updated = True
                msg = (
                    f"🛑 <b>FUTURES STOP LOSS HIT: {symbol}</b>\n\n"
                    f"• <b>Direction:</b> LONG\n"
                    f"• <b>Entry:</b> ${entry:,.2f}\n"
                    f"• <b>Exit Price:</b> ${mark_price:,.2f}\n"
                    f"• <b>Stop Loss:</b> ${sl:,.2f}\n\n"
                    f"⚠️ <b>Paper Loss:</b> {leveraged_pnl_pct:.2f}% (-${risk_usd:.2f})"
                )
                send_telegram(msg)

        elif direction == "SHORT":
            raw_pnl_pct = ((entry - mark_price) / entry) * 100
            leveraged_pnl_pct = raw_pnl_pct * LEVERAGE
            row["pnl_pct"] = f"{leveraged_pnl_pct:+.2f}%"

            # Check Take Profit
            if mark_price <= tp:
                row["status"] = "TP HIT"
                updated = True
                msg = (
                    f"🟢 <b>FUTURES TAKE PROFIT HIT: {symbol}</b>\n\n"
                    f"• <b>Direction:</b> SHORT\n"
                    f"• <b>Entry:</b> ${entry:,.2f}\n"
                    f"• <b>Exit Price:</b> ${mark_price:,.2f}\n"
                    f"• <b>Target:</b> ${tp:,.2f}\n\n"
                    f"💰 <b>Paper Return:</b> +{leveraged_pnl_pct:.2f}% (+${reward_usd:.2f})"
                )
                send_telegram(msg)

            # Check Stop Loss
            elif mark_price >= sl:
                row["status"] = "SL HIT"
                updated = True
                msg = (
                    f"🛑 <b>FUTURES STOP LOSS HIT: {symbol}</b>\n\n"
                    f"• <b>Direction:</b> SHORT\n"
                    f"• <b>Entry:</b> ${entry:,.2f}\n"
                    f"• <b>Exit Price:</b> ${mark_price:,.2f}\n"
                    f"• <b>Stop Loss:</b> ${sl:,.2f}\n\n"
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
        for row in reader[-6:]:
            if row.get("symbol") == symbol and row.get("direction") == direction and row.get("status") == "ACTIVE":
                return True
    return False

def log_signal(symbol, direction, entry, sl, tp, risk_usd, reward_usd):
    with open(CSV_FILE, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        writer.writerow([
            now_str, symbol, direction, f"{entry:.2f}", f"{sl:.2f}",
            f"{tp:.2f}", f"{risk_usd:.2f}", f"{reward_usd:.2f}", f"1:{RR_RATIO_TARGET}",
            f"{entry:.2f}", "0.00%", "ACTIVE"
        ])

def analyze_market(symbol: str):
    k_15m = get_klines(symbol, "15m", limit=50)
    k_1h = get_klines(symbol, "1h", limit=60)
    if len(k_15m) < 10 or len(k_1h) < 50:
        return

    # 1. 1-Hour Trend Bias
    closes_1h = [c["close"] for c in k_1h]
    ema_50_1h = calculate_ema(closes_1h, 50)
    current_price = k_15m[-1]["close"]
    trend_bias = "BULLISH" if current_price > ema_50_1h else "BEARISH"

    # 2. 15-Minute FVG Imbalance
    c1, c2, c3 = k_15m[-4], k_15m[-3], k_15m[-2]
    bullish_fvg = c3["low"] > c1["high"] and c2["close"] > c2["open"]
    bearish_fvg = c3["high"] < c1["low"] and c2["close"] < c2["open"]

    swing_low = min(c["low"] for c in k_15m[-10:])
    swing_high = max(c["high"] for c in k_15m[-10:])

    # 3. Long Setup Evaluation
    if trend_bias == "BULLISH" and bullish_fvg:
        entry_price = c3["low"]
        stop_loss = swing_low
        risk_per_unit = entry_price - stop_loss

        if risk_per_unit > 0:
            target_price = entry_price + (risk_per_unit * RR_RATIO_TARGET)
            risk_pct = (risk_per_unit / entry_price) * 100

            if 0.5 <= risk_pct <= 2.5 and not is_recent_setup_logged(symbol, "LONG"):
                notional_position = MARGIN_PER_TRADE * LEVERAGE
                dollar_risk = (risk_pct / 100) * notional_position
                dollar_reward = dollar_risk * RR_RATIO_TARGET

                msg = (
                    f"📊 <b>NEW PAPER TRADE LOGGED: {symbol}</b>\n\n"
                    f"<b>Direction:</b> 🟢 <b>LONG</b>\n"
                    f"• <b>Leverage:</b> {LEVERAGE}x (Isolated)\n"
                    f"• <b>Margin:</b> ${MARGIN_PER_TRADE:.2f} (Notional: ${notional_position:.2f})\n\n"
                    f"🎯 <b>Entry:</b> ${entry_price:,.2f}\n"
                    f"🛑 <b>Stop Loss:</b> ${stop_loss:,.2f}\n"
                    f"🏁 <b>Take Profit:</b> ${target_price:,.2f} (1:{RR_RATIO_TARGET} R:R)\n\n"
                    f"• <b>Risk:</b> -${dollar_risk:.2f} | <b>Reward:</b> +${dollar_reward:.2f}\n"
                    f"• <b>Trend:</b> 1H Bullish above EMA 50"
                )
                send_telegram(msg)
                log_signal(symbol, "LONG", entry_price, stop_loss, target_price, dollar_risk, dollar_reward)

    # 4. Short Setup Evaluation
    elif trend_bias == "BEARISH" and bearish_fvg:
        entry_price = c3["high"]
        stop_loss = swing_high
        risk_per_unit = stop_loss - entry_price

        if risk_per_unit > 0:
            target_price = entry_price - (risk_per_unit * RR_RATIO_TARGET)
            risk_pct = (risk_per_unit / entry_price) * 100

            if 0.5 <= risk_pct <= 2.5 and not is_recent_setup_logged(symbol, "SHORT"):
                notional_position = MARGIN_PER_TRADE * LEVERAGE
                dollar_risk = (risk_pct / 100) * notional_position
                dollar_reward = dollar_risk * RR_RATIO_TARGET

                msg = (
                    f"📊 <b>NEW PAPER TRADE LOGGED: {symbol}</b>\n\n"
                    f"<b>Direction:</b> 🔴 <b>SHORT</b>\n"
                    f"• <b>Leverage:</b> {LEVERAGE}x (Isolated)\n"
                    f"• <b>Margin:</b> ${MARGIN_PER_TRADE:.2f} (Notional: ${notional_position:.2f})\n\n"
                    f"🎯 <b>Entry:</b> ${entry_price:,.2f}\n"
                    f"🛑 <b>Stop Loss:</b> ${stop_loss:,.2f}\n"
                    f"🏁 <b>Take Profit:</b> ${target_price:,.2f} (1:{RR_RATIO_TARGET} R:R)\n\n"
                    f"• <b>Risk:</b> -${dollar_risk:.2f} | <b>Reward:</b> +${dollar_reward:.2f}\n"
                    f"• <b>Trend:</b> 1H Bearish below EMA 50"
                )
                send_telegram(msg)
                log_signal(symbol, "SHORT", entry_price, stop_loss, target_price, dollar_risk, dollar_reward)

if __name__ == "__main__":
    initialize_journal()
    print("Auditing active paper trades...")
    update_active_paper_trades()

    print("Scanning for new 15m confluence setups...")
    for pair in PAIRS:
        analyze_market(pair)
    print("Execution complete.")
