# Commuter

Commuter connects a personal Strava account, marks configured bike trips as
commutes, estimates fuel and CO₂ savings, and exports training measurements to
Google Sheets. Strava events arrive through a Cloudflare Worker and Queue; a
Linux Temporal worker pulls queued events and runs the shared Python pipeline.
Local commands provide setup, backfill, and recovery. There is no scheduled
polling service.

## Runtime

```text
Strava webhook → Cloudflare Worker → Cloudflare Queue
                                         ↓ outbound pull
Linux Temporal worker → ordered activity processors → Strava + Google Sheets
```

The Cloudflare Worker verifies Strava's callback challenge and enqueues event
JSON. It does not run activity processing. The Linux service polls the Queue
outbound over HTTPS, starts `processStravaEvent` on Temporal task queue
`commuter-strava`, and acknowledges messages after Temporal accepts them. The
worker needs the local SQLite database containing the connected Strava account
and commute configuration. Queue delivery is at least once; workflow IDs and
idempotent activity/Sheets writes make replay safe.

The processor list is registered at startup. Each processor receives the
processing context first and the current activity second, then returns the
updated activity. The commuter processor handles commute classification and
Strava updates; the Sheets processor exports training data. Both run in the
same order for local sync and Temporal workflows.

## Local setup

Use Python 3.13 and `uv`. Put `STRAVA_CLIENT_ID` and
`STRAVA_CLIENT_SECRET` in a private `.env` file, then install and start the
local application:

```sh
uv sync --group dev
chmod 600 .env
uv run commuter
```

Configure the Strava OAuth callback as `127.0.0.1` and visit
`http://127.0.0.1:8000`. Connect the account with `activity:read_all` and
`activity:write`. The local SQLite database stores the refresh token and
commute settings; protect it as a secret. To configure commute endpoints:

```sh
uv run commuter configure-commute \
  --location home,LATITUDE,LONGITUDE \
  --location work,LATITUDE,LONGITUDE \
  --radius-m 150 \
  --combined-mpg 25 \
  --gas-price 4.34 \
  --vehicle "2016 Subaru Forester"
```

The local server is only needed for OAuth and administration. The event worker
is a separate process.

## Cloudflare receiver

The repository's Wrangler config binds a producer named `commuter` to a
Cloudflare Queue with that name. Deploy the Worker through the connected Git
build or with `npx wrangler deploy`. Set `STRAVA_VERIFY_TOKEN` as an encrypted
Worker secret, then register the callback URL
`https://<worker-host>/strava` with Strava. The receiver requires no Temporal,
Strava API, or Google credentials. It returns success after enqueueing the
notification; failed publishes return an error so Strava can retry.

## Linux Temporal worker

Install `uv`, clone the repository on the Linux host, and run:

```sh
make install-temporal-worker
```

The first run creates `/etc/commuter/commuter.env` and exits. Edit it with
`sudoedit /etc/commuter/commuter.env`, then run the make command again. The
installer configures and starts `commuter-temporal-worker.service`.

Required settings are listed in
[`deploy/systemd/commuter.env.example`](deploy/systemd/commuter.env.example):
Strava client ID/secret, Discord bot token/guild/channel, Temporal address,
namespace and API key, Cloudflare account ID/queue ID/API token, plus Sheets
enabled, workbook ID and Google service-account credential path. Set
`COMMUTER_DATABASE_PATH` and `COMMUTER_TRAINING_CACHE_DIR` to persistent,
private paths. The database must already contain the connected Strava account
and commute rule. Keep both the environment file and Google credential file
readable only by the service owner.

Check service health and logs:

```sh
sudo systemctl status --no-pager commuter-temporal-worker.service
sudo journalctl -u commuter-temporal-worker.service -n 100 --no-pager
sudo journalctl -fu commuter-temporal-worker.service
```

After updates, pull the repository and rerun `make install-temporal-worker`.
To provision the Cloudflare Queue API token, create a custom Cloudflare API
token with account-scoped **Queues: Edit** permission. Store it only in the
Linux environment file. The Queue ID is available under **Workers & Pages →
Queues** in the Cloudflare dashboard.

