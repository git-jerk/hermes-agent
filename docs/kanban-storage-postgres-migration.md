# Hermes Kanban: SQLite → PostgreSQL storage migration

## Why this exists

The original `hermes_cli/kanban_db.py` opened SQLite directly from
every gateway process, dispatcher tick, worker subprocess, CLI
command, cron watchdog, and recovery script. Under the OpenClaw 24/7
load on Mini we saw repeated:

```
sqlite3.OperationalError: disk I/O error
sqlite3.DatabaseError: database disk image is malformed
```

on at least three boards (`trading-lab`, `research`, `qraav`) over a
single weekend, with `kanban.db.corrupt-*.bak`,
`kanban-recover-*.sql`, and recovery dumps growing on disk.

This is **not** SQLite contention — that would surface as
`database is locked`. The corruption signature plus the WAL/shm
sidecar file states points at APFS plus many independent process
lifecycles racing on the WAL files. Single-host multi-writer SQLite
is the wrong substrate for the workload; the durability fix is to
move the source-of-truth to a server-mode database with proper
multi-process semantics.

## Architecture

A new package, `hermes_cli/kanban_storage/`, sits between
`kanban_db.py` and the database. It exposes a single resolver entry
point and lets each board pick its backend independently:

```
+-------------------------+
| hermes_cli/kanban_db.py |
| (~80 public functions,  |
|  unchanged surface)     |
+-----------+-------------+
            |
            v
+-------------------------+         resolves backend per
| kanban_storage.selector |         board (board.json / env)
+-----+-----+-------+-----+
      |     |       |
      v     v       v
  sqlite  postgres  ...
  backend backend
```

Both backends return a connection object that quacks like
`sqlite3.Connection` (`.execute(sql, params)` → cursor with
`fetchone/fetchall/rowcount/lastrowid`, `row["col"]` access). The
Postgres path achieves this duck-typing via
`kanban_storage.connection.PgConnectionWrapper`, which transparently
translates the SQLite-dialect SQL strings:

- `?` placeholders → `%s`
- `INSERT OR IGNORE` → `INSERT ... ON CONFLICT DO NOTHING`
- `PRAGMA table_info(t)` → `SELECT … FROM information_schema.columns …`
- `PRAGMA synchronous=...` / `foreign_keys=...` → `SELECT 1` (no-op)
- `SELECT name FROM sqlite_master WHERE …` → `SELECT … FROM information_schema.tables …`
- bare `INSERT INTO <autoincrement_table>` → appends `RETURNING id` so
  `cursor.lastrowid` keeps working

All translations are deterministic, conservative, and tested in
`hermes_cli/kanban_storage/dialect.py`. Anything outside the covered
patterns reaches PG unchanged and either works (most queries are
identical between the dialects) or fails loudly with a PG syntax
error — the failure mode we want.

### Per-board PG schema isolation

Each board lives in its own PG schema named `kanban_<slug>` (hyphens
in slugs are normalized to underscores, e.g. `trading-lab` →
`kanban_trading_lab`). A worker pinned to one board sees only that
board's tables, exactly as before when each board had its own
`kanban.db` file. The `search_path` is set on every pool checkout so
bare table names in the existing SQL strings resolve inside the
board's schema without rewriting them.

### Why PostgreSQL was the right choice

Documented in the deep-dive at the top of the
[migration session transcript]. Headline reasons:

- **Scale fits**: largest board is 1.7 MB / 131 tasks / 1,771 events.
  OpenClaw runs ~10 concurrent workers max. The contrarian
  "Postgres-as-queue is bad" failure modes
  (MultiXact SLRU contention, vacuum bloat over 10s of GB) need
  thousands of concurrent workers — we're 100× under that.
- **Alternatives don't fix the actual problem**:
  - LiteFS / Litestream / rqlite / dqlite — designed for cross-host
    SQLite replication, not multi-process single-host. LiteFS adds a
    FUSE layer (another corruption surface).
  - River's SetMaxOpenConns(1) — single-writer proxy pattern. Would
    require building a proxy daemon and still leaves us on the
    corrupting substrate.
  - EventStoreDB / NATS JetStream — would require rewriting
    `tasks`/`task_runs`/CAS logic. Big project, marginal benefit at
    our scale.
  - CockroachDB / TigerBeetle / FoundationDB — overkill for
    single-host.
  - PGlite — JS/Wasm only.

## File layout

```
hermes_cli/kanban_storage/
├── __init__.py             # public exports: open_connection, init_schema,
│                           # write_txn, resolve_storage_config, clear_caches
├── base.py                 # Dialect dataclass, StorageConfig, KanbanBackend
│                           # protocol
├── dialect.py              # SQL translation pipeline (sqlite → postgres)
├── connection.py           # PgConnectionWrapper / PgCursorWrapper:
│                           # psycopg3 connection that quacks like sqlite3
├── sqlite_backend.py       # extracted current behaviour (WAL, PRAGMAs,
│                           # header validation, idempotent schema init)
├── postgres_backend.py     # psycopg3 + ConnectionPool, per-board PG schema
├── selector.py             # board.json + env → StorageConfig; chooses
│                           # the backend; dispatches write_txn
├── migrate_data.py         # one-shot SQLite → PG data migrator
├── schema_sqlite.sql       # base schema, matches the original SCHEMA_SQL
│                           # constant in kanban_db.py (additive-column
│                           # indexes still come from
│                           # _migrate_add_optional_columns)
└── schema_postgres.sql     # PG-dialect translation; includes all
                            # additive columns so fresh PG boards skip
                            # the post-v1 migration entirely
```

## Configuration

In `board.json`, the new shape is:

