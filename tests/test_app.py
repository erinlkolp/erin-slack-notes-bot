"""Tests for the Slack notes bot modules.

Each test class targets a specific module (database, tags, blocks, middleware,
health) so imports are clean and patches point to the right namespace.
"""

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

    def test_long_note_truncated(self):
        now = datetime(2025, 6, 15, 10, 30)
        long_text = "x" * 300
        result = blocks.build_notes_blocks([(1, long_text, now, None)], page=1, per_page=5, total_count=1)
        section = [b for b in result if b.get("type") == "section"][0]
        display = section["text"]["text"]
        assert display.endswith("...")
        assert "x" * 197 in display

    def test_page_value_in_button_payload(self):
        result = blocks.build_notes_blocks(self._make_notes(5), page=1, per_page=5, total_count=10)
        action_blocks = [b for b in result if b.get("type") == "actions"]
        payload = json.loads(action_blocks[0]["elements"][0]["value"])
        assert payload == {"page": 2, "per_page": 5}

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
