import requests
import time
import csv
import os
import json
from datetime import datetime, timedelta
print("")
print("################################################")
print("### v32.3 QUALITY + HTF VERIFIED LOADED ########")
print("################################################")
print("")
BLOFIN_TICKERS_URL = "https://openapi.blofin.com/api/v1/market/tickers"
BLOFIN_CANDLES_URL = "https://openapi.blofin.com/api/v1/market/candles"

# Local use:
# Paste your webhook between the quotes below.
# Railway use later:
# Add DISCORD_WEBHOOK_URL as an environment variable and Railway will override this.
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1515481202365038632/p3V0Se4CAdGYmEJ0wOld5rM-oHlsdg8P1TPvRhxiWm9qS95OYLiQ3U1JnYvsJlzy5ctH"

SCAN_EVERY_SECONDS = 300
LOG_FILE = "scanner_log.csv"
ALERTS_FILE = "alerts.json"
ACTIVE_TRADES_FILE = "active_trades.json"
TRADE_HISTORY_FILE = "trade_history.csv"
SCANNER_STATS_FILE = "scanner_stats.json"


MIN_24H_MOVE_FOR_CANDLES = 5
MAX_CANDLE_CHECKS = 150

A_PLUS_SCORE = 85
A_SCORE = 75
B_SCORE = 65

ENTRY_READY_SCORE = 70

DISCORD_ENTRY_THRESHOLD = 95
DISCORD_MIN_RR = 2.0
MAX_DISCORD_ALERTS_PER_SCAN = 3

# Optional role ping:
# To ping a Discord role, right-click the role, copy role ID, and paste it like: "<@&123456789012345678>"
# Leave blank to disable pings.
VIP_ROLE_MENTION = ""
PING_ROLE_ON_ELITE = False

# v26 Discord embed settings
USE_DISCORD_EMBEDS = True
CHART_IMAGE_URL = ""  # Optional static image URL for embeds; leave blank for now.
ALERT_BRAND_NAME = "🏰 LIQUIDITY CITADEL ELITE SCANNER"

# Elite filter:
# Only Discord-alert A+ setups with strong entry readiness and 5m/15m structure agreement.
ELITE_ONLY = True

COOLDOWN_MINUTES = 30
SCORE_IMPROVEMENT_REQUIRED = 10
ENTRY_IMPROVEMENT_REQUIRED = 10

# v27 trade tracking settings
TRADE_UPDATE_COOLDOWN_MINUTES = 30
TRACK_TRADE_UPDATES_IN_DISCORD = True

# v28 performance analytics settings
POST_STATS_AFTER_CLOSED_TRADE = True
POST_STATS_AFTER_TP1 = False
MIN_CLOSED_TRADES_FOR_WIN_RATE = 1

# v29 leaderboard settings
POST_LEADERBOARD_AFTER_CLOSED_TRADE = True
LEADERBOARD_LOOKBACK_HOURS = 24
LEADERBOARD_TOP_N = 5

# v31 multi-timeframe confirmation + daily report settings
ENABLE_HTF_CONFIRMATION = True
HTF_1H_BAR = "1H"
HTF_4H_BAR = "4H"
HTF_CANDLE_LIMIT = 80
HTF_ALIGNMENT_BONUS = 10
S_TIER_REQUIRES_1H = True
S_TIER_REQUIRES_4H = False
DAILY_REPORT_FILE = "daily_report_state.json"
POST_DAILY_REPORT_TO_DISCORD = True
DAILY_REPORT_HOUR_UTC = 0


def safe_float(value):
    try:
        return float(value)
    except:
        return 0.0


def get_blofin_tickers():
    r = requests.get(BLOFIN_TICKERS_URL, timeout=10)
    r.raise_for_status()
    return r.json().get("data", [])


def get_candles(symbol, bar="5m", limit=80):
    try:
        params = {"instId": symbol, "bar": bar, "limit": str(limit)}
        r = requests.get(BLOFIN_CANDLES_URL, params=params, timeout=5)
        r.raise_for_status()
        data = r.json().get("data", [])

        candles = []
        for c in data:
            if len(c) >= 8:
                candles.append({
                    "time": c[0],
                    "open": safe_float(c[1]),
                    "high": safe_float(c[2]),
                    "low": safe_float(c[3]),
                    "close": safe_float(c[4]),
                    "volume_usdt": safe_float(c[7]),
                })

        candles.reverse()
        return candles
    except:
        return []


def get_volume_spike(candles):
    if len(candles) < 20:
        return 0.0, 1.0

    current_volume = candles[-1]["volume_usdt"]
    previous = [c["volume_usdt"] for c in candles[-20:-1] if c["volume_usdt"] > 0]

    if len(previous) < 5:
        return current_volume, 1.0

    avg_volume = sum(previous) / len(previous)

    if avg_volume <= 0:
        return current_volume, 1.0

    return current_volume, round(current_volume / avg_volume, 2)


def get_recent_high(candles):
    if not candles:
        return 0.0
    return max(c["high"] for c in candles)


def get_recent_low(candles):
    if not candles:
        return 0.0
    return min(c["low"] for c in candles)


def build_short_trade_plan(candles):
    if len(candles) < 10:
        return None

    current_price = candles[-1]["close"]
    swing_high = get_recent_high(candles[-30:])

    if current_price <= 0 or swing_high <= current_price:
        return None

    entry_low = current_price
    entry_high = current_price * 1.01
    stop = swing_high * 1.002
    risk = stop - current_price

    if risk <= 0:
        return None

    target1 = current_price - (risk * 2)
    target2 = current_price - (risk * 3)
    rr = round(abs(target1 - current_price) / abs(stop - current_price), 2)

    return {
        "bias": "SHORT",
        "entry_low": round(entry_low, 8),
        "entry_high": round(entry_high, 8),
        "stop": round(stop, 8),
        "target1": round(target1, 8),
        "target2": round(target2, 8),
        "rr": rr,
    }


def build_long_trade_plan(candles):
    if len(candles) < 10:
        return None

    current_price = candles[-1]["close"]
    swing_low = get_recent_low(candles[-30:])

    if current_price <= 0 or swing_low >= current_price:
        return None

    entry_low = current_price * 0.99
    entry_high = current_price
    stop = swing_low * 0.998
    risk = current_price - stop

    if risk <= 0:
        return None

    target1 = current_price + (risk * 2)
    target2 = current_price + (risk * 3)
    rr = round(abs(target1 - current_price) / abs(current_price - stop), 2)

    return {
        "bias": "LONG",
        "entry_low": round(entry_low, 8),
        "entry_high": round(entry_high, 8),
        "stop": round(stop, 8),
        "target1": round(target1, 8),
        "target2": round(target2, 8),
        "rr": rr,
    }


def find_swings(candles, left=3, right=3):
    swing_highs = []
    swing_lows = []

    if len(candles) < left + right + 5:
        return swing_highs, swing_lows

    for i in range(left, len(candles) - right):
        high = candles[i]["high"]
        low = candles[i]["low"]

        is_high = True
        is_low = True

        for j in range(i - left, i + right + 1):
            if j == i:
                continue
            if candles[j]["high"] >= high:
                is_high = False
            if candles[j]["low"] <= low:
                is_low = False

        if is_high:
            swing_highs.append({"index": i, "price": high})
        if is_low:
            swing_lows.append({"index": i, "price": low})

    return swing_highs, swing_lows


def detect_structure(candles):
    default = {
        "bearish_choch": False,
        "bullish_choch": False,
        "bearish_bos": False,
        "bullish_bos": False,
        "sweep_high": False,
        "sweep_low": False,
        "structure_note": "No structure"
    }

    if len(candles) < 30:
        return default

    swing_highs, swing_lows = find_swings(candles)

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return default

    last = candles[-1]
    last_close = last["close"]
    last_high = last["high"]
    last_low = last["low"]

    last_swing_high = swing_highs[-1]["price"]
    last_swing_low = swing_lows[-1]["price"]

    recent_high = max([x["price"] for x in swing_highs[-4:]])
    recent_low = min([x["price"] for x in swing_lows[-4:]])

    bearish_choch = last_close < last_swing_low
    bullish_choch = last_close > last_swing_high
    bearish_bos = last_close < recent_low
    bullish_bos = last_close > recent_high
    sweep_high = last_high > last_swing_high and last_close < last_swing_high
    sweep_low = last_low < last_swing_low and last_close > last_swing_low

    note = "No structure"
    if bearish_bos:
        note = "Bearish BOS"
    elif bullish_bos:
        note = "Bullish BOS"
    elif bearish_choch:
        note = "Bearish CHOCH"
    elif bullish_choch:
        note = "Bullish CHOCH"
    elif sweep_high:
        note = "High sweep"
    elif sweep_low:
        note = "Low sweep"

    return {
        "bearish_choch": bearish_choch,
        "bullish_choch": bullish_choch,
        "bearish_bos": bearish_bos,
        "bullish_bos": bullish_bos,
        "sweep_high": sweep_high,
        "sweep_low": sweep_low,
        "structure_note": note
    }


def analyze_candle_behavior(candles):
    default = {
        "last_candle_bearish_5m": False,
        "last_candle_bullish_5m": False,
        "lower_high_5m": False,
        "higher_low_5m": False,
    }

    if len(candles) < 6:
        return default

    last = candles[-1]
    prev = candles[-2]
    prev2 = candles[-3]

    return {
        "last_candle_bearish_5m": last["close"] < last["open"],
        "last_candle_bullish_5m": last["close"] > last["open"],
        "lower_high_5m": last["high"] < prev["high"] and prev["high"] < prev2["high"],
        "higher_low_5m": last["low"] > prev["low"] and prev["low"] > prev2["low"],
    }