```json
{
  "kanban": {
    "storage": {
      "backend": "postgres",
      "postgres_dsn_env": "HERMES_KANBAN_POSTGRES_DSN",
      "postgres_schema": "kanban_trading_lab",
      "postgres_pool_min": 2,
      "postgres_pool_max": 10
    }
  }
}
```

Resolution order:

1. Explicit `backend=` argument (tests).
2. `HERMES_KANBAN_BACKEND` env var (`sqlite` | `postgres`).
3. `board.json` → `kanban.storage.backend`.
4. Default: `sqlite` (back-compat, zero-config).

DSN resolution (when backend = postgres):

1. Explicit `postgres_dsn=` argument.
2. Env var named in `board.json` → `kanban.storage.postgres_dsn_env`.
3. `HERMES_KANBAN_POSTGRES_DSN` (the conventional name).
4. Literal DSN in `board.json` → `kanban.storage.postgres_dsn` (least
   secure — credentials live on disk).

The migrator writes `postgres_dsn_env`, not the raw DSN, so the
credential lives in env rather than on disk.

## Cutover runbook

The migration is a coordinated cutover: the running gateway /
dispatcher / workers must stop writing to SQLite while data copies,
then restart pointing at PG.

### Pre-flight (already done by this session)

- [x] `docker-compose.kanban-pg.yml` brings up `hermes-kanban-pg` on
      `127.0.0.1:8434` (separate from the existing
      `openclaw-postgres` / `openclaw-timescaledb` instances so kanban
      durability is decoupled).
- [x] `psycopg[binary,pool]==3.2.10` pinned in `pyproject.toml`.
- [x] All 452 SQLite tests pass on the new abstraction (no
      regression).
- [x] 6 PG-backend tests pass end-to-end (round-trip, parent/child
      promotion, CAS race, event log append-only, OR IGNORE dedup,
      sqlite→PG migrator parity).
- [x] Dry-run `hermes kanban storage migrate --all` validates every
      live board (default / agnostek / cancer / qraav / research /
      trading-lab) — 353 tasks, ~6.5k events total, all under 100 ms
      per board.

### Cutover (operator-driven, ~2 minutes downtime)

```bash
# 0. Ensure the PG container is up
docker compose -f docker-compose.kanban-pg.yml up -d
docker exec hermes-kanban-pg pg_isready -U hermes -d hermes_kanban

# 1. Set the DSN in your shell (and in any service env that needs it)
export HERMES_KANBAN_POSTGRES_DSN='postgresql://hermes:hermes_kanban_local@127.0.0.1:8434/hermes_kanban'

# 2. Stop the gateway / dispatcher so no more SQLite writes happen
hermes gateway stop          # or: pkill -f 'hermes_cli.main gateway'

# 3. Stop any active kanban workers (look for "kanban-worker" skill)
pkill -f 'skills kanban-worker'

# 4. Wait until ps shows no kanban-touching processes
ps aux | grep -E 'hermes (gateway|kanban|chat)' | grep -v grep

# 5. Run the migration
hermes kanban storage migrate --all --to postgres

# 6. Inspect what happened — every board should report row counts +
#    "board.json updated" + "sqlite renamed"
hermes kanban storage status

# 7. Restart the gateway with the DSN in its env
HERMES_KANBAN_POSTGRES_DSN="$HERMES_KANBAN_POSTGRES_DSN" hermes gateway run --replace

# 8. Smoke-test from another shell
hermes kanban list
hermes kanban dispatch --once   # one tick to confirm the dispatcher works
```

### Rollback (per board, ~30 seconds)

If something goes wrong, each board can be rolled back independently:

```bash
# 1. Stop the gateway again
hermes gateway stop

# 2. Find the migrated-aside sqlite file and put it back
ls ~/.hermes/kanban/boards/<board>/kanban.db.migrated-*
mv ~/.hermes/kanban/boards/<board>/kanban.db.migrated-<ts> \
   ~/.hermes/kanban/boards/<board>/kanban.db

# 3. Edit board.json: remove the "storage" block so it falls back to sqlite
# (or set "backend": "sqlite")
vim ~/.hermes/kanban/boards/<board>/board.json

# 4. Restart
hermes gateway run --replace
```

The PG schema stays on disk after rollback — drop it manually with
`docker exec hermes-kanban-pg psql -U hermes -d hermes_kanban -c 'DROP
SCHEMA kanban_<slug> CASCADE'` when you're confident the rollback is
permanent.

### Post-cutover monitoring (24h)

Watch for:

- New `kanban.db-wal` or `kanban.db-shm` sidecars appearing —
  shouldn't happen on the new path, would indicate a stray writer
  still on SQLite.
- `psycopg.errors.OperationalError` in gateway logs — connection or
  pool problems. The pool is configured to validate connections on
  checkout, so server restarts should be recovered automatically.
- Dispatcher tick latency — should be the same or better (SQLite was
  already fast at this scale; PG removes the corruption-recovery
  overhead).

## Future work (deliberately out of scope for this round)

- LISTEN/NOTIFY for the event stream — replaces the current cursor
  polling of `task_events.id` with server-pushed notifications. Pure
  perf win, doesn't affect correctness.
- `payload` / `metadata` as `JSONB` rather than `TEXT` — would unlock
  server-side JSON queries (e.g. "find tasks whose summary contains
  `pr_url`"). Out of scope because nothing in `kanban_db.py` currently
  needs that — they treat the columns as opaque strings.
- Per-tenant row-level security — PG has the primitives, the v1
  schema doesn't use them. Worth revisiting once we have a real
  multi-tenant install.
- Auto-migration on first connect, gated by a config flag — currently
  the migrator is a deliberate one-shot CLI step. Auto-migration is
  too foot-gun for the durability problem this fix is solving.
