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

## Test database isolation

`tests/test_api.py` requires `TEST_DATABASE_URL` explicitly and will
**skip entirely** (not error, not fall back) if it isn't set. It never
reads `DATABASE_URL` as a fallback, specifically because
`setup_module`/`teardown_module` call `Base.metadata.create_all()` /
`drop_all()` -- pointed at the real database, that would destroy
production/dev data.

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
