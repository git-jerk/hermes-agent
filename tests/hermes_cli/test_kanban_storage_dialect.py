"""Unit tests for ``hermes_cli.kanban_storage.dialect``.

These hammer the corner cases the live PG cutover surfaced — every
known dialect difference between SQLite and PostgreSQL that the
translator has to bridge for the existing 186 inlined SQL strings
in ``kanban_db.py`` to keep working unchanged.

Each test name calls out the failure mode it would have caught:

* `test_is_placeholder_*` — the ``claim_lock IS ?`` → ``IS NOT DISTINCT
  FROM ?`` rewrite. Without it, the PG dispatcher tick fails with
  ``psycopg.errors.SyntaxError: syntax error at or near "$3"``.
* `test_or_ignore_*` — the ``INSERT OR IGNORE`` → ``ON CONFLICT DO
  NOTHING`` rewrite. Without it, link_tasks raises SyntaxError on PG.
* `test_pragma_*` — PRAGMA introspection forwards. Without translation,
  ``_migrate_add_optional_columns`` crashes on PG with "PRAGMA is not
  a supported statement".
* `test_sqlite_master_*` — table-existence probe. Without translation,
  the additive migration's table-existence check crashes on PG.
* `test_placeholder_in_string_literal` — confirms ``?`` inside ``'…'``
  is NOT rewritten. Otherwise prose containing ``?`` could be corrupted.
* `test_returning_id_*` — the autoincrement-table INSERT…RETURNING
  injection so ``cursor.lastrowid`` works on PG.

The tests run without a PG server — they exercise the pure-text
translator only.
"""

from __future__ import annotations

import pytest

from hermes_cli.kanban_storage.dialect import (
    AUTOINCREMENT_TABLES,
    append_returning_id,
    needs_returning_id,
    translate_is_placeholder,
    translate_or_ignore,
    translate_or_replace,
    translate_placeholders,
    translate_pragma,
    translate_sql_for_postgres,
    translate_sqlite_master,
)


# --- placeholder translation ----------------------------------------------

class TestPlaceholders:
    def test_simple(self):
        assert translate_placeholders("SELECT * FROM x WHERE id = ?") == \
            "SELECT * FROM x WHERE id = %s"

    def test_multiple(self):
        assert translate_placeholders("UPDATE x SET y = ?, z = ? WHERE id = ?") == \
            "UPDATE x SET y = %s, z = %s WHERE id = %s"

    def test_question_mark_in_single_quoted_string_preserved(self):
        # Otherwise a task body containing "?" would get mangled.
        assert translate_placeholders("INSERT INTO x VALUES ('why?', ?)") == \
            "INSERT INTO x VALUES ('why?', %s)"

    def test_escaped_quote_doesnt_break_quote_tracking(self):
        # ''  is SQL's escaped single quote inside a string literal.
        # The quote tracker must handle the toggle-off-then-on
        # pattern without false-positive "outside string" detection.
        assert translate_placeholders("SELECT 'it''s ok?' WHERE x = ?") == \
            "SELECT 'it''s ok?' WHERE x = %s"

    def test_no_change_when_no_placeholders(self):
        s = "SELECT COUNT(*) FROM tasks"
        assert translate_placeholders(s) == s


# --- IS ? -> IS NOT DISTINCT FROM ? -----------------------------------------

class TestIsPlaceholder:
    """Live cutover surfaced this as ``syntax error at or near "$3"`` on
    the first dispatcher tick that hit release_stale_claims after the
    pool-release fix landed."""

    def test_is_placeholder_becomes_is_not_distinct_from(self):
        assert translate_is_placeholder(
            "UPDATE x WHERE claim_lock IS ?"
        ) == "UPDATE x WHERE claim_lock IS NOT DISTINCT FROM ?"

    def test_is_not_placeholder_becomes_is_distinct_from(self):
        assert translate_is_placeholder(
            "UPDATE x WHERE claim_lock IS NOT ?"
        ) == "UPDATE x WHERE claim_lock IS DISTINCT FROM ?"

    def test_literal_null_left_untouched(self):
        # IS NULL is valid PG syntax; only the placeholder form is broken.
        assert translate_is_placeholder(
            "UPDATE x WHERE claim_lock IS NULL"
        ) == "UPDATE x WHERE claim_lock IS NULL"
        assert translate_is_placeholder(
            "UPDATE x WHERE claim_expires IS NOT NULL"
        ) == "UPDATE x WHERE claim_expires IS NOT NULL"

    def test_case_insensitive(self):
        assert "IS NOT DISTINCT FROM" in translate_is_placeholder(
            "UPDATE x WHERE claim_lock is ?"
        ).upper()
        assert "IS DISTINCT FROM" in translate_is_placeholder(
            "UPDATE x WHERE claim_lock Is Not ?"
        ).upper()

    def test_multiple_is_placeholders_in_one_query(self):
        sql = (
            "UPDATE x SET claim_lock = ?, started_at = ? "
            "WHERE id = ? AND claim_lock IS ? AND worker_pid IS NOT ?"
        )
        out = translate_is_placeholder(sql)
        assert "claim_lock IS NOT DISTINCT FROM ?" in out
        assert "worker_pid IS DISTINCT FROM ?" in out
        # SET assignments preserved verbatim (the `?` after `=` isn't
        # an IS-shape; the placeholder regex must not greedy-match it).
        assert "SET claim_lock = ?" in out


