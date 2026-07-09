#!/usr/bin/env python3
"""
Liquidity Citadel Auto Result Tracker v70.4

Fixes:
- Uses Blofin klines by default.
- Strict target parser that avoids reading the "1" in TP1 as a target.
- Forces Result, Status, RR, Closed Time values into Airtable-safe text strings.
- Dry-run safe by default.

This script is intended to run as a separate Railway service:
python citadel_result_tracker_v70.py
"""

from __future__ import annotations

import os
import re
import sys
import time
import json
import math
import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

import requests


VERSION = "v70.4-auto-result-tracker-rr-text-fix"

AIRTABLE_API = "https://api.airtable.com/v0"
BLOFIN_CANDLES_URL = "https://openapi.blofin.com/api/v1/market/candles"
BINANCE_FAPI = "https://fapi.binance.com/fapi/v1"


TRUE_VALUES = {"1", "true", "yes", "y", "on"}
ACTIVE_STATUSES = {"", "active", "open", "tracking", "in progress", "developing", "still developing"}
CLOSED_STATUSES = {"closed", "win", "loss", "tp1", "tp2", "stopped", "invalidated"}

FIELD_ALIASES = {
    "symbol": [
        "Pair", "pair", "Symbol", "symbol", "Ticker", "ticker", "Asset", "asset", "Market", "market", "Coin", "coin"
    ],
    "direction": [
        "Direction", "direction", "Bias", "bias", "Side", "side", "Trade Direction", "Setup Direction", "Signal", "signal"
    ],
    "entry": [
        "Entry", "entry", "Entry Zone", "entry zone", "Entry Range", "entry range", "Planned Entry", "Price Entry"
    ],
    "invalidation": [
        "Invalidation", "invalidation", "Stop", "stop", "Stop Loss", "SL", "Invalidation Level", "Risk", "Stop Price"
    ],
    "targets": [
        "Targets", "targets", "Target", "target", "TP", "tp",
        "TP1", "tp1", "TP 1", "Take Profit 1", "Target 1", "Target 1 Price", "T1",
        "TP2", "tp2", "TP 2", "Take Profit 2", "Target 2", "Target 2 Price", "T2",
    ],
    "target1": ["TP1", "tp1", "TP 1", "Take Profit 1", "Target 1", "Target 1 Price", "T1", "Target"],
    "target2": ["TP2", "tp2", "TP 2", "Take Profit 2", "Target 2", "Target 2 Price", "T2"],
    "timeframe": ["Timeframe", "timeframe", "TF", "tf", "Time Frame", "Scan TF"],
    "status": ["Status", "status"],
    "result": ["Result", "result"],
    "rr": ["RR", "R:R", "Risk Reward", "Risk/Reward", "R Multiple"],
    "closed_time": ["Closed Time", "Closed At", "Close Time", "Resolved Time"],
    "scan_time": ["Scan Time", "scan_time", "Date", "Created", "Created Time", "Alert Time", "Timestamp"],
    "reason": ["Reason", "Reasons", "Summary", "Message", "Notes", "Setup", "Description", "Trade Plan"],
}


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return str(value).strip().lower() in TRUE_VALUES


def env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, default)).strip())
    except Exception:
        return default


def now_iso() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def normalize_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    s = s.replace("/", "-").replace("_", "-").replace(" ", "")
    if s and "-" not in s and s.endswith("USDT"):
        s = s[:-4] + "-USDT"
    return s


def compact_symbol(symbol: str) -> str:
    return normalize_symbol(symbol).replace("-", "").replace("/", "")


def parse_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        return Decimal(str(value))
    text = str(value).strip()
    if not text:
        return None
    text = text.replace(",", "")
    text = re.sub(r"(?i)\bUSDT\b", "", text).strip()
    # Pull first normal decimal-looking number.
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return Decimal(match.group(0))
    except InvalidOperation:
        return None


def decimals_from_text(value: Any) -> List[Decimal]:
    if value is None:
        return []
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(decimals_from_text(item))
        return out
    text = str(value)
    # Remove target labels so TP1/TP2 do not contribute standalone 1/2.
    text = re.sub(r"(?i)\b(?:tp|t|target|take\s*profit)\s*#?\s*[12]\b", " ", text)
    nums = []
    for raw in re.findall(r"-?\d+(?:\.\d+)?", text.replace(",", "")):
        try:
            nums.append(Decimal(raw))
        except InvalidOperation:
            pass
    return nums