def get_base_metrics(ticker):
    symbol = ticker.get("instId", "UNKNOWN")
    last = safe_float(ticker.get("last"))
    open_24h = safe_float(ticker.get("open24h"))
    high_24h = safe_float(ticker.get("high24h"))
    low_24h = safe_float(ticker.get("low24h"))
    volume_24h = safe_float(ticker.get("volCurrency24h") or ticker.get("vol24h"))

    if last <= 0 or open_24h <= 0 or high_24h <= low_24h:
        return None

    change_24h = ((last - open_24h) / open_24h) * 100
    range_pos = ((last - low_24h) / (high_24h - low_24h)) * 100
    pullback_from_high = ((high_24h - last) / high_24h) * 100
    bounce_from_low = ((last - low_24h) / low_24h) * 100

    return {
        "symbol": symbol,
        "last": last,
        "change_24h": change_24h,
        "range_pos": range_pos,
        "pullback_from_high": pullback_from_high,
        "bounce_from_low": bounce_from_low,
        "volume_24h": volume_24h,
    }


def volume_confirmation_score(spike_5m, spike_15m):
    score = 0

    if spike_5m >= 5:
        score += 25
    elif spike_5m >= 3:
        score += 20
    elif spike_5m >= 2:
        score += 14
    elif spike_5m >= 1.5:
        score += 8
    elif spike_5m < 0.5:
        score -= 10

    if spike_15m >= 4:
        score += 25
    elif spike_15m >= 2.5:
        score += 18
    elif spike_15m >= 1.5:
        score += 10
    elif spike_15m < 0.5:
        score -= 10

    return score


def short_opportunity_score(m):
    score = 0
    change = m["change_24h"]
    range_pos = m["range_pos"]
    pullback = m["pullback_from_high"]

    if change >= 50:
        score += 35
    elif change >= 40:
        score += 30
    elif change >= 30:
        score += 25
    elif change >= 25:
        score += 20
    elif change >= 20:
        score += 15
    elif change >= 10:
        score += 8
    elif change >= 5:
        score += 4

    if range_pos >= 98:
        score += 25
    elif range_pos >= 95:
        score += 22
    elif range_pos >= 90:
        score += 18
    elif range_pos >= 80:
        score += 10

    if pullback <= 1:
        score += 15
    elif pullback <= 3:
        score += 12
    elif pullback <= 5:
        score += 8
    elif pullback >= 8:
        score += 10

    score += volume_confirmation_score(m["volume_spike_5m"], m["volume_spike_15m"])

    if m["sweep_high_5m"]:
        score += 10
    if m["bearish_choch_5m"]:
        score += 15
    if m["bearish_bos_5m"]:
        score += 15
    if m["bearish_choch_15m"]:
        score += 20
    if m["bearish_bos_15m"]:
        score += 20

    return max(0, min(score, 100))


def long_opportunity_score(m):
    score = 0
    change = m["change_24h"]
    range_pos = m["range_pos"]
    bounce = m["bounce_from_low"]

    if change <= -50:
        score += 35
    elif change <= -40:
        score += 30
    elif change <= -30:
        score += 25
    elif change <= -25:
        score += 20
    elif change <= -20:
        score += 15
    elif change <= -10:
        score += 8
    elif change <= -5:
        score += 4

    if range_pos <= 2:
        score += 25
    elif range_pos <= 5:
        score += 22
    elif range_pos <= 10:
        score += 18
    elif range_pos <= 20:
        score += 10

    if bounce <= 1:
        score += 15
    elif bounce <= 3:
        score += 12
    elif bounce <= 5:
        score += 8
    elif bounce >= 8:
        score += 10

    score += volume_confirmation_score(m["volume_spike_5m"], m["volume_spike_15m"])

    if m["sweep_low_5m"]:
        score += 10
    if m["bullish_choch_5m"]:
        score += 15
    if m["bullish_bos_5m"]:
        score += 15
    if m["bullish_choch_15m"]:
        score += 20
    if m["bullish_bos_15m"]:
        score += 20

    return max(0, min(score, 100))


def entry_readiness_score(m, side):
    score = 0

    if side == "short":
        if m["short_score"] >= 85:
            score += 25
        elif m["short_score"] >= 75:
            score += 18
        elif m["short_score"] >= 65:
            score += 10

        if m["sweep_high_5m"]:
            score += 20
        if m["bearish_choch_5m"]:
            score += 20
        if m["bearish_choch_15m"]:
            score += 25
        if m["bearish_bos_5m"]:
            score += 15
        if m["last_candle_bearish_5m"]:
            score += 10
        if m["lower_high_5m"]:
            score += 15
        if m["pullback_from_high"] >= 3:
            score += 10

    if side == "long":
        if m["long_score"] >= 85:
            score += 25
        elif m["long_score"] >= 75:
            score += 18
        elif m["long_score"] >= 65:
            score += 10

        if m["sweep_low_5m"]:
            score += 20
        if m["bullish_choch_5m"]:
            score += 20
        if m["bullish_choch_15m"]:
            score += 25
        if m["bullish_bos_5m"]:
            score += 15
        if m["last_candle_bullish_5m"]:
            score += 10
        if m["higher_low_5m"]:
            score += 15
        if m["bounce_from_low"] >= 3:
            score += 10

    return max(0, min(score, 100))


def grade(score):
    if score >= A_PLUS_SCORE:
        return "A+"
    if score >= A_SCORE:
        return "A"
    if score >= B_SCORE:
        return "B"
    return ""


def classify_coin(m):
    if m["short_score"] >= A_PLUS_SCORE:
        return "A+ SHORT WATCH"
    if m["long_score"] >= A_PLUS_SCORE:
        return "A+ LONG WATCH"
    if m["short_score"] >= A_SCORE:
        return "A SHORT WATCH"
    if m["long_score"] >= A_SCORE:
        return "A LONG WATCH"
    if m["short_score"] >= B_SCORE:
        return "B SHORT WATCH"
    if m["long_score"] >= B_SCORE:
        return "B LONG WATCH"
    if m["change_24h"] >= 10:
        return "MOMENTUM WATCH"
    if m["change_24h"] <= -10:
        return "CAPITULATION WATCH"

    return "Neutral"


def build_setup_reasons(coin, side):
    reasons = []

    if side == "short":
        if coin["change_24h"] >= 20:
            reasons.append(f"Large 24h pump: +{coin['change_24h']}%")
        if coin["range_pos"] >= 90:
            reasons.append(f"Price near 24h highs: {coin['range_pos']}% of range")
        if coin["pullback_from_high"] >= 3:
            reasons.append(f"Pullback from high: {coin['pullback_from_high']}%")
        if coin["volume_spike_5m"] >= 2:
            reasons.append(f"5m volume spike: {coin['volume_spike_5m']}x")
        if coin["volume_spike_15m"] >= 1.5:
            reasons.append(f"15m volume spike: {coin['volume_spike_15m']}x")
        if coin["sweep_high_5m"]:
            reasons.append("5m high sweep detected")
        if coin["structure_note_5m"] in ["Bearish CHOCH", "Bearish BOS"]:
            reasons.append(f"5m structure: {coin['structure_note_5m']}")
        if coin["structure_note_15m"] in ["Bearish CHOCH", "Bearish BOS"]:
            reasons.append(f"15m structure: {coin['structure_note_15m']}")
        if coin["last_candle_bearish_5m"]:
            reasons.append("Last 5m candle closed bearish")
        if coin["lower_high_5m"]:
            reasons.append("5m lower-high sequence forming")

    if side == "long":
        if coin["change_24h"] <= -20:
            reasons.append(f"Large 24h dump: {coin['change_24h']}%")
        if coin["range_pos"] <= 10:
            reasons.append(f"Price near 24h lows: {coin['range_pos']}% of range")
        if coin["bounce_from_low"] >= 3:
            reasons.append(f"Bounce from low: {coin['bounce_from_low']}%")
        if coin["volume_spike_5m"] >= 2:
            reasons.append(f"5m volume spike: {coin['volume_spike_5m']}x")
        if coin["volume_spike_15m"] >= 1.5:
            reasons.append(f"15m volume spike: {coin['volume_spike_15m']}x")
        if coin["sweep_low_5m"]:
            reasons.append("5m low sweep detected")
        if coin["structure_note_5m"] in ["Bullish CHOCH", "Bullish BOS"]:
            reasons.append(f"5m structure: {coin['structure_note_5m']}")
        if coin["structure_note_15m"] in ["Bullish CHOCH", "Bullish BOS"]:
            reasons.append(f"15m structure: {coin['structure_note_15m']}")
        if coin["last_candle_bullish_5m"]:
            reasons.append("Last 5m candle closed bullish")
        if coin["higher_low_5m"]:
            reasons.append("5m higher-low sequence forming")

    if not reasons:
        reasons.append("Score-based watchlist candidate. Manual chart confirmation required.")

    return reasons


def get_coin_active_side(coin):
    if coin["trade_bias"] == "SHORT":
        return {
            "side": "SHORT",
            "score": coin["short_score"],
            "grade": coin["short_grade"],
            "entry": coin["short_entry_readiness"],
        }

    if coin["trade_bias"] == "LONG":
        return {
            "side": "LONG",
            "score": coin["long_score"],
            "grade": coin["long_grade"],
            "entry": coin["long_entry_readiness"],
        }

    return {
        "side": "",
        "score": 0,
        "grade": "",
        "entry": 0,
    }


def structure_agrees(coin):
    side = coin.get("trade_bias", "")

    if side == "SHORT":
        return (
            coin["structure_note_5m"] in ["Bearish CHOCH", "Bearish BOS"]
            and coin["structure_note_15m"] in ["Bearish CHOCH", "Bearish BOS"]
        )

    if side == "LONG":
        return (
            coin["structure_note_5m"] in ["Bullish CHOCH", "Bullish BOS"]
            and coin["structure_note_15m"] in ["Bullish CHOCH", "Bullish BOS"]
        )

    return False


