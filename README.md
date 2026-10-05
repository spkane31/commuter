# Commuter

Commuter is a personal, server-side service that recognizes bicycle trips
between user-defined places, marks qualifying Strava activities as commutes,
and appends an estimate of avoided gasoline and fuel cost to the activity
description.

The first release is deliberately personal and narrow: one athlete and manual
vehicle and fuel inputs. It uses each matched activity's recorded Strava
distance as the avoided driving distance; it does not need a browser
extension, Google Maps, or an automatic fuel-price provider.

## Product decisions

- **Python is the implementation language.** Python's HTTP, web, data
  validation, and system-integration ecosystems fit this local service better
  than growing the initial Go bootstrap.
- **Server-side only.** The application owns OAuth, local settings, scheduled
  polling, and activity updates. It must never scrape or automate Strava's
  website.
- **Activity descriptions** Strava supports updating an
  activity's `commute`, `hide_from_home`, and `description` fields. [Strava's
  activity reference](https://developers.strava.com/docs/reference/)
- **Manual inputs first.** The user supplies combined MPG and current gas
  price for each commute rule. Commuter uses the matched activity's recorded
  Strava distance for its savings estimate.
- **Feed muting is automatic.** Every Commuter update sets
  `hide_from_home: true`, removing it from the home feed while retaining it on
  the athlete's profile.
- **Persistent total for this private deployment.** The owner has chosen to
  retain the per-activity savings record and cumulative total in an owner-only
  local database.

## First-release experience

1. Sign in with Strava and grant `activity:read_all,activity:write`.
2. Configure a commute rule with at least two named locations, an endpoint
   radius, vehicle fuel economy, and gas price.
3. Upload a qualifying `Ride` after the rule is created.
4. The Pi polls Strava every 15 minutes, marks the ride as a commute, and
   appends a managed description block:

   ```text
   --- Commuter ---
   Fuel avoided: 0.20 gal
   CO₂ avoided: 1.78 kg
   Estimated fuel savings: $0.87
   Cumulative fuel savings: $12.34
   Cumulative CO₂ avoided: 27.45 kg
   --- /Commuter ---
   ```

The service replaces only the block between these delimiters and preserves the
rest of the athlete's description. It records the durable activity outcome only
after the Strava update and Discord notification succeed, so either remote call
is retried if it fails.

## Matching and calculation rules

An activity matches when its selected sport type is allowed and both endpoint
tests are true:

```text
distance(activity.start, origin.location) <= origin.radius
distance(activity.end, destination.location) <= destination.radius
```

Any two distinct configured locations are direction-independent; a ride
matching Location A ↔ Location B also matches Location B ↔ Location A. A ride
that starts and ends at the same location never matches. Missing or
privacy-obscured endpoint coordinates, or a missing activity distance, are
safe non-matches, never an automatic commute.

For a matched activity:

```text
ride_miles      = activity.distance_meters / 1609.344
gallons_avoided = ride_miles / combined_mpg
fuel_savings    = gallons_avoided * gas_price_per_gallon
co2_avoided     = gallons_avoided * 8,887 grams CO₂/gallon
```

The activity distance is a practical estimate, not a driving-route calculation.
CO₂ is estimated as gasoline tailpipe emissions using EPA's 8,887 grams per
gallon factor; it excludes fuel-production and bicycle lifecycle emissions.
[EPA vehicle emissions reference](https://www.epa.gov/greenvehicles/greenhouse-gas-emissions-typical-passenger-vehicle)
The numbers are not a claim about actual gasoline, parking, or vehicle wear
savings.

## Technical shape

| Concern | First-release choice |
| --- | --- |
| Web/API service | Python, FastAPI, and HTTPX |
| Scheduled work | `systemd` one-shot service and persistent 15-minute timer |
| Credential and configuration storage | Owner-only local SQLite |
| Authentication | Strava OAuth authorization-code flow and local refresh tokens |
| Activity trigger | Poll `/athlete/activities` after the configured rule time |
| Secrets | Owner-only local environment file and database; no credentials in source or logs |
| Tests | Pytest and mocked Strava HTTP responses |

New Strava applications begin in single-player mode, which fits the personal
launch. OAuth access tokens are short-lived and refresh tokens must remain
server-side. [Strava getting started](https://developers.strava.com/docs/getting-started/)
[OAuth documentation](https://developers.strava.com/docs/authentication/)

The sync command obtains a valid access token, lists recent activities, fetches
the unprocessed candidates, and updates matching rides. It has no public
webhook endpoint or Temporal dependency. [Strava activity reference](https://developers.strava.com/docs/reference/)

Every successful live activity update also sends its Strava link, estimated and
cumulative fuel savings, and estimated and cumulative CO₂ avoided to the
configured Discord channel. It uses the Discord bot API; the bot must be in the
configured guild and have permission to view and send messages in the
configured channel. [Discord message API](https://discord.com/developers/docs/resources/message#create-message)

## Local OAuth setup

The first implemented slice is local Strava connection management. It starts a
browser OAuth flow, checks state and granted scopes, stores the returned access
and refresh tokens in an owner-only SQLite database, refreshes expired access
tokens, and revokes/deletes the local connection on disconnect.

1. In the Strava API settings, configure the authorization callback domain as
   `127.0.0.1` for local development. The application callback is
   `http://127.0.0.1:8000/auth/strava/callback`.
2. Keep `STRAVA_CLIENT_ID`, `STRAVA_CLIENT_SECRET`, `DISCORD_BOT_TOKEN`,
   `DISCORD_GUILD_ID`, and `DISCORD_CHANNEL_ID` in `.env`. The existing
   `STRAVA_ACCESS_TOKEN` and `STRAVA_REFRESH_TOKEN` are not imported by the web
   service; visiting the OAuth flow obtains a new athlete token set and writes
   it to local storage. Discord settings are required when running `sync`.
3. Install and run the service:

   ```sh
   uv sync --group dev
   uv run commuter
   ```

4. Visit `http://127.0.0.1:8000`, select **Connect with Strava**, and approve
   `activity:read_all` plus `activity:write`.

The service creates `commuter.db` in the project directory with mode `0600`.
The database holds access/refresh tokens and owner-entered settings in
plaintext, which is appropriate only for this personal, local-network
deployment. It is ignored by Git. The local privacy, terms, support, and
deletion pages are available at `/privacy`, `/terms`, `/support`, and
`/data-deletion`.

## Raspberry Pi web service

`make install` generates systemd units for the path of the current checkout
and runs them as the user that invokes the command. It stores mutable state in
`/var/lib/commuter` and binds the optional OAuth/local-administration web
service to `127.0.0.1`, so it does not expose a port to the LAN or internet.

On Raspberry Pi OS or another Debian-based system, install `uv`, then run the
target from the checkout's actual location. The target installs Python 3.13:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
cd /path/to/commuter
make install
```

The first invocation creates `/etc/commuter/commuter.env` and exits. Populate
that file, then run `make install` again. Enable the web service only when you
need to use it:

```sh
sudo systemctl enable commuter-web.service
sudo systemctl start commuter-web.service
sudo systemctl status commuter-web.service
```

For OAuth or local administration from another computer, tunnel the service
over SSH, then visit `http://127.0.0.1:8000` in that computer's browser:

```sh
ssh -L 8000:127.0.0.1:8000 pi@commuter-pi
```

## Configure and poll commuter rides

After connecting exactly one Strava account, configure the commute rule in
the local database. Coordinates are command-line input and are not
committed to this repository.

```sh
uv run commuter configure-commute \
  --location home,LATITUDE,LONGITUDE \
  --location work,LATITUDE,LONGITUDE \
  --location gym,LATITUDE,LONGITUDE \
  --radius-m 150 \
  --combined-mpg 25 \
  --gas-price 4.34 \
  --vehicle "2016 Subaru Forester"
```

Repeat `--location NAME,LATITUDE,LONGITUDE` for each place you want the rule to
cover; at least two are required. The rule applies to `Ride` activities that
begin within the configured radius of one location and finish within the
radius of a *different* configured location, in either direction—a ride that
starts and ends at the same location does not match. For each matching ride,
Commuter converts the ride's Strava distance from
meters to miles, then uses it with the configured MPG and gas price. It marks
the activity as a Strava commute and appends a managed description block with
that ride's savings and the persistent cumulative total. Existing non-Commuter
description text is preserved. Every matching activity is kept off the home
feed while remaining visible on the athlete's profile.

By default a poll only considers rides after the rule was saved. To safely
inspect older rides from the last week, make a read-only dry run:

```sh
uv run commuter sync --backfill-days 7 --dry-run
```

It prints the matching Strava activity IDs but does not update Strava, change
the cumulative total, or record any processing state locally. If those are the
rides you expect, apply that same historical window once:

```sh
uv run commuter sync --backfill-days 7
```

That live command marks matching historical rides as commutes, hides them from
the home feed, and adds their fuel savings and CO₂ avoided to persistent
cumulative totals. Future scheduled polls use the default window, so they do
not repeatedly backfill old rides.

To explain why previously evaluated rides did or did not match, use the
read-only diagnostic recheck. Each non-match logs its activity ID, local date,
Strava activity type, distance in miles, and the matching reason:

```sh
uv run commuter sync --backfill-days 2 --recheck-non-matches --dry-run --verbose
```

After reviewing that output, remove `--dry-run` to apply newly matched rides.
Rides already marked as a Strava commute are treated as Commuter matches even
when their saved endpoints do not fall within any configured location's radius.

If a poll reports HTTP 401 or 403, start the local web application with
`uv run commuter`, visit http://127.0.0.1:8000, and use **Connect with Strava**
to replace the local authorization. Approve `activity:read_all` and
`activity:write`. Reconnecting the same athlete retains the saved commute rule
and cumulative total.

On HTTP 429, Commuter treats the request as retryable. It honors a short
`Retry-After` delay once and logs the backoff with `--verbose`. A delay longer
than 60 seconds is deferred to the next 15-minute poll so the Pi service stays
within its two-minute startup limit; the CLI reports the calculated retry delay.

After verifying one live update, enable the persistent 15-minute timer on the
Pi:

```sh
sudo systemctl enable commuter-sync.timer
sudo systemctl start commuter-sync.timer
sudo systemctl list-timers commuter-sync.timer
sudo journalctl -u commuter-sync.service -f
```

The timer waits two minutes after boot and then runs every 15 minutes. A timer
run that is delayed by an outage is retried at the next interval; systemd will
not run overlapping `commuter sync` processes.

### Updating an installed Pi

Keep the repository checkout wherever is convenient (for example,
`~/git/github.com/spkane31/commuter`), then SSH to the Pi and run the following
from that checkout as the normal SSH user—not with `sudo`:

```sh
make install
```

On its first invocation, the target creates the state/configuration directories
and an `/etc/commuter/commuter.env` template, then stops. Populate the Strava
and Discord settings, copy an existing plaintext database into
`/var/lib/commuter` as shown below, then run `make install` again.

On later updates, run `git pull` in that checkout, then run `make install`
again. The target synchronizes dependencies, regenerates the units with that
checkout's current absolute path, reloads systemd, and restarts the active web
service/timer. It deliberately does not replace the database or populated
`/etc/commuter/commuter.env`, preserving OAuth credentials, commute totals, and
Discord configuration. The default `uv` path is `~/.local/bin/uv`; override it
when necessary, for example `make install UV_BIN=/usr/local/bin/uv`.

If the account connection and rule are configured on another computer before
moving to the Pi, copy the database:

```sh
scp -p commuter.db pi@raspberrypi.local:/tmp/
ssh pi@raspberrypi.local '
  sudo install -o pi -g pi -m 0600 /tmp/commuter.db /var/lib/commuter/commuter.db
'
```

## Wipe local state

To stop using Commuter, revoke its Strava authorization and remove all local
Commuter data—including credentials, settings, and activity-processing state—run:

```sh
uv run commuter wipe
```

The command keeps all local files if Strava cannot be reached or revocation
fails. Retry when online. If the Pi is being retired or local deletion is more
important than revocation, use:

```sh
uv run commuter wipe --force-local
```

`--force-local` removes local data without revoking the authorization held by
Strava. Revoke the application separately from Strava's connected-app settings
when possible. Neither command removes `.env`, which contains the application
client secret rather than connected-account data; protect it with owner-only
permissions (`chmod 600 .env`).

## Local data

The owner-only plaintext database stores the OAuth connection, configured
location coordinates, vehicle inputs, durable cumulative savings and CO₂
totals, and activity outcomes. It does not retain raw Strava activity payloads. `commuter
wipe` removes the database when the Pi or service is retired.

## Deferred work

- Automatic gas-price and vehicle-economy sources
- Google Routes driving-distance calculation and map picker
- Backfills, multi-athlete onboarding, a browser extension, and public hosting


## Training export to Google Sheets

Two processors run in declaration order: `CommuteProcessor`, then
`TrainingSheetsProcessor`. Each receives `context` first and the updated activity
second. Training-only commands do not edit Strava, notify Discord, or change
commute savings. Existing home/work configurations remain readable.

Install the locked dependencies with `uv sync --frozen --group dev`. Enable the
Sheets API, share the target workbook with the service-account email as an
editor, and keep its JSON credentials outside version control. The adapter uses
the official Google SDK with the `https://www.googleapis.com/auth/spreadsheets`
scope; scheduled runs require no browser. Example private configuration:

```dotenv
COMMUTER_SHEETS_ENABLED=true
COMMUTER_SPREADSHEET_ID=YOUR_WORKBOOK_ID
COMMUTER_ACTIVITY_SHEET=RAW DATA
GOOGLE_APPLICATION_CREDENTIALS=/absolute/private/path/credentials.json
COMMUTER_TRAINING_CACHE_DIR=/absolute/private/path/training-cache
COMMUTER_REPORTING_TIMEZONE=America/Denver
COMMUTER_TRAINING_RECENT_DAYS=7
COMMUTER_TRAINING_MAX_ACTIVITIES=5
COMMUTER_TRAINING_TIME_BUDGET_S=90
```

Protect the environment and credential files with mode `0600`. `credentials.json`
is ignored by Git. Local defaults use `training-cache` alongside the configured
database and a worksheet named `Activities` if no activity title is supplied.

```sh
uv run commuter sheets-setup
uv run commuter training-backfill --months 2
uv run commuter training-backfill --resume
uv run commuter sync --processors sheets
uv run commuter sync --processors commuter,sheets
uv run commuter training-refresh --activity-id ACTIVITY_ID
uv run commuter training-recalculate
uv run commuter training-reconcile
```

Backfill captures exact timestamps two calendar months apart and resumes that
same range. Sync and backfill process bounded batches; repeat them when `pending=True`.
`training-recalculate` without `--max-activities` processes all cached data,
automatically continuing after a batch reaches its time budget. Specify
`--max-activities` to process only a bounded batch. Errors stop the command with
remaining work saved for resumption; a batch that makes no progress also stops.
Dry runs remain bounded by the run time budget and do not auto-resume.
Sheets requests retry HTTP 429 and transient server errors up to seven times.
Retries honor `Retry-After` (seconds or an HTTP date) and use exponential backoff
with jitter, capped at 64 seconds, when no longer server delay is specified.
All Sheets requests in the run respect the cooldown. If the wait cannot fit
within `COMMUTER_TRAINING_TIME_BUDGET_S`, the export stays pending for a later run.
For larger recalculation batches, allow more time for quota recovery:

```sh
COMMUTER_TRAINING_TIME_BUDGET_S=300 uv run commuter training-recalculate --max-activities 20
```

Repeat the same command until `pending=False`; completed rows are upserted safely.
A normal Sheets-enabled `sync` selects both processors, refreshes the last seven
days, resumes an existing backfill when its remaining budget permits, and performs
weekly full-ID reconciliation after backfill starts. Missing IDs become pending
review; `training-remove --activity-id ACTIVITY_ID` confirms a reporting tombstone.
That command does not delete a Strava activity. `--dry-run` computes results
without exporting or changing processing/cache checkpoints. It still reads APIs
and may refresh an expired credential.

Keep the existing 15-minute systemd timer; no second scheduler is needed. On the
Pi, put the above settings in `/etc/commuter/commuter.env`, use absolute writable
state paths under `/var/lib/commuter`, and install locked dependencies in the
service's Python 3.13 environment. Run setup and the first backfill with that
same environment file loaded. Back up the database, private configuration,
Google credentials, and application-owned raw cache using the existing backup
process. Actual Pi installation/restart checks require access to that host.

### Measurements and ownership

`RAW DATA` (or the configured activity tab) has one row per text Strava ID.
Distances use metres; durations and pace use seconds, HR uses bpm, and running
miles use metres / 1609.344. Dates are ISO text: UTC timestamps include a UTC
explicit offset, reporting dates/weeks use `YYYY-MM-DD`, and weeks begin Monday
in the configured timezone. Real zeros remain numeric; missing measurements are
blank with availability columns. Schema version `1` uses stable column order.

HR uses a trailing time-weighted window of **up to three available seconds**.
One or two trusted seconds are usable at startup or after an invalid interval;
a full three seconds is used whenever available. Each HR sample holds until
the next timestamp, and distance is interpolated linearly within recorded
intervals. Pace requires trusted distance and continuous movement. Windows
reset after invalid measurements, recording gaps, or pauses for pace; nothing
is extrapolated beyond the final sample.

The automatic recording-gap cutoff is the larger of three seconds and three
times the recording's median positive timestamp interval. Thus ordinary
four-second recordings use a twelve-second cutoff; longer gaps remain unknown.
`COMMUTER_TRAINING_MAX_GAP_S` or a selected dated zone setting can override it.
`recording_gap_cutoff_s` records the actual cutoff, `rolling_window_s` records
the maximum window, and method version `hr-pace-available-trailing-3s-v2`
identifies the calculation. Sparse-sample interpolation is an estimate within
the accepted recording cadence, rather than extra measured samples.

The activity row retains Strava average/max HR and workout moving/elapsed pace
separately from rolling measurements. Its rolling HR mean weights valid rolling
samples by interval duration; rolling pace aggregates matched valid duration and
speed-derived distance. Raw per-sample time/HR/distance/movement streams remain
in owner-only cache files, indexed by athlete/activity ID. The workbook stores
activity summaries and later per-zone totals, rather than sample arrays.

Strava's reported running and cycling HR zones are fetched with each activity,
cached, and exported in `Strava HR Zones`, including the exact returned bpm
bounds, seconds, sport, sensor flag, and boundary version. These results use
method `strava-reported-v1`; they are platform calculations, independent of our
rolling algorithm and any review of your personal thresholds. In the absence
of confirmed custom settings, they also populate `Zone Time`, weekly summaries,
and HR charts. Unassigned elapsed duration is shown separately as unknown.
A missing or restricted Strava zone response stays unavailable; measurements
still export. Running and cycling may have different returned boundaries.
For manual inspection, `Analysis!A7:E` contains a deduplicated catalog of the
imported boundaries by sport, zone, and settings version. Bounds are numeric;
a blank upper bound means unbounded. Older imported versions remain visible.
Python owns this catalog range and refuses to expand it into occupied user
cells. Keep formulas and manual analyses outside this reserved range.

Leave `Settings` empty to use those platform results without choosing a custom
zone method. Later, supply one row per zone with sport `run`
or `cycling`, effective date, version, lower/upper bpm, confirmation, recording-gap
cutoff, method source, and retrospective flag. Boundaries must be contiguous and
ordered, with inclusive lower and exclusive upper bounds; a blank upper bound
is allowed for the final zone. Confirm personal methods/boundaries before marking
settings confirmed. Current settings applied retrospectively must be labelled.
`effective_from` is the earliest activity date that uses these boundaries, not
the date you entered them. The `retrospective` flag labels historical use; it
does not override that cutoff. To apply current boundaries to all imported
history, set the date on every zone row for each sport to the earliest imported
activity date (for example, `2026-07-11`), and set `retrospective` to `TRUE`.
Earlier workouts otherwise retain Strava-reported zones when available.

Settings edits take effect when an activity is processed again. Recalculate
cached activities and their Dashboard summaries with:

```sh
uv run commuter training-recalculate
```

Cached recalculation makes no Strava requests. Omit `--max-activities` to finish
all cached data in one command; use it for resumable bounded batches.
Existing Z1–Z5 boundaries do not require rebuilding charts.
Custom-calculated zone duration uses recorded elapsed stream time; valid percentages
use classified seconds, excluding unknown time. Moving and elapsed workout
seconds remain visible. Weekly ratios use aggregated matched inputs instead of
averaging workout percentages or paces. Different settings versions are separate
in zone summaries and chart labels.

Python owns reporting tables, summary/chart source tables, and `Sync Log`.
Keep manual category/source overrides, notes, and RPE in `Manual Inputs`, keyed by
text activity ID. Overrides change reporting, not commute side effects. Edit
formulas and additional analyses in `Analysis` or separate tabs. Ordinary sync
preserves those formulas, formatting, and chart edits; explicit
`--refresh-charts` replaces only recorded managed chart IDs. Do not reorder
writer-owned columns. Row sorting is supported because indexes are rebuilt;
duplicate IDs or incompatible headers stop export.

Exports upsert by activity ID, replace obsolete zone rows, and clear stale cells.
A shared process lock prevents overlapping local writers. Activity completion
follows raw writes and rebuilt summaries; a crash can replay the same IDs safely.
Quota limits come from Strava headers; throttled work remains resumable. `Sync Log`
distinguishes partial attempts from the last fully successful sync. Disconnect
and local wipe also remove application-owned cached streams; existing Sheets
rows and Discord messages remain separately managed. The existing commute path
can repeat a Discord notification after a crash between notification and local
completion; it does not guarantee exactly-once remote delivery.

Advanced training-load methods and webhook delivery are deferred. Live validation
of zone calculations against Garmin/Strava remains pending personal boundaries.

### Dashboard range and duration charts

Dashboard weekly and duration charts cover the current Monday-based week and the preceding eleven
weeks in the reporting timezone. Weekly totals include duration by category,
running mileage, non-commute minutes, and sport-separated HR zone minutes.
The duration stack includes running, virtual biking, bike commutes, other
biking, and other activities. Each stack shows total moving minutes including
commutes; the non-commute training chart excludes flagged commutes and Strava
Hike activities. Hikes remain visible in RAW DATA and the all-activity duration
stack, but do not contribute to training duration, rolling run/bike duration,
running mileage, or HR zone summaries and pies, even with a category override.
Other activity types retain their existing behavior.
Minute labels use one decimal place while stored values retain full precision.
The Chart Data schema appends other-biking and other-activity columns without
moving existing chart inputs.
Daily line charts show trailing seven-calendar-day moving minutes for running
and non-commute biking (virtual and outdoor rides). Commute flags and the
`bike_commute` category exclude a ride from these non-commute totals. Missing
activities cannot be inferred from an empty calendar day.

Three pie charts show running, non-commute biking, and combined HR zone minutes
for today and the previous six reporting calendar dates. They exclude both
source/effective commute flags and the commute category. Biking includes
virtual and outdoor rides. Unknown/unassigned elapsed time is a separate slice,
including workouts with missing HR. Combined slices sum sport-relative zone
labels using each sport's own boundaries; they do not imply identical bpm
ranges. Sources rebuild from current activities and zone rows on each sync.

`commuter sheets-setup` adds the new pies to an existing Dashboard without
recreating existing charts. Once registered, a manually deleted pie stays
deleted during ordinary setup/sync. The default layout omits the weekly commute
HR chart. `--refresh-charts` explicitly recreates the managed layout.

The weekly rolling pace chart has been removed; its existing source column
remains reserved to preserve column order. Ordinary sync refreshes chart data
without recreating user-edited charts. To apply this Dashboard layout once:

```bash
commuter sheets-setup --refresh-charts
commuter training-backfill --months 3 --max-activities 20
```

The three-month backfill has its own fixed, resumable checkpoint, preserving
the original two-month import. It also provides the six seed days needed for
the first seven-day total. Repeat the command until pending work is complete;
historical backfill reuses cached details/streams and fetches missing zone data.
Use `training-refresh --activity-id ID` to refetch an older edited activity.
