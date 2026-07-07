#!/usr/bin/env python3
"""
Liquidity Citadel v69 Snapshot Backfill Tool

One-off runner for Airtable scanner rows that are missing Screenshot URL.
It fetches recent/historical candles, calls the existing Netlify snapshot
function, then writes the returned snapshot URL back to Airtable.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote

import requests


VERSION = "v69.1-snapshot-backfill-table-id-support"


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


AIRTABLE_TOKEN = env("AIRTABLE_TOKEN") or env("AIRTABLE_API_KEY")
AIRTABLE_BASE_ID = env("AIRTABLE_BASE_ID")
AIRTABLE_TABLE_REF = (
    env("AIRTABLE_SCANNER_TABLE_ID")
    or env("AIRTABLE_TABLE_ID")
    or env("AIRTABLE_SCANNER_TABLE")
    or env("AIRTABLE_TABLE_NAME")
    or env("AIRTABLE_TABLE")
    or "Scanner Alerts"
)
AIRTABLE_VIEW = env("AIRTABLE_VIEW")
AIRTABLE_SCREENSHOT_FIELD = env("AIRTABLE_SCREENSHOT_FIELD", "Screenshot URL")

NETLIFY_SITE_URL = (env("NETLIFY_SITE_URL", "") or "").rstrip("/")
NETLIFY_SAVE_FUNCTION_PATH = env(
    "NETLIFY_SAVE_FUNCTION_PATH", "/.netlify/functions/save-scanner-snapshot"
)
NETLIFY_SNAPSHOT_SECRET = env("NETLIFY_SNAPSHOT_SECRET")

BACKFILL_LIMIT = env_int("BACKFILL_LIMIT", 50)
BACKFILL_DRY_RUN = env_bool("BACKFILL_DRY_RUN", True)
BACKFILL_SLEEP_SECONDS = float(env("BACKFILL_SLEEP_SECONDS", "0.6") or "0.6")
CANDLE_LIMIT = env_int("BACKFILL_CANDLE_LIMIT", 120)


FIELD_CANDIDATES = {
    "symbol": ["Symbol", "Pair", "Market", "Ticker", "Asset", "Coin"],
    "timeframe": ["Timeframe", "TF", "Chart TF", "Snapshot Timeframe"],
    "bias": ["Bias", "Direction", "Side", "Setup Bias", "Trade Bias"],
    "entry": ["Entry", "Entry Zone", "Entry Price", "Entry Low", "Entry Area"],
    "invalidation": ["Invalidation", "Stop", "Stop Loss", "SL", "Stop Price"],
    "target_1": ["Target 1", "TP1", "Take Profit 1", "T1"],
    "target_2": ["Target 2", "TP2", "Take Profit 2", "T2"],
    "score": ["Score", "Setup Score", "Quality Score"],
    "grade": ["Grade", "Quality", "Setup Grade"],
    "status": ["Status", "Trade Status"],
    "result": ["Result", "Outcome"],
    "rr": ["RR", "R:R", "R Multiple", "R-Multiple"],
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


def require_config() -> None:
    missing = []
    if not AIRTABLE_TOKEN:
        missing.append("AIRTABLE_TOKEN or AIRTABLE_API_KEY")
    if not AIRTABLE_BASE_ID:
        missing.append("AIRTABLE_BASE_ID")
    if not NETLIFY_SITE_URL:
        missing.append("NETLIFY_SITE_URL")
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


def field(fields: Dict[str, Any], key: str, default: Any = None) -> Any:
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


def airtable_url(path: str = "") -> str:
    table = quote(AIRTABLE_TABLE_REF, safe="")
    return f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table}{path}"


def list_missing_records(limit: int) -> List[Dict[str, Any]]:
    formula = (
        f"OR({{{AIRTABLE_SCREENSHOT_FIELD}}}=BLANK(),"
        f"{{{AIRTABLE_SCREENSHOT_FIELD}}}='')"
    )
    params: Dict[str, Any] = {
        "pageSize": min(100, max(1, limit)),
        "maxRecords": limit,
        "filterByFormula": formula,
    }
    if AIRTABLE_VIEW:
        params["view"] = AIRTABLE_VIEW

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
                f"Airtable list failed {response.status_code}: {response.text[:600]}\n"
                f"Checked table reference: {AIRTABLE_TABLE_REF}\n"
                "Fix: copy the exact table variable from the live SCANNER service. "
                "If the live scanner uses a tbl... ID, set AIRTABLE_TABLE_ID or "
                "AIRTABLE_SCANNER_TABLE_ID on BACKFILL."
            )
        data = response.json()
        records.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            break
    return records[:limit]


def fetch_binance_candles(
    symbol: str, timeframe: str, alert_time: Optional[datetime]
) -> List[Dict[str, Any]]:
    end_time_ms = None
    if alert_time:
        end_time_ms = int(alert_time.timestamp() * 1000)

    endpoints = [
        "https://fapi.binance.com/fapi/v1/klines",
        "https://api.binance.com/api/v3/klines",
    ]
    last_error = ""

    for endpoint in endpoints:
        params: Dict[str, Any] = {
            "symbol": symbol,
            "interval": timeframe,
            "limit": CANDLE_LIMIT,
        }
        if end_time_ms:
            params["endTime"] = end_time_ms

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
                        "time": int(row[0]),
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


def snapshot_endpoint() -> str:
    return f"{NETLIFY_SITE_URL}{NETLIFY_SAVE_FUNCTION_PATH}"


def build_snapshot_payload(record: Dict[str, Any], candles: List[Dict[str, Any]]) -> Dict[str, Any]:
    fields = record.get("fields", {})
    raw_symbol = field(fields, "symbol", "UNKNOWN")
    timeframe = normalize_timeframe(field(fields, "timeframe", "15m"))
    bias = field(fields, "bias", "WATCH")
    alert_time = parse_time(field(fields, "alert_time"), record.get("createdTime"))

    ohlcv = [
        [
            candle["time"],
            candle["open"],
            candle["high"],
            candle["low"],
            candle["close"],
            candle["volume"],
        ]
        for candle in candles
    ]

    payload = {
        "source": VERSION,
        "record_id": record["id"],
        "airtable_record_id": record["id"],
        "symbol": str(raw_symbol),
        "pair": str(raw_symbol),
        "timeframe": timeframe,
        "snapshot_timeframe": timeframe,
        "bias": bias,
        "direction": bias,
        "side": bias,
        "alert_time": alert_time.isoformat() if alert_time else None,
        "entry": field(fields, "entry"),
        "entry_zone": field(fields, "entry"),
        "invalidation": field(fields, "invalidation"),
        "stop_loss": field(fields, "invalidation"),
        "target_1": field(fields, "target_1"),
        "tp1": field(fields, "target_1"),
        "target_2": field(fields, "target_2"),
        "tp2": field(fields, "target_2"),
        "score": field(fields, "score"),
        "grade": field(fields, "grade"),
        "status": field(fields, "status"),
        "result": field(fields, "result"),
        "rr": field(fields, "rr"),
        "candles": candles,
        "klines": candles,
        "ohlcv": ohlcv,
        "meta": {
            "backfilled": True,
            "version": VERSION,
            "airtable_record_id": record["id"],
        },
    }
    return {key: value for key, value in payload.items() if value is not None}


def extract_snapshot_url(data: Dict[str, Any]) -> Optional[str]:
    for key in ("screenshot_url", "snapshot_url", "url", "publicUrl", "href"):
        value = data.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    nested = data.get("snapshot") or data.get("data") or {}
    if isinstance(nested, dict):
        return extract_snapshot_url(nested)
    return None


def save_snapshot(payload: Dict[str, Any]) -> str:
    headers = {"Content-Type": "application/json"}
    if NETLIFY_SNAPSHOT_SECRET:
        headers["Authorization"] = f"Bearer {NETLIFY_SNAPSHOT_SECRET}"

    response = requests.post(
        snapshot_endpoint(), headers=headers, data=json.dumps(payload), timeout=45
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"snapshot save failed {response.status_code}: {response.text[:800]}"
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"snapshot save returned non-JSON: {response.text[:400]}") from exc

    url = extract_snapshot_url(data)
    if not url:
        raise RuntimeError(f"snapshot response did not include URL: {data}")
    return url


def update_airtable(record_id: str, screenshot_url: str) -> None:
    body = {"fields": {AIRTABLE_SCREENSHOT_FIELD: screenshot_url}}
    response = requests.patch(
        airtable_url(f"/{record_id}"),
        headers=airtable_headers(),
        data=json.dumps(body),
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Airtable update failed {response.status_code}: {response.text[:600]}"
        )


def describe_record(record: Dict[str, Any]) -> Tuple[str, str, Optional[datetime]]:
    fields = record.get("fields", {})
    raw_symbol = field(fields, "symbol", "UNKNOWN")
    timeframe = normalize_timeframe(field(fields, "timeframe", "15m"))
    alert_time = parse_time(field(fields, "alert_time"), record.get("createdTime"))
    return str(raw_symbol), timeframe, alert_time


def run() -> None:
    require_config()
    print(f"BOOT CHECK: {VERSION}")
    print(
        "Backfill config | "
        f"table={AIRTABLE_TABLE_REF} | field={AIRTABLE_SCREENSHOT_FIELD} | "
        f"limit={BACKFILL_LIMIT} | dry_run={BACKFILL_DRY_RUN}"
    )
    print(f"Snapshot endpoint: {snapshot_endpoint()}")

    records = list_missing_records(BACKFILL_LIMIT)
    print(f"Found {len(records)} Airtable rows missing {AIRTABLE_SCREENSHOT_FIELD}.")
    if not records:
        return

    saved = 0
    skipped = 0
    failed = 0

    for index, record in enumerate(records, start=1):
        raw_symbol, timeframe, alert_time = describe_record(record)
        normalized_symbol = normalize_symbol(raw_symbol)
        label = f"{index}/{len(records)} {raw_symbol} {timeframe} {record['id']}"

        if not normalized_symbol:
            skipped += 1
            print(f"Backfill skipped: missing symbol | {label}")
            continue

        try:
            candles = fetch_binance_candles(normalized_symbol, timeframe, alert_time)
            payload = build_snapshot_payload(record, candles)

            if BACKFILL_DRY_RUN:
                print(
                    f"Dry run: would save snapshot for {label} "
                    f"with {len(candles)} candles."
                )
            else:
                url = save_snapshot(payload)
                update_airtable(record["id"], url)
                saved += 1
                print(f"Backfill saved: {raw_symbol} -> {url}")

            time.sleep(BACKFILL_SLEEP_SECONDS)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"Backfill failed: {label} | {exc}")

    print(
        "Backfill complete | "
        f"saved={saved} | skipped={skipped} | failed={failed} | dry_run={BACKFILL_DRY_RUN}"
    )


if __name__ == "__main__":
    run()