def htf_structure_agrees(coin, timeframe):
    side = coin.get("trade_bias", "")
    key = f"structure_note_{timeframe}"
    note = coin.get(key, "Not checked")

    if side == "SHORT":
        return note in ["Bearish CHOCH", "Bearish BOS"]

    if side == "LONG":
        return note in ["Bullish CHOCH", "Bullish BOS"]

    return False


def htf_alignment_count(coin):
    count = 0
    if htf_structure_agrees(coin, "1h"):
        count += 1
    if htf_structure_agrees(coin, "4h"):
        count += 1
    return count


def quality_breakdown(coin):
    """Institutional-style quality score with visible HTF diagnostics."""
    side = coin.get("trade_bias", "")
    structure = 0
    htf = 0
    volume = 0
    location = 0
    liquidity = 0

    # Structure: 5m + 15m confirmation
    if side == "SHORT":
        if coin.get("structure_note_5m") in ["Bearish CHOCH", "Bearish BOS"]:
            structure += 10
        if coin.get("structure_note_15m") in ["Bearish CHOCH", "Bearish BOS"]:
            structure += 10

        if coin.get("structure_note_1h") in ["Bearish CHOCH", "Bearish BOS"]:
            htf += 10
        if coin.get("structure_note_4h") in ["Bearish CHOCH", "Bearish BOS"]:
            htf += 10

        if coin.get("range_pos", 50) >= 90:
            location += 12
        elif coin.get("range_pos", 50) >= 80:
            location += 8
        if coin.get("pullback_from_high", 0) >= 3:
            location += 8

        if coin.get("sweep_high_5m"):
            liquidity += 14
        if coin.get("lower_high_5m"):
            liquidity += 6

    elif side == "LONG":
        if coin.get("structure_note_5m") in ["Bullish CHOCH", "Bullish BOS"]:
            structure += 10
        if coin.get("structure_note_15m") in ["Bullish CHOCH", "Bullish BOS"]:
            structure += 10

        if coin.get("structure_note_1h") in ["Bullish CHOCH", "Bullish BOS"]:
            htf += 10
        if coin.get("structure_note_4h") in ["Bullish CHOCH", "Bullish BOS"]:
            htf += 10

        if coin.get("range_pos", 50) <= 10:
            location += 12
        elif coin.get("range_pos", 50) <= 20:
            location += 8
        if coin.get("bounce_from_low", 0) >= 3:
            location += 8

        if coin.get("sweep_low_5m"):
            liquidity += 14
        if coin.get("higher_low_5m"):
            liquidity += 6

    # Volume quality from 5m and 15m spike
    spike_5m = safe_float(coin.get("volume_spike_5m"))
    spike_15m = safe_float(coin.get("volume_spike_15m"))

    if spike_5m >= 5:
        volume += 10
    elif spike_5m >= 3:
        volume += 8
    elif spike_5m >= 2:
        volume += 6
    elif spike_5m >= 1.25:
        volume += 3

    if spike_15m >= 4:
        volume += 10
    elif spike_15m >= 2.5:
        volume += 8
    elif spike_15m >= 1.5:
        volume += 6
    elif spike_15m >= 1.25:
        volume += 3

    structure = min(structure, 20)
    htf = min(htf, 20)
    volume = min(volume, 20)
    location = min(location, 20)
    liquidity = min(liquidity, 20)
    total = structure + htf + volume + location + liquidity

    return {
        "quality_structure": structure,
        "quality_htf": htf,
        "quality_volume": volume,
        "quality_location": location,
        "quality_liquidity": liquidity,
        "quality_total": total,
        "htf_alignment": htf_alignment_count(coin),
        "htf_1h_aligned": htf_structure_agrees(coin, "1h"),
        "htf_4h_aligned": htf_structure_agrees(coin, "4h"),
    }


def quality_grade(total):
    if total >= 95:
        return "S"
    if total >= 90:
        return "A+"
    if total >= 80:
        return "A"
    if total >= 70:
        return "B"
    return "C"


def print_quality_breakdown(coin):
    print("Quality Breakdown:")
    print(f"  Structure: {coin.get('quality_structure', 0)}/20")
    print(f"  HTF Alignment: {coin.get('quality_htf', 0)}/20")
    print(f"  Volume Quality: {coin.get('quality_volume', 0)}/20")
    print(f"  Range Location: {coin.get('quality_location', 0)}/20")
    print(f"  Liquidity Event: {coin.get('quality_liquidity', 0)}/20")
    print(f"  TOTAL QUALITY: {coin.get('quality_total', 0)}/100")
    print("HTF Confirmation:")
    print(f"  1H: {coin.get('structure_note_1h', 'Not checked')} | Aligned: {coin.get('htf_1h_aligned', False)}")
    print(f"  4H: {coin.get('structure_note_4h', 'Not checked')} | Aligned: {coin.get('htf_4h_aligned', False)}")
    print(f"  HTF Alignment: {coin.get('htf_alignment', 0)}/2")


def is_s_tier_setup(coin):
    if not structure_agrees(coin):
        return False

    if S_TIER_REQUIRES_1H and not htf_structure_agrees(coin, "1h"):
        return False

    if S_TIER_REQUIRES_4H and not htf_structure_agrees(coin, "4h"):
        return False

    return True


def get_alert_tier(coin):
    active = get_coin_active_side(coin)
    side = active["side"]
    grade_value = active["grade"]
    entry = active["entry"]

    if side == "SHORT":
        both_structure = structure_agrees(coin)

        if grade_value == "A+" and entry >= DISCORD_ENTRY_THRESHOLD and both_structure and is_s_tier_setup(coin):
            return "🏆 S-TIER A+ SHORT SETUP"
        if grade_value == "A+" and entry >= DISCORD_ENTRY_THRESHOLD and both_structure:
            return "🚨 ELITE A+ SHORT SETUP"
        if not ELITE_ONLY and grade_value in ["A+", "A"] and entry >= DISCORD_ENTRY_THRESHOLD:
            return "⚠️ A SHORT WATCHLIST"

    if side == "LONG":
        both_structure = structure_agrees(coin)

        if grade_value == "A+" and entry >= DISCORD_ENTRY_THRESHOLD and both_structure and is_s_tier_setup(coin):
            return "🏆 S-TIER A+ LONG SETUP"
        if grade_value == "A+" and entry >= DISCORD_ENTRY_THRESHOLD and both_structure:
            return "💰 ELITE A+ LONG SETUP"
        if not ELITE_ONLY and grade_value in ["A+", "A"] and entry >= DISCORD_ENTRY_THRESHOLD:
            return "🟢 A LONG WATCHLIST"

    return ""


def load_alerts():
    if not os.path.exists(ALERTS_FILE):
        return {}

    try:
        with open(ALERTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}


def save_alerts(alerts):
    with open(ALERTS_FILE, "w", encoding="utf-8") as f:
        json.dump(alerts, f, indent=2)


def should_send_alert(coin):
    if not DISCORD_WEBHOOK_URL:
        return False

    active = get_coin_active_side(coin)
    side = active["side"]
    score = active["score"]
    grade_value = active["grade"]
    entry = active["entry"]

    if side not in ["SHORT", "LONG"]:
        return False

    if ELITE_ONLY:
        if grade_value != "A+":
            return False
        if not structure_agrees(coin):
            return False
    else:
        if grade_value not in ["A", "A+"]:
            return False

    if entry < DISCORD_ENTRY_THRESHOLD:
        return False

    if coin["rr"] == "":
        return False

    try:
        if float(coin["rr"]) < DISCORD_MIN_RR:
            return False
    except:
        return False

    tier = get_alert_tier(coin)

    if tier == "":
        return False

    alerts = load_alerts()
    key = f"{coin['symbol']}_{side}"
    now = datetime.now()

    if key not in alerts:
        return True

    previous = alerts[key]
    last_time = datetime.fromisoformat(previous["last_alert"])
    cooldown_end = last_time + timedelta(minutes=COOLDOWN_MINUTES)

    previous_score = previous.get("score", 0)
    previous_entry = previous.get("entry", 0)
    previous_tier = previous.get("tier", "")

    tier_improved = tier != previous_tier and "A+" in tier and "A+" not in previous_tier
    score_improved = score >= previous_score + SCORE_IMPROVEMENT_REQUIRED
    entry_improved = entry >= previous_entry + ENTRY_IMPROVEMENT_REQUIRED

    if now >= cooldown_end:
        return True

    if tier_improved or score_improved or entry_improved:
        return True

    return False


def mark_alert_sent(coin):
    alerts = load_alerts()
    active = get_coin_active_side(coin)
    tier = get_alert_tier(coin)
    key = f"{coin['symbol']}_{active['side']}"

    alerts[key] = {
        "last_alert": datetime.now().isoformat(),
        "symbol": coin["symbol"],
        "bias": active["side"],
        "tier": tier,
        "grade": active["grade"],
        "score": active["score"],
        "entry": active["entry"],
        "rr": coin["rr"],
    }

    save_alerts(alerts)


def confidence_bar(value):
    try:
        value = int(value)
    except:
        value = 0

    filled = max(0, min(10, round(value / 10)))
    empty = 10 - filled
    return "█" * filled + "░" * empty


def get_embed_color(coin):
    active = get_coin_active_side(coin)

    if active["side"] == "LONG":
        return 0x2ECC71

    if active["side"] == "SHORT":
        return 0xE74C3C

    return 0xF1C40F


def get_direction_emoji(coin):
    active = get_coin_active_side(coin)
    if active["side"] == "LONG":
        return "🟢"
    if active["side"] == "SHORT":
        return "🔴"
    return "🟡"


