# SQLite → Postgres dev data migration

One-off walkthrough for moving an existing local `db.sqlite3` into the
Postgres dev DB introduced by issue #421 (`docker-compose.dev.yml`) and
wired in #422 (`settings.dev` / `settings.test`).

> **Who runs this?** Only developers who had data in `db.sqlite3` before
> the Postgres switch. New checkouts have no SQLite file and skip this
> step entirely — `make migrate` against Postgres produces an empty
> schema, which is the expected state.

## Prerequisites

1. `db.sqlite3` exists in the djangoproject root with data you want to
   preserve. After issue #420 the file is gitignored; if you ran the
   project before that point, the file is still on disk.
2. Local Postgres + Redis + MinIO stack is up:
   ```
   docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
   ```
   (Or just the `db` service: `… up -d db`.)
3. `.env` has POSTGRES_* vars matching the compose `db` service. The
   defaults in `.env.example` (`beautygo / beautygo / beautygo`) are
   correct out of the box.
4. Python deps installed: `pip install -r requirements.txt`.

## Run the migration

From the djangoproject root:

```
DJANGO_SETTINGS_MODULE=djangoProject.settings.dev \
    python scripts/migrate_sqlite_to_postgres.py
```

The script:

1. Verifies `db.sqlite3` exists and Postgres is reachable.
2. Adds a runtime-only `sqlite_legacy` connection alias pointing at
   `db.sqlite3` (not persisted in `settings.dev`).
3. Applies the current Django migrations to Postgres on the default
   connection (idempotent — re-running is safe).
4. Dumps SQLite data to a temporary JSON file via
   `dumpdata --database=sqlite_legacy --natural-foreign --natural-primary`,
   excluding tables that Django regenerates on its own
   (`contenttypes`, `auth.permission`, `admin.logentry`, `sessions`,
   `token_blacklist.*`).
5. Loads the JSON into Postgres via `loaddata --database=default`.
6. Prints a row-count comparison for the user-data tables
   (`users.User`, `services.Service`, `appointments.Appointment`, etc).

A non-zero diff in the row-count table is **not** automatic failure
— some divergence is expected for Django-managed bootstrap rows
already present after `migrate`. Eyeball the table.

> **Re-running the script**: `loaddata` is **upsert-by-PK**, not by
> natural key. Re-running with the same SQLite source overwrites
> matching rows in Postgres; rows added to Postgres after the first
> run are preserved.

## Verify

After the script finishes, run the test suite against Postgres:

```
DJANGO_SETTINGS_MODULE=djangoProject.settings.test \
    pytest --tb=short -q
```

A green suite confirms the schema accepts the loaded data. The 4
`@pytest.mark.xfail` tests tracked in
[`ai-bot-platform#477`](https://github.com/AndreyDeveloper84/ai-bot-platform/issues/477)
will still report as expected-failures — that is fine.

**This pytest run is the closure of issue #424 acceptance item 2**
(`pytest passes against Postgres with migrated data`).

For a faster smoke-only check:

```
make celery-ping       # confirms compose stack is alive end-to-end
make migrate           # idempotent re-run
make test-app APP=appointments  # exercises a slice of the migrated data
```

## Fallback (if the script fails partway)

The script does not roll back partial loads — `loaddata` is one shot.
If something looks wrong, blow away Postgres and start clean:

> **Local machines only.** `down -v` removes the project's named volumes,
> `<project>_postgres_data` included. Run in a checkout whose compose project
> name matches a live environment — `/home/taximeter/beautygo/dev` on the dev
> VPS resolves to project `dev`, i.e. `dev_postgres_data` — and it deletes the
> pilot's database. Nothing in `.github/workflows/` runs `down` in any form,
> and nothing should.

```
docker compose -f docker-compose.yml -f docker-compose.dev.yml down -v
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d db
make migrate                     # rebuild empty Postgres schema
```

You now have a fresh Postgres. Reseed by:

- Re-running the migration script (re-attempt).
- Or accepting an empty DB and creating a new superuser + test fixtures
  by hand (`make superuser`, your app's fixture loaders, etc).

`db.sqlite3` is untouched by the failed run — you can attempt the
migration as many times as needed.

## Reverse migration

Not implemented. By Phase 0 close criteria, SQLite is no longer a
supported dev backend (#422). If you genuinely need to round-trip the
other direction, you can run the script's logic in reverse manually:
inject a `sqlite_legacy` alias, `dumpdata --database=default`, then
`loaddata --database=sqlite_legacy`. There is no Phase 0 ticket
covering this and no plan to add one — the Phase 0 freeze keeps the
direction one-way.

## Caveats

- The script's `loaddata` step assumes the Postgres schema matches the
  current source-tree migrations. If you migrated a SQLite snapshot
  from an older code version, run `manage.py migrate` against SQLite
  *first* (using the `sqlite_legacy` alias) so the schemas line up
  before dumping.
- Custom Postgres types (e.g. PostGIS geography) that SQLite emulated
  via Decimal/Text columns may need a manual touch-up. Inspect the
  dumped JSON if `loaddata` errors on a specific row.
- The script uses `--natural-foreign --natural-primary` to keep FKs
  stable across PK renumbering. Models without a `Meta.unique_together`
  or natural-key manager will still rely on PK; this is fine for the
  small dev-data scale where collisions are unlikely.

## References

- Issue #424 (this script's tracking ticket).
- Issue #420 (db.sqlite3 untracked) — context for why the SQLite file
  is no longer in the repo.
- Issue #422 (settings wired to Postgres) — the change that made this
  migration necessary.
- ADR-0009 §Phase 0 stabilization — Phase 0 close criteria #4.
