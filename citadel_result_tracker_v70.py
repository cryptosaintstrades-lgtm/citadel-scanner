#!/usr/bin/env python3
"""
Citadel Auto Result Tracker v70.2

Standalone Railway worker for checking active Airtable scanner rows and marking
whether target or invalidation was reached. Safe by default: dry-run is enabled
unless TRACKER_DRY_RUN=false is set.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

import requests


VERSION = "v70.2-auto-result-tracker-blofin-klines"
AIRTABLE_API = "https://api.airtable.com/v0"
BLOFIN_CANDLES_URL = "https://openapi.blofin.com/api/v1/market/candles"
BINANCE_FAPI = "https://fapi.binance.com/fapi/v1"


TRUE_VALUES = {"1", "true", "yes", "y", "on"}
ACTIVE_STATUSES = {"", "active", "open", "tracking", "in progress", "developing", "still developing"}
CLOSED_STATUSES = {"closed", "win", "loss", "tp1", "tp2", "stopped", "invalidated"}


FIELD_ALIASES = {
    "symbol": [
        "symbol",
        "ticker",
        "asset",
        "pair",
        "market",
        "coin",
    ],
    "direction": [
        "direction",
        "bias",
        "side",
        "trade direction",
        "setup direction",
        "signal",
        "setup",
    ],
    "entry": [
        "entry",
        "entry price",
        "entry zone",
        "entry_zone",
        "entry area",
        "entry range",
        "entry low",
        "trigger price",
        "alert price",
        "price",
    ],
    "stop": [
        "invalidation",
        "invalid",
        "invalidated at",
        "stop",
        "stop loss",
        "stop_loss",
        "sl",
        "risk",
        "risk level",
    ],
    "target1": [
        "target 1",
        "target1",
        "target_1",
        "target 1 price",
        "target 1 level",
        "target one",
        "tp1",
        "tp 1",
        "t1",
        "take profit 1",
        "take-profit 1",
        "profit target 1",
        "first target",
    ],
    "target2": [
        "target 2",
        "target2",
        "target_2",
        "target 2 price",
        "target 2 level",
        "target two",
        "tp2",
        "tp 2",
        "t2",
        "take profit 2",
        "take-profit 2",
        "profit target 2",
        "second target",
    ],
    "target_any": [
        "target",
        "targets",
        "target price",
        "target zone",
        "take profit",
        "take profits",
        "profit target",
        "tp",
        "tps",
    ],
    "alert_time": [
        "alert time",
        "created time",
        "created",
        "time",
        "timestamp",
        "date",
        "published",
        "scan time",
    ],
    "status": ["status"],
}


TEXT_SCAN_FIELDS = [
    "trade plan",
    "plan",
    "setup thesis",
    "summary",
    "notes",
    "why it triggered",
    "targets",
    "key levels",
    "reason",
    "reasons",
    "description",
]


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in TRUE_VALUES


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def airtable_headers() -> Dict[str, str]:
    token = os.getenv("AIRTABLE_TOKEN") or os.getenv("AIRTABLE_API_KEY")
    if not token:
        raise RuntimeError("Missing AIRTABLE_TOKEN")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def airtable_base_id() -> str:
    base_id = os.getenv("AIRTABLE_BASE_ID")
    if not base_id:
        raise RuntimeError("Missing AIRTABLE_BASE_ID")
    return base_id


def airtable_table() -> str:
    return os.getenv("AIRTABLE_SCANNER_TABLE") or os.getenv("AIRTABLE_TABLE_NAME") or "Scanner"


def airtable_url(table: Optional[str] = None) -> str:
    table_name = table or airtable_table()
    return f"{AIRTABLE_API}/{airtable_base_id()}/{quote(table_name, safe='')}"


def get_field(fields: Dict[str, Any], logical_name: str) -> Tuple[Any, Optional[str]]:
    normalized = {normalize_key(k): k for k in fields.keys()}
    for alias in FIELD_ALIASES.get(logical_name, []):
        key = normalized.get(normalize_key(alias))
        if key is not None:
            value = fields.get(key)
            if value not in (None, ""):
                return value, key
    return None, None


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(as_text(v) for v in value)
    if isinstance(value, dict):
        return " ".join(as_text(v) for v in value.values())
    return str(value)


def parse_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None

    text = as_text(value)
    if not text:
        return None
    matches = re.findall(r"-?\$?\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)", text)
    if not matches:
        return None
    try:
        return Decimal(matches[0].replace(",", ""))
    except InvalidOperation:
        return None


def parse_price_list(value: Any) -> List[Decimal]:
    text = as_text(value)
    prices: List[Decimal] = []
    for raw in re.findall(r"-?\$?\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)", text):
        try:
            prices.append(Decimal(raw.replace(",", "")))
        except InvalidOperation:
            continue
    return prices


def parse_direction(value: Any) -> Optional[str]:
    text = as_text(value).lower()
    if "short" in text or "bear" in text or "sell" in text:
        return "short"
    if "long" in text or "bull" in text or "buy" in text:
        return "long"
    return None


def parse_symbol(value: Any) -> Optional[str]:
    text = as_text(value).upper().strip()
    if not text:
        return None
    text = text.replace("/", "").replace("-", "").replace("_", "")
    text = re.sub(r"[^A-Z0-9]", "", text)
    if text.endswith("PERP"):
        text = text[:-4]
    if text and not text.endswith("USDT"):
        text += "USDT"
    return text or None


def text_scan_pool(fields: Dict[str, Any]) -> str:
    chunks: List[str] = []
    normalized = {normalize_key(k): k for k in fields.keys()}
    for wanted in TEXT_SCAN_FIELDS:
        key = normalized.get(normalize_key(wanted))
        if key:
            chunks.append(as_text(fields.get(key)))
    return "\n".join(chunks)


def parse_targets_from_text(fields: Dict[str, Any], direction: Optional[str], entry: Optional[Decimal]) -> List[Decimal]:
    text = text_scan_pool(fields)
    if not text:
        return []

    labeled: List[Decimal] = []
    patterns = [
        r"(?:target|tp|t)\s*1[^0-9$-]*\$?\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)",
        r"(?:target|tp|t)\s*2[^0-9$-]*\$?\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)",
        r"(?:targets?|take profits?|tps?)[^0-9$-]*\$?\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)",
    ]
    for pattern in patterns:
        for match in re.findall(pattern, text, flags=re.IGNORECASE):
            try:
                labeled.append(Decimal(match.replace(",", "")))
            except InvalidOperation:
                pass

    if labeled:
        return dedupe_prices(labeled)

    candidates = parse_price_list(text)
    if direction and entry and candidates:
        if direction == "long":
            candidates = [p for p in candidates if p > entry]
        else:
            candidates = [p for p in candidates if p < entry]
    return dedupe_prices(candidates[:2])


def dedupe_prices(values: Iterable[Decimal]) -> List[Decimal]:
    seen = set()
    out: List[Decimal] = []
    for value in values:
        key = str(value.normalize())
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def resolve_targets(fields: Dict[str, Any], direction: Optional[str], entry: Optional[Decimal]) -> Tuple[List[Decimal], List[str]]:
    found_from: List[str] = []
    targets: List[Decimal] = []

    for logical in ("target1", "target2"):
        raw, field_name = get_field(fields, logical)
        price = parse_decimal(raw)
        if price is not None:
            targets.append(price)
            found_from.append(field_name or logical)

    if not targets:
        raw, field_name = get_field(fields, "target_any")
        prices = parse_price_list(raw)
        if prices:
            targets.extend(prices[:2])
            found_from.append(field_name or "target_any")

    if not targets:
        text_prices = parse_targets_from_text(fields, direction, entry)
        if text_prices:
            targets.extend(text_prices[:2])
            found_from.append("parsed from trade-plan text")

    targets = dedupe_prices(targets)
    if direction == "long":
        targets.sort()
    elif direction == "short":
        targets.sort(reverse=True)
    return targets, found_from


def list_records(limit: int) -> List[Dict[str, Any]]:
    params = {
        "pageSize": min(max(limit * 3, 10), 100),
        "maxRecords": min(max(limit * 3, 10), 100),
    }
    response = requests.get(airtable_url(), headers=airtable_headers(), params=params, timeout=30)
    if response.status_code >= 400:
        raise RuntimeError(f"Airtable list failed {response.status_code}: {response.text[:500]}")
    return response.json().get("records", [])


def is_candidate(fields: Dict[str, Any], include_blank: bool) -> bool:
    raw_status, _ = get_field(fields, "status")
    status = as_text(raw_status).strip().lower()
    if status in CLOSED_STATUSES:
        return False
    if include_blank and not status:
        return True
    return status in ACTIVE_STATUSES


def parse_alert_time(fields: Dict[str, Any]) -> Optional[int]:
    raw, _ = get_field(fields, "alert_time")
    text = as_text(raw).strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        return int(parsed.timestamp() * 1000)
    except ValueError:
        return None


def to_blofin_inst_id(symbol: str) -> str:
    text = symbol.upper().strip().replace("/", "-").replace("_", "-")
    if "-" in text:
        return text
    if text.endswith("USDT"):
        return f"{text[:-4]}-USDT"
    return text


def normalize_interval_for_blofin(interval: str) -> str:
    raw = (interval or "5m").strip()
    aliases = {
        "1min": "1m",
        "3min": "3m",
        "5min": "5m",
        "15min": "15m",
        "30min": "30m",
        "60m": "1H",
        "1h": "1H",
        "2h": "2H",
        "4h": "4H",
        "1d": "1D",
    }
    return aliases.get(raw.lower(), raw)


def fetch_blofin_klines(symbol: str, start_ms: Optional[int], interval: str = "5m", limit: int = 1000) -> List[Dict[str, Decimal]]:
    inst_id = to_blofin_inst_id(symbol)
    bar = normalize_interval_for_blofin(interval)
    safe_limit = min(max(int(limit or 1000), 10), 1000)
    params = {"instId": inst_id, "bar": bar, "limit": str(safe_limit)}
    response = requests.get(BLOFIN_CANDLES_URL, params=params, timeout=30)
    if response.status_code >= 400:
        raise RuntimeError(f"Blofin klines failed for {inst_id}: {response.status_code} {response.text[:300]}")

    payload = response.json()
    data = payload.get("data", []) if isinstance(payload, dict) else []
    candles: List[Dict[str, Decimal]] = []
    for row in data:
        if not isinstance(row, list) or len(row) < 5:
            continue
        try:
            open_time = int(str(row[0]))
            candle = {
                "open_time": Decimal(str(open_time)),
                "high": Decimal(str(row[2])),
                "low": Decimal(str(row[3])),
                "close": Decimal(str(row[4])),
            }
        except (InvalidOperation, ValueError, TypeError):
            continue
        if start_ms and open_time < int(start_ms):
            continue
        candles.append(candle)

    candles.sort(key=lambda item: item["open_time"])
    print(f"Blofin candles loaded for {inst_id}: {len(candles)}")
    return candles


def fetch_binance_klines(symbol: str, start_ms: Optional[int], interval: str = "5m", limit: int = 1000) -> List[Dict[str, Decimal]]:
    params: Dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": limit}
    if start_ms:
        params["startTime"] = start_ms
    response = requests.get(f"{BINANCE_FAPI}/klines", params=params, timeout=30)
    if response.status_code >= 400:
        raise RuntimeError(f"Binance klines failed for {symbol}: {response.status_code} {response.text[:300]}")
    candles = []
    for row in response.json():
        candles.append(
            {
                "open_time": Decimal(str(row[0])),
                "high": Decimal(str(row[2])),
                "low": Decimal(str(row[3])),
                "close": Decimal(str(row[4])),
            }
        )
    return candles


def fetch_klines(symbol: str, start_ms: Optional[int], interval: str = "5m", limit: int = 1000) -> List[Dict[str, Decimal]]:
    source = (os.getenv("TRACKER_KLINE_SOURCE") or os.getenv("TRACKER_DATA_SOURCE") or "blofin").strip().lower()
    if source == "binance":
        return fetch_binance_klines(symbol, start_ms, interval, limit)
    return fetch_blofin_klines(symbol, start_ms, interval, limit)


def determine_result(direction: str, entry: Decimal, stop: Decimal, targets: List[Decimal], candles: List[Dict[str, Decimal]]) -> Tuple[str, str, Decimal]:
    target1 = targets[0]
    target2 = targets[1] if len(targets) > 1 else None
    risk = abs(entry - stop)
    if risk == 0:
        return "Skipped", "Invalid Risk", Decimal("0")

    best_result = "Still Active"
    best_status = "Active"
    best_rr = Decimal("0")

    for candle in candles:
        high = candle["high"]
        low = candle["low"]
        if direction == "long":
            stop_hit = low <= stop
            tp2_hit = target2 is not None and high >= target2
            tp1_hit = high >= target1
        else:
            stop_hit = high >= stop
            tp2_hit = target2 is not None and low <= target2
            tp1_hit = low <= target1

        if stop_hit and (tp1_hit or tp2_hit):
            return "Closed", "Ambiguous - Stop and Target Same Candle", Decimal("-1")
        if stop_hit:
            return "Closed", "Loss", Decimal("-1")
        if tp2_hit and target2 is not None:
            rr = abs(target2 - entry) / risk
            return "Closed", "TP2", rr
        if tp1_hit:
            rr = abs(target1 - entry) / risk
            best_status = "Closed"
            best_result = "TP1"
            best_rr = rr
            return best_status, best_result, best_rr

    return best_status, best_result, best_rr


def patch_record(record_id: str, status: str, result: str, rr: Decimal) -> None:
    fields = {
        os.getenv("TRACKER_STATUS_FIELD", "Status"): status,
        os.getenv("TRACKER_RESULT_FIELD", "Result"): result,
        os.getenv("TRACKER_RR_FIELD", "RR"): float(round(rr, 2)),
        os.getenv("TRACKER_CLOSED_TIME_FIELD", "Closed Time"): now_iso(),
    }
    response = requests.patch(
        f"{airtable_url()}/{record_id}",
        headers=airtable_headers(),
        data=json.dumps({"fields": fields}),
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Airtable update failed {response.status_code}: {response.text[:500]}")


def field_names(fields: Dict[str, Any]) -> str:
    names = list(fields.keys())
    return ", ".join(names[:30]) + (" ..." if len(names) > 30 else "")


def process_record(record: Dict[str, Any], dry_run: bool) -> str:
    record_id = record["id"]
    fields = record.get("fields", {})

    symbol = parse_symbol(get_field(fields, "symbol")[0])
    direction = parse_direction(get_field(fields, "direction")[0])
    entry = parse_decimal(get_field(fields, "entry")[0])
    stop = parse_decimal(get_field(fields, "stop")[0])
    targets, target_sources = resolve_targets(fields, direction, entry)

    missing = []
    if not symbol:
        missing.append("symbol")
    if not direction:
        missing.append("direction")
    if entry is None:
        missing.append("entry")
    if stop is None:
        missing.append("invalidation/stop")
    if not targets:
        missing.append("targets")

    label = as_text(get_field(fields, "symbol")[0]) or record_id
    if missing:
        print(f"skipped {label}: missing {', '.join(missing)} | fields seen: {field_names(fields)}")
        return "skipped"

    start_ms = parse_alert_time(fields)
    candles = fetch_klines(symbol, start_ms)
    if not candles:
        print(f"skipped {label}: no candles returned for {symbol}")
        return "skipped"

    status, result, rr = determine_result(direction, entry, stop, targets, candles)
    if status != "Closed":
        print(f"still active {label} | {direction} entry={entry} stop={stop} targets={targets} via={target_sources}")
        return "active"

    if dry_run:
        print(f"Dry run: would close {label} | result={result} rr={round(rr, 2)} targets={targets} via={target_sources}")
    else:
        patch_record(record_id, status, result, rr)
        print(f"Closed {label} | result={result} rr={round(rr, 2)} targets={targets} via={target_sources}")
    return "closed"


def run_once() -> None:
    dry_run = env_bool("TRACKER_DRY_RUN", True)
    include_blank = env_bool("TRACKER_INCLUDE_BLANK_STATUS", False)
    limit = env_int("TRACKER_LIMIT", 10)

    records = list_records(limit)
    candidates = [r for r in records if is_candidate(r.get("fields", {}), include_blank)][:limit]
    print(f"Tracker scan: {len(candidates)} candidate active rows.")

    closed = 0
    active = 0
    skipped = 0
    for idx, record in enumerate(candidates, start=1):
        try:
            result = process_record(record, dry_run)
        except Exception as exc:
            print(f"{idx}/{len(candidates)} failed {record.get('id')}: {exc}")
            skipped += 1
            continue
        if result == "closed":
            closed += 1
        elif result == "active":
            active += 1
        else:
            skipped += 1

    print(
        f"Tracker complete | closed_candidates={closed} | "
        f"still_active={active} | skipped_or_failed={skipped} | dry_run={dry_run}"
    )


def main() -> int:
    print(f"BOOT CHECK: {VERSION}")
    print(
        "Tracker config | "
        f"table={airtable_table()} | "
        f"dry_run={env_bool('TRACKER_DRY_RUN', True)} | "
        f"include_blank={env_bool('TRACKER_INCLUDE_BLANK_STATUS', False)} | "
        f"limit={env_int('TRACKER_LIMIT', 10)} | "
        f"kline_source={(os.getenv('TRACKER_KLINE_SOURCE') or os.getenv('TRACKER_DATA_SOURCE') or 'blofin')}"
    )

    interval = env_int("TRACKER_INTERVAL_SECONDS", 300)
    run_forever = env_bool("TRACKER_RUN_FOREVER", True)
    while True:
        run_once()
        if not run_forever:
            break
        time.sleep(max(interval, 60))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        print(f"Fatal tracker error: {exc}", file=sys.stderr)
        raise
