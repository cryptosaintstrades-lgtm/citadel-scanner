# Liquidity Citadel v70 Auto Result Tracker

This is a separate Railway-side service for tracking scanner alert outcomes.

It reads active Airtable scanner rows, checks candles after the alert time, and updates:

- `Status`
- `Result`
- `RR`
- `Closed Time`

The live scanner can keep running untouched.

## Files

- `citadel_result_tracker_v70.py`
- `requirements-tracker.txt`

## Railway service

Create a separate service, like:

```text
RESULT-TRACKER
```

Use this custom start command:

```bash
python citadel_result_tracker_v70.py
```

Do not replace the live scanner start command.

## Required variables

Copy these from the live scanner:

```text
AIRTABLE_TOKEN
AIRTABLE_BASE_ID
AIRTABLE_SCANNER_TABLE=Scanner
```

If your scanner uses a table ID, use:

```text
AIRTABLE_TABLE_ID=tbl...
```

## Safe first run

Start with:

```text
TRACKER_DRY_RUN=true
TRACKER_LIMIT=10
TRACKER_RUN_ONCE=false
TRACKER_INTERVAL_SECONDS=300
```

Expected boot log:

```text
BOOT CHECK: v70-auto-result-tracker
Tracker config | table=Scanner
Tracker scan: X candidate active rows.
Dry run: would close ...
```

## Live mode

After dry-run looks clean:

```text
TRACKER_DRY_RUN=false
TRACKER_LIMIT=50
```

## Optional variables

```text
AIRTABLE_STATUS_FIELD=Status
AIRTABLE_RESULT_FIELD=Result
AIRTABLE_RR_FIELD=RR
AIRTABLE_CLOSED_TIME_FIELD=Closed Time
TRACKER_ACTIVE_STATUSES=Active,Open,Tracking,In Progress
TRACKER_CLOSED_STATUSES=Closed,Complete,Completed,Archived
TRACKER_INCLUDE_BLANK_STATUS=false
TRACKER_AMBIGUOUS_CANDLE_RULE=conservative
TRACKER_MAX_CANDLES=1000
TRACKER_LOOKBACK_DAYS=14
```

## Conservative handling

If a stop and target are both touched inside the same candle, the tracker defaults to:

```text
Loss
```

That prevents the performance board from overstating wins when intrabar order is unknown.