def get_alert_header(coin):
    active = get_coin_active_side(coin)
    tier = get_alert_tier(coin)
    emoji = get_direction_emoji(coin)

    if "A+" in tier:
        return f"{emoji} ENTRY READY {active['side']} — {coin['symbol']}"

    return f"{emoji} {active['side']} WATCHLIST — {coin['symbol']}"


def categorize_reasons(coin):
    raw_reasons = coin.get("setup_reasons", "").split(" | ")

    structure_keywords = ["structure", "sweep", "lower-high", "higher-low", "candle"]
    volume_keywords = ["volume", "pump", "dump", "range", "highs", "lows", "pullback", "bounce"]

    structure = []
    volume = []
    other = []

    for reason in raw_reasons:
        clean = reason.strip()
        if not clean:
            continue

        lower = clean.lower()

        if any(k in lower for k in structure_keywords):
            structure.append(clean)
        elif any(k in lower for k in volume_keywords):
            volume.append(clean)
        else:
            other.append(clean)

    return structure[:6], volume[:6], other[:4]


def build_discord_embed(coin):
    active = get_coin_active_side(coin)
    tier = get_alert_tier(coin)
    structure_reasons, catalyst_reasons, other_reasons = categorize_reasons(coin)

    title = f"{ALERT_BRAND_NAME}"

    description = (
        f"{get_alert_header(coin)}\n"
        f"**Alert Type:** {tier}\n"
        f"**Grade:** {active['grade']} | **Score:** {active['score']} | "
        f"**Entry Ready:** {active['entry']} | **R:R:** {coin['rr']}\n"
        f"**Confidence:** `{confidence_bar(active['entry'])}` {active['entry']}%"
    )

    def list_or_none(items):
        if not items:
            return "• Manual confirmation required."
        return "\n".join([f"✅ {item}" for item in items])

    embed = {
        "title": title,
        "url": coin["tradingview"],
        "description": description,
        "color": get_embed_color(coin),
        "fields": [
            {
                "name": "Setup Snapshot",
                "value": (
                    f"Bias: **{coin['trade_bias']}**\n"
                    f"24h Move: **{coin['change_24h']}%**\n"
                    f"Range Position: **{coin['range_pos']}%**\n"
                    f"5m Spike: **{coin['volume_spike_5m']}x**\n"
                    f"15m Spike: **{coin['volume_spike_15m']}x**"
                ),
                "inline": True,
            },
            {
                "name": "Market Structure",
                "value": (
                    f"5m: **{coin['structure_note_5m']}**\n"
                    f"15m: **{coin['structure_note_15m']}**\n"
                    f"1H: **{coin.get('structure_note_1h', 'Not checked')}**\n"
                    f"4H: **{coin.get('structure_note_4h', 'Not checked')}**\n\n"
                    f"{list_or_none(structure_reasons)}"
                )[:1000],
                "inline": True,
            },
            {
                "name": "HTF Confirmation",
                "value": (
                    f"1H: **{coin.get('structure_note_1h', 'Not checked')}** | Aligned: **{coin.get('htf_1h_aligned', False)}**\n"
                    f"4H: **{coin.get('structure_note_4h', 'Not checked')}** | Aligned: **{coin.get('htf_4h_aligned', False)}**\n"
                    f"HTF Alignment: **{coin.get('htf_alignment', 0)}/2**"
                ),
                "inline": False,
            },
            {
                "name": "Quality Breakdown",
                "value": (
                    f"Structure: **{coin.get('quality_structure', 0)}/20**\n"
                    f"HTF Alignment: **{coin.get('quality_htf', 0)}/20**\n"
                    f"Volume Quality: **{coin.get('quality_volume', 0)}/20**\n"
                    f"Range Location: **{coin.get('quality_location', 0)}/20**\n"
                    f"Liquidity Event: **{coin.get('quality_liquidity', 0)}/20**\n"
                    f"Total Quality: **{coin.get('quality_total', 0)}/100**"
                ),
                "inline": False,
            },
            {
                "name": "Catalysts",
                "value": list_or_none(catalyst_reasons)[:1000],
                "inline": False,
            },
            {
                "name": "Trade Plan",
                "value": (
                    f"Entry Zone: **{coin['entry_low']} - {coin['entry_high']}**\n"
                    f"Invalidation: **{coin['stop']}**\n"
                    f"Target 1: **{coin['target1']}**\n"
                    f"Target 2: **{coin['target2']}**\n"
                    f"Estimated R:R: **{coin['rr']}**"
                ),
                "inline": False,
            },
        ],
        "footer": {
            "text": "Liquidity Citadel • Elite scanner alert • Not financial advice. Confirm manually before entry."
        },
        "timestamp": datetime.utcnow().isoformat(),
    }

    if other_reasons:
        embed["fields"].append({
            "name": "Additional Notes",
            "value": list_or_none(other_reasons)[:1000],
            "inline": False,
        })

    if CHART_IMAGE_URL:
        embed["image"] = {"url": CHART_IMAGE_URL}

    return embed


def build_plain_discord_message(coin):
    active = get_coin_active_side(coin)
    tier = get_alert_tier(coin)
    structure_reasons, catalyst_reasons, other_reasons = categorize_reasons(coin)

    def list_lines(items):
        if not items:
            return "• Manual confirmation required."
        return "\n".join([f"✅ {item}" for item in items[:8]])

    return f"""
{ALERT_BRAND_NAME}
{get_alert_header(coin)}

**Alert Type:** {tier}
**Bias:** {coin['trade_bias']}
**Grade:** {active['grade']}
**Score:** {active['score']}
**Entry Readiness:** {active['entry']}
**Confidence:** `{confidence_bar(active['entry'])}` {active['entry']}%
**Estimated R:R:** {coin['rr']}

**Market Snapshot**
24h Move: {coin['change_24h']}%
Range Position: {coin['range_pos']}%
5m Volume Spike: {coin['volume_spike_5m']}x
15m Volume Spike: {coin['volume_spike_15m']}x

**Market Structure**
5m: {coin['structure_note_5m']}
15m: {coin['structure_note_15m']}
1H: {coin.get('structure_note_1h', 'Not checked')}
4H: {coin.get('structure_note_4h', 'Not checked')}
{list_lines(structure_reasons)}

**Catalysts**
{list_lines(catalyst_reasons)}

**Trade Plan**
Entry Zone: {coin['entry_low']} - {coin['entry_high']}
Invalidation: {coin['stop']}
Target 1: {coin['target1']}
Target 2: {coin['target2']}

**Chart:** {coin['tradingview']}

_Not financial advice. Confirm manually before entry._
"""


def load_active_trades():
    if not os.path.exists(ACTIVE_TRADES_FILE):
        return {}

    try:
        with open(ACTIVE_TRADES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}


def save_active_trades(trades):
    with open(ACTIVE_TRADES_FILE, "w", encoding="utf-8") as f:
        json.dump(trades, f, indent=2)


def calculate_trade_return(trade, price):
    """Return percentage and R-multiple from alert price to event price."""
    bias = trade.get("bias", "")
    alert_price = safe_float(trade.get("alert_price"))
    stop = safe_float(trade.get("stop"))
    price = safe_float(price)

    if alert_price <= 0 or price <= 0:
        return 0.0, 0.0

    if bias == "LONG":
        pct = ((price - alert_price) / alert_price) * 100
        risk = alert_price - stop
        r_multiple = (price - alert_price) / risk if risk > 0 else 0.0
    elif bias == "SHORT":
        pct = ((alert_price - price) / alert_price) * 100
        risk = stop - alert_price
        r_multiple = (alert_price - price) / risk if risk > 0 else 0.0
    else:
        pct = 0.0
        r_multiple = 0.0

    return round(pct, 2), round(r_multiple, 2)


