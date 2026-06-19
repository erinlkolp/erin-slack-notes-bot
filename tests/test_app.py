"""Tests for the Slack notes bot modules.

Each test class targets a specific module (database, tags, blocks, middleware,
health) so imports are clean and patches point to the right namespace.
"""

import inspect
import json
import time
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import the modules under test.  None of them perform heavy side effects at
# import time, so no patching is needed just to import them.
# ---------------------------------------------------------------------------

from app import config
from app import database
from app import tags
from app import blocks
from app import middleware
from app import health
from app.handlers import register_handlers

# Set the allowed user that middleware reads at import time.
middleware.allowed_user_id = "U_ALLOWED"


# ── parse_tags ───────────────────────────────────────────────────────────────


class TestParseTags:
    def test_single_tag(self):
        assert tags.parse_tags("meeting #standup notes") == ["standup"]

    def test_multiple_tags(self):
        assert tags.parse_tags("#bug fix for #backend and #api") == [
            "bug",
            "backend",
            "api",
        ]

    def test_duplicate_tags_deduplicated(self):
        assert tags.parse_tags("#Bug report #bug") == ["bug"]

    def test_no_tags(self):
        assert tags.parse_tags("no tags here") == []

    def test_tags_lowercased(self):
        assert tags.parse_tags("#Meeting #IMPORTANT") == ["meeting", "important"]

    def test_tag_with_underscores_and_digits(self):
        assert tags.parse_tags("#q4_2025 planning") == ["q4_2025"]


# ── build_notes_blocks ───────────────────────────────────────────────────────


class TestBuildNotesBlocks:
    def _make_notes(self, count):
        now = datetime(2025, 6, 15, 10, 30)
        return [(i, f"Note text {i}", now, "general") for i in range(1, count + 1)]

    def test_single_page_no_nav_buttons(self):
        result = blocks.build_notes_blocks(self._make_notes(3), page=1, per_page=5, total_count=3)
        assert [b for b in result if b.get("type") == "actions"] == []

    def test_first_page_has_next_only(self):
        result = blocks.build_notes_blocks(self._make_notes(5), page=1, per_page=5, total_count=12)
        action_blocks = [b for b in result if b.get("type") == "actions"]
        assert len(action_blocks) == 1
        btns = action_blocks[0]["elements"]
        assert len(btns) == 1
        assert btns[0]["action_id"] == "notes_next_page"

    def test_middle_page_has_both_buttons(self):
        result = blocks.build_notes_blocks(self._make_notes(5), page=2, per_page=5, total_count=15)
        action_blocks = [b for b in result if b.get("type") == "actions"]
        ids = [b["action_id"] for b in action_blocks[0]["elements"]]
        assert "notes_prev_page" in ids
        assert "notes_next_page" in ids

    def test_last_page_has_prev_only(self):
        result = blocks.build_notes_blocks(self._make_notes(2), page=3, per_page=5, total_count=12)
        action_blocks = [b for b in result if b.get("type") == "actions"]
        btns = action_blocks[0]["elements"]
        assert len(btns) == 1
        assert btns[0]["action_id"] == "notes_prev_page"

    def test_header_and_context_present(self):
        result = blocks.build_notes_blocks(self._make_notes(1), page=1, per_page=5, total_count=1)
        assert result[0]["type"] == "header"
        assert result[1]["type"] == "context"
        assert "1 notes total" in result[1]["elements"][0]["text"]

    def test_long_note_not_truncated(self):
        now = datetime(2025, 6, 15, 10, 30)
        long_text = "x" * 300
        result = blocks.build_notes_blocks([(1, long_text, now, None)], page=1, per_page=5, total_count=1)
        section = [b for b in result if b.get("type") == "section"][0]
        display = section["text"]["text"]
        assert "x" * 300 in display
        assert "..." not in display

    def test_page_value_in_button_payload(self):
        result = blocks.build_notes_blocks(self._make_notes(5), page=1, per_page=5, total_count=10)
        action_blocks = [b for b in result if b.get("type") == "actions"]
        payload = json.loads(action_blocks[0]["elements"][0]["value"])
        assert payload == {"page": 2, "per_page": 5, "sort": "newest"}

    def test_note_text_mentions_are_escaped(self):
        """Slack mention/broadcast syntax in note text must be escaped in mrkdwn output."""
        now = datetime(2025, 6, 15, 10, 30)
        note_text = "ping <!here> and <@U12345> about <#C99999|general> & updates"
        result = blocks.build_notes_blocks([(1, note_text, now, None)], page=1, per_page=5, total_count=1)
        section = [b for b in result if b.get("type") == "section"][0]
        display = section["text"]["text"]
        assert "<!here>" not in display
        assert "<@U12345>" not in display
        assert "<#C99999|general>" not in display
        assert "&lt;!here&gt;" in display
        assert "&lt;@U12345&gt;" in display
        assert "&amp;" in display

    def test_note_header_uses_hash_id(self):
        now = datetime(2025, 6, 15, 10, 30)
        result = blocks.build_notes_blocks([(42, "some text", now, None)], page=1, per_page=5, total_count=1)
        section = [b for b in result if b.get("type") == "section"][0]
        assert "*#42*" in section["text"]["text"]

    def test_note_header_includes_channel_name(self):
        now = datetime(2025, 6, 15, 10, 30)
        result = blocks.build_notes_blocks([(1, "text", now, "general")], page=1, per_page=5, total_count=1)
        section = [b for b in result if b.get("type") == "section"][0]
        assert "#general" in section["text"]["text"]

    def test_note_header_omits_channel_when_none(self):
        now = datetime(2025, 6, 15, 10, 30)
        result = blocks.build_notes_blocks([(1, "text", now, None)], page=1, per_page=5, total_count=1)
        section = [b for b in result if b.get("type") == "section"][0]
        assert " — #" not in section["text"]["text"].split("\n")[0]


# ── escape_mrkdwn ─────────────────────────────────────────────────────────────


class TestEscapeMrkdwn:
    def test_user_mention(self):
        assert blocks.escape_mrkdwn("<@U12345>") == "&lt;@U12345&gt;"

    def test_broadcast_here(self):
        assert blocks.escape_mrkdwn("<!here>") == "&lt;!here&gt;"

    def test_broadcast_channel(self):
        assert blocks.escape_mrkdwn("<!channel>") == "&lt;!channel&gt;"

    def test_broadcast_everyone(self):
        assert blocks.escape_mrkdwn("<!everyone>") == "&lt;!everyone&gt;"

    def test_ampersand_escaped_first(self):
        # & must be replaced before < and > to avoid double-escaping
        assert blocks.escape_mrkdwn("a & b") == "a &amp; b"
        assert blocks.escape_mrkdwn("&lt;") == "&amp;lt;"

    def test_plain_text_unchanged(self):
        assert blocks.escape_mrkdwn("hello world #tag") == "hello world #tag"

    def test_empty_string(self):
        assert blocks.escape_mrkdwn("") == ""


# ── check_rate_limit ─────────────────────────────────────────────────────────


class TestCheckRateLimit:
    def setup_method(self):
        middleware._last_command_time.clear()

    def test_first_call_allowed(self):
        assert middleware.check_rate_limit("U1", "cmd") is False

    def test_rapid_second_call_blocked(self):
        middleware.check_rate_limit("U1", "cmd")
        assert middleware.check_rate_limit("U1", "cmd") is True

    def test_different_users_independent(self):
        middleware.check_rate_limit("U1", "cmd")
        assert middleware.check_rate_limit("U2", "cmd") is False

    def test_different_commands_independent(self):
        middleware.check_rate_limit("U1", "cmd_a")
        assert middleware.check_rate_limit("U1", "cmd_b") is False

    def test_allowed_after_cooldown(self):
        middleware.check_rate_limit("U1", "cmd")
        key = "U1:cmd"
        middleware._last_command_time[key] -= config.RATE_LIMIT_SECONDS + 1
        assert middleware.check_rate_limit("U1", "cmd") is False


# ── require_allowed_user decorator ───────────────────────────────────────────


class TestRequireAllowedUser:
    def setup_method(self):
        middleware.allowed_user_id = "U_ALLOWED"
        middleware._last_command_time.clear()

    def test_authorized_user_proceeds(self):
        inner = MagicMock()
        decorated = middleware.require_allowed_user()(inner)
        decorated(ack=MagicMock(), respond=MagicMock(), command={"user_id": "U_ALLOWED"})
        inner.assert_called_once()

    def test_unauthorized_user_blocked(self):
        inner = MagicMock()
        respond = MagicMock()
        decorated = middleware.require_allowed_user()(inner)
        decorated(ack=MagicMock(), respond=respond, command={"user_id": "U_OTHER"})
        inner.assert_not_called()
        respond.assert_called_once()
        assert "restricted" in respond.call_args[0][0]

    def test_rate_limited_user_blocked(self):
        inner = MagicMock()
        respond = MagicMock()
        decorated = middleware.require_allowed_user(command_name="test_cmd")(inner)

        decorated(ack=MagicMock(), respond=MagicMock(), command={"user_id": "U_ALLOWED"})
        assert inner.call_count == 1

        decorated(ack=MagicMock(), respond=respond, command={"user_id": "U_ALLOWED"})
        assert inner.call_count == 1
        respond.assert_called_once()
        assert "wait" in respond.call_args[0][0].lower()

    def test_ack_always_called(self):
        ack = MagicMock()
        middleware.require_allowed_user()(MagicMock())(
            ack=ack, respond=MagicMock(), command={"user_id": "U_OTHER"}
        )
        ack.assert_called_once()

    def test_body_fallback_for_actions(self):
        inner = MagicMock()
        decorated = middleware.require_allowed_user()(inner)
        decorated(ack=MagicMock(), respond=MagicMock(), body={"user": {"id": "U_ALLOWED"}})
        inner.assert_called_once()

    def test_view_authorized_user_proceeds_without_pre_ack(self):
        """For view handlers, the decorator must NOT pre-ack so the handler controls ack."""
        inner = MagicMock()
        ack = MagicMock()
        decorated = middleware.require_allowed_user(is_view=True)(inner)
        decorated(ack=ack, body={"user": {"id": "U_ALLOWED"}})
        inner.assert_called_once()
        ack.assert_not_called()

    def test_view_unauthorized_user_is_acked_and_blocked(self):
        """For view handlers, unauthorized submission must be ack()d (modal dismissed) and blocked."""
        inner = MagicMock()
        ack = MagicMock()
        decorated = middleware.require_allowed_user(is_view=True)(inner)
        decorated(ack=ack, body={"user": {"id": "U_OTHER"}})
        inner.assert_not_called()
        ack.assert_called_once()