# --- OR IGNORE -> ON CONFLICT DO NOTHING -------------------------------------

class TestOrIgnore:
    def test_basic(self):
        assert translate_or_ignore(
            "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)"
        ) == (
            "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?) "
            "ON CONFLICT DO NOTHING"
        )

    def test_trailing_semicolon_position(self):
        # ON CONFLICT must be spliced BEFORE the semicolon.
        out = translate_or_ignore(
            "INSERT OR IGNORE INTO x VALUES (?);"
        )
        assert out.startswith("INSERT INTO x VALUES (?)")
        assert "ON CONFLICT DO NOTHING" in out
        assert out.rstrip(";").endswith("ON CONFLICT DO NOTHING") or out.endswith("DO NOTHING;")

    def test_no_change_when_not_or_ignore(self):
        s = "INSERT INTO x VALUES (?)"
        assert translate_or_ignore(s) == s

    def test_case_insensitive(self):
        out = translate_or_ignore(
            "insert or ignore into task_links values (?, ?)"
        )
        assert "ON CONFLICT DO NOTHING" in out


# --- OR REPLACE - intentionally not implemented -----------------------------

class TestOrReplace:
    def test_or_replace_raises_when_present(self):
        # kanban_db.py doesn't use OR REPLACE today. If a future query
        # does, the translator should refuse loudly (NotImplementedError)
        # rather than silently miscompile.
        with pytest.raises(NotImplementedError):
            translate_or_replace("INSERT OR REPLACE INTO x VALUES (?)")

    def test_no_op_when_not_or_replace(self):
        s = "INSERT INTO x VALUES (?)"
        assert translate_or_replace(s) == s


# --- PRAGMA translation -----------------------------------------------------

class TestPragma:
    def test_table_info_rewritten(self):
        out = translate_pragma(
            "PRAGMA table_info(tasks)", search_path_schema="kanban_default"
        )
        assert out is not None
        assert "information_schema.columns" in out
        assert "table_schema = 'kanban_default'" in out
        assert "table_name = 'tasks'" in out
        assert "name" in out  # alias

    def test_table_info_uses_current_schema_when_no_search_path(self):
        out = translate_pragma("PRAGMA table_info(tasks)")
        assert out is not None
        assert "current_schema()" in out

    def test_synchronous_pragma_becomes_noop(self):
        # _kb.connect calls these for sqlite tuning; on PG they're
        # meaningless but should not error.
        assert translate_pragma("PRAGMA synchronous=NORMAL") == "SELECT 1"
        assert translate_pragma("PRAGMA foreign_keys=ON") == "SELECT 1"
        assert translate_pragma("PRAGMA journal_mode=WAL") == "SELECT 1"

    def test_journal_mode_query_form_returns_wal(self):
        # The dashboard's WAL-status check expects journal_mode='wal'.
        out = translate_pragma("PRAGMA journal_mode")
        assert out is not None and "'wal'" in out

    def test_non_pragma_returns_none(self):
        # The translator only returns a rewrite when it matches; other
        # statements pass through unchanged (handled by the caller).
        assert translate_pragma("SELECT * FROM tasks") is None
        assert translate_pragma("UPDATE tasks SET ...") is None


# --- sqlite_master probe ----------------------------------------------------

class TestSqliteMaster:
    def test_table_existence_probe_rewritten(self):
        out = translate_sqlite_master(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='task_runs'",
            search_path_schema="kanban_default",
        )
        assert out is not None
        assert "information_schema.tables" in out
        assert "table_schema = 'kanban_default'" in out
        assert "table_name = 'task_runs'" in out

    def test_non_master_returns_none(self):
        assert translate_sqlite_master("SELECT * FROM tasks") is None

    def test_other_master_shapes_unchanged(self):
        # The translator only handles the exact "name FROM sqlite_master
        # WHERE type='table' AND name=...'" shape. Other sqlite_master
        # queries (e.g. SELECT sql FROM sqlite_master) aren't used by
        # kanban_db.py and return None; the caller will pass them through
        # to PG and get a loud failure rather than a silent miscompile.
        assert translate_sqlite_master(
            "SELECT sql FROM sqlite_master WHERE name='tasks'"
        ) is None