def append_trade_history(trade, event, price):
    file_exists = os.path.isfile(TRADE_HISTORY_FILE)
    percent_return, r_multiple = calculate_trade_return(trade, price)

    row = {
        "recorded_at": datetime.now().isoformat(),
        "event": event,
        "symbol": trade.get("symbol", ""),
        "bias": trade.get("bias", ""),
        "grade": trade.get("grade", ""),
        "score": trade.get("score", ""),
        "entry_readiness": trade.get("entry_readiness", ""),
        "entry_low": trade.get("entry_low", ""),
        "entry_high": trade.get("entry_high", ""),
        "stop": trade.get("stop", ""),
        "target1": trade.get("target1", ""),
        "target2": trade.get("target2", ""),
        "rr": trade.get("rr", ""),
        "alert_price": trade.get("alert_price", ""),
        "event_price": price,
        "percent_return": percent_return,
        "r_multiple": r_multiple,
        "opened_at": trade.get("opened_at", ""),
    }

    with open(TRADE_HISTORY_FILE, "a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(row.keys()))
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def read_trade_history():
    if not os.path.exists(TRADE_HISTORY_FILE):
        return []

    try:
        with open(TRADE_HISTORY_FILE, "r", newline="", encoding="utf-8") as file:
            return list(csv.DictReader(file))
    except:
        return []


def calculate_performance_stats():
    rows = read_trade_history()
    trades = load_active_trades()

    tp1_hits = [r for r in rows if r.get("event") == "TP1_HIT"]
    tp2_hits = [r for r in rows if r.get("event") == "TP2_HIT"]
    stops = [r for r in rows if r.get("event") == "STOP_LOSS"]
    closed_rows = tp2_hits + stops

    open_trades = [t for t in trades.values() if t.get("status") == "OPEN"]
    closed_count = len(closed_rows)
    win_count = len(tp2_hits)
    loss_count = len(stops)
    win_rate = round((win_count / closed_count) * 100, 2) if closed_count else 0.0

    def get_float(row, key):
        try:
            return float(row.get(key, 0) or 0)
        except:
            return 0.0

    avg_r = round(sum(get_float(r, "r_multiple") for r in closed_rows) / closed_count, 2) if closed_count else 0.0
    avg_return = round(sum(get_float(r, "percent_return") for r in closed_rows) / closed_count, 2) if closed_count else 0.0

    best = None
    worst = None
    if rows:
        best = max(rows, key=lambda r: get_float(r, "percent_return"))
        worst = min(rows, key=lambda r: get_float(r, "percent_return"))

    stats = {
        "updated_at": datetime.now().isoformat(),
        "history_events": len(rows),
        "open_trades": len(open_trades),
        "closed_trades": closed_count,
        "tp1_hits": len(tp1_hits),
        "tp2_hits": len(tp2_hits),
        "stop_losses": len(stops),
        "wins": win_count,
        "losses": loss_count,
        "win_rate": win_rate,
        "average_r": avg_r,
        "average_return_pct": avg_return,
        "best_symbol": best.get("symbol", "") if best else "",
        "best_event": best.get("event", "") if best else "",
        "best_return_pct": get_float(best, "percent_return") if best else 0.0,
        "worst_symbol": worst.get("symbol", "") if worst else "",
        "worst_event": worst.get("event", "") if worst else "",
        "worst_return_pct": get_float(worst, "percent_return") if worst else 0.0,
    }

    return stats


def save_performance_stats(stats):
    try:
        with open(SCANNER_STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=2)
    except Exception as e:
        print(f"Stats save error: {e}")


def print_performance_dashboard():
    stats = calculate_performance_stats()
    save_performance_stats(stats)

    print("\n" + "=" * 80)
    print("SCANNER PERFORMANCE DASHBOARD v28")
    print("=" * 80)
    print(f"Open Trades: {stats['open_trades']}")
    print(f"Closed Trades: {stats['closed_trades']}")
    print(f"TP1 Hits: {stats['tp1_hits']} | TP2 Hits: {stats['tp2_hits']} | Stops: {stats['stop_losses']}")
    print(f"Win Rate: {stats['win_rate']}% | Avg R: {stats['average_r']} | Avg Return: {stats['average_return_pct']}%")
    if stats['best_symbol']:
        print(f"Best: {stats['best_symbol']} {stats['best_return_pct']}% | Worst: {stats['worst_symbol']} {stats['worst_return_pct']}%")
    print("=" * 80)


def send_performance_stats_discord(trigger_event="TRADE_UPDATE"):
    if not DISCORD_WEBHOOK_URL:
        return

    stats = calculate_performance_stats()
    save_performance_stats(stats)

    if stats["closed_trades"] < MIN_CLOSED_TRADES_FOR_WIN_RATE:
        return

    embed = {
        "title": "🏰 LIQUIDITY CITADEL SCANNER PERFORMANCE",
        "description": f"Performance update triggered by **{trigger_event}**.",
        "color": 0xF1C40F,
        "fields": [
            {
                "name": "Trade Results",
                "value": (
                    f"Open Trades: **{stats['open_trades']}**\n"
                    f"Closed Trades: **{stats['closed_trades']}**\n"
                    f"TP1 Hits: **{stats['tp1_hits']}**\n"
                    f"TP2 Hits: **{stats['tp2_hits']}**\n"
                    f"Stop Losses: **{stats['stop_losses']}**"
                ),
                "inline": True,
            },
            {
                "name": "Performance",
                "value": (
                    f"Win Rate: **{stats['win_rate']}%**\n"
                    f"Average R: **{stats['average_r']}R**\n"
                    f"Average Return: **{stats['average_return_pct']}%**"
                ),
                "inline": True,
            },
            {
                "name": "Best / Worst",
                "value": (
                    f"Best: **{stats['best_symbol']} {stats['best_return_pct']}%**\n"
                    f"Worst: **{stats['worst_symbol']} {stats['worst_return_pct']}%**"
                ),
                "inline": False,
            },
        ],
        "footer": {"text": "Liquidity Citadel • Scanner performance analytics • Not financial advice."},
        "timestamp": datetime.utcnow().isoformat(),
    }

    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
        if r.status_code in [200, 204]:
            print("Performance stats posted to Discord.")
        else:
            print(f"Performance stats failed: {r.status_code} {r.text}")
    except Exception as e:
        print(f"Performance stats error: {e}")



def parse_iso_datetime(value):
    try:
        return datetime.fromisoformat(value)
    except:
        return None


def recent_trade_history(hours=24):
    rows = read_trade_history()
    cutoff = datetime.now() - timedelta(hours=hours)
    recent = []

    for row in rows:
        ts = parse_iso_datetime(row.get("recorded_at", ""))
        if ts and ts >= cutoff:
            recent.append(row)

    return recent


def leaderboard_rows(hours=24, top_n=5):
    rows = recent_trade_history(hours)

    closed = [r for r in rows if r.get("event") in ["TP2_HIT", "STOP_LOSS"]]
    tp1 = [r for r in rows if r.get("event") == "TP1_HIT"]

    def get_float(row, key):
        try:
            return float(row.get(key, 0) or 0)
        except:
            return 0.0

    winners = sorted(
        [r for r in closed if get_float(r, "percent_return") > 0],
        key=lambda r: get_float(r, "percent_return"),
        reverse=True,
    )[:top_n]

    losers = sorted(
        [r for r in closed if get_float(r, "percent_return") < 0],
        key=lambda r: get_float(r, "percent_return"),
    )[:top_n]

    momentum = sorted(
        rows,
        key=lambda r: get_float(r, "percent_return"),
        reverse=True,
    )[:top_n]

    closed_count = len(closed)
    wins = len([r for r in closed if r.get("event") == "TP2_HIT"])
    losses = len([r for r in closed if r.get("event") == "STOP_LOSS"])
    win_rate = round((wins / closed_count) * 100, 2) if closed_count else 0.0

    return {
        "lookback_hours": hours,
        "closed_count": closed_count,
        "tp1_count": len(tp1),
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "winners": winners,
        "losers": losers,
        "momentum": momentum,
    }


def format_leaderboard_line(row, rank=None):
    symbol = row.get("symbol", "")
    event = row.get("event", "")
    bias = row.get("bias", "")

    try:
        pct = float(row.get("percent_return", 0) or 0)
    except:
        pct = 0.0

    try:
        r_mult = float(row.get("r_multiple", 0) or 0)
    except:
        r_mult = 0.0

    prefix = f"{rank}. " if rank is not None else ""
    sign = "+" if pct > 0 else ""
    return f"{prefix}**{symbol}** {bias} | {event} | {sign}{pct}% | {r_mult}R"


def print_leaderboard_dashboard():
    board = leaderboard_rows(LEADERBOARD_LOOKBACK_HOURS, LEADERBOARD_TOP_N)

    print("\n" + "=" * 80)
    print(f"SCANNER LEADERBOARD v29 — LAST {LEADERBOARD_LOOKBACK_HOURS} HOURS")
    print("=" * 80)
    print(f"Closed: {board['closed_count']} | TP1 Events: {board['tp1_count']} | Wins: {board['wins']} | Losses: {board['losses']} | Win Rate: {board['win_rate']}%")

    if board["winners"]:
        print("\nTop Winners:")
        for i, row in enumerate(board["winners"], 1):
            print(format_leaderboard_line(row, i).replace("**", ""))
    else:
        print("\nTop Winners: none yet.")

    if board["losers"]:
        print("\nStopped Trades:")
        for i, row in enumerate(board["losers"], 1):
            print(format_leaderboard_line(row, i).replace("**", ""))
    else:
        print("\nStopped Trades: none yet.")

    print("=" * 80)


def send_leaderboard_discord(trigger_event="TRADE CLOSED"):
    if not DISCORD_WEBHOOK_URL:
        return

    board = leaderboard_rows(LEADERBOARD_LOOKBACK_HOURS, LEADERBOARD_TOP_N)

    if board["closed_count"] <= 0 and board["tp1_count"] <= 0:
        return

    winners_text = "None yet."
    if board["winners"]:
        winners_text = "\n".join([
            format_leaderboard_line(row, i)
            for i, row in enumerate(board["winners"], 1)
        ])

    losers_text = "None yet."
    if board["losers"]:
        losers_text = "\n".join([
            format_leaderboard_line(row, i)
            for i, row in enumerate(board["losers"], 1)
        ])

    momentum_text = "None yet."
    if board["momentum"]:
        momentum_text = "\n".join([
            format_leaderboard_line(row, i)
            for i, row in enumerate(board["momentum"], 1)
        ])

    embed = {
        "title": "🏆 LIQUIDITY CITADEL SCANNER LEADERBOARD",
        "description": (
            f"Leaderboard update triggered by **{trigger_event}**.\n"
            f"Lookback: **Last {LEADERBOARD_LOOKBACK_HOURS} hours**"
        ),
        "color": 0x2ECC71,
        "fields": [
            {
                "name": "Session Summary",
                "value": (
                    f"Closed Trades: **{board['closed_count']}**\n"
                    f"TP1 Events: **{board['tp1_count']}**\n"
                    f"Wins: **{board['wins']}**\n"
                    f"Losses: **{board['losses']}**\n"
                    f"Win Rate: **{board['win_rate']}%**"
                ),
                "inline": False,
            },
            {
                "name": "🏆 Top Winners",
                "value": winners_text[:1000],
                "inline": False,
            },
            {
                "name": "📈 Strongest Moves / TP Events",
                "value": momentum_text[:1000],
                "inline": False,
            },
            {
                "name": "🛑 Stopped Trades",
                "value": losers_text[:1000],
                "inline": False,
            },
        ],
        "footer": {"text": "Liquidity Citadel • Scanner leaderboard • Not financial advice."},
        "timestamp": datetime.utcnow().isoformat(),
    }

    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
        if r.status_code in [200, 204]:
            print("Leaderboard posted to Discord.")
        else:
            print(f"Leaderboard failed: {r.status_code} {r.text}")
    except Exception as e:
        print(f"Leaderboard error: {e}")

def register_active_trade(coin):
    if not coin.get("trade_bias"):
        return

    try:
        entry_low = float(coin["entry_low"])
        entry_high = float(coin["entry_high"])
        stop = float(coin["stop"])
        target1 = float(coin["target1"])
        target2 = float(coin["target2"])
        rr = float(coin["rr"])
    except:
        return

    active = get_coin_active_side(coin)
    trades = load_active_trades()
    key = f"{coin['symbol']}_{coin['trade_bias']}"

    existing = trades.get(key)
    if existing and existing.get("status") == "OPEN":
        return

    trades[key] = {
        "symbol": coin["symbol"],
        "bias": coin["trade_bias"],
        "grade": active["grade"],
        "score": active["score"],
        "entry_readiness": active["entry"],
        "entry_low": entry_low,
        "entry_high": entry_high,
        "stop": stop,
        "target1": target1,
        "target2": target2,
        "rr": rr,
        "alert_price": safe_float(coin.get("last", 0)),
        "opened_at": datetime.now().isoformat(),
        "last_update_at": "",
        "tp1_hit": False,
        "tp2_hit": False,
        "status": "OPEN",
    }

    save_active_trades(trades)
    print(f"Tracking active trade: {coin['symbol']} {coin['trade_bias']}")


def ticker_price_map(tickers):
    prices = {}
    for ticker in tickers:
        symbol = ticker.get("instId", "")
        last = safe_float(ticker.get("last"))
        if symbol and last > 0:
            prices[symbol] = last
    return prices


def trade_event_from_price(trade, price):
    bias = trade.get("bias")
    stop = safe_float(trade.get("stop"))
    target1 = safe_float(trade.get("target1"))
    target2 = safe_float(trade.get("target2"))

    if bias == "LONG":
        if price <= stop:
            return "STOP_LOSS"
        if price >= target2:
            return "TP2_HIT"
        if price >= target1 and not trade.get("tp1_hit"):
            return "TP1_HIT"

    if bias == "SHORT":
        if price >= stop:
            return "STOP_LOSS"
        if price <= target2:
            return "TP2_HIT"
        if price <= target1 and not trade.get("tp1_hit"):
            return "TP1_HIT"

    return ""


def trade_update_title(event):
    if event == "TP1_HIT":
        return "🟢 TP1 HIT"
    if event == "TP2_HIT":
        return "🏁 TP2 HIT — TRADE COMPLETE"
    if event == "STOP_LOSS":
        return "🔴 STOP LOSS HIT — TRADE CLOSED"
    return "Trade Update"


def trade_update_color(event):
    if event in ["TP1_HIT", "TP2_HIT"]:
        return 0x2ECC71
    if event == "STOP_LOSS":
        return 0xE74C3C
    return 0x3498DB


def send_trade_update_discord(trade, event, price):
    if not DISCORD_WEBHOOK_URL or not TRACK_TRADE_UPDATES_IN_DISCORD:
        return

    title = trade_update_title(event)
    symbol = trade.get("symbol", "")
    bias = trade.get("bias", "")

    if USE_DISCORD_EMBEDS:
        embed = {
            "title": f"{title} — {symbol}",
            "description": f"**Bias:** {bias}\n**Current Price:** {price}\n**Opened:** {trade.get('opened_at', '')}",
            "color": trade_update_color(event),
            "fields": [
                {
                    "name": "Original Trade Plan",
                    "value": (
                        f"Entry Zone: **{trade.get('entry_low')} - {trade.get('entry_high')}**\n"
                        f"Invalidation: **{trade.get('stop')}**\n"
                        f"Target 1: **{trade.get('target1')}**\n"
                        f"Target 2: **{trade.get('target2')}**\n"
                        f"Estimated R:R: **{trade.get('rr')}**"
                    ),
                    "inline": False,
                },
                {
                    "name": "Tracker Status",
                    "value": (
                        f"TP1 Hit: **{trade.get('tp1_hit', False)}**\n"
                        f"TP2 Hit: **{trade.get('tp2_hit', False)}**\n"
                        f"Status: **{trade.get('status', 'OPEN')}**"
                    ),
                    "inline": False,
                },
            ],
            "footer": {"text": "Liquidity Citadel • Trade tracker • Not financial advice."},
            "timestamp": datetime.utcnow().isoformat(),
        }
        payload = {"embeds": [embed]}
    else:
        payload = {
            "content": (
                f"{title} — {symbol}\n\n"
                f"Bias: {bias}\n"
                f"Current Price: {price}\n"
                f"Entry: {trade.get('entry_low')} - {trade.get('entry_high')}\n"
                f"Invalidation: {trade.get('stop')}\n"
                f"TP1: {trade.get('target1')}\n"
                f"TP2: {trade.get('target2')}\n"
            )
        }

    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        if r.status_code in [200, 204]:
            print(f"Trade update sent: {symbol} {event}")
        else:
            print(f"Trade update failed: {r.status_code} {r.text}")
    except Exception as e:
        print(f"Trade update error: {e}")


def update_active_trades(tickers):
    trades = load_active_trades()
    if not trades:
        print("Trade tracker: no active trades.")
        return

    prices = ticker_price_map(tickers)
    changed = False
    updates_sent = 0
    closed_updates = 0
    tp1_updates = 0
    now = datetime.now()

    for key, trade in list(trades.items()):
        if trade.get("status") != "OPEN":
            continue

        symbol = trade.get("symbol")
        if symbol not in prices:
            continue

        price = prices[symbol]
        event = trade_event_from_price(trade, price)
        if not event:
            continue

        last_update = trade.get("last_update_at")
        if last_update:
            try:
                cooldown_end = datetime.fromisoformat(last_update) + timedelta(minutes=TRADE_UPDATE_COOLDOWN_MINUTES)
                if now < cooldown_end and event == "TP1_HIT":
                    continue
            except:
                pass

        if event == "TP1_HIT":
            trade["tp1_hit"] = True
            trade["last_update_at"] = now.isoformat()
            send_trade_update_discord(trade, event, price)
            append_trade_history(trade, event, price)
            updates_sent += 1
            tp1_updates += 1
            changed = True

        elif event == "TP2_HIT":
            trade["tp1_hit"] = True
            trade["tp2_hit"] = True
            trade["status"] = "CLOSED_TP2"
            trade["closed_at"] = now.isoformat()
            trade["last_update_at"] = now.isoformat()
            send_trade_update_discord(trade, event, price)
            append_trade_history(trade, event, price)
            updates_sent += 1
            closed_updates += 1
            changed = True

        elif event == "STOP_LOSS":
            trade["status"] = "CLOSED_STOP"
            trade["closed_at"] = now.isoformat()
            trade["last_update_at"] = now.isoformat()
            send_trade_update_discord(trade, event, price)
            append_trade_history(trade, event, price)
            updates_sent += 1
            closed_updates += 1
            changed = True

    if changed:
        save_active_trades(trades)

    open_count = sum(1 for t in trades.values() if t.get("status") == "OPEN")
    print(f"Trade tracker processed. Open trades: {open_count} | Updates sent: {updates_sent}")

    if POST_STATS_AFTER_CLOSED_TRADE and closed_updates > 0:
        send_performance_stats_discord("TRADE CLOSED")
        if POST_LEADERBOARD_AFTER_CLOSED_TRADE:
            send_leaderboard_discord("TRADE CLOSED")
    elif POST_STATS_AFTER_TP1 and tp1_updates > 0:
        send_performance_stats_discord("TP1 HIT")

def send_discord_alert(coin):
    if not DISCORD_WEBHOOK_URL:
        return

    tier = get_alert_tier(coin)
    mention = f"{VIP_ROLE_MENTION}\n" if PING_ROLE_ON_ELITE and VIP_ROLE_MENTION else ""

    if USE_DISCORD_EMBEDS:
        payload = {
            "content": mention if mention else None,
            "embeds": [build_discord_embed(coin)],
        }
    else:
        payload = {"content": mention + build_plain_discord_message(coin)}

    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)

        if r.status_code in [200, 204]:
            print(f"Discord alert sent: {coin['symbol']} {coin['trade_bias']} {tier}")
            mark_alert_sent(coin)
            register_active_trade(coin)
        else:
            print(f"Discord alert failed: {r.status_code} {r.text}")

    except Exception as e:
        print(f"Discord alert error: {e}")