def find_field(fields: Dict[str, Any], aliases: List[str]) -> Tuple[Optional[str], Any]:
    lower_map = {k.lower().strip(): k for k in fields.keys()}
    for alias in aliases:
        key = lower_map.get(alias.lower().strip())
        if key is not None:
            return key, fields.get(key)
    return None, None


def get_value(fields: Dict[str, Any], field_group: str) -> Tuple[Optional[str], Any]:
    return find_field(fields, FIELD_ALIASES.get(field_group, []))


def infer_direction(fields: Dict[str, Any]) -> str:
    _, direct = get_value(fields, "direction")
    blob = " ".join(str(x) for x in [direct, get_value(fields, "reason")[1], get_value(fields, "symbol")[1]] if x)
    up = blob.upper()
    if "SHORT" in up or "SELL" in up or "BEAR" in up:
        return "SHORT"
    if "LONG" in up or "BUY" in up or "BULL" in up:
        return "LONG"
    return ""


def target_candidates(fields: Dict[str, Any], entry: Optional[Decimal], invalidation: Optional[Decimal], direction: str, max_rr: Decimal) -> Tuple[List[Decimal], List[str], List[Decimal]]:
    raw_values = []
    used_fields = []
    rejected = []

    for group in ("target1", "target2", "targets", "reason"):
        for alias in FIELD_ALIASES.get(group, []):
            key, value = find_field(fields, [alias])
            if key and value not in (None, ""):
                raw_values.append(value)
                used_fields.append(key)

    nums: List[Decimal] = []
    for v in raw_values:
        nums.extend(decimals_from_text(v))

    # Remove exact label artifacts and invalid numbers.
    cleaned: List[Decimal] = []
    for n in nums:
        if n <= 0:
            rejected.append(n)
            continue
        # Reject pure target-label artifacts.
        if n in {Decimal("1"), Decimal("2")}:
            rejected.append(n)
            continue
        cleaned.append(n)

    if entry is not None and invalidation is not None:
        risk = abs(entry - invalidation)
        if risk > 0:
            bounded = []
            for t in cleaned:
                rr = abs(t - entry) / risk
                # Reject impossible or wrong-direction targets.
                if direction == "LONG" and t <= entry:
                    rejected.append(t)
                    continue
                if direction == "SHORT" and t >= entry:
                    rejected.append(t)
                    continue
                if rr <= 0 or rr > max_rr:
                    rejected.append(t)
                    continue
                bounded.append(t)
            cleaned = bounded

    # Deduplicate while preserving order.
    seen = set()
    out = []
    for n in cleaned:
        key = str(n.normalize())
        if key not in seen:
            seen.add(key)
            out.append(n)

    return out[:2], sorted(set(used_fields)), rejected


def airtable_base_url(base_id: str, table_name: str) -> str:
    return f"{AIRTABLE_API}/{base_id}/{quote(table_name, safe='')}"


def airtable_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def list_records(token: str, base_id: str, table_name: str, limit: int, include_blank: bool) -> List[Dict[str, Any]]:
    url = airtable_base_url(base_id, table_name)
    headers = airtable_headers(token)

    # Pull a slightly larger page so filters can happen locally.
    params = {
        "pageSize": min(max(limit * 3, 20), 100),
    }

    records = []
    offset = None

    while len(records) < limit:
        if offset:
            params["offset"] = offset
        resp = requests.get(url, headers=headers, params=params, timeout=25)
        if not resp.ok:
            raise RuntimeError(f"Airtable list failed {resp.status_code}: {resp.text}")
        data = resp.json()
        for rec in data.get("records", []):
            fields = rec.get("fields", {})
            _, status_val = get_value(fields, "status")
            status = str(status_val or "").strip().lower()
            _, result_val = get_value(fields, "result")
            if result_val not in (None, ""):
                continue
            if status in CLOSED_STATUSES:
                continue
            if status in ACTIVE_STATUSES or (include_blank and not status):
                records.append(rec)
            if len(records) >= limit:
                break
        offset = data.get("offset")
        if not offset:
            break

    return records[:limit]


def blofin_interval(tf: str) -> str:
    text = str(tf or "5m").strip().lower().replace(" ", "")
    mapping = {
        "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
        "1h": "1H", "60m": "1H", "4h": "4H", "1d": "1D", "daily": "1D"
    }
    if "/" in text:
        text = text.split("/")[0].strip()
    return mapping.get(text, "5m")


