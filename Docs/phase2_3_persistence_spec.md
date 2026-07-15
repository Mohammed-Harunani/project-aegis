# Aegis Phase 2.3 -- PostgreSQL Persistence Spec

## Purpose

Replaces the in-memory ApprovalQueue (Phase 2.2) with durable PostgreSQL
storage. A pending ticket, and every Healing Manifest, must survive an
API restart.

## Database architecture

One PostgreSQL database, `aegis`, with two tables:

- `approval_tickets` -- one row per repair plan that required human
  approval. Holds everything Surgeon needs to execute later
  (`observed_schema`, `gold_schema`, `target_dataset` as JSONB), plus
  the decision audit trail (`decided_by`, `decided_at`, `decision_note`).
- `healing_manifests` -- one row per Surgeon execution, whether
  auto-approved or manually approved. `ticket_id` is a nullable
  foreign key into `approval_tickets`: null for auto-approved
  executions, since those never go through the approval queue.

A separate database, `aegis_test`, exists solely for integration tests
(`tests/test_api.py`) -- created automatically by
`docker/init-test-db.sql` on first Postgres container startup. Tests
must never run against the `aegis` database itself.

See `src/db/models.py` for exact column definitions and
`alembic/versions/0001_initial_tables.py` for the migration that
creates them.

## Environment variables

No credentials are hardcoded anywhere in the codebase. Copy
`.env.example` to `.env` (gitignored) and fill in real values:

- `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` -- read by the
  Postgres container itself.
- `DATABASE_URL` -- read by the application (`src/db/session.py`) and
  Alembic (`alembic/env.py`). Format:
  `postgresql+psycopg://<user>:<password>@<host>:5432/<db>`.
  Host is `postgres` only when running inside the Compose network;
  use `localhost` when running Alembic or pytest directly on the host.
- `TEST_DATABASE_URL` -- read only by `tests/test_api.py`. Must point
  at `aegis_test`, never at the same value as `DATABASE_URL`.

## Approval ticket lifecycle

```
PENDING --approve()--> APPROVED
PENDING --reject()-->  REJECTED
```

Both are terminal -- `TicketNotPendingError` (HTTP 409) is raised if
either is called on a ticket that isn't currently PENDING. Reads
(`GET /approvals`, `GET /approvals/{id}`) use a plain unlocked query.
`approve()` and `reject()` use `SELECT ... FOR UPDATE`
(`_get_record_for_update()` in `approval_repository.py`) so two
concurrent decisions on the same ticket can't both read PENDING and
both proceed -- the second blocks until the first's transaction ends,
then correctly sees the updated status.

## Transaction boundaries

`submit()` (creating a ticket) and `reject()` each commit
independently -- both are a single, self-contained state change with
no follow-up action.

`approve()` does **not** commit internally. It flushes the APPROVED
status change and returns, but the actual commit happens in
`app.py`'s `approve_ticket` endpoint only after Surgeon execution AND
manifest persistence (`save_manifest(..., commit=False)`) both
succeed. If either raises, the endpoint calls `db.rollback()`,
discarding the APPROVED status change along with everything else --
the ticket is left PENDING, not stranded as APPROVED with no manifest.

A `UniqueConstraint` on `healing_manifests.ticket_id` is a second,
database-level layer of protection against a duplicate manifest for
the same ticket (Postgres treats multiple NULLs as distinct, so this
never blocks auto-approved executions, which all have `ticket_id`
null).

## Manifest relationship

Every Surgeon execution -- both `AUTO_APPROVE` (immediate) and
manually approved -- calls `save_manifest()`. The only difference is
`ticket_id`: null for auto-approved, the real ticket UUID otherwise.

## Migration commands

```powershell
docker compose up -d postgres
docker compose run --rm api alembic upgrade head
docker compose up -d api
```

Run migrations inside the Compose network via `docker compose run`,
not directly on the host -- `DATABASE_URL`'s `postgres` hostname only
resolves inside that network.

## Dataset persistence format

`target_dataset` is stored as a versioned envelope, not a plain
`{column: [values]}` dict -- that plain form was verified to lose four
separate things: column order (JSON key order is not something
Postgres JSONB guarantees to preserve -- it's a decomposed binary
format, not text, unlike the plain `json` type), per-column dtype (a
nullable `Int64` column with a null silently became `float64`/`NaN`),
the DataFrame's index (name, dtype, and values all discarded, which
matters since Surgeon's diagnostics reference rows by index label),
and the distinction between `datetime.datetime` and `pandas.Timestamp`
(both were tagged identically, so a plain `datetime.datetime` silently
came back as a `Timestamp`).

