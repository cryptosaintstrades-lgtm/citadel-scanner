#!/usr/bin/env python3
"""
Liquidity Citadel v70 Auto Result Tracker

Polls Airtable scanner rows marked Active/Open/Tracking, checks post-alert
candles, and updates Status, Result, RR, and Closed Time when TP or invalidation
is hit.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

import requests


VERSION = "v70-auto-result-tracker"


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value


def env_bool(name: str, default: bool = False) -> bool:
    value = env(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    value = env(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    value = env(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


AIRTABLE_TOKEN = env("AIRTABLE_TOKEN") or env("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = env("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_REF = (
    env("AIRTABLE_SCANNER_TABLE_ID")
    or env("AIRTABLE_TABLE_ID")
    or env("AIRTABLE_SCANNER_TABLE")
    or env("AIRTABLE_TABLE_NAME")
    or env("AIRTABLE_TABLE")
    or "Scanner"
)

AIRTABLE_STATUS_FIELD = env("AIRTABLE_STATUS_FIELD", "Status")
AIRTABLE_RESULT_FIELD = env("AIRTABLE_RESULT_FIELD", "Result")
AIRTABLE_RR_FIELD = env("AIRTABLE_RR_FIELD", "RR")
AIRTABLE_CLOSED_TIME_FIELD = env("AIRTABLE_CLOSED_TIME_FIELD", "Closed Time")
AIRTABLE_LAST_CHECKED_FIELD = env("AIRTABLE_LAST_CHECKED_FIELD")

TRACKER_DRY_RUN = env_bool("TRACKER_DRY_RUN", True)
TRACKER_LIMIT = env_int("TRACKER_LIMIT", 100)
TRACKER_INTERVAL_SECONDS = env_int("TRACKER_INTERVAL_SECONDS", 300)
TRACKER_RUN_ONCE = env_bool("TRACKER_RUN_ONCE", False)
TRACKER_INCLUDE_BLANK_STATUS = env_bool("TRACKER_INCLUDE_BLANK_STATUS", False)
TRACKER_MAX_CANDLES = env_int("TRACKER_MAX_CANDLES", 1000)
TRACKER_LOOKBACK_DAYS = env_int("TRACKER_LOOKBACK_DAYS", 14)
TRACKER_SLEEP_BETWEEN_ROWS = env_float("TRACKER_SLEEP_BETWEEN_ROWS", 0.25)
AMBIGUOUS_CANDLE_RULE = (env("TRACKER_AMBIGUOUS_CANDLE_RULE", "conservative") or "").lower()

ACTIVE_STATUS_VALUES = {
    item.strip().lower()
    for item in (env("TRACKER_ACTIVE_STATUSES", "Active,Open,Tracking,In Progress") or "").split(",")
    if item.strip()
}
CLOSED_STATUS_VALUES = {
    item.strip().lower()
    for item in (env("TRACKER_CLOSED_STATUSES", "Closed,Complete,Completed,Archived") or "").split(",")
    if item.strip()
}


FIELD_CANDIDATES = {
    "symbol": ["Symbol", "Pair", "Market", "Ticker", "Asset", "Coin"],
    "timeframe": ["Timeframe", "TF", "Chart TF", "Snapshot Timeframe"],
    "direction": ["Direction", "Bias", "Side", "Setup Bias", "Trade Bias"],
    "entry": ["Entry", "Entry Zone", "Entry Price", "Entry Low", "Entry Area"],
    "invalidation": ["Invalidation", "Stop", "Stop Loss", "SL", "Stop Price"],
    "target_1": ["Target 1", "TP1", "Take Profit 1", "T1"],
    "target_2": ["Target 2", "TP2", "Take Profit 2", "T2"],
    "status": [AIRTABLE_STATUS_FIELD, "Trade Status", "State"],
    "result": [AIRTABLE_RESULT_FIELD, "Outcome"],
    "alert_time": [
        "Alert Time",
        "Created",
        "Created At",
        "Date",
        "Time",
        "Timestamp",
        "Published",
    ],
}


TIMEFRAME_SECONDS = {
    "1m": 60,
    "3m": 180,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "1d": 86400,
}


def require_config() -> None:
    missing = []
    if not AIRTABLE_TOKEN:
        missing.append("AIRTABLE_TOKEN or AIRTABLE_API_KEY")
    if not AIRTABLE_BASE_ID:
        missing.append("AIRTABLE_BASE_ID")
    if missing:
        print("Missing required environment variables:")
        for key in missing:
            print(f"  - {key}")
        sys.exit(2)


def airtable_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {AIRTABLE_TOKEN}",
        "Content-Type": "application/json",
    }


def airtable_url(path: str = "") -> str:
    table = quote(AIRTABLE_TABLE_REF, safe="")
    return f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table}{path}"


def get_field(fields: Dict[str, Any], key: str, default: Any = None) -> Any:
    for candidate in FIELD_CANDIDATES.get(key, []):
        value = fields.get(candidate)
        if value not in (None, ""):
            return value
    return default


def parse_time(value: Any, fallback_created_time: Optional[str]) -> Optional[datetime]:
    raw = value or fallback_created_time
    if not raw:
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def normalize_symbol(symbol: Any) -> Optional[str]:
    if symbol is None:
        return None
    text = str(symbol).strip().upper()
    if not text:
        return None
    text = text.replace(".P", "")
    text = re.sub(r"[^A-Z0-9]", "", text)
    text = text.replace("PERP", "")
    if not text.endswith("USDT"):
        text = f"{text}USDT"
    return text


def normalize_timeframe(timeframe: Any) -> str:
    text = str(timeframe or "15m").strip()
    lookup = {
        "5": "5m",
        "5M": "5m",
        "15": "15m",
        "15M": "15m",
        "30": "30m",
        "30M": "30m",
        "60": "1h",
        "1H": "1h",
        "1HR": "1h",
        "4H": "4h",
        "1D": "1d",
        "D": "1d",
        "DAILY": "1d",
    }
    return lookup.get(text.upper(), text.lower())


def parse_number(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "")
    matches = re.findall(r"-?\d+(?:\.\d+)?", text)
    if not matches:
        return None
    values = [float(item) for item in matches]
    if len(values) >= 2 and ("-" in text or "to" in text.lower()):
        return sum(values[:2]) / 2
    return values[0]


def direction_from(value: Any) -> Optional[str]:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if any(token in text for token in ("short", "bear", "sell", "down")):
        return "short"
    if any(token in text for token in ("long", "bull", "buy", "up")):
        return "long"
    return None


def should_track(record: Dict[str, Any]) -> bool:
    fields = record.get("fields", {})
    status = str(get_field(fields, "status", "") or "").strip().lower()
    result = str(get_field(fields, "result", "") or "").strip()

    if result:
        return False
    if status in CLOSED_STATUS_VALUES:
        return False
    if not status:
        return TRACKER_INCLUDE_BLANK_STATUS
    return status in ACTIVE_STATUS_VALUES


def list_candidate_records(limit: int) -> List[Dict[str, Any]]:
    params: Dict[str, Any] = {
        "pageSize": min(100, max(1, limit)),
        "maxRecords": limit,
    }

    records: List[Dict[str, Any]] = []
    offset = None
    while len(records) < limit:
        if offset:
            params["offset"] = offset
        response = requests.get(
            airtable_url(), headers=airtable_headers(), params=params, timeout=30
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Airtable list failed {response.status_code}: {response.text[:800]}\n"
                f"Checked table reference: {AIRTABLE_TABLE_REF}"
            )
        data = response.json()
        records.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            break
    return [record for record in records if should_track(record)]


def fetch_binance_candles(
    symbol: str, timeframe: str, start_time: Optional[datetime]
) -> List[Dict[str, Any]]:
    if start_time is None:
        start_time = datetime.now(timezone.utc) - timedelta(days=TRACKER_LOOKBACK_DAYS)

    start_ms = int(start_time.timestamp() * 1000)
    interval_seconds = TIMEFRAME_SECONDS.get(timeframe, 900)
    max_candles = max(10, min(TRACKER_MAX_CANDLES, 1000))
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    endpoints = [
        "https://fapi.binance.com/fapi/v1/klines",
        "https://api.binance.com/api/v3/klines",
    ]
    last_error = ""

    for endpoint in endpoints:
        params = {
            "symbol": symbol,
            "interval": timeframe,
            "startTime": max(start_ms - interval_seconds * 1000, 0),
            "endTime": end_ms,
            "limit": max_candles,
        }
        try:
            response = requests.get(endpoint, params=params, timeout=20)
            if response.status_code >= 400:
                last_error = f"{endpoint} -> {response.status_code}: {response.text[:300]}"
                continue
            rows = response.json()
            candles = []
            for row in rows:
                candles.append(
                    {
                        "open_time": int(row[0]),
                        "open": float(row[1]),
                        "high": float(row[2]),
                        "low": float(row[3]),
                        "close": float(row[4]),
                        "volume": float(row[5]),
                    }
                )
            if candles:
                return candles
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)

    raise RuntimeError(f"no candles for {symbol} {timeframe}: {last_error}")


def rr_for(direction: str, entry: float, stop: float, exit_price: float) -> Optional[float]:
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    if direction == "long":
        return round((exit_price - entry) / risk, 2)
    return round((entry - exit_price) / risk, 2)


def candle_hit_result(
    direction: str,
    candle: Dict[str, Any],
    entry: float,
    stop: float,
    target_1: Optional[float],
    target_2: Optional[float],
) -> Optional[Dict[str, Any]]:
    high = candle["high"]
    low = candle["low"]
    close_time = datetime.fromtimestamp(candle["open_time"] / 1000, tz=timezone.utc)

    if direction == "long":
        stop_hit = low <= stop
        tp2_hit = target_2 is not None and high >= target_2
        tp1_hit = target_1 is not None and high >= target_1
    else:
        stop_hit = high >= stop
        tp2_hit = target_2 is not None and low <= target_2
        tp1_hit = target_1 is not None and low <= target_1

    any_target_hit = tp1_hit or tp2_hit
    if stop_hit and any_target_hit and AMBIGUOUS_CANDLE_RULE == "conservative":
        return {
            "result": "Loss",
            "exit_price": stop,
            "rr": -1.0,
            "closed_time": close_time,
            "note": "ambiguous candle: stop and target touched",
        }

    if tp2_hit:
        exit_price = target_2
        return {
            "result": "TP2",
            "exit_price": exit_price,
            "rr": rr_for(direction, entry, stop, exit_price),
            "closed_time": close_time,
            "note": "target 2 touched",
        }

    if tp1_hit:
        exit_price = target_1
        return {
            "result": "TP1",
            "exit_price": exit_price,
            "rr": rr_for(direction, entry, stop, exit_price),
            "closed_time": close_time,
            "note": "target 1 touched",
        }

    if stop_hit:
        return {
            "result": "Loss",
            "exit_price": stop,
            "rr": -1.0,
            "closed_time": close_time,
            "note": "invalidation touched",
        }

    return None


def evaluate_record(record: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    fields = record.get("fields", {})
    raw_symbol = get_field(fields, "symbol")
    symbol = normalize_symbol(raw_symbol)
    timeframe = normalize_timeframe(get_field(fields, "timeframe", "15m"))
    direction = direction_from(get_field(fields, "direction"))
    entry = parse_number(get_field(fields, "entry"))
    stop = parse_number(get_field(fields, "invalidation"))
    target_1 = parse_number(get_field(fields, "target_1"))
    target_2 = parse_number(get_field(fields, "target_2"))
    alert_time = parse_time(get_field(fields, "alert_time"), record.get("createdTime"))

    label = f"{raw_symbol or 'UNKNOWN'} {timeframe} {record['id']}"

    if not symbol:
        return None, f"skipped {label}: missing symbol"
    if not direction:
        return None, f"skipped {label}: missing/unclear direction"
    if entry is None:
        return None, f"skipped {label}: missing entry"
    if stop is None:
        return None, f"skipped {label}: missing invalidation"
    if target_1 is None and target_2 is None:
        return None, f"skipped {label}: missing targets"

    candles = fetch_binance_candles(symbol, timeframe, alert_time)
    for candle in candles:
        result = candle_hit_result(direction, candle, entry, stop, target_1, target_2)
        if result:
            result["symbol"] = raw_symbol
            result["timeframe"] = timeframe
            result["direction"] = direction
            return result, (
                f"{label}: {result['result']} at {result['exit_price']} "
                f"RR={result['rr']} | {result['note']}"
            )

    last_close = candles[-1]["close"] if candles else "n/a"
    return None, f"still active {label}: last close={last_close}"


def update_airtable_result(record_id: str, result: Dict[str, Any]) -> None:
    fields: Dict[str, Any] = {}
    if AIRTABLE_STATUS_FIELD:
        fields[AIRTABLE_STATUS_FIELD] = "Closed"
    if AIRTABLE_RESULT_FIELD:
        fields[AIRTABLE_RESULT_FIELD] = result["result"]
    if AIRTABLE_RR_FIELD and result.get("rr") is not None:
        fields[AIRTABLE_RR_FIELD] = result["rr"]
    if AIRTABLE_CLOSED_TIME_FIELD:
        fields[AIRTABLE_CLOSED_TIME_FIELD] = result["closed_time"].isoformat()
    if AIRTABLE_LAST_CHECKED_FIELD:
        fields[AIRTABLE_LAST_CHECKED_FIELD] = datetime.now(timezone.utc).isoformat()

    body = {"fields": fields}
    response = requests.patch(
        airtable_url(f"/{record_id}"),
        headers=airtable_headers(),
        data=json.dumps(body),
        timeout=30,
    )
    if response.status_code < 400:
        return

    # Fallback for bases that do not have every optional output field.
    fallback_fields = {
        key: value
        for key, value in fields.items()
        if key in {AIRTABLE_STATUS_FIELD, AIRTABLE_RESULT_FIELD}
    }
    if fallback_fields and fallback_fields != fields:
        retry = requests.patch(
            airtable_url(f"/{record_id}"),
            headers=airtable_headers(),
            data=json.dumps({"fields": fallback_fields}),
            timeout=30,
        )
        if retry.status_code < 400:
            print(
                f"Updated {record_id} with fallback fields only. "
                f"Full update failed: {response.text[:400]}"
            )
            return

    raise RuntimeError(
        f"Airtable update failed {response.status_code}: {response.text[:800]}"
    )


def run_once() -> None:
    records = list_candidate_records(TRACKER_LIMIT)
    print(f"Tracker scan: {len(records)} candidate active rows.")

    closed = 0
    active = 0
    skipped_or_failed = 0

    for index, record in enumerate(records, start=1):
        try:
            result, message = evaluate_record(record)
            print(f"{index}/{len(records)} {message}")
            if result:
                closed += 1
                if TRACKER_DRY_RUN:
                    print(f"Dry run: would close {record['id']} as {result['result']}.")
                else:
                    update_airtable_result(record["id"], result)
                    print(f"Tracker updated: {record['id']} -> {result['result']}")
            else:
                if message.startswith("still active"):
                    active += 1
                else:
                    skipped_or_failed += 1
            time.sleep(TRACKER_SLEEP_BETWEEN_ROWS)
        except Exception as exc:  # noqa: BLE001
            skipped_or_failed += 1
            print(f"Tracker failed: {record.get('id', 'unknown')} | {exc}")

    print(
        "Tracker complete | "
        f"closed_candidates={closed} | still_active={active} | "
        f"skipped_or_failed={skipped_or_failed} | dry_run={TRACKER_DRY_RUN}"
    )


def main() -> None:
    require_config()
    print(f"BOOT CHECK: {VERSION}")
    print(
        "Tracker config | "
        f"table={AIRTABLE_TABLE_REF} | limit={TRACKER_LIMIT} | "
        f"dry_run={TRACKER_DRY_RUN} | interval={TRACKER_INTERVAL_SECONDS}s | "
        f"run_once={TRACKER_RUN_ONCE}"
    )

    while True:
        run_once()
        if TRACKER_RUN_ONCE:
            return
        time.sleep(max(60, TRACKER_INTERVAL_SECONDS))


if __name__ == "__main__":
    main()