For end-to-end validation, send a valid update event for a real, already
imported activity using the Cloudflare Queue dashboard. Confirm the queue
backlog drains, the workflow completes on `commuter-strava`, the service stays
active, and the existing Sheets row is updated without duplication. Then stop
the service, enqueue a second test message, verify it remains queued, restart
the service, and verify recovery.

## Manual sync and recovery

Local `sync` uses the same registered processor pipeline. Use it for an
explicit historical import, reconciliation, or recovery. The machine must have
the local Commuter database and configured Google credentials.

```sh
uv run commuter sheets-setup
uv run commuter sync --processors commuter,sheets --backfill-days 30
```

With `--max-activities` omitted, an explicit backfill continues through
batches until the range completes, errors occur, or the time budget requires a
later rerun. Progress is persisted, so rerunning the same command resumes the
captured range. Add `--dry-run --verbose` to inspect commute matches without
applying changes or exporting. To refresh older edited data or recalculate
cached data after settings changes:

```sh
uv run commuter training-refresh --activity-id ACTIVITY_ID
uv run commuter training-recalculate
uv run commuter training-reconcile
```

`training-recalculate` uses cached source data and avoids Strava requests.
`training-reconcile` refreshes the full activity-ID list. A reporting removal
can be recorded with `training-remove --activity-id ACTIVITY_ID`; it does not
delete the Strava activity. `commuter wipe` revokes Strava access and removes
local state; `--force-local` removes local files when revocation is unavailable.

## Google Sheets

Enable the Sheets API, share the workbook with the service-account email as an
editor, and set:

```dotenv
COMMUTER_SHEETS_ENABLED=true
COMMUTER_SPREADSHEET_ID=YOUR_WORKBOOK_ID
COMMUTER_ACTIVITY_SHEET=RAW DATA
GOOGLE_APPLICATION_CREDENTIALS=/absolute/private/path/credentials.json
COMMUTER_TRAINING_CACHE_DIR=/absolute/private/path/training-cache
COMMUTER_REPORTING_TIMEZONE=America/Denver
```

The adapter uses Google's official Sheets SDK and spreadsheet write scope.
Secrets stay outside the workbook and source control. `RAW DATA` contains one
row per text Strava activity ID. Other managed tabs provide zone time, weekly
summaries, settings, sync status, and Dashboard charts. Manual inputs and
analysis remain in user-owned ranges; sync preserves user charts and formulas.

Imported measurements retain native units: distance in metres, durations and
pace in seconds, and heart rate in bpm. Dates use ISO format and weeks begin
Monday in the configured timezone. Missing values are blank and accompanied by
availability information; real zeroes remain numeric.

### Heart-rate and pace calculations

HR uses a trailing time-weighted window up to three available seconds. Shorter
valid windows (one or two seconds) are used when less data is available. Pace
uses matched distance and time over the same rolling window. Windows reset at
invalid measurements, recording gaps, or pauses; no duration is extrapolated
past the final stream sample. The gap cutoff defaults to the greater of three
seconds and three times the median positive sample interval, and the actual
cutoff is recorded with the result.

Strava-reported running and cycling zone totals are exported separately for
inspection. Manual boundaries can be entered in `Settings` with sport,
effective date, version, lower/upper bpm, confirmation, gap cutoff, source, and
retrospective flag. Boundaries must be ordered and contiguous; lower limits
are inclusive and upper limits exclusive. A blank upper limit is unbounded for
the final zone. Current settings applied to older activity must be labelled as
retrospective. Zone charts show unknown/unassigned time separately, and
percentages use classified time as their denominator.

### Dashboard

Weekly charts cover the current week and the previous eleven weeks. They show
moving duration by category, running mileage, non-commute training minutes,
and HR zones by sport. Hiking and commute activities are excluded from
training totals. The rolling duration charts show trailing seven-calendar-day
run and non-commute bike totals. The running, biking, and combined HR zone pie
charts cover the last seven reporting dates, exclude commutes, and show
unknown time as a separate slice.

## Development

```sh
uv run pytest
```

The Cloudflare Worker has its own TypeScript build:

```sh
npm ci
npm run build
```