def build_results(tickers):
    base = []

    for ticker in tickers:
        metrics = get_base_metrics(ticker)
        if metrics:
            base.append(metrics)

    movers = [x for x in base if abs(x["change_24h"]) >= MIN_24H_MOVE_FOR_CANDLES]
    movers = sorted(movers, key=lambda x: abs(x["change_24h"]), reverse=True)
    movers_to_check = movers[:MAX_CANDLE_CHECKS]

    candle_cache = {}

    for m in movers_to_check:
        candles_5m = get_candles(m["symbol"], "5m", 80)
        candles_15m = get_candles(m["symbol"], "15m", 80)
        candles_1h = get_candles(m["symbol"], HTF_1H_BAR, HTF_CANDLE_LIMIT) if ENABLE_HTF_CONFIRMATION else []
        candles_4h = get_candles(m["symbol"], HTF_4H_BAR, HTF_CANDLE_LIMIT) if ENABLE_HTF_CONFIRMATION else []

        volume_5m, spike_5m = get_volume_spike(candles_5m)
        volume_15m, spike_15m = get_volume_spike(candles_15m)

        structure_5m = detect_structure(candles_5m)
        structure_15m = detect_structure(candles_15m)
        structure_1h = detect_structure(candles_1h) if ENABLE_HTF_CONFIRMATION else {"structure_note": "Not checked", "bearish_choch": False, "bullish_choch": False, "bearish_bos": False, "bullish_bos": False, "sweep_high": False, "sweep_low": False}
        structure_4h = detect_structure(candles_4h) if ENABLE_HTF_CONFIRMATION else {"structure_note": "Not checked", "bearish_choch": False, "bullish_choch": False, "bearish_bos": False, "bullish_bos": False, "sweep_high": False, "sweep_low": False}
        behavior = analyze_candle_behavior(candles_5m)

        candle_cache[m["symbol"]] = {
            "candles_5m": candles_5m,
            "volume_5m": volume_5m,
            "volume_spike_5m": spike_5m,
            "volume_15m": volume_15m,
            "volume_spike_15m": spike_15m,
            "bearish_choch_5m": structure_5m["bearish_choch"],
            "bullish_choch_5m": structure_5m["bullish_choch"],
            "bearish_bos_5m": structure_5m["bearish_bos"],
            "bullish_bos_5m": structure_5m["bullish_bos"],
            "sweep_high_5m": structure_5m["sweep_high"],
            "sweep_low_5m": structure_5m["sweep_low"],
            "structure_note_5m": structure_5m["structure_note"],
            "bearish_choch_15m": structure_15m["bearish_choch"],
            "bullish_choch_15m": structure_15m["bullish_choch"],
            "bearish_bos_15m": structure_15m["bearish_bos"],
            "bullish_bos_15m": structure_15m["bullish_bos"],
            "sweep_high_15m": structure_15m["sweep_high"],
            "sweep_low_15m": structure_15m["sweep_low"],
            "structure_note_15m": structure_15m["structure_note"],
            "bearish_choch_1h": structure_1h["bearish_choch"],
            "bullish_choch_1h": structure_1h["bullish_choch"],
            "bearish_bos_1h": structure_1h["bearish_bos"],
            "bullish_bos_1h": structure_1h["bullish_bos"],
            "structure_note_1h": structure_1h["structure_note"],
            "bearish_choch_4h": structure_4h["bearish_choch"],
            "bullish_choch_4h": structure_4h["bullish_choch"],
            "bearish_bos_4h": structure_4h["bearish_bos"],
            "bullish_bos_4h": structure_4h["bullish_bos"],
            "structure_note_4h": structure_4h["structure_note"],
            **behavior,
        }

    results = []

    for m in base:
        symbol = m["symbol"]

        if symbol in candle_cache:
            m.update(candle_cache[symbol])
        else:
            m.update({
                "candles_5m": [],
                "volume_5m": 0.0,
                "volume_spike_5m": 1.0,
                "volume_15m": 0.0,
                "volume_spike_15m": 1.0,
                "bearish_choch_5m": False,
                "bullish_choch_5m": False,
                "bearish_bos_5m": False,
                "bullish_bos_5m": False,
                "sweep_high_5m": False,
                "sweep_low_5m": False,
                "structure_note_5m": "Not checked",
                "bearish_choch_15m": False,
                "bullish_choch_15m": False,
                "bearish_bos_15m": False,
                "bullish_bos_15m": False,
                "sweep_high_15m": False,
                "sweep_low_15m": False,
                "structure_note_15m": "Not checked",
                "bearish_choch_1h": False,
                "bullish_choch_1h": False,
                "bearish_bos_1h": False,
                "bullish_bos_1h": False,
                "structure_note_1h": "Not checked",
                "bearish_choch_4h": False,
                "bullish_choch_4h": False,
                "bearish_bos_4h": False,
                "bullish_bos_4h": False,
                "structure_note_4h": "Not checked",
                "last_candle_bearish_5m": False,
                "last_candle_bullish_5m": False,
                "lower_high_5m": False,
                "higher_low_5m": False,
            })

        m["short_score"] = short_opportunity_score(m)
        m["long_score"] = long_opportunity_score(m)

        short_htf_bonus = 0
        long_htf_bonus = 0
        if m.get("structure_note_1h") in ["Bearish CHOCH", "Bearish BOS"]:
            short_htf_bonus += HTF_ALIGNMENT_BONUS
        if m.get("structure_note_4h") in ["Bearish CHOCH", "Bearish BOS"]:
            short_htf_bonus += HTF_ALIGNMENT_BONUS
        if m.get("structure_note_1h") in ["Bullish CHOCH", "Bullish BOS"]:
            long_htf_bonus += HTF_ALIGNMENT_BONUS
        if m.get("structure_note_4h") in ["Bullish CHOCH", "Bullish BOS"]:
            long_htf_bonus += HTF_ALIGNMENT_BONUS

        m["short_score"] = max(0, min(100, m["short_score"] + short_htf_bonus))
        m["long_score"] = max(0, min(100, m["long_score"] + long_htf_bonus))
        m["short_htf_bonus"] = short_htf_bonus
        m["long_htf_bonus"] = long_htf_bonus

        m["short_grade"] = grade(m["short_score"])
        m["long_grade"] = grade(m["long_score"])
        m["short_entry_readiness"] = entry_readiness_score(m, "short")
        m["long_entry_readiness"] = entry_readiness_score(m, "long")

        signal = classify_coin(m)

        if signal == "Neutral":
            continue

        tv_symbol = symbol.replace("-", "")
        tradingview = f"https://www.tradingview.com/chart/?symbol=BLOFIN:{tv_symbol}.P"

        if "SHORT" in signal:
            trade_plan = build_short_trade_plan(m["candles_5m"])
        elif "LONG" in signal:
            trade_plan = build_long_trade_plan(m["candles_5m"])
        elif m["short_score"] > m["long_score"]:
            trade_plan = build_short_trade_plan(m["candles_5m"])
        else:
            trade_plan = build_long_trade_plan(m["candles_5m"])

        result = {
            "symbol": symbol,
            "signal": signal,
            "short_grade": m["short_grade"],
            "long_grade": m["long_grade"],
            "short_score": m["short_score"],
            "long_score": m["long_score"],
            "short_entry_readiness": m["short_entry_readiness"],
            "long_entry_readiness": m["long_entry_readiness"],
            "last": round(m["last"], 8),
            "change_24h": round(m["change_24h"], 2),
            "range_pos": round(m["range_pos"], 2),
            "pullback_from_high": round(m["pullback_from_high"], 2),
            "bounce_from_low": round(m["bounce_from_low"], 2),
            "volume_24h": round(m["volume_24h"], 2),
            "volume_5m": round(m["volume_5m"], 2),
            "volume_spike_5m": m["volume_spike_5m"],
            "volume_15m": round(m["volume_15m"], 2),
            "volume_spike_15m": m["volume_spike_15m"],
            "sweep_high_5m": m["sweep_high_5m"],
            "sweep_low_5m": m["sweep_low_5m"],
            "structure_note_5m": m["structure_note_5m"],
            "structure_note_15m": m["structure_note_15m"],
            "structure_note_1h": m.get("structure_note_1h", "Not checked"),
            "structure_note_4h": m.get("structure_note_4h", "Not checked"),
            "htf_bonus": m.get("short_htf_bonus", 0) if trade_plan and trade_plan["bias"] == "SHORT" else m.get("long_htf_bonus", 0),
            "last_candle_bearish_5m": m["last_candle_bearish_5m"],
            "last_candle_bullish_5m": m["last_candle_bullish_5m"],
            "lower_high_5m": m["lower_high_5m"],
            "higher_low_5m": m["higher_low_5m"],
            "tradingview": tradingview,
            "trade_bias": trade_plan["bias"] if trade_plan else "",
            "entry_low": trade_plan["entry_low"] if trade_plan else "",
            "entry_high": trade_plan["entry_high"] if trade_plan else "",
            "stop": trade_plan["stop"] if trade_plan else "",
            "target1": trade_plan["target1"] if trade_plan else "",
            "target2": trade_plan["target2"] if trade_plan else "",
            "rr": trade_plan["rr"] if trade_plan else "",
        }

        q = quality_breakdown(result)
        result.update(q)

        # v32.3: Use quality engine to rebalance visible grade/score for the active side.
        # This reduces "everything is A+" clustering while preserving the legacy fields as backup.
        q_total = result["quality_total"]
        q_grade = quality_grade(q_total)
        if result["trade_bias"] == "SHORT":
            result["short_score"] = q_total
            result["short_grade"] = q_grade
        elif result["trade_bias"] == "LONG":
            result["long_score"] = q_total
            result["long_grade"] = q_grade

        if result["trade_bias"] == "SHORT":
            setup_reasons = build_setup_reasons(result, "short")
        elif result["trade_bias"] == "LONG":
            setup_reasons = build_setup_reasons(result, "long")
        else:
            setup_reasons = ["Score-based watchlist candidate. Manual chart confirmation required."]

        result["setup_reasons"] = " | ".join(setup_reasons)
        results.append(result)

    return results