The envelope (`_dataset_to_json`/`_dataset_from_json` in
`approval_repository.py`) explicitly stores `column_order`, per-column
`dtypes`, and `index` (name/dtype/values) alongside `data`, and tags
non-JSON-native scalars: `decimal`, `pandas_timestamp`,
`python_datetime`, `date`, `positive_infinity`, `negative_infinity`.
Reconstruction rebuilds each column as its own object-dtype Series
first and only then reapplies the recorded dtype -- building the whole
frame in one shot from a plain dict instead lets pandas re-infer types
across all columns at once, which is exactly what silently promoted
`datetime.datetime` back to `Timestamp` in an earlier version.

## Test database isolation

`tests/test_api.py` requires `TEST_DATABASE_URL` explicitly and will
**skip entirely** (not error, not fall back) if it isn't set. It never
reads `DATABASE_URL` as a fallback, specifically because
`setup_module`/`teardown_module` call `Base.metadata.create_all()` /
`drop_all()` -- pointed at the real database, that would destroy
production/dev data. Beyond not falling back, it also parses
`TEST_DATABASE_URL` with SQLAlchemy's `make_url()` and refuses to run
at all unless the database name is exactly `aegis_test` -- closing the
gap where the variable is set, just to the wrong value. It also sets
`os.environ["DATABASE_URL"] = TEST_DATABASE_URL` itself, before
importing `src.api.app` -- that import transitively imports
`src.db.session`, which builds a module-level engine from
`DATABASE_URL` and refuses to start without it, independently of
`TEST_DATABASE_URL`. Only `TEST_DATABASE_URL` needs to be exported;
the test file handles the rest.

`alembic/env.py` prefers a URL already configured programmatically
(`config.set_main_option("sqlalchemy.url", ...)`, used by
`test_alembic_upgrade_and_downgrade`) over the `DATABASE_URL`
environment variable, only falling back to the environment when
nothing was set on the `Config` object. This keeps `docker compose
run --rm api alembic upgrade head` (env-var driven) and the
programmatic test (config-object driven) both correct without either
depending on the other.

## Credential isolation in the Docker build

`.dockerignore` excludes `.env` (and `.git`, `__pycache__`, etc.) from
the build context. Without it, `COPY . .` in the Dockerfile would
happily copy a real `.env` -- containing real Postgres credentials --
into the image at `/app/.env`, independent of `.gitignore` (which only
governs Git, not Docker's build context). Verify after building:

```powershell
docker compose run --rm api sh -c "test ! -f /app/.env && echo '.env not present'"
```

## Financial datatypes

`Decimal`, `Timestamp`, `datetime`, `date`, and `+inf`/`-inf` are not
JSON-serializable on their own -- confirmed directly, all of them
raise `TypeError` (or, for infinity, produce the invalid JSON literal
`Infinity`) from `json.dumps`. The dataset envelope above tags each
with `{"__aegis_type__": ..., "value": ...}` so they come back as the
exact original Python type, never a `Decimal` silently turned into a
`float` (floating-point imprecision on money values would be exactly
the kind of silent corruption this project exists to prevent). These
types can't arrive via a real HTTP request today (JSON itself has no
such types), but are what a future SQL-sourced dataset would contain --
covered by a repository-level test, not an HTTP one, since HTTP can't
construct them in the first place.

## Restart-persistence verification

Manually: submit a migration that lands in `REQUIRES_HUMAN_APPROVAL`,
note the `ticket_id`, restart the API container
(`docker compose restart api`), then `GET /approvals/{ticket_id}` --
it must still return the ticket as PENDING.

In the test suite, `test_pending_ticket_survives_a_fresh_session`
fetches through a brand-new Session (not the one that created the
ticket) as the closest practical proxy for this without killing and
restarting the process mid-test-run.

## Failure and rollback behaviour

| Failure point | Result |
|---|---|
| Surgeon.execute() raises | 500; ticket rolled back to PENDING; no manifest |
| save_manifest() raises | 500; ticket rolled back to PENDING; no manifest |
| Concurrent approve on same ticket | One succeeds; the other gets 409, not a race |
| Approve/reject on non-PENDING ticket | 409 |
| Unknown ticket_id | 404 |
