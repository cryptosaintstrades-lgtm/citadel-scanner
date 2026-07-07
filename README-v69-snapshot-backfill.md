# Liquidity Citadel v69 Snapshot Backfill Tool

This is a one-off runner for older Airtable scanner alerts that are missing `Screenshot URL`.

It does three things:

1. Finds scanner rows in Airtable where `Screenshot URL` is blank.
2. Fetches candles for the symbol/timeframe.
3. Calls the existing Netlify `save-scanner-snapshot` function and writes the returned URL back to Airtable.

## Files

- `citadel_snapshot_backfill_v69.py`
- `requirements-backfill.txt`

Do not replace the live scanner `Procfile` with this. Your live scanner should keep running:

```text
worker: python citadel_scanner_v35_netlify_snapshots.py
```

## Required environment variables

Use the same Railway environment where your scanner already runs:

```text
AIRTABLE_TOKEN
AIRTABLE_BASE_ID
NETLIFY_SITE_URL
```

Optional:

```text
AIRTABLE_SCANNER_TABLE=Scanner Alerts
AIRTABLE_SCREENSHOT_FIELD=Screenshot URL
BACKFILL_LIMIT=50
BACKFILL_DRY_RUN=true
BACKFILL_CANDLE_LIMIT=120
BACKFILL_SLEEP_SECONDS=0.6
```

## Safe first run

The script defaults to dry run:

```text
BACKFILL_DRY_RUN=true
```

That means it will find rows and fetch candles, but it will not save snapshots or update Airtable.

The boot log should show:

```text
BOOT CHECK: v69-snapshot-backfill
```

## Live backfill run

After the dry run looks clean, set:

```text
BACKFILL_DRY_RUN=false
BACKFILL_LIMIT=25
```

Then run:

```bash
python citadel_snapshot_backfill_v69.py
```

Successful rows print:

```text
Backfill saved: ETHFI-USDT -> https://theliquiditycitadel.trade/...
```

## Important

This is meant to be run as a controlled one-off job or separate Railway service. Do not point the main scanner service start command at this file unless you are intentionally pausing the live scanner during the backfill.