def get_blofin_candles(symbol: str, tf: str, limit: int = 1000) -> List[Dict[str, Decimal]]:
    inst_id = normalize_symbol(symbol)
    params = {
        "instId": inst_id,
        "bar": blofin_interval(tf),
        "limit": min(max(int(limit), 10), 1000),
    }
    resp = requests.get(BLOFIN_CANDLES_URL, params=params, timeout=25)
    if not resp.ok:
        raise RuntimeError(f"Blofin candles failed for {inst_id}: {resp.status_code} {resp.text}")
    data = resp.json()
    raw = data.get("data", [])
    candles = []
    for row in raw:
        # Blofin returns arrays like [ts, open, high, low, close, vol, ...]
        if not isinstance(row, list) or len(row) < 5:
            continue
        try:
            candles.append({
                "ts": int(row[0]),
                "open": Decimal(str(row[1])),
                "high": Decimal(str(row[2])),
                "low": Decimal(str(row[3])),
                "close": Decimal(str(row[4])),
            })
        except Exception:
            continue
    candles.sort(key=lambda c: c["ts"])
    return candles


def evaluate_trade(symbol: str, direction: str, entry: Decimal, invalidation: Decimal, targets: List[Decimal], candles: List[Dict[str, Decimal]]) -> Tuple[str, Decimal, str]:
    if not targets:
        return "Active", Decimal("0"), "No target available"

    target1 = targets[0]
    target2 = targets[1] if len(targets) > 1 else None
    risk = abs(entry - invalidation)
    if risk <= 0:
        return "Active", Decimal("0"), "Invalid risk"

    direction = direction.upper()

    for c in candles:
        high, low = c["high"], c["low"]
        if direction == "LONG":
            stopped = low <= invalidation
            hit_t2 = target2 is not None and high >= target2
            hit_t1 = high >= target1
        else:
            stopped = high >= invalidation
            hit_t2 = target2 is not None and low <= target2
            hit_t1 = low <= target1

        # Conservative handling if stop and target occur on same candle.
        if stopped and (hit_t1 or hit_t2):
            return "Loss", Decimal("-1"), "Ambiguous: stop and target same candle"

        if stopped:
            return "Loss", Decimal("-1"), "Invalidation hit"

        if hit_t2:
            rr = abs(target2 - entry) / risk
            return "TP2", rr, "Target 2 hit"

        if hit_t1:
            rr = abs(target1 - entry) / risk
            return "TP1", rr, "Target 1 hit"

    return "Active", Decimal("0"), "Still active"


def fmt_rr(rr: Decimal) -> str:
    try:
        q = rr.quantize(Decimal("0.01"))
    except Exception:
        q = Decimal("0.00")
    return f"{q}R"


def airtable_safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def update_record(token: str, base_id: str, table_name: str, rec_id: str, status: str, result: str, rr: Decimal, reason: str, dry_run: bool) -> Tuple[bool, str]:
    fields = {
        "Status": airtable_safe_text("Closed" if result in {"TP1", "TP2", "Loss"} else status),
        "Result": airtable_safe_text(result),
        "RR": airtable_safe_text(fmt_rr(rr)),
        "Closed Time": airtable_safe_text(now_iso()),
    }

    # Optional Notes field if it exists is not guaranteed. Do not write it by default.
    if dry_run:
        return True, f"Dry run: would update {rec_id} fields={fields}"

    url = f"{airtable_base_url(base_id, table_name)}/{rec_id}"
    payload = {"fields": fields, "typecast": True}
    resp = requests.patch(url, headers=airtable_headers(token), json=payload, timeout=25)
    if not resp.ok:
        # Fallback: if Closed Time does not exist, try without it.
        if "Closed Time" in fields:
            fields2 = dict(fields)
            fields2.pop("Closed Time", None)
            resp2 = requests.patch(url, headers=airtable_headers(token), json={"fields": fields2, "typecast": True}, timeout=25)
            if resp2.ok:
                return True, f"Updated Airtable {rec_id} without Closed Time result={result} rr={fields2['RR']}"
        return False, f"Airtable update failed {resp.status_code}: {resp.text} payload={json.dumps(payload)}"
    return True, f"Updated Airtable {rec_id} result={result} rr={fields['RR']}"