# ── init_db_pool ─────────────────────────────────────────────────────────────


class TestInitDbPool:
    def setup_method(self):
        self._orig_pool = database._db_pool

    def teardown_method(self):
        database._db_pool = self._orig_pool

    @patch("app.database.time.sleep")
    @patch("app.database.MySQLConnectionPool")
    def test_creates_pool_on_first_try(self, mock_pool_cls, mock_sleep):
        mock_pool = MagicMock()
        mock_pool_cls.return_value = mock_pool

        assert database.init_db_pool() is True
        assert database._db_pool is mock_pool
        mock_sleep.assert_not_called()

    @patch("app.database.time.sleep")
    @patch("app.database.MySQLConnectionPool")
    def test_retries_then_succeeds(self, mock_pool_cls, mock_sleep):
        from mysql.connector import Error

        mock_pool = MagicMock()
        mock_pool_cls.side_effect = [Error("fail"), mock_pool]

        assert database.init_db_pool() is True
        assert database._db_pool is mock_pool
        mock_sleep.assert_called_once_with(1)

    @patch("app.database.time.sleep")
    @patch("app.database.MySQLConnectionPool")
    def test_returns_false_after_all_retries(self, mock_pool_cls, mock_sleep):
        from mysql.connector import Error

        mock_pool_cls.side_effect = Error("down")
        assert database.init_db_pool() is False


# ── get_db_connection ─────────────────────────────────────────────────────────


class TestGetDbConnectionRetry:
    def setup_method(self):
        self._orig_pool = database._db_pool

    def teardown_method(self):
        database._db_pool = self._orig_pool

    @patch("app.database.time.sleep")
    def test_succeeds_on_first_try(self, mock_sleep):
        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_pool.get_connection.return_value = mock_conn
        database._db_pool = mock_pool

        assert database.get_db_connection() is mock_conn
        mock_sleep.assert_not_called()

    @patch("app.database.time.sleep")
    def test_retries_then_succeeds(self, mock_sleep):
        from mysql.connector import Error

        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_pool.get_connection.side_effect = [Error("fail"), mock_conn]
        database._db_pool = mock_pool

        assert database.get_db_connection() is mock_conn
        mock_sleep.assert_called_once_with(1)

    @patch("app.database.time.sleep")
    def test_all_retries_fail_returns_none(self, mock_sleep):
        from mysql.connector import Error

        mock_pool = MagicMock()
        mock_pool.get_connection.side_effect = Error("persistent failure")
        database._db_pool = mock_pool

        assert database.get_db_connection() is None
        assert mock_sleep.call_count == config.DB_CONNECT_MAX_RETRIES - 1

    @patch("app.database.time.sleep")
    def test_exponential_backoff_delays(self, mock_sleep):
        from mysql.connector import Error

        mock_pool = MagicMock()
        mock_pool.get_connection.side_effect = Error("down")
        database._db_pool = mock_pool

        database.get_db_connection()
        delays = [c[0][0] for c in mock_sleep.call_args_list]
        assert delays == [1, 2]

    @patch("app.database.time.sleep")
    def test_returns_none_when_pool_not_initialized(self, mock_sleep):
        database._db_pool = None
        assert database.get_db_connection() is None
        mock_sleep.assert_not_called()


# ── save_note ─────────────────────────────────────────────────────────────────


