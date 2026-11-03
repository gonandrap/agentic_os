# os.db schema fence (issue 1500)

## What is broken

`scripts/shipit.sh` swaps the code (step 5), restarts `jarvis-ui.service` inline (step 6)
and `jarvis.service` ~3s later from a transient unit (step 8). For that window the previous
release still opens os.db. jarvis-0.14.6 (PR #1475) made `CentralStore._rebuild_morpheus_items`
copy `morpheus_items` into `monitoring_orders` and DROP it; jarvis-0.14.5's `SCHEMA`
(`CREATE TABLE IF NOT EXISTS morpheus_items`, its index, and its `ADDED_COLUMNS` ALTERs)
re-creates it on every open. Evidence: 2026-11-01 22:52:41 the dashboard's cost/cache/week
task died in `CentralStore._migrate` with `OperationalError: no such table: main.morpheus_items`.

## Root cause

os.db carries no record of which release shaped it, so any opener of any version runs its
own DDL unconditionally. A destructive migration's one-shot guard (re-check inside
`BEGIN IMMEDIATE`) only protects against openers of the SAME version.

## Fix

Lives in `src/jarvis/central_store.py`, the only place os.db DDL runs.

* `SCHEMA_VERSION` is stored as `PRAGMA user_version`. Every migration that drops or
  rewrites a table bumps it and sets it in the SAME transaction as the drop (`_fence`).
* `CentralStore._build_schema` runs `SCHEMA` and `_migrate` only when
  `user_version <= SCHEMA_VERSION`. An older release finding a newer fence abstains from all
  DDL, so it cannot re-create what the newer one dropped. Reads and writes still proceed.
* The schema step retries `SCHEMA_ATTEMPTS` times with linear backoff on
  `sqlite3.OperationalError` (except `locked`, which already waited `busy_timeout`), so an
  opener racing another version's DDL retries instead of surfacing a crash.

## Rejected

Stopping every service before the code swap: `jarvis.service` can host the shipit run
itself (why step 8 restarts it from a transient unit), so stopping it pre-swap kills the
release; CLI processes from the old checkout are not services anyway. (Neo Q3171.)

## Limit

The fence binds only releases that carry it. The first fenced release still races the
pre-fence one; the retry alone covers that window.