def run_once() -> None:
    token = os.environ.get("AIRTABLE_TOKEN", "").strip()
    base_id = os.environ.get("AIRTABLE_BASE_ID", "").strip()
    table_name = (
        os.environ.get("AIRTABLE_SCANNER_TABLE")
        or os.environ.get("AIRTABLE_TABLE_NAME")
        or "Scanner"
    ).strip()

    dry_run = env_bool("TRACKER_DRY_RUN", True)
    include_blank = env_bool("TRACKER_INCLUDE_BLANK_STATUS", False)
    limit = env_int("TRACKER_LIMIT", 10)
    interval = env_int("TRACKER_INTERVAL_SECONDS", 300)
    kline_source = os.environ.get("TRACKER_KLINE_SOURCE", "blofin").strip().lower()
    max_target_rr = Decimal(str(os.environ.get("TRACKER_MAX_TARGET_RR", "20")))

    if not token or not base_id:
        raise RuntimeError("AIRTABLE_TOKEN and AIRTABLE_BASE_ID are required.")

    print(f"BOOT CHECK: {VERSION}", flush=True)
    print(
        f"Tracker config | table={table_name} | dry_run={dry_run} | include_blank={include_blank} | "
        f"limit={limit} | kline_source={kline_source} | max_target_rr={max_target_rr}",
        flush=True,
    )

    records = list_records(token, base_id, table_name, limit, include_blank)
    print(f"Tracker scan: {len(records)} candidate active rows.", flush=True)

    closed_count = 0
    still_active = 0
    failed = 0

    for idx, rec in enumerate(records, 1):
        rec_id = rec.get("id", "")
        fields = rec.get("fields", {})

        _, symbol_val = get_value(fields, "symbol")
        symbol = normalize_symbol(str(symbol_val or ""))
        direction = infer_direction(fields)

        _, entry_val = get_value(fields, "entry")
        entry = parse_decimal(entry_val)

        _, inval_val = get_value(fields, "invalidation")
        invalidation = parse_decimal(inval_val)

        _, tf_val = get_value(fields, "timeframe")
        tf = str(tf_val or "5m")

        if not symbol or not direction or entry is None or invalidation is None:
            print(f"{idx}/{len(records)} skipped {rec_id}: missing symbol/direction/entry/invalidation | symbol={symbol} direction={direction} entry={entry} invalidation={invalidation}", flush=True)
            failed += 1
            continue

        targets, target_fields, rejected = target_candidates(fields, entry, invalidation, direction, max_target_rr)
        if not targets:
            print(f"{idx}/{len(records)} skipped {symbol} {rec_id}: missing usable targets | target_fields={target_fields} rejected_targets={rejected} available_fields={list(fields.keys())}", flush=True)
            failed += 1
            continue

        try:
            candles = get_blofin_candles(symbol, tf, limit=1000)
            print(f"Blofin candles loaded for {symbol}: {len(candles)}", flush=True)
        except Exception as exc:
            print(f"{idx}/{len(records)} failed {symbol} {rec_id}: {exc}", flush=True)
            failed += 1
            continue

        if not candles:
            print(f"{idx}/{len(records)} skipped {symbol} {rec_id}: no candles", flush=True)
            failed += 1
            continue

        result, rr, reason = evaluate_trade(symbol, direction, entry, invalidation, targets, candles)
        if result == "Active":
            print(f"{idx}/{len(records)} still active {symbol} {rec_id}: targets={targets} reason={reason}", flush=True)
            still_active += 1
            continue

        ok, msg = update_record(token, base_id, table_name, rec_id, "Closed", result, rr, reason, dry_run)
        print(f"{idx}/{len(records)} {'closed' if ok else 'failed'} {symbol} {rec_id}: {msg} targets={targets}", flush=True)
        if ok:
            closed_count += 1
        else:
            failed += 1

    print(
        f"Tracker complete | closed_candidates={closed_count} | still_active={still_active} | skipped_or_failed={failed} | dry_run={dry_run}",
        flush=True,
    )


def main() -> None:
    run_once_flag = env_bool("TRACKER_RUN_ONCE", False)
    interval = env_int("TRACKER_INTERVAL_SECONDS", 300)

    while True:
        try:
            run_once()
        except Exception as exc:
            print(f"Tracker error: {exc}", flush=True)
        if run_once_flag:
            break
        time.sleep(max(interval, 60))


if __name__ == "__main__":
    main()