# --- INSERT ... RETURNING for autoincrement tables -------------------------

class TestReturningInjection:
    def test_autoincrement_tables_are_known(self):
        assert AUTOINCREMENT_TABLES == frozenset(
            {"task_runs", "task_comments", "task_events", "task_attachments"}
        )

    def test_bare_insert_into_autoincrement_needs_returning(self):
        assert needs_returning_id(
            "INSERT INTO task_runs (task_id, status, started_at) VALUES (?, ?, ?)"
        )
        assert needs_returning_id(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)"
        )
        assert needs_returning_id(
            "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, ?, ?, ?)"
        )

    def test_insert_with_explicit_returning_is_skipped(self):
        # If caller already wrote RETURNING, don't re-inject.
        assert not needs_returning_id(
            "INSERT INTO task_runs (task_id) VALUES (?) RETURNING id"
        )

    def test_non_autoincrement_table_insert_skipped(self):
        # tasks.id is TEXT (no sequence); injecting RETURNING id would
        # work but be redundant. The translator only injects on the
        # three tables that need it.
        assert not needs_returning_id(
            "INSERT INTO tasks (id, title) VALUES (?, ?)"
        )
        assert not needs_returning_id(
            "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)"
        )

    def test_append_returning_id_basic(self):
        assert append_returning_id(
            "INSERT INTO task_runs (task_id) VALUES (?)"
        ) == "INSERT INTO task_runs (task_id) VALUES (?) RETURNING id"

    def test_append_returning_id_before_semicolon(self):
        assert append_returning_id(
            "INSERT INTO task_runs (task_id) VALUES (?);"
        ).rstrip() in {
            "INSERT INTO task_runs (task_id) VALUES (?) RETURNING id;",
            "INSERT INTO task_runs (task_id) VALUES (?) RETURNING id ;",
        }


# --- Composite pipeline ----------------------------------------------------

class TestPipeline:
    def test_release_stale_claims_query_translated_correctly(self):
        # This is the exact query that failed on the live cutover
        # before the IS-placeholder fix.
        sql = (
            "UPDATE tasks SET claim_expires = ? "
            "WHERE id = ? AND status = 'running' "
            "AND claim_lock IS ? "
            "AND claim_expires IS NOT NULL AND claim_expires < ?"
        )
        out = translate_sql_for_postgres(sql, search_path_schema="kanban_default")
        # All four placeholders converted.
        assert "?" not in out
        assert out.count("%s") == 4
        # The two pre-existing literal-NULL checks survive intact.
        assert "IS NOT NULL" in out
        # The placeholder-NULL check is now SQL-standard.
        assert "IS NOT DISTINCT FROM %s" in out

    def test_link_tasks_query_translated_correctly(self):
        sql = "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)"
        out = translate_sql_for_postgres(sql)
        assert "INSERT INTO task_links" in out
        assert "ON CONFLICT DO NOTHING" in out
        assert "%s" in out and "?" not in out

    def test_pragma_table_info_in_pipeline(self):
        out = translate_sql_for_postgres(
            "PRAGMA table_info(tasks)", search_path_schema="kanban_trading_lab"
        )
        # PRAGMA path short-circuits; downstream substitutions don't
        # need to run (placeholder substitution must not corrupt the
        # rewritten SQL).
        assert "information_schema.columns" in out
        assert "kanban_trading_lab" in out
        # No stray placeholders.
        assert "?" not in out

    def test_sqlite_master_in_pipeline(self):
        out = translate_sql_for_postgres(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='task_runs'",
            search_path_schema="kanban_research",
        )
        assert "information_schema.tables" in out
        assert "kanban_research" in out

    def test_unaffected_query_passes_through_with_only_placeholder_rewrite(self):
        sql = "SELECT * FROM tasks WHERE assignee = ? ORDER BY priority DESC"
        out = translate_sql_for_postgres(sql)
        assert out == "SELECT * FROM tasks WHERE assignee = %s ORDER BY priority DESC"

    def test_combined_or_ignore_and_is_placeholder(self):
        # Hypothetical pathological case: a single SQL that exercises
        # both translations. Confirms order-of-operations is correct.
        sql = (
            "INSERT OR IGNORE INTO kanban_notify_subs "
            "(task_id, platform, chat_id) VALUES (?, ?, ?) "
            "WHERE thread_id IS ?"
        )
        out = translate_sql_for_postgres(sql)
        assert "INSERT INTO kanban_notify_subs" in out
        assert "ON CONFLICT DO NOTHING" in out
        assert "IS NOT DISTINCT FROM %s" in out
        assert "?" not in out