def log_results(results):
    if not results:
        return

    file_exists = os.path.isfile(LOG_FILE)

    with open(LOG_FILE, "a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(results[0].keys()) + ["scan_time"])

        if not file_exists:
            writer.writeheader()

        scan_time = datetime.now().isoformat()

        for row in results:
            writer.writerow({**row, "scan_time": scan_time})


def print_reasons(coin):
    reasons = coin["setup_reasons"].split(" | ")
    print("Reasons:")
    for reason in reasons:
        print(f"  - {reason}")


def print_trade_plan(coin):
    if not coin["trade_bias"]:
        return

    print("Trade Plan:")
    print(f"  Bias: {coin['trade_bias']}")
    print(f"  Entry Zone: {coin['entry_low']} - {coin['entry_high']}")
    print(f"  Invalidation: {coin['stop']}")
    print(f"  Target 1: {coin['target1']}")
    print(f"  Target 2: {coin['target2']}")
    print(f"  Estimated R:R: {coin['rr']}")


def print_section(title, coins, sort_key, limit=10):
    print(f"\n{title}")
    print("-" * 190)

    if not coins:
        print("None right now.")
        return

    coins = sorted(coins, key=lambda x: x[sort_key], reverse=True)

    for coin in coins[:limit]:
        is_short = "SHORT" in coin["signal"]
        active_grade = coin["short_grade"] if is_short else coin["long_grade"]
        active_score = coin["short_score"] if is_short else coin["long_score"]
        active_entry = coin["short_entry_readiness"] if is_short else coin["long_entry_readiness"]

        print(
            f"{coin['symbol']} | "
            f"Bias: {coin['trade_bias']} | "
            f"Grade: {active_grade} | "
            f"Score: {active_score} | "
            f"Entry: {active_entry} | "
            f"24h: {coin['change_24h']}% | "
            f"Range: {coin['range_pos']}% | "
            f"5m Spike: {coin['volume_spike_5m']}x | "
            f"15m Spike: {coin['volume_spike_15m']}x | "
            f"5m: {coin['structure_note_5m']} | "
            f"15m: {coin['structure_note_15m']} | "
            f"1H: {coin.get('structure_note_1h', 'Not checked')} | "
            f"4H: {coin.get('structure_note_4h', 'Not checked')} | "
            f"HTF: {coin.get('htf_alignment', 0)}/2 | "
            f"Quality: {coin.get('quality_total', 0)}/100 | "
            f"{coin['signal']}"
        )
        print_quality_breakdown(coin)
        print_reasons(coin)
        print_trade_plan(coin)


def print_entry_ready(shorts, longs):
    print("\n" + "=" * 190)
    print("ENTRY READY WATCHLIST")
    print("=" * 190)

    ready_shorts = [x for x in shorts if x["short_entry_readiness"] >= ENTRY_READY_SCORE]
    ready_longs = [x for x in longs if x["long_entry_readiness"] >= ENTRY_READY_SCORE]

    if not ready_shorts and not ready_longs:
        print("No entry-ready setups currently.")
        return

    if ready_shorts:
        print("\nENTRY-READY SHORTS")
        print("-" * 190)

        for coin in sorted(ready_shorts, key=lambda x: x["short_entry_readiness"], reverse=True)[:3]:
            print_section("", [coin], "short_entry_readiness", limit=1)
            print(f"Chart: {coin['tradingview']}")

    if ready_longs:
        print("\nENTRY-READY LONGS")
        print("-" * 190)

        for coin in sorted(ready_longs, key=lambda x: x["long_entry_readiness"], reverse=True)[:3]:
            print_section("", [coin], "long_entry_readiness", limit=1)
            print(f"Chart: {coin['tradingview']}")


def print_high_conviction(shorts, longs):
    print("\n" + "=" * 190)
    print("HIGH CONVICTION TRADE OPPORTUNITIES")
    print("=" * 190)

    a_shorts = [x for x in shorts if x["short_grade"] in ["A+", "A"]]
    a_longs = [x for x in longs if x["long_grade"] in ["A+", "A"]]

    if not a_shorts and not a_longs:
        print("No A-grade setups currently.")
        return

    if a_shorts:
        print("\nA-GRADE SHORTS")
        print("-" * 190)

        for coin in sorted(a_shorts, key=lambda x: x["short_score"], reverse=True)[:3]:
            print_section("", [coin], "short_score", limit=1)

    if a_longs:
        print("\nA-GRADE LONGS")
        print("-" * 190)

        for coin in sorted(a_longs, key=lambda x: x["long_score"], reverse=True)[:3]:
            print_section("", [coin], "long_score", limit=1)


def process_discord_alerts(results):
    eligible = [coin for coin in results if should_send_alert(coin)]

    def alert_sort_key(coin):
        active = get_coin_active_side(coin)
        return (
            1 if active["grade"] == "A+" else 0,
            active["entry"],
            active["score"],
        )

    eligible = sorted(eligible, key=alert_sort_key, reverse=True)
    alerts_sent = 0

    for coin in eligible[:MAX_DISCORD_ALERTS_PER_SCAN]:
        send_discord_alert(coin)
        alerts_sent += 1

    if DISCORD_WEBHOOK_URL and DISCORD_WEBHOOK_URL != "PASTE_YOUR_WEBHOOK_HERE":
        print(f"Discord elite alerts processed. Eligible: {len(eligible)} | Sent: {alerts_sent}")
    else:
        print("Discord webhook not set. Skipping Discord alerts.")


def load_daily_report_state():
    if not os.path.exists(DAILY_REPORT_FILE):
        return {}
    try:
        with open(DAILY_REPORT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}


def save_daily_report_state(state):
    try:
        with open(DAILY_REPORT_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"Daily report state save error: {e}")


def should_send_daily_report():
    if not POST_DAILY_REPORT_TO_DISCORD:
        return False

    now = datetime.utcnow()
    if now.hour != DAILY_REPORT_HOUR_UTC:
        return False

    today_key = now.strftime("%Y-%m-%d")
    state = load_daily_report_state()
    return state.get("last_report_date") != today_key


def mark_daily_report_sent():
    state = load_daily_report_state()
    state["last_report_date"] = datetime.utcnow().strftime("%Y-%m-%d")
    state["sent_at"] = datetime.utcnow().isoformat()
    save_daily_report_state(state)


def send_daily_report_discord():
    if not DISCORD_WEBHOOK_URL or not should_send_daily_report():
        return

    stats = calculate_performance_stats()
    board = leaderboard_rows(24, LEADERBOARD_TOP_N)

    winners_text = "None yet."
    if board["winners"]:
        winners_text = "\n".join([format_leaderboard_line(row, i) for i, row in enumerate(board["winners"], 1)])

    losers_text = "None yet."
    if board["losers"]:
        losers_text = "\n".join([format_leaderboard_line(row, i) for i, row in enumerate(board["losers"], 1)])

    embed = {
        "title": "📊 LIQUIDITY CITADEL DAILY SCANNER REPORT",
        "description": "Automated 24h scanner performance summary.",
        "color": 0x3498DB,
        "fields": [
            {
                "name": "Performance Snapshot",
                "value": (
                    f"Open Trades: **{stats['open_trades']}**\n"
                    f"Closed Trades: **{stats['closed_trades']}**\n"
                    f"TP1 Hits: **{stats['tp1_hits']}**\n"
                    f"TP2 Hits: **{stats['tp2_hits']}**\n"
                    f"Stops: **{stats['stop_losses']}**\n"
                    f"Win Rate: **{stats['win_rate']}%**\n"
                    f"Average R: **{stats['average_r']}R**"
                ),
                "inline": False,
            },
            {
                "name": "🏆 Top Winners Last 24h",
                "value": winners_text[:1000],
                "inline": False,
            },
            {
                "name": "🛑 Stopped Trades Last 24h",
                "value": losers_text[:1000],
                "inline": False,
            },
        ],
        "footer": {"text": "Liquidity Citadel • Daily scanner report • Not financial advice."},
        "timestamp": datetime.utcnow().isoformat(),
    }

    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=10)
        if r.status_code in [200, 204]:
            print("Daily report posted to Discord.")
            mark_daily_report_sent()
        else:
            print(f"Daily report failed: {r.status_code} {r.text}")
    except Exception as e:
        print(f"Daily report error: {e}")