class TestSaveNote:
    @patch("app.database.get_db_connection")
    def test_returns_note_id_on_success(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        mock_cursor = MagicMock()
        mock_cursor.lastrowid = 42
        mock_conn.cursor.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn

        assert database.save_note("U1", "alice", "hello world") == 42
        mock_cursor.execute.assert_called_once()
        mock_conn.commit.assert_called_once()

    @patch("app.database.get_db_connection")
    def test_returns_false_when_no_connection(self, mock_get_conn):
        mock_get_conn.return_value = None
        assert database.save_note("U1", "alice", "hello") is False

    @patch("app.database.get_db_connection")
    def test_returns_false_on_db_error(self, mock_get_conn):
        from mysql.connector import Error

        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = Error("insert failed")
        mock_conn.cursor.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn

        assert database.save_note("U1", "alice", "hello") is False


# ── get_notes_page ────────────────────────────────────────────────────────────


class TestGetNotesPage:
    @patch("app.database.get_db_connection")
    def test_returns_notes_and_count(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        mock_cursor = MagicMock()
        now = datetime.now()
        mock_cursor.fetchone.return_value = (3,)
        mock_cursor.fetchall.return_value = [
            (1, "note a", now, "general"),
            (2, "note b", now, None),
        ]
        mock_conn.cursor.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn

        notes, total = database.get_notes_page("U1", page=1, per_page=5)
        assert total == 3
        assert len(notes) == 2

    @patch("app.database.get_db_connection")
    def test_returns_none_on_no_connection(self, mock_get_conn):
        mock_get_conn.return_value = None
        notes, total = database.get_notes_page("U1", page=1, per_page=5)
        assert notes is None
        assert total == 0


# ── save_tags ─────────────────────────────────────────────────────────────────


class TestSaveTags:
    @patch("app.tags.get_db_connection")
    def test_inserts_tags(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn

        tags.save_tags(42, ["bug", "backend"])
        mock_cursor.executemany.assert_called_once()
        assert mock_cursor.executemany.call_args[0][1] == [(42, "bug"), (42, "backend")]
        mock_conn.commit.assert_called_once()

    @patch("app.tags.get_db_connection")
    def test_noop_for_empty_tags(self, mock_get_conn):
        tags.save_tags(42, [])
        mock_get_conn.assert_not_called()


# ── get_note_by_id ────────────────────────────────────────────────────────────


class TestGetNoteById:
    @patch("app.database.get_db_connection")
    def test_returns_note_when_found(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        mock_cursor = MagicMock()
        now = datetime.now()
        mock_cursor.fetchone.return_value = (1, "hello", now, "general")
        mock_conn.cursor.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn

        assert database.get_note_by_id(1, "U1") == (1, "hello", now, "general")

    @patch("app.database.get_db_connection")
    def test_returns_none_when_not_found(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        mock_conn.cursor.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn

        assert database.get_note_by_id(999, "U1") is None

    @patch("app.database.get_db_connection")
    def test_returns_none_on_no_connection(self, mock_get_conn):
        mock_get_conn.return_value = None
        assert database.get_note_by_id(1, "U1") is None


# ── update_note ───────────────────────────────────────────────────────────────


class TestUpdateNote:
    @patch("app.database.get_db_connection")
    def test_returns_true_on_success(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1
        mock_conn.cursor.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn

        assert database.update_note(1, "U1", "updated text") is True
        mock_conn.commit.assert_called_once()

    @patch("app.database.get_db_connection")
    def test_returns_false_when_not_found(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 0
        mock_conn.cursor.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn

        assert database.update_note(999, "U1", "updated text") is False

    @patch("app.database.get_db_connection")
    def test_returns_false_on_no_connection(self, mock_get_conn):
        mock_get_conn.return_value = None
        assert database.update_note(1, "U1", "updated") is False


# ── delete_note ───────────────────────────────────────────────────────────────


class TestDeleteNote:
    @patch("app.database.get_db_connection")
    def test_returns_true_on_success(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1
        mock_conn.cursor.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn

        assert database.delete_note(1, "U1") is True
        mock_conn.commit.assert_called_once()

    @patch("app.database.get_db_connection")
    def test_returns_false_when_not_found(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 0
        mock_conn.cursor.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn

        assert database.delete_note(999, "U1") is False

    @patch("app.database.get_db_connection")
    def test_returns_false_on_no_connection(self, mock_get_conn):
        mock_get_conn.return_value = None
        assert database.delete_note(1, "U1") is False


# ── delete_tags_for_note ──────────────────────────────────────────────────────


class TestDeleteTagsForNote:
    @patch("app.tags.get_db_connection")
    def test_returns_true_on_success(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn

        assert tags.delete_tags_for_note(42) is True
        mock_cursor.execute.assert_called_once()
        mock_conn.commit.assert_called_once()

    @patch("app.tags.get_db_connection")
    def test_returns_false_on_no_connection(self, mock_get_conn):
        mock_get_conn.return_value = None
        assert tags.delete_tags_for_note(42) is False


# ── get_notes_by_tag ──────────────────────────────────────────────────────────


class TestGetNotesByTag:
    def _make_mock_conn(self, fetchone_return, fetchall_return):
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = fetchone_return
        mock_cursor.fetchall.return_value = fetchall_return
        mock_conn.cursor.return_value = mock_cursor
        return mock_conn, mock_cursor

    @patch("app.tags.get_db_connection")
    def test_returns_notes_for_single_tag(self, mock_get_conn):
        now = datetime.now()
        mock_conn, mock_cursor = self._make_mock_conn(
            fetchone_return=(2,),
            fetchall_return=[(1, "note a", now, "general"), (2, "note b", now, None)],
        )
        mock_get_conn.return_value = mock_conn

        notes, total = tags.get_notes_by_tag("U1", ["bug"], page=1, per_page=5)
        assert total == 2
        assert len(notes) == 2

    @patch("app.tags.get_db_connection")
    def test_returns_notes_for_multiple_tags_and_semantics(self, mock_get_conn):
        now = datetime.now()
        mock_conn, mock_cursor = self._make_mock_conn(
            fetchone_return=(1,),
            fetchall_return=[(3, "shared note", now, "dev")],
        )
        mock_get_conn.return_value = mock_conn

        notes, total = tags.get_notes_by_tag("U1", ["bug", "backend"], page=1, per_page=5)
        assert total == 1
        assert len(notes) == 1
        # Both tags must appear in the IN clause parameters
        count_call_args = mock_cursor.execute.call_args_list[0][0]
        assert "bug" in count_call_args[1]
        assert "backend" in count_call_args[1]
        # HAVING count must equal the number of tags (AND semantics)
        assert 2 in count_call_args[1]

    @patch("app.tags.get_db_connection")
    def test_tags_are_lowercased(self, mock_get_conn):
        now = datetime.now()
        mock_conn, mock_cursor = self._make_mock_conn(
            fetchone_return=(1,),
            fetchall_return=[(1, "note", now, None)],
        )
        mock_get_conn.return_value = mock_conn

        tags.get_notes_by_tag("U1", ["Bug", "BACKEND"], page=1, per_page=5)
        count_call_args = mock_cursor.execute.call_args_list[0][0]
        assert "bug" in count_call_args[1]
        assert "backend" in count_call_args[1]
        assert "Bug" not in count_call_args[1]

    @patch("app.tags.get_db_connection")
    def test_returns_empty_list_when_no_match(self, mock_get_conn):
        mock_conn, _ = self._make_mock_conn(
            fetchone_return=(0,),
            fetchall_return=[],
        )
        mock_get_conn.return_value = mock_conn

        notes, total = tags.get_notes_by_tag("U1", ["nonexistent"], page=1, per_page=5)
        assert total == 0
        assert notes == []

    @patch("app.tags.get_db_connection")
    def test_returns_none_on_no_connection(self, mock_get_conn):
        mock_get_conn.return_value = None
        notes, total = tags.get_notes_by_tag("U1", ["bug"], page=1, per_page=5)
        assert notes is None
        assert total == 0

    @patch("app.tags.get_db_connection")
    def test_duplicate_tags_deduplicated_for_and_semantics(self, mock_get_conn):
        now = datetime.now()
        mock_conn, mock_cursor = self._make_mock_conn(
            fetchone_return=(1,),
            fetchall_return=[(1, "note", now, None)],
        )
        mock_get_conn.return_value = mock_conn

        tags.get_notes_by_tag("U1", ["bug", "bug"], page=1, per_page=5)
        count_call_args = mock_cursor.execute.call_args_list[0][0]
        # HAVING count must reflect distinct tags only — a duplicate tag in
        # the input shouldn't require two matches for a note tagged once.
        assert count_call_args[1][-1] == 1

    @patch("app.tags.get_db_connection")
    def test_pagination_offset_applied(self, mock_get_conn):
        now = datetime.now()
        mock_conn, mock_cursor = self._make_mock_conn(
            fetchone_return=(10,),
            fetchall_return=[(6, "note", now, None)],
        )
        mock_get_conn.return_value = mock_conn

        tags.get_notes_by_tag("U1", ["bug"], page=3, per_page=5)
        fetch_call_args = mock_cursor.execute.call_args_list[1][0]
        # offset should be (3-1)*5 = 10, per_page = 5
        assert 10 in fetch_call_args[1]
        assert 5 in fetch_call_args[1]


# ── search_notes ──────────────────────────────────────────────────────────────


class TestSearchNotes:
    @patch("app.database.get_db_connection")
    def test_returns_matching_notes(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        mock_cursor = MagicMock()
        now = datetime.now()
        mock_cursor.fetchone.return_value = (2,)
        mock_cursor.fetchall.return_value = [
            (1, "meeting notes", now, "general"),
            (3, "meeting agenda", now, "work"),
        ]
        mock_conn.cursor.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn

        notes, total = database.search_notes("U1", "meeting", page=1, per_page=5)
        assert total == 2
        assert len(notes) == 2

    @patch("app.database.get_db_connection")
    def test_returns_none_on_no_connection(self, mock_get_conn):
        mock_get_conn.return_value = None
        notes, total = database.search_notes("U1", "keyword", page=1, per_page=5)
        assert notes is None
        assert total == 0

    @patch("app.database.get_db_connection")
    def test_uses_like_pattern(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (0,)
        mock_cursor.fetchall.return_value = []
        mock_conn.cursor.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn

        database.search_notes("U1", "test", page=1, per_page=5)
        count_call = mock_cursor.execute.call_args_list[0]
        assert count_call[0][1] == ("U1", "%test%")


# ── check_health ──────────────────────────────────────────────────────────────


class TestCheckHealth:
    @patch("app.health.get_db_connection")
    def test_healthy_when_db_connected(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        mock_get_conn.return_value = mock_conn

        healthy, message = health.check_health()
        assert healthy is True
        assert message == "ok"

    @patch("app.health.get_db_connection")
    def test_unhealthy_when_db_unavailable(self, mock_get_conn):
        mock_get_conn.return_value = None

        healthy, message = health.check_health()
        assert healthy is False
        assert "database" in message

    @patch("app.health.get_db_connection")
    def test_unhealthy_on_exception(self, mock_get_conn):
        mock_get_conn.side_effect = RuntimeError("boom")

        healthy, message = health.check_health()
        assert healthy is False
        assert "boom" in message


# ── build_edit_note_modal ────────────────────────────────────────────────────


class TestBuildEditNoteModal:
    def test_modal_structure(self):
        modal = blocks.build_edit_note_modal(42, "Hello world", "C123")
        assert modal["type"] == "modal"
        assert modal["callback_id"] == "edit_note_modal"
        assert modal["title"]["text"] == "Edit Note #42"
        assert modal["submit"]["text"] == "Save"
        assert modal["close"]["text"] == "Cancel"

    def test_private_metadata_contains_note_id_and_channel(self):
        modal = blocks.build_edit_note_modal(7, "some text", "C456")
        meta = json.loads(modal["private_metadata"])
        assert meta["note_id"] == 7
        assert meta["channel_id"] == "C456"

    def test_input_block_prefilled(self):
        modal = blocks.build_edit_note_modal(1, "pre-filled text", "")
        input_block = modal["blocks"][0]
        assert input_block["type"] == "input"
        assert input_block["block_id"] == "note_text_block"
        element = input_block["element"]
        assert element["type"] == "plain_text_input"
        assert element["action_id"] == "note_text"
        assert element["multiline"] is True
        assert element["initial_value"] == "pre-filled text"

    def test_max_length_set(self):
        modal = blocks.build_edit_note_modal(1, "text", "")
        element = modal["blocks"][0]["element"]
        assert element["max_length"] == config.MAX_NOTE_LENGTH

    def test_channel_defaults_to_empty_string(self):
        modal = blocks.build_edit_note_modal(5, "text")
        meta = json.loads(modal["private_metadata"])
        assert meta["channel_id"] == ""


# ── get_notes_by_tag OR mode ────────────────────────────────────────────────


class TestGetNotesByTagOrMode:
    def _make_mock_conn(self, fetchone_return, fetchall_return):
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = fetchone_return
        mock_cursor.fetchall.return_value = fetchall_return
        mock_conn.cursor.return_value = mock_cursor
        return mock_conn, mock_cursor

    @patch("app.tags.get_db_connection")
    def test_or_mode_uses_having_ge_one(self, mock_get_conn):
        now = datetime.now()
        mock_conn, mock_cursor = self._make_mock_conn(
            fetchone_return=(3,),
            fetchall_return=[(1, "note", now, None)],
        )
        mock_get_conn.return_value = mock_conn

        tags.get_notes_by_tag("U1", ["bug", "backend"], page=1, per_page=5, mode="or")
        count_call_args = mock_cursor.execute.call_args_list[0][0]
        # OR mode: HAVING count >= 1 (not 2)
        assert 1 in count_call_args[1]

    @patch("app.tags.get_db_connection")
    def test_and_mode_uses_having_eq_tag_count(self, mock_get_conn):
        now = datetime.now()
        mock_conn, mock_cursor = self._make_mock_conn(
            fetchone_return=(1,),
            fetchall_return=[(1, "note", now, None)],
        )
        mock_get_conn.return_value = mock_conn

        tags.get_notes_by_tag("U1", ["bug", "backend"], page=1, per_page=5, mode="and")
        count_call_args = mock_cursor.execute.call_args_list[0][0]
        # AND mode: HAVING count >= 2 (number of tags)
        assert 2 in count_call_args[1]

    @patch("app.tags.get_db_connection")
    def test_default_mode_is_and(self, mock_get_conn):
        now = datetime.now()
        mock_conn, mock_cursor = self._make_mock_conn(
            fetchone_return=(1,),
            fetchall_return=[(1, "note", now, None)],
        )
        mock_get_conn.return_value = mock_conn

        tags.get_notes_by_tag("U1", ["bug", "backend"], page=1, per_page=5)
        count_call_args = mock_cursor.execute.call_args_list[0][0]
        assert 2 in count_call_args[1]


# ── close_db_pool ────────────────────────────────────────────────────────────


class TestCloseDbPool:
    def setup_method(self):
        self._orig_pool = database._db_pool

    def teardown_method(self):
        database._db_pool = self._orig_pool

    def test_closes_all_connections(self):
        mock_pool = MagicMock()
        mock_conns = [MagicMock() for _ in range(config.DB_POOL_SIZE)]
        mock_pool.get_connection.side_effect = mock_conns
        database._db_pool = mock_pool

        database.close_db_pool()

        for conn in mock_conns:
            conn.close.assert_called_once()
        assert database._db_pool is None

    def test_noop_when_pool_is_none(self):
        database._db_pool = None
        database.close_db_pool()  # should not raise
        assert database._db_pool is None

    def test_handles_partial_drain(self):
        from mysql.connector import Error

        mock_pool = MagicMock()
        mock_conn = MagicMock()
        mock_pool.get_connection.side_effect = [mock_conn, Error("empty")]
        database._db_pool = mock_pool

        database.close_db_pool()

        mock_conn.close.assert_called_once()
        assert database._db_pool is None


# ── toggle_pin_note ───────────────────────────────────────────────────────────


class TestTogglePinNote:
    @patch("app.database.get_db_connection")
    def test_pins_unpinned_note(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1
        mock_cursor.fetchone.return_value = (1,)
        mock_conn.cursor.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn

        assert database.toggle_pin_note(1, "U1") is True
        mock_conn.commit.assert_called_once()

    @patch("app.database.get_db_connection")
    def test_unpins_pinned_note(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1
        mock_cursor.fetchone.return_value = (0,)
        mock_conn.cursor.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn

        assert database.toggle_pin_note(1, "U1") is False

    @patch("app.database.get_db_connection")
    def test_returns_none_when_note_not_found(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 0
        mock_conn.cursor.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn

        assert database.toggle_pin_note(999, "U1") is None

    @patch("app.database.get_db_connection")
    def test_returns_none_on_no_connection(self, mock_get_conn):
        mock_get_conn.return_value = None
        assert database.toggle_pin_note(1, "U1") is None


# ── get_note_stats ────────────────────────────────────────────────────────────


class TestGetNoteStats:
    @patch("app.database.get_db_connection")
    def test_returns_stats_dict_on_success(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        mock_cursor = MagicMock()
        now = datetime.now()
        mock_cursor.fetchone.side_effect = [
            (10, now, now),  # total_notes, oldest, newest
            (2,),            # pinned_count
            (4,),            # total_tags
        ]
        mock_cursor.fetchall.side_effect = [
            [("meeting", 5), ("todo", 3)],  # top_tags
            [("general", 7)],               # top_channels
        ]
        mock_conn.cursor.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn

        stats = database.get_note_stats("U1")

        assert stats["total_notes"] == 10
        assert stats["pinned_count"] == 2
        assert stats["total_tags"] == 4
        assert len(stats["top_tags"]) == 2
        assert stats["top_tags"][0] == ("meeting", 5)
        assert len(stats["top_channels"]) == 1

    @patch("app.database.get_db_connection")
    def test_returns_none_on_no_connection(self, mock_get_conn):
        mock_get_conn.return_value = None
        assert database.get_note_stats("U1") is None

    @patch("app.database.get_db_connection")
    def test_handles_zero_notes(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        mock_cursor = MagicMock()
        mock_cursor.fetchone.side_effect = [
            (0, None, None),
            (0,),
            (0,),
        ]
        mock_cursor.fetchall.side_effect = [[], []]
        mock_conn.cursor.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn

        stats = database.get_note_stats("U1")

        assert stats["total_notes"] == 0
        assert stats["oldest"] is None
        assert stats["top_tags"] == []


# ── build_stats_blocks ────────────────────────────────────────────────────────


class TestBuildStatsBlocks:
    def _make_stats(self, total=10, pinned=2, total_tags=3, oldest=None, newest=None,
                    top_tags=None, top_channels=None):
        return {
            "total_notes": total,
            "pinned_count": pinned,
            "total_tags": total_tags,
            "oldest": oldest or datetime(2024, 1, 1),
            "newest": newest or datetime(2025, 3, 1),
            "top_tags": top_tags if top_tags is not None else [("meeting", 5), ("todo", 2)],
            "top_channels": top_channels if top_channels is not None else [("general", 7)],
        }

    def test_returns_header_block(self):
        result = blocks.build_stats_blocks(self._make_stats())
        assert result[0]["type"] == "header"
        assert "Stats" in result[0]["text"]["text"]

    def test_includes_total_note_count(self):
        result = blocks.build_stats_blocks(self._make_stats(total=42))
        fields_block = next(b for b in result if b.get("type") == "section" and "fields" in b)
        all_text = " ".join(f["text"] for f in fields_block["fields"])
        assert "42" in all_text

    def test_includes_pinned_count(self):
        result = blocks.build_stats_blocks(self._make_stats(pinned=3))
        fields_block = next(b for b in result if b.get("type") == "section" and "fields" in b)
        all_text = " ".join(f["text"] for f in fields_block["fields"])
        assert "3" in all_text

    def test_top_tags_section_present_when_tags_exist(self):
        result = blocks.build_stats_blocks(self._make_stats(top_tags=[("bug", 4)]))
        text_blocks = [b for b in result if b.get("type") == "section" and "text" in b]
        combined = " ".join(b["text"]["text"] for b in text_blocks)
        assert "#bug" in combined

    def test_top_tags_section_absent_when_no_tags(self):
        result = blocks.build_stats_blocks(self._make_stats(top_tags=[]))
        text_blocks = [b for b in result if b.get("type") == "section" and "text" in b]
        combined = " ".join(b["text"]["text"] for b in text_blocks)
        assert "Top Tags" not in combined

    def test_top_channels_section_present_when_channels_exist(self):
        result = blocks.build_stats_blocks(self._make_stats(top_channels=[("general", 5)]))
        text_blocks = [b for b in result if b.get("type") == "section" and "text" in b]
        combined = " ".join(b["text"]["text"] for b in text_blocks)
        assert "#general" in combined

    def test_top_channels_absent_when_no_channels(self):
        result = blocks.build_stats_blocks(self._make_stats(top_channels=[]))
        text_blocks = [b for b in result if b.get("type") == "section" and "text" in b]
        combined = " ".join(b["text"]["text"] for b in text_blocks)
        assert "Top Channels" not in combined

    def test_date_range_uses_none_fallback(self):
        result = blocks.build_stats_blocks(self._make_stats(oldest=None, newest=None) | {"oldest": None, "newest": None})
        fields_block = next(b for b in result if b.get("type") == "section" and "fields" in b)
        all_text = " ".join(f["text"] for f in fields_block["fields"])
        assert "—" in all_text


# ── build_notes_blocks pinned indicator ──────────────────────────────────────


class TestBuildNotesBlocksPinned:
    def test_pinned_note_shows_pin_emoji(self):
        now = datetime(2025, 6, 15, 10, 30)
        result = blocks.build_notes_blocks([(1, "pinned note", now, None, 1)], page=1, per_page=5, total_count=1)
        section = next(b for b in result if b.get("type") == "section")
        assert "📌" in section["text"]["text"]

    def test_unpinned_note_no_pin_emoji(self):
        now = datetime(2025, 6, 15, 10, 30)
        result = blocks.build_notes_blocks([(1, "regular note", now, None, 0)], page=1, per_page=5, total_count=1)
        section = next(b for b in result if b.get("type") == "section")
        assert "📌" not in section["text"]["text"]

    def test_four_tuple_note_defaults_to_unpinned(self):
        now = datetime(2025, 6, 15, 10, 30)
        result = blocks.build_notes_blocks([(1, "old format", now, None)], page=1, per_page=5, total_count=1)
        section = next(b for b in result if b.get("type") == "section")
        assert "📌" not in section["text"]["text"]

    def test_sort_preserved_in_button_payload(self):
        now = datetime(2025, 6, 15, 10, 30)
        notes = [(i, f"note {i}", now, None) for i in range(1, 6)]
        result = blocks.build_notes_blocks(notes, page=1, per_page=5, total_count=10, sort="oldest")
        action_blocks = [b for b in result if b.get("type") == "actions"]
        payload = json.loads(action_blocks[0]["elements"][0]["value"])
        assert payload["sort"] == "oldest"


# ── Slash command handlers (register_handlers) ───────────────────────────────
#
# FakeBoltApp stands in for slack_bolt.App: it just records the functions
# passed to @app.command/@app.action/@app.view so tests can invoke the real
# decorated handlers (including the require_allowed_user wrapper) directly.


class FakeBoltApp:
    def __init__(self):
        self.commands = {}
        self.actions = {}
        self.views = {}
        self.messages = {}
        self.events = {}
        self.error_handler = None

    def command(self, name):
        def decorator(fn):
            self.commands[name] = fn
            return fn
        return decorator

    def action(self, action_id):
        def decorator(fn):
            self.actions[action_id] = fn
            return fn
        return decorator

    def view(self, callback_id):
        def decorator(fn):
            self.views[callback_id] = fn
            return fn
        return decorator

    def message(self, pattern):
        def decorator(fn):
            self.messages[pattern] = fn
            return fn
        return decorator

    def event(self, event_type):
        def decorator(fn):
            self.events[event_type] = fn
            return fn
        return decorator

    def error(self, fn):
        self.error_handler = fn
        return fn


def _build_test_app():
    app = FakeBoltApp()
    register_handlers(app)
    return app


def make_command(text="", user_id="U_ALLOWED", user_name="erin", channel_id="C1"):
    return {
        "user_id": user_id,
        "user_name": user_name,
        "text": text,
        "channel_id": channel_id,
    }


def call_handler(fn, **kwargs):
    """Invoke a registered handler with only the kwargs its real signature accepts."""
    accepted = inspect.signature(fn).parameters
    return fn(**{k: v for k, v in kwargs.items() if k in accepted})


SLASH_COMMANDS = [
    "/take_notes",
    "/my_notes",
    "/notes_by_tag",
    "/edit_note",
    "/delete_note",
    "/search_notes",
    "/note_stats",
    "/pin_note",
]


# ── Message / mention listeners ───────────────────────────────────────────────
#
# These two handlers aren't decorated with @require_allowed_user — they check
# middleware.allowed_user_id by hand — so authorization needs direct coverage.


class TestMessageListener:
    def setup_method(self):
        self.app = _build_test_app()
        self.fn = self.app.messages[".*"]

    def test_allowed_user_dm_gets_confirmation(self):
        say = MagicMock()
        call_handler(
            self.fn, message={"user": "U_ALLOWED", "channel": "D1"}, say=say, logger=MagicMock()
        )
        say.assert_called_once_with("✅ Message received!")

    def test_unauthorized_user_ignored(self):
        say = MagicMock()
        call_handler(
            self.fn, message={"user": "U_OTHER", "channel": "D1"}, say=say, logger=MagicMock()
        )
        say.assert_not_called()

    def test_bot_id_present_ignored(self):
        say = MagicMock()
        call_handler(
            self.fn, message={"user": "U_ALLOWED", "channel": "D1", "bot_id": "B1"},
            say=say, logger=MagicMock(),
        )
        say.assert_not_called()

    def test_bot_message_subtype_ignored(self):
        say = MagicMock()
        call_handler(
            self.fn,
            message={"user": "U_ALLOWED", "channel": "D1", "subtype": "bot_message"},
            say=say, logger=MagicMock(),
        )
        say.assert_not_called()

    def test_exception_is_caught_and_logged(self):
        say = MagicMock(side_effect=RuntimeError("boom"))
        logger = MagicMock()
        call_handler(
            self.fn, message={"user": "U_ALLOWED", "channel": "D1"}, say=say, logger=logger
        )
        logger.error.assert_called_once()


class TestMentionListener:
    def setup_method(self):
        self.app = _build_test_app()
        self.fn = self.app.events["app_mention"]

    def test_allowed_user_gets_greeting_with_cleaned_text(self):
        say = MagicMock()
        call_handler(
            self.fn, event={"user": "U_ALLOWED", "text": "<@BOTID> hello there"},
            say=say, logger=MagicMock(),
        )
        text = say.call_args[0][0]
        assert "hello there" in text
        assert "<@BOTID>" not in text

    def test_unauthorized_user_ignored(self):
        say = MagicMock()
        call_handler(
            self.fn, event={"user": "U_OTHER", "text": "<@BOTID> hello"},
            say=say, logger=MagicMock(),
        )
        say.assert_not_called()

    def test_text_without_mention_prefix_used_as_is(self):
        say = MagicMock()
        call_handler(self.fn, event={"user": "U_ALLOWED", "text": "hello"}, say=say, logger=MagicMock())
        assert "hello" in say.call_args[0][0]

    def test_exception_is_caught_and_logged(self):
        say = MagicMock(side_effect=RuntimeError("boom"))
        logger = MagicMock()
        call_handler(self.fn, event={"user": "U_ALLOWED", "text": "hi"}, say=say, logger=logger)
        logger.error.assert_called_once()


class TestSlashCommandAuthorization:
    """Every slash command must reject unauthorized users before touching the DB."""

    def setup_method(self):
        middleware._last_command_time.clear()
        self.app = _build_test_app()

    @pytest.mark.parametrize("command_name", SLASH_COMMANDS)
    def test_unauthorized_user_is_blocked(self, command_name):
        fn = self.app.commands[command_name]
        respond = MagicMock()
        call_handler(
            fn,
            ack=MagicMock(),
            respond=respond,
            command=make_command(text="1", user_id="U_OTHER"),
            client=MagicMock(),
            logger=MagicMock(),
        )
        respond.assert_called_once()
        assert "restricted" in respond.call_args[0][0]


class TestSlashCommandRateLimiting:
    def setup_method(self):
        middleware._last_command_time.clear()
        self.app = _build_test_app()

    @patch("app.handlers.toggle_pin_note")
    def test_rapid_repeat_command_is_rate_limited(self, mock_toggle):
        mock_toggle.return_value = True
        fn = self.app.commands["/pin_note"]
        respond = MagicMock()
        call_handler(fn, ack=MagicMock(), respond=respond, command=make_command(text="1"), logger=MagicMock())
        call_handler(fn, ack=MagicMock(), respond=respond, command=make_command(text="1"), logger=MagicMock())
        assert mock_toggle.call_count == 1
        assert "wait" in respond.call_args[0][0].lower()


class TestTakeNotesHandler:
    def setup_method(self):
        middleware._last_command_time.clear()
        self.app = _build_test_app()
        self.fn = self.app.commands["/take_notes"]

    @patch("app.handlers.save_note")
    def test_empty_text_rejected(self, mock_save):
        respond = MagicMock()
        call_handler(
            self.fn, ack=MagicMock(), respond=respond, command=make_command(text=""),
            client=MagicMock(), logger=MagicMock(),
        )
        mock_save.assert_not_called()
        assert "provide some text" in respond.call_args[0][0]

    @patch("app.handlers.save_note")
    def test_note_too_long_rejected(self, mock_save):
        respond = MagicMock()
        long_text = "x" * (config.MAX_NOTE_LENGTH + 1)
        call_handler(
            self.fn, ack=MagicMock(), respond=respond, command=make_command(text=long_text),
            client=MagicMock(), logger=MagicMock(),
        )
        mock_save.assert_not_called()
        assert "too long" in respond.call_args[0][0]

    @patch("app.handlers.save_tags")
    @patch("app.handlers.save_note")
    def test_saves_note_and_tags(self, mock_save_note, mock_save_tags):
        mock_save_note.return_value = 7
        respond = MagicMock()
        client = MagicMock()
        client.conversations_info.return_value = {"channel": {"name": "general"}}
        call_handler(
            self.fn, ack=MagicMock(), respond=respond, command=make_command(text="ship it #release"),
            client=client, logger=MagicMock(),
        )
        mock_save_note.assert_called_once_with("U_ALLOWED", "erin", "ship it #release", "C1", "general")
        mock_save_tags.assert_called_once_with(7, ["release"])
        response_text = respond.call_args[0][0]
        assert "Note ID: 7" in response_text
        assert "#release" in response_text

    @patch("app.handlers.save_tags")
    @patch("app.handlers.save_note")
    def test_no_tags_skips_save_tags(self, mock_save_note, mock_save_tags):
        mock_save_note.return_value = 3
        client = MagicMock()
        client.conversations_info.return_value = {"channel": {"name": "general"}}
        call_handler(
            self.fn, ack=MagicMock(), respond=MagicMock(), command=make_command(text="no tags here"),
            client=client, logger=MagicMock(),
        )
        mock_save_tags.assert_not_called()

    @patch("app.handlers.save_note")
    def test_db_error_reports_failure(self, mock_save_note):
        mock_save_note.return_value = False
        respond = MagicMock()
        client = MagicMock()
        client.conversations_info.return_value = {"channel": {"name": "general"}}
        call_handler(
            self.fn, ack=MagicMock(), respond=respond, command=make_command(text="hello"),
            client=client, logger=MagicMock(),
        )
        assert "error saving" in respond.call_args[0][0]

    @patch("app.handlers.save_note")
    def test_channel_lookup_failure_does_not_block_save(self, mock_save_note):
        mock_save_note.return_value = 9
        client = MagicMock()
        client.conversations_info.side_effect = RuntimeError("no channel access")
        call_handler(
            self.fn, ack=MagicMock(), respond=MagicMock(), command=make_command(text="no tags"),
            client=client, logger=MagicMock(),
        )
        mock_save_note.assert_called_once_with("U_ALLOWED", "erin", "no tags", "C1", None)

    @patch("app.handlers.save_note")
    def test_exception_reports_generic_error(self, mock_save_note):
        mock_save_note.side_effect = RuntimeError("boom")
        respond = MagicMock()
        client = MagicMock()
        client.conversations_info.return_value = {"channel": {"name": "general"}}
        call_handler(
            self.fn, ack=MagicMock(), respond=respond, command=make_command(text="hello"),
            client=client, logger=MagicMock(),
        )
        assert "An error occurred while saving" in respond.call_args[0][0]


class TestMyNotesHandler:
    def setup_method(self):
        middleware._last_command_time.clear()
        self.app = _build_test_app()
        self.fn = self.app.commands["/my_notes"]

    @patch("app.handlers.get_notes_page")
    def test_db_error(self, mock_get_notes):
        mock_get_notes.return_value = (None, 0)
        respond = MagicMock()
        call_handler(self.fn, ack=MagicMock(), respond=respond, command=make_command(text=""), logger=MagicMock())
        assert "Database connection error" in respond.call_args[0][0]

    @patch("app.handlers.get_notes_page")
    def test_no_notes_found(self, mock_get_notes):
        mock_get_notes.return_value = ([], 0)
        respond = MagicMock()
        call_handler(self.fn, ack=MagicMock(), respond=respond, command=make_command(text=""), logger=MagicMock())
        assert "No notes found for erin" in respond.call_args[0][0]

    @patch("app.handlers.get_notes_page")
    def test_default_paging_and_sort(self, mock_get_notes):
        now = datetime(2025, 6, 15, 10, 30)
        mock_get_notes.return_value = ([(1, "note", now, None, 0)], 1)
        respond = MagicMock()
        call_handler(self.fn, ack=MagicMock(), respond=respond, command=make_command(text=""), logger=MagicMock())
        mock_get_notes.assert_called_once_with("U_ALLOWED", 1, config.NOTES_PER_PAGE, sort="newest")
        assert "blocks" in respond.call_args.kwargs

    @patch("app.handlers.get_notes_page")
    def test_custom_per_page_clamped(self, mock_get_notes):
        mock_get_notes.return_value = ([], 0)
        call_handler(self.fn, ack=MagicMock(), respond=MagicMock(), command=make_command(text="50"), logger=MagicMock())
        assert mock_get_notes.call_args[0][2] == 20

    @patch("app.handlers.get_notes_page")
    def test_sort_oldest_parsed(self, mock_get_notes):
        mock_get_notes.return_value = ([], 0)
        call_handler(
            self.fn, ack=MagicMock(), respond=MagicMock(), command=make_command(text="sort:oldest"),
            logger=MagicMock(),
        )
        assert mock_get_notes.call_args.kwargs["sort"] == "oldest"

    @patch("app.handlers.get_notes_page")
    def test_per_page_and_sort_combined(self, mock_get_notes):
        mock_get_notes.return_value = ([], 0)
        call_handler(
            self.fn, ack=MagicMock(), respond=MagicMock(), command=make_command(text="3 sort:oldest"),
            logger=MagicMock(),
        )
        assert mock_get_notes.call_args[0][2] == 3
        assert mock_get_notes.call_args.kwargs["sort"] == "oldest"

    @patch("app.handlers.get_notes_page")
    def test_minimum_per_page_clamped(self, mock_get_notes):
        mock_get_notes.return_value = ([], 0)
        call_handler(self.fn, ack=MagicMock(), respond=MagicMock(), command=make_command(text="0"), logger=MagicMock())
        assert mock_get_notes.call_args[0][2] == 1

    @patch("app.handlers.get_notes_page")
    def test_exception_reports_generic_error(self, mock_get_notes):
        mock_get_notes.side_effect = RuntimeError("boom")
        respond = MagicMock()
        call_handler(self.fn, ack=MagicMock(), respond=respond, command=make_command(text=""), logger=MagicMock())
        assert "An error occurred while retrieving" in respond.call_args[0][0]


class TestNotesByTagHandler:
    def setup_method(self):
        middleware._last_command_time.clear()
        self.app = _build_test_app()
        self.fn = self.app.commands["/notes_by_tag"]

    @patch("app.handlers.get_user_tags")
    def test_no_args_lists_tags(self, mock_get_user_tags):
        mock_get_user_tags.return_value = [("work", 3), ("bug", 1)]
        respond = MagicMock()
        call_handler(self.fn, ack=MagicMock(), respond=respond, command=make_command(text=""), logger=MagicMock())
        text = respond.call_args[0][0]
        assert "#work" in text and "#bug" in text

    @patch("app.handlers.get_user_tags")
    def test_no_args_db_error(self, mock_get_user_tags):
        mock_get_user_tags.return_value = None
        respond = MagicMock()
        call_handler(self.fn, ack=MagicMock(), respond=respond, command=make_command(text=""), logger=MagicMock())
        assert "Database connection error" in respond.call_args[0][0]

    @patch("app.handlers.get_user_tags")
    def test_no_args_no_tags_yet(self, mock_get_user_tags):
        mock_get_user_tags.return_value = []
        respond = MagicMock()
        call_handler(self.fn, ack=MagicMock(), respond=respond, command=make_command(text=""), logger=MagicMock())
        assert "No tags found" in respond.call_args[0][0]

    @patch("app.handlers.get_notes_by_tag")
    def test_and_mode_for_space_separated_tags(self, mock_get_notes):
        now = datetime(2025, 6, 15, 10, 30)
        mock_get_notes.return_value = ([(1, "note", now, None, 0)], 1)
        call_handler(
            self.fn, ack=MagicMock(), respond=MagicMock(), command=make_command(text="work urgent"),
            logger=MagicMock(),
        )
        args = mock_get_notes.call_args[0]
        assert args[1] == ["work", "urgent"]
        assert args[4] == "and"

    @patch("app.handlers.get_notes_by_tag")
    def test_or_mode_for_pipe_separated_tags(self, mock_get_notes):
        mock_get_notes.return_value = ([], 0)
        call_handler(
            self.fn, ack=MagicMock(), respond=MagicMock(), command=make_command(text="work|urgent"),
            logger=MagicMock(),
        )
        args = mock_get_notes.call_args[0]
        assert args[1] == ["work", "urgent"]
        assert args[4] == "or"

    @patch("app.handlers.get_notes_by_tag")
    def test_db_error_on_filtered_lookup(self, mock_get_notes):
        mock_get_notes.return_value = (None, 0)
        respond = MagicMock()
        call_handler(
            self.fn, ack=MagicMock(), respond=respond, command=make_command(text="work"), logger=MagicMock(),
        )
        assert "Database connection error" in respond.call_args[0][0]

    @patch("app.handlers.get_notes_by_tag")
    def test_no_matching_notes(self, mock_get_notes):
        mock_get_notes.return_value = ([], 0)
        respond = MagicMock()
        call_handler(
            self.fn, ack=MagicMock(), respond=respond, command=make_command(text="ghost"), logger=MagicMock(),
        )
        assert "No notes found with tags" in respond.call_args[0][0]

    @patch("app.handlers.get_notes_by_tag")
    def test_pagination_action_ids_renamed_for_tag_filter(self, mock_get_notes):
        now = datetime(2025, 6, 15, 10, 30)
        mock_get_notes.return_value = ([(i, f"note {i}", now, None, 0) for i in range(1, 6)], 12)
        respond = MagicMock()
        call_handler(
            self.fn, ack=MagicMock(), respond=respond, command=make_command(text="work"), logger=MagicMock(),
        )
        blocks_arg = respond.call_args.kwargs["blocks"]
        action_block = next(b for b in blocks_arg if b.get("type") == "actions")
        ids = [el["action_id"] for el in action_block["elements"]]
        assert ids == ["tag_notes_next_page"]
        payload = json.loads(action_block["elements"][0]["value"])
        assert payload["tags"] == ["work"]
        assert payload["tag_mode"] == "and"

    @patch("app.handlers.get_notes_by_tag")
    def test_pagination_action_ids_renamed_for_or_mode(self, mock_get_notes):
        now = datetime(2025, 6, 15, 10, 30)
        mock_get_notes.return_value = ([(i, f"note {i}", now, None, 0) for i in range(1, 6)], 12)
        respond = MagicMock()
        call_handler(
            self.fn, ack=MagicMock(), respond=respond, command=make_command(text="work|urgent"),
            logger=MagicMock(),
        )
        blocks_arg = respond.call_args.kwargs["blocks"]
        assert blocks_arg[0]["text"]["text"] == "Notes with any of #work | #urgent"
        action_block = next(b for b in blocks_arg if b.get("type") == "actions")
        ids = [el["action_id"] for el in action_block["elements"]]
        assert ids == ["tag_notes_next_page"]
        payload = json.loads(action_block["elements"][0]["value"])
        assert payload["tags"] == ["work", "urgent"]
        assert payload["tag_mode"] == "or"

    @patch("app.handlers.get_user_tags")
    def test_exception_reports_generic_error(self, mock_get_user_tags):
        mock_get_user_tags.side_effect = RuntimeError("boom")
        respond = MagicMock()
        call_handler(self.fn, ack=MagicMock(), respond=respond, command=make_command(text=""), logger=MagicMock())
        assert "An error occurred while retrieving" in respond.call_args[0][0]


class TestEditNoteHandler:
    def setup_method(self):
        middleware._last_command_time.clear()
        self.app = _build_test_app()
        self.fn = self.app.commands["/edit_note"]

    def test_missing_note_id(self):
        respond = MagicMock()
        call_handler(
            self.fn, ack=MagicMock(), respond=respond, command=make_command(text=""),
            client=MagicMock(), logger=MagicMock(),
        )
        assert "provide a note ID" in respond.call_args[0][0]

    def test_invalid_note_id(self):
        respond = MagicMock()
        call_handler(
            self.fn, ack=MagicMock(), respond=respond, command=make_command(text="abc"),
            client=MagicMock(), logger=MagicMock(),
        )
        assert "Invalid note ID" in respond.call_args[0][0]

    @patch("app.handlers.get_note_by_id")
    def test_note_not_found(self, mock_get_note):
        mock_get_note.return_value = None
        respond = MagicMock()
        call_handler(
            self.fn, ack=MagicMock(), respond=respond, command=make_command(text="42"),
            client=MagicMock(), logger=MagicMock(),
        )
        assert "not found" in respond.call_args[0][0]

    @patch("app.handlers.get_note_by_id")
    def test_opens_modal_with_current_text(self, mock_get_note):
        now = datetime(2025, 6, 15, 10, 30)
        mock_get_note.return_value = (42, "current note text", now, "general")
        client = MagicMock()
        command = make_command(text="42")
        command["trigger_id"] = "T1"
        call_handler(
            self.fn, ack=MagicMock(), respond=MagicMock(), command=command, client=client, logger=MagicMock(),
        )
        client.views_open.assert_called_once()
        kwargs = client.views_open.call_args.kwargs
        assert kwargs["trigger_id"] == "T1"
        view = kwargs["view"]
        assert view["callback_id"] == "edit_note_modal"
        meta = json.loads(view["private_metadata"])
        assert meta["note_id"] == 42

    @patch("app.handlers.get_note_by_id")
    def test_exception_reports_generic_error(self, mock_get_note):
        mock_get_note.side_effect = RuntimeError("boom")
        respond = MagicMock()
        call_handler(
            self.fn, ack=MagicMock(), respond=respond, command=make_command(text="42"),
            client=MagicMock(), logger=MagicMock(),
        )
        assert "An error occurred while opening the edit modal" in respond.call_args[0][0]


def make_view(note_id=42, text="updated text", channel_id=""):
    return {
        "private_metadata": json.dumps({"note_id": note_id, "channel_id": channel_id}),
        "state": {"values": {"note_text_block": {"note_text": {"value": text}}}},
    }


class TestEditNoteModalHandler:
    def setup_method(self):
        middleware._last_command_time.clear()
        self.app = _build_test_app()
        self.fn = self.app.views["edit_note_modal"]

    def test_too_long_text_rejected(self):
        ack = MagicMock()
        long_text = "x" * (config.MAX_NOTE_LENGTH + 1)
        call_handler(
            self.fn, ack=ack, body={"user": {"id": "U_ALLOWED"}}, view=make_view(text=long_text),
            client=MagicMock(), logger=MagicMock(),
        )
        kwargs = ack.call_args.kwargs
        assert kwargs["response_action"] == "errors"
        assert "too long" in kwargs["errors"]["note_text_block"]

    @patch("app.handlers.get_note_by_id")
    def test_note_not_found(self, mock_get_note):
        mock_get_note.return_value = None
        ack = MagicMock()
        call_handler(
            self.fn, ack=ack, body={"user": {"id": "U_ALLOWED"}}, view=make_view(),
            client=MagicMock(), logger=MagicMock(),
        )
        kwargs = ack.call_args.kwargs
        assert kwargs["response_action"] == "errors"
        assert "not found" in kwargs["errors"]["note_text_block"]

    @patch("app.handlers.update_note")
    @patch("app.handlers.get_note_by_id")
    def test_update_failure_reported(self, mock_get_note, mock_update):
        mock_get_note.return_value = (42, "old text", datetime.now(), None)
        mock_update.return_value = False
        ack = MagicMock()
        call_handler(
            self.fn, ack=ack, body={"user": {"id": "U_ALLOWED"}}, view=make_view(),
            client=MagicMock(), logger=MagicMock(),
        )
        kwargs = ack.call_args.kwargs
        assert kwargs["response_action"] == "errors"
        assert "Failed to update" in kwargs["errors"]["note_text_block"]

    @patch("app.handlers.save_tags")
    @patch("app.handlers.delete_tags_for_note")
    @patch("app.handlers.update_note")
    @patch("app.handlers.get_note_by_id")
    def test_successful_update_with_tags(self, mock_get_note, mock_update, mock_delete_tags, mock_save_tags):
        mock_get_note.return_value = (42, "old text", datetime.now(), None)
        mock_update.return_value = True
        ack = MagicMock()
        client = MagicMock()
        call_handler(
            self.fn, ack=ack, body={"user": {"id": "U_ALLOWED"}},
            view=make_view(text="new text #done", channel_id="C12345"),
            client=client, logger=MagicMock(),
        )
        mock_update.assert_called_once_with(42, "U_ALLOWED", "new text #done")
        mock_delete_tags.assert_called_once_with(42)
        mock_save_tags.assert_called_once_with(42, ["done"])
        ack.assert_called_once_with()
        post_kwargs = client.chat_postEphemeral.call_args.kwargs
        assert post_kwargs["channel"] == "C12345"
        assert post_kwargs["user"] == "U_ALLOWED"
        assert "#done" in post_kwargs["text"]

    @patch("app.handlers.save_tags")
    @patch("app.handlers.delete_tags_for_note")
    @patch("app.handlers.update_note")
    @patch("app.handlers.get_note_by_id")
    def test_successful_update_without_tags_skips_save_tags(
        self, mock_get_note, mock_update, mock_delete_tags, mock_save_tags
    ):
        mock_get_note.return_value = (42, "old text", datetime.now(), None)
        mock_update.return_value = True
        client = MagicMock()
        call_handler(
            self.fn, ack=MagicMock(), body={"user": {"id": "U_ALLOWED"}},
            view=make_view(text="no tags here", channel_id="C12345"),
            client=client, logger=MagicMock(),
        )
        mock_delete_tags.assert_called_once_with(42)
        mock_save_tags.assert_not_called()

    @patch("app.handlers.update_note")
    @patch("app.handlers.get_note_by_id")
    def test_valid_channel_id_used_for_confirmation(self, mock_get_note, mock_update):
        mock_get_note.return_value = (42, "old text", datetime.now(), None)
        mock_update.return_value = True
        client = MagicMock()
        call_handler(
            self.fn, ack=MagicMock(), body={"user": {"id": "U_ALLOWED"}},
            view=make_view(channel_id="D98765"),
            client=client, logger=MagicMock(),
        )
        assert client.chat_postEphemeral.call_args.kwargs["channel"] == "D98765"

    @patch("app.handlers.update_note")
    @patch("app.handlers.get_note_by_id")
    def test_malformed_channel_id_falls_back_to_user_id(self, mock_get_note, mock_update):
        """A channel_id that doesn't look like a real Slack channel/DM/group ID must
        not be trusted — fall back to the submitting user's own ID."""
        mock_get_note.return_value = (42, "old text", datetime.now(), None)
        mock_update.return_value = True
        client = MagicMock()
        call_handler(
            self.fn, ack=MagicMock(), body={"user": {"id": "U_ALLOWED"}},
            view=make_view(channel_id="X not-a-real-channel"),
            client=client, logger=MagicMock(),
        )
        assert client.chat_postEphemeral.call_args.kwargs["channel"] == "U_ALLOWED"

    @patch("app.handlers.update_note")
    @patch("app.handlers.get_note_by_id")
    def test_empty_channel_id_falls_back_to_user_id(self, mock_get_note, mock_update):
        mock_get_note.return_value = (42, "old text", datetime.now(), None)
        mock_update.return_value = True
        client = MagicMock()
        call_handler(
            self.fn, ack=MagicMock(), body={"user": {"id": "U_ALLOWED"}},
            view=make_view(channel_id=""),
            client=client, logger=MagicMock(),
        )
        assert client.chat_postEphemeral.call_args.kwargs["channel"] == "U_ALLOWED"

    @patch("app.handlers.get_note_by_id")
    def test_exception_acks_generic_error(self, mock_get_note):
        mock_get_note.side_effect = RuntimeError("boom")
        ack = MagicMock()
        call_handler(
            self.fn, ack=ack, body={"user": {"id": "U_ALLOWED"}}, view=make_view(),
            client=MagicMock(), logger=MagicMock(),
        )
        kwargs = ack.call_args.kwargs
        assert kwargs["response_action"] == "errors"
        assert "unexpected error" in kwargs["errors"]["note_text_block"]


class TestDeleteNoteHandler:
    def setup_method(self):
        middleware._last_command_time.clear()
        self.app = _build_test_app()
        self.fn = self.app.commands["/delete_note"]

    @patch("app.handlers.get_note_by_id")
    def test_missing_note_id(self, mock_get_note):
        respond = MagicMock()
        call_handler(self.fn, ack=MagicMock(), respond=respond, command=make_command(text=""), logger=MagicMock())
        assert "Usage" in respond.call_args[0][0]
        mock_get_note.assert_not_called()

    def test_invalid_note_id(self):
        respond = MagicMock()
        call_handler(self.fn, ack=MagicMock(), respond=respond, command=make_command(text="abc"), logger=MagicMock())
        assert "Invalid note ID" in respond.call_args[0][0]

    @patch("app.handlers.get_note_by_id")
    def test_note_not_found(self, mock_get_note):
        mock_get_note.return_value = None
        respond = MagicMock()
        call_handler(self.fn, ack=MagicMock(), respond=respond, command=make_command(text="42"), logger=MagicMock())
        assert "not found" in respond.call_args[0][0]

    @patch("app.handlers.delete_note")
    @patch("app.handlers.get_note_by_id")
    def test_delete_failure_reported(self, mock_get_note, mock_delete):
        mock_get_note.return_value = (42, "text", datetime.now(), None)
        mock_delete.return_value = False
        respond = MagicMock()
        call_handler(self.fn, ack=MagicMock(), respond=respond, command=make_command(text="42"), logger=MagicMock())
        assert "Failed to delete" in respond.call_args[0][0]

    @patch("app.handlers.delete_note")
    @patch("app.handlers.get_note_by_id")
    def test_successful_delete(self, mock_get_note, mock_delete):
        mock_get_note.return_value = (42, "text", datetime.now(), None)
        mock_delete.return_value = True
        respond = MagicMock()
        call_handler(self.fn, ack=MagicMock(), respond=respond, command=make_command(text="42"), logger=MagicMock())
        mock_delete.assert_called_once_with(42, "U_ALLOWED")
        assert "deleted" in respond.call_args[0][0]

    @patch("app.handlers.get_note_by_id")
    def test_exception_reports_generic_error(self, mock_get_note):
        mock_get_note.side_effect = RuntimeError("boom")
        respond = MagicMock()
        call_handler(self.fn, ack=MagicMock(), respond=respond, command=make_command(text="42"), logger=MagicMock())
        assert "An error occurred while deleting" in respond.call_args[0][0]


class TestSearchNotesHandler:
    def setup_method(self):
        middleware._last_command_time.clear()
        self.app = _build_test_app()
        self.fn = self.app.commands["/search_notes"]

    def test_missing_keyword(self):
        respond = MagicMock()
        call_handler(self.fn, ack=MagicMock(), respond=respond, command=make_command(text=""), logger=MagicMock())
        assert "provide a search term" in respond.call_args[0][0]

    @patch("app.handlers.search_notes")
    def test_db_error(self, mock_search):
        mock_search.return_value = (None, 0)
        respond = MagicMock()
        call_handler(
            self.fn, ack=MagicMock(), respond=respond, command=make_command(text="meeting"), logger=MagicMock(),
        )
        assert "Database connection error" in respond.call_args[0][0]

    @patch("app.handlers.search_notes")
    def test_no_matches(self, mock_search):
        mock_search.return_value = ([], 0)
        respond = MagicMock()
        call_handler(
            self.fn, ack=MagicMock(), respond=respond, command=make_command(text="meeting"), logger=MagicMock(),
        )
        assert "No notes found matching" in respond.call_args[0][0]

    @patch("app.handlers.search_notes")
    def test_matches_rename_pagination_action_ids(self, mock_search):
        now = datetime(2025, 6, 15, 10, 30)
        mock_search.return_value = ([(i, f"note {i}", now, None, 0) for i in range(1, 6)], 12)
        respond = MagicMock()
        call_handler(
            self.fn, ack=MagicMock(), respond=respond, command=make_command(text="meeting"), logger=MagicMock(),
        )
        blocks_arg = respond.call_args.kwargs["blocks"]
        assert blocks_arg[0]["text"]["text"] == "Search: meeting"
        action_block = next(b for b in blocks_arg if b.get("type") == "actions")
        ids = [el["action_id"] for el in action_block["elements"]]
        assert ids == ["search_notes_next_page"]
        payload = json.loads(action_block["elements"][0]["value"])
        assert payload["keyword"] == "meeting"

    @patch("app.handlers.search_notes")
    def test_exception_reports_generic_error(self, mock_search):
        mock_search.side_effect = RuntimeError("boom")
        respond = MagicMock()
        call_handler(
            self.fn, ack=MagicMock(), respond=respond, command=make_command(text="meeting"), logger=MagicMock(),
        )
        assert "An error occurred while searching" in respond.call_args[0][0]


class TestNoteStatsHandler:
    def setup_method(self):
        middleware._last_command_time.clear()
        self.app = _build_test_app()
        self.fn = self.app.commands["/note_stats"]

    @patch("app.handlers.get_note_stats")
    def test_db_error(self, mock_stats):
        mock_stats.return_value = None
        respond = MagicMock()
        call_handler(self.fn, ack=MagicMock(), respond=respond, command=make_command(text=""), logger=MagicMock())
        assert "Database connection error" in respond.call_args[0][0]

    @patch("app.handlers.get_note_stats")
    def test_zero_notes(self, mock_stats):
        mock_stats.return_value = {"total_notes": 0}
        respond = MagicMock()
        call_handler(self.fn, ack=MagicMock(), respond=respond, command=make_command(text=""), logger=MagicMock())
        assert "No notes yet" in respond.call_args[0][0]

    @patch("app.handlers.get_note_stats")
    def test_stats_rendered(self, mock_stats):
        mock_stats.return_value = {
            "total_notes": 5,
            "pinned_count": 1,
            "total_tags": 2,
            "oldest": datetime(2025, 1, 1),
            "newest": datetime(2025, 6, 1),
            "top_tags": [("work", 3)],
            "top_channels": [("general", 4)],
        }
        respond = MagicMock()
        call_handler(self.fn, ack=MagicMock(), respond=respond, command=make_command(text=""), logger=MagicMock())
        blocks_arg = respond.call_args.kwargs["blocks"]
        assert blocks_arg[0]["type"] == "header"

    @patch("app.handlers.get_note_stats")
    def test_exception_reports_generic_error(self, mock_stats):
        mock_stats.side_effect = RuntimeError("boom")
        respond = MagicMock()
        call_handler(self.fn, ack=MagicMock(), respond=respond, command=make_command(text=""), logger=MagicMock())
        assert "An error occurred while fetching" in respond.call_args[0][0]


class TestPinNoteHandler:
    def setup_method(self):
        middleware._last_command_time.clear()
        self.app = _build_test_app()
        self.fn = self.app.commands["/pin_note"]

    def test_missing_note_id(self):
        respond = MagicMock()
        call_handler(self.fn, ack=MagicMock(), respond=respond, command=make_command(text=""), logger=MagicMock())
        assert "provide a note ID" in respond.call_args[0][0]

    def test_invalid_note_id(self):
        respond = MagicMock()
        call_handler(self.fn, ack=MagicMock(), respond=respond, command=make_command(text="abc"), logger=MagicMock())
        assert "Invalid note ID" in respond.call_args[0][0]

    @patch("app.handlers.toggle_pin_note")
    def test_note_not_found(self, mock_toggle):
        mock_toggle.return_value = None
        respond = MagicMock()
        call_handler(self.fn, ack=MagicMock(), respond=respond, command=make_command(text="42"), logger=MagicMock())
        assert "not found" in respond.call_args[0][0]

    @patch("app.handlers.toggle_pin_note")
    def test_pins_note(self, mock_toggle):
        mock_toggle.return_value = True
        respond = MagicMock()
        call_handler(self.fn, ack=MagicMock(), respond=respond, command=make_command(text="42"), logger=MagicMock())
        text = respond.call_args[0][0]
        assert "📌" in text and "pinned" in text

    @patch("app.handlers.toggle_pin_note")
    def test_unpins_note(self, mock_toggle):
        mock_toggle.return_value = False
        respond = MagicMock()
        call_handler(self.fn, ack=MagicMock(), respond=respond, command=make_command(text="42"), logger=MagicMock())
        text = respond.call_args[0][0]
        assert "🔓" in text and "unpinned" in text

    @patch("app.handlers.toggle_pin_note")
    def test_exception_reports_generic_error(self, mock_toggle):
        mock_toggle.side_effect = RuntimeError("boom")
        respond = MagicMock()
        call_handler(self.fn, ack=MagicMock(), respond=respond, command=make_command(text="42"), logger=MagicMock())
        assert "An error occurred while toggling pin" in respond.call_args[0][0]


# ── Pagination action handlers ────────────────────────────────────────────────


def make_action_body(payload, user_id="U_ALLOWED"):
    return {"user": {"id": user_id}, "actions": [{"value": json.dumps(payload)}]}


def _paged_notes(count=5):
    now = datetime(2025, 6, 15, 10, 30)
    return [(i, f"note {i}", now, None, 0) for i in range(1, count + 1)]


class TestNotesPaginationAction:
    def setup_method(self):
        middleware._last_command_time.clear()
        self.app = _build_test_app()
        self.fn = self.app.actions["notes_prev_page"]
        assert self.app.actions["notes_next_page"] is self.fn

    @patch("app.handlers.get_notes_page")
    def test_renders_page_with_sort_preserved(self, mock_get_notes):
        mock_get_notes.return_value = (_paged_notes(), 12)
        respond = MagicMock()
        call_handler(
            self.fn, ack=MagicMock(), respond=respond, logger=MagicMock(),
            body=make_action_body({"page": 2, "per_page": 5, "sort": "oldest"}),
        )
        mock_get_notes.assert_called_once_with("U_ALLOWED", 2, 5, sort="oldest")
        assert respond.call_args.kwargs["replace_original"] is True
        payload = json.loads(
            next(b for b in respond.call_args.kwargs["blocks"] if b.get("type") == "actions")
            ["elements"][0]["value"]
        )
        assert payload["sort"] == "oldest"

    @patch("app.handlers.get_notes_page")
    def test_defaults_sort_to_newest_when_missing(self, mock_get_notes):
        mock_get_notes.return_value = (_paged_notes(), 12)
        call_handler(
            self.fn, ack=MagicMock(), respond=MagicMock(), logger=MagicMock(),
            body=make_action_body({"page": 1, "per_page": 5}),
        )
        assert mock_get_notes.call_args.kwargs["sort"] == "newest"

    @patch("app.handlers.get_notes_page")
    def test_db_error_does_not_respond(self, mock_get_notes):
        mock_get_notes.return_value = (None, 0)
        respond = MagicMock()
        call_handler(
            self.fn, ack=MagicMock(), respond=respond, logger=MagicMock(),
            body=make_action_body({"page": 1, "per_page": 5}),
        )
        respond.assert_not_called()

    @patch("app.handlers.get_notes_page")
    def test_exception_is_caught_and_logged(self, mock_get_notes):
        mock_get_notes.side_effect = RuntimeError("boom")
        logger = MagicMock()
        respond = MagicMock()
        call_handler(
            self.fn, ack=MagicMock(), respond=respond, logger=logger,
            body=make_action_body({"page": 1, "per_page": 5}),
        )
        respond.assert_not_called()
        logger.error.assert_called_once()


class TestTagNotesPaginationAction:
    def setup_method(self):
        middleware._last_command_time.clear()
        self.app = _build_test_app()
        self.fn = self.app.actions["tag_notes_prev_page"]
        assert self.app.actions["tag_notes_next_page"] is self.fn

    @patch("app.handlers.get_notes_by_tag")
    def test_and_mode_header_and_action_ids(self, mock_get_notes):
        mock_get_notes.return_value = (_paged_notes(), 12)
        respond = MagicMock()
        call_handler(
            self.fn, ack=MagicMock(), respond=respond, logger=MagicMock(),
            body=make_action_body({"page": 2, "per_page": 5, "tags": ["work"], "tag_mode": "and"}),
        )
        mock_get_notes.assert_called_once_with("U_ALLOWED", ["work"], 2, 5, "and")
        blocks_arg = respond.call_args.kwargs["blocks"]
        assert blocks_arg[0]["text"]["text"] == "Notes tagged #work"
        action_block = next(b for b in blocks_arg if b.get("type") == "actions")
        ids = [el["action_id"] for el in action_block["elements"]]
        assert ids == ["tag_notes_prev_page", "tag_notes_next_page"]
        payload = json.loads(action_block["elements"][0]["value"])
        assert payload["tags"] == ["work"]
        assert payload["tag_mode"] == "and"

    @patch("app.handlers.get_notes_by_tag")
    def test_or_mode_header_and_payload(self, mock_get_notes):
        mock_get_notes.return_value = (_paged_notes(), 12)
        respond = MagicMock()
        call_handler(
            self.fn, ack=MagicMock(), respond=respond, logger=MagicMock(),
            body=make_action_body({"page": 2, "per_page": 5, "tags": ["work", "urgent"], "tag_mode": "or"}),
        )
        mock_get_notes.assert_called_once_with("U_ALLOWED", ["work", "urgent"], 2, 5, "or")
        blocks_arg = respond.call_args.kwargs["blocks"]
        assert blocks_arg[0]["text"]["text"] == "Notes with any of #work | #urgent"
        action_block = next(b for b in blocks_arg if b.get("type") == "actions")
        payload = json.loads(action_block["elements"][0]["value"])
        assert payload["tag_mode"] == "or"

    @patch("app.handlers.get_notes_by_tag")
    def test_defaults_to_and_mode_when_missing(self, mock_get_notes):
        mock_get_notes.return_value = ([], 0)
        call_handler(
            self.fn, ack=MagicMock(), respond=MagicMock(), logger=MagicMock(),
            body=make_action_body({"page": 1, "per_page": 5, "tags": ["work"]}),
        )
        assert mock_get_notes.call_args[0][4] == "and"

    @patch("app.handlers.get_notes_by_tag")
    def test_db_error_does_not_respond(self, mock_get_notes):
        mock_get_notes.return_value = (None, 0)
        respond = MagicMock()
        call_handler(
            self.fn, ack=MagicMock(), respond=respond, logger=MagicMock(),
            body=make_action_body({"page": 1, "per_page": 5, "tags": ["work"], "tag_mode": "and"}),
        )
        respond.assert_not_called()

    @patch("app.handlers.get_notes_by_tag")
    def test_exception_is_caught_and_logged(self, mock_get_notes):
        mock_get_notes.side_effect = RuntimeError("boom")
        logger = MagicMock()
        respond = MagicMock()
        call_handler(
            self.fn, ack=MagicMock(), respond=respond, logger=logger,
            body=make_action_body({"page": 1, "per_page": 5, "tags": ["work"], "tag_mode": "and"}),
        )
        respond.assert_not_called()
        logger.error.assert_called_once()


class TestSearchNotesPaginationAction:
    def setup_method(self):
        middleware._last_command_time.clear()
        self.app = _build_test_app()
        self.fn = self.app.actions["search_notes_prev_page"]
        assert self.app.actions["search_notes_next_page"] is self.fn

    @patch("app.handlers.search_notes")
    def test_keyword_carried_through_and_header(self, mock_search):
        mock_search.return_value = (_paged_notes(), 12)
        respond = MagicMock()
        call_handler(
            self.fn, ack=MagicMock(), respond=respond, logger=MagicMock(),
            body=make_action_body({"page": 2, "per_page": 5, "keyword": "meeting"}),
        )
        mock_search.assert_called_once_with("U_ALLOWED", "meeting", 2, 5)
        blocks_arg = respond.call_args.kwargs["blocks"]
        assert blocks_arg[0]["text"]["text"] == "Search: meeting"
        action_block = next(b for b in blocks_arg if b.get("type") == "actions")
        ids = [el["action_id"] for el in action_block["elements"]]
        assert ids == ["search_notes_prev_page", "search_notes_next_page"]
        payload = json.loads(action_block["elements"][0]["value"])
        assert payload["keyword"] == "meeting"
        assert respond.call_args.kwargs["replace_original"] is True

    @patch("app.handlers.search_notes")
    def test_db_error_does_not_respond(self, mock_search):
        mock_search.return_value = (None, 0)
        respond = MagicMock()
        call_handler(
            self.fn, ack=MagicMock(), respond=respond, logger=MagicMock(),
            body=make_action_body({"page": 1, "per_page": 5, "keyword": "meeting"}),
        )
        respond.assert_not_called()

    @patch("app.handlers.search_notes")
    def test_exception_is_caught_and_logged(self, mock_search):
        mock_search.side_effect = RuntimeError("boom")
        logger = MagicMock()
        respond = MagicMock()
        call_handler(
            self.fn, ack=MagicMock(), respond=respond, logger=logger,
            body=make_action_body({"page": 1, "per_page": 5, "keyword": "meeting"}),
        )
        respond.assert_not_called()
        logger.error.assert_called_once()


# ── Global error handler ──────────────────────────────────────────────────────


class TestGlobalErrorHandler:
    def setup_method(self):
        self.app = _build_test_app()

    def test_logs_without_raising(self):
        logger = MagicMock()
        self.app.error_handler(error=RuntimeError("boom"), body={}, logger=logger)
        logger.error.assert_called_once()
