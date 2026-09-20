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
  retain the per-activity savings record and cumulative total in the encrypted
  local database.

## First-release experience

1. Sign in with Strava and grant `activity:read_all,activity:write`.
2. Configure a Home-to-Work rule, its endpoint radius, vehicle fuel economy,
   and gas price.
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

`Home ↔ Work` is direction-independent; it matches either Home-to-Work or
Work-to-Home. Future rules can be one-way. Missing or privacy-obscured endpoint
coordinates, or a missing activity distance, are safe non-matches, never an
automatic commute.

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
| Credential and configuration storage | Encrypted local SQLite |
| Authentication | Strava OAuth authorization-code flow and encrypted refresh tokens |
| Activity trigger | Poll `/athlete/activities` after the configured rule time |
| Secrets | Owner-only local environment file and encryption key; no credentials in source or logs |
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
browser OAuth flow, checks state and granted scopes, encrypts the returned
access and refresh tokens in SQLite, refreshes expired access tokens, and
revokes/deletes the local connection on disconnect.

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

The service creates `commuter.db` and `.commuter.key` in the project directory.
The database holds encrypted access/refresh tokens; the Fernet key is local,
created with owner-only permissions, and must be backed up alongside the
database if the connection should survive a machine migration. Both artifacts
are ignored by Git. The local privacy, terms, support, and deletion pages are
available at `/privacy`, `/terms`, `/support`, and `/data-deletion`.

## Raspberry Pi web service

`make install` generates systemd units for the path of the current checkout
and runs them as the user that invokes the command. It stores mutable state in
`/var/lib/commuter` and binds the optional OAuth/local-administration web
service to `127.0.0.1`, so it does not expose a port to the LAN or internet.

On Raspberry Pi OS or another Debian-based system, install `uv`, then run the
target from the checkout's actual location:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
cd /path/to/commuter
make install
```

The first invocation creates `/etc/commuter/commuter.env` and exits. Populate
that file, copy the encrypted database and key as described below, then run
`make install` again. Enable the web service only when you need to use it:

```sh
sudo systemctl enable --now commuter-web.service
sudo systemctl status commuter-web.service
```

For OAuth or local administration from another computer, tunnel the service
over SSH, then visit `http://127.0.0.1:8000` in that computer's browser:

```sh
ssh -L 8000:127.0.0.1:8000 pi@commuter-pi
```

## Configure and poll commuter rides

After connecting exactly one Strava account, configure the Home-to-Work rule in
the encrypted local database. Coordinates are command-line input and are not
committed to this repository.

```sh
uv run commuter configure-commute \
  --home LATITUDE,LONGITUDE \
  --work LATITUDE,LONGITUDE \
  --radius-m 150 \
  --combined-mpg 25 \
  --gas-price 4.34 \
  --vehicle "2016 Subaru Forester"
```

The rule applies to `Ride` activities that begin within the configured radius
of one endpoint and finish within the radius of the other, in either direction.
For each matching ride, Commuter converts the ride's Strava distance from
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
when their saved endpoints do not fall within the configured Home/Work radius.

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
sudo systemctl enable --now commuter-sync.timer
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
and Discord settings, copy the encrypted database and matching encryption key
into `/var/lib/commuter` as shown below, then run `make install` again.

On later updates, run `git pull` in that checkout, then run `make install`
again. The target synchronizes dependencies, regenerates the units with that
checkout's current absolute path, reloads systemd, and restarts the active web
service/timer. It deliberately does not replace the encrypted database,
encryption key, or populated
`/etc/commuter/commuter.env`, preserving OAuth credentials, commute totals, and
Discord configuration. The default `uv` path is `~/.local/bin/uv`; override it
when necessary, for example `make install UV_BIN=/usr/local/bin/uv`.

If the account connection and rule are configured on another computer before
moving to the Pi, copy both the database and its matching encryption key. The
database cannot be decrypted without the key:

```sh
scp -p commuter.db .commuter.key pi@commuter-pi:/tmp/
ssh pi@commuter-pi '
  sudo install -o commuter -g commuter -m 0600 /tmp/commuter.db /var/lib/commuter/commuter.db
  sudo install -o commuter -g commuter -m 0600 /tmp/.commuter.key /var/lib/commuter/.commuter.key
'
```

## Wipe local state

To stop using Commuter, revoke its Strava authorization and remove all local
Commuter data—including credentials, settings, activity-processing state, and
the encryption key—run:

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

The encrypted database stores the OAuth connection, Home/Work coordinates,
vehicle inputs, durable cumulative savings and CO₂ totals, and activity
outcomes. It does not retain raw Strava activity payloads. `commuter wipe`
removes the database and encryption key when the Pi or service is retired.

## Deferred work

- Automatic gas-price and vehicle-economy sources
- Google Routes driving-distance calculation and map picker
- Backfills, multi-athlete onboarding, a browser extension, and public hosting