def run_scanner():
    print("\n" + "=" * 190)
    print("CITADEL TRADE OPPORTUNITY SCANNER v32.3 — QUALITY ENGINE + VISIBLE HTF")
    print(f"Scan Time: {datetime.now()}")
    print("=" * 190)

    try:
        tickers = get_blofin_tickers()
    except Exception as e:
        print(f"API error: {e}")
        return

    update_active_trades(tickers)
    print_performance_dashboard()
    print_leaderboard_dashboard()
    send_daily_report_discord()

    results = build_results(tickers)

    if not results:
        print("No candidates found right now.")
        return

    short_candidates = [x for x in results if x["short_score"] >= B_SCORE]
    long_candidates = [x for x in results if x["long_score"] >= B_SCORE]

    a_shorts = [x for x in short_candidates if x["short_grade"] in ["A+", "A"]]
    a_longs = [x for x in long_candidates if x["long_grade"] in ["A+", "A"]]
    b_shorts = [x for x in short_candidates if x["short_grade"] == "B"]
    b_longs = [x for x in long_candidates if x["long_grade"] == "B"]

    print_entry_ready(short_candidates, long_candidates)
    print_high_conviction(short_candidates, long_candidates)

    print_section("TOP A-GRADE SHORT SETUPS", a_shorts, "short_score", limit=5)
    print_section("TOP A-GRADE LONG SETUPS", a_longs, "long_score", limit=5)
    print_section("B-GRADE SHORT WATCHLIST", b_shorts, "short_score", limit=5)
    print_section("B-GRADE LONG WATCHLIST", b_longs, "long_score", limit=5)

    process_discord_alerts(results)
    log_results(results)

    print("\n" + "-" * 190)
    print(f"Saved {len(results)} rows to {LOG_FILE}")
    print(f"Checked 5m + 15m candles for top {MAX_CANDLE_CHECKS} movers only.")
    print("Reminder: v32.3 shows Quality Breakdown + visible 1H/4H HTF confirmation.")


if __name__ == "__main__":
    while True:
        run_scanner()
        print(f"\nNext scan in {SCAN_EVERY_SECONDS // 60} minutes...")
        print("Press CTRL + C to stop.")
        time.sleep(SCAN_EVERY_SECONDS)
