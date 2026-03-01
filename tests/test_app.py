"""Tests for the Slack notes bot.

These tests exercise the pure-logic helpers and database functions using mocks
so they can run without a real MySQL server or Slack workspace.
"""

import json
import time
from datetime import datetime
from unittest.mock import MagicMock, patch, call

import pytest

# app.py performs Slack/DB setup at import time.  We need to patch the
# environment and heavy side-effects so the module can be imported in a
# test environment without real credentials.

_ENV = {
    "SLACK_BOT_TOKEN": "xoxb-test",
    "SLACK_SIGNING_SECRET": "test-secret",
    "SLACK_APP_TOKEN": "xapp-test",
    "ALLOWED_SLACK_USER_ID": "U_ALLOWED",
    "MYSQL_HOST": "localhost",
    "MYSQL_PORT": "3306",
    "MYSQL_DATABASE": "testdb",
    "MYSQL_USER": "testuser",
    "MYSQL_PASSWORD": "testpass",
    "LOG_LEVEL": "WARNING",
}


def _import_app():
    """Import app.py with all external dependencies stubbed out."""
    import importlib

    with (
        patch.dict("os.environ", _ENV, clear=False),
        patch("slack_bolt.App") as MockApp,
        patch("slack_bolt.adapter.socket_mode.SocketModeHandler"),
        patch("mysql.connector.connect") as mock_connect,
    ):
        mock_app_instance = MagicMock()
        MockApp.return_value = mock_app_instance
        mock_app_instance.client.auth_test.return_value = {
            "user": "testbot",
            "team": "T_TEST",
        }

        # Make setup_database succeed via a mock connection
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        mock_connect.return_value = mock_conn

        import app as app_module

        importlib.reload(app_module)

    return app_module


app_module = _import_app()


# ── parse_tags ──────────────────────────────────────────────────────────


class TestParseTags:
    def test_single_tag(self):
        assert app_module.parse_tags("meeting #standup notes") == ["standup"]

    def test_multiple_tags(self):
        assert app_module.parse_tags("#bug fix for #backend and #api") == [
            "bug",
            "backend",
            "api",
        ]

    def test_duplicate_tags_deduplicated(self):
        assert app_module.parse_tags("#Bug report #bug") == ["bug"]

    def test_no_tags(self):
        assert app_module.parse_tags("no tags here") == []

    def test_tags_lowercased(self):
        assert app_module.parse_tags("#Meeting #IMPORTANT") == ["meeting", "important"]

    def test_tag_with_underscores_and_digits(self):
        assert app_module.parse_tags("#q4_2025 planning") == ["q4_2025"]


# ── build_notes_blocks ──────────────────────────────────────────────────


class TestBuildNotesBlocks:
    def _make_notes(self, count):
        """Generate a list of fake note tuples."""
        now = datetime(2025, 6, 15, 10, 30)
        return [(i, f"Note text {i}", now, "general") for i in range(1, count + 1)]

    def test_single_page_no_nav_buttons(self):
        notes = self._make_notes(3)
        blocks = app_module.build_notes_blocks(notes, page=1, per_page=5, total_count=3)
        action_blocks = [b for b in blocks if b.get("type") == "actions"]
        assert action_blocks == []

    def test_first_page_has_next_only(self):
        notes = self._make_notes(5)
        blocks = app_module.build_notes_blocks(notes, page=1, per_page=5, total_count=12)
        action_blocks = [b for b in blocks if b.get("type") == "actions"]
        assert len(action_blocks) == 1
        buttons = action_blocks[0]["elements"]
        assert len(buttons) == 1
        assert buttons[0]["action_id"] == "notes_next_page"

    def test_middle_page_has_both_buttons(self):
        notes = self._make_notes(5)
        blocks = app_module.build_notes_blocks(notes, page=2, per_page=5, total_count=15)
        action_blocks = [b for b in blocks if b.get("type") == "actions"]
        buttons = action_blocks[0]["elements"]
        action_ids = [b["action_id"] for b in buttons]
        assert "notes_prev_page" in action_ids
        assert "notes_next_page" in action_ids

    def test_last_page_has_prev_only(self):
        notes = self._make_notes(2)
        blocks = app_module.build_notes_blocks(notes, page=3, per_page=5, total_count=12)
        action_blocks = [b for b in blocks if b.get("type") == "actions"]
        buttons = action_blocks[0]["elements"]
        assert len(buttons) == 1
        assert buttons[0]["action_id"] == "notes_prev_page"

    def test_header_and_context_present(self):
        notes = self._make_notes(1)
        blocks = app_module.build_notes_blocks(notes, page=1, per_page=5, total_count=1)
        assert blocks[0]["type"] == "header"
        assert blocks[1]["type"] == "context"
        assert "1 notes total" in blocks[1]["elements"][0]["text"]

    def test_long_note_truncated(self):
        now = datetime(2025, 6, 15, 10, 30)
        long_text = "x" * 300
        notes = [(1, long_text, now, None)]
        blocks = app_module.build_notes_blocks(notes, page=1, per_page=5, total_count=1)
        section = [b for b in blocks if b.get("type") == "section"][0]
        display = section["text"]["text"]
        # The display text should be truncated and end with "..."
        assert display.endswith("...")
        # Truncated display: 197 chars + "..." = 200 chars for the note portion
        assert "x" * 197 in display

    def test_page_value_in_button_payload(self):
        notes = self._make_notes(5)
        blocks = app_module.build_notes_blocks(notes, page=1, per_page=5, total_count=10)
        action_blocks = [b for b in blocks if b.get("type") == "actions"]
        next_btn = action_blocks[0]["elements"][0]
        payload = json.loads(next_btn["value"])
        assert payload == {"page": 2, "per_page": 5}


# ── check_rate_limit ───────────────────────────────────────────────────


class TestCheckRateLimit:
    def setup_method(self):
        app_module._last_command_time.clear()

    def test_first_call_allowed(self):
        assert app_module.check_rate_limit("U1", "cmd") is False

    def test_rapid_second_call_blocked(self):
        app_module.check_rate_limit("U1", "cmd")
        assert app_module.check_rate_limit("U1", "cmd") is True

    def test_different_users_independent(self):
        app_module.check_rate_limit("U1", "cmd")
        assert app_module.check_rate_limit("U2", "cmd") is False

    def test_different_commands_independent(self):
        app_module.check_rate_limit("U1", "cmd_a")
        assert app_module.check_rate_limit("U1", "cmd_b") is False

    def test_allowed_after_cooldown(self):
        app_module.check_rate_limit("U1", "cmd")
        # Simulate time passing beyond the rate limit window
        key = "U1:cmd"
        app_module._last_command_time[key] -= app_module.RATE_LIMIT_SECONDS + 1
        assert app_module.check_rate_limit("U1", "cmd") is False


# ── require_allowed_user decorator ─────────────────────────────────────


class TestRequireAllowedUser:
    def test_authorized_user_proceeds(self):
        inner = MagicMock()
        decorated = app_module.require_allowed_user()(inner)
        decorated(
            ack=MagicMock(),
            respond=MagicMock(),
            command={"user_id": "U_ALLOWED"},
        )
        inner.assert_called_once()

    def test_unauthorized_user_blocked(self):
        inner = MagicMock()
        respond = MagicMock()
        decorated = app_module.require_allowed_user()(inner)
        decorated(
            ack=MagicMock(),
            respond=respond,
            command={"user_id": "U_OTHER"},
        )
        inner.assert_not_called()
        respond.assert_called_once()
        assert "restricted" in respond.call_args[0][0]

    def test_rate_limited_user_blocked(self):
        app_module._last_command_time.clear()
        inner = MagicMock()
        respond = MagicMock()
        decorated = app_module.require_allowed_user(command_name="test_cmd")(inner)

        # First call succeeds
        decorated(
            ack=MagicMock(),
            respond=MagicMock(),
            command={"user_id": "U_ALLOWED"},
        )
        assert inner.call_count == 1

        # Immediate second call is rate-limited
        decorated(
            ack=MagicMock(),
            respond=respond,
            command={"user_id": "U_ALLOWED"},
        )
        assert inner.call_count == 1  # not called again
        respond.assert_called_once()
        assert "wait" in respond.call_args[0][0].lower()

    def test_ack_always_called(self):
        ack = MagicMock()
        decorated = app_module.require_allowed_user()(MagicMock())
        decorated(ack=ack, respond=MagicMock(), command={"user_id": "U_OTHER"})
        ack.assert_called_once()

    def test_body_fallback_for_actions(self):
        inner = MagicMock()
        decorated = app_module.require_allowed_user()(inner)
        decorated(
            ack=MagicMock(),
            respond=MagicMock(),
            body={"user": {"id": "U_ALLOWED"}},
        )
        inner.assert_called_once()


# ── get_db_connection (retry behaviour) ────────────────────────────────


class TestGetDbConnectionRetry:
    @patch("app.time.sleep")
    @patch("app.mysql.connector.connect")
    def test_succeeds_on_first_try(self, mock_connect, mock_sleep):
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn
        result = app_module.get_db_connection()
        assert result is mock_conn
        mock_sleep.assert_not_called()

    @patch("app.time.sleep")
    @patch("app.mysql.connector.connect")
    def test_retries_then_succeeds(self, mock_connect, mock_sleep):
        from mysql.connector import Error

        mock_conn = MagicMock()
        mock_connect.side_effect = [Error("fail"), mock_conn]
        result = app_module.get_db_connection()
        assert result is mock_conn
        mock_sleep.assert_called_once_with(1)  # base delay * 2^0

    @patch("app.time.sleep")
    @patch("app.mysql.connector.connect")
    def test_all_retries_fail_returns_none(self, mock_connect, mock_sleep):
        from mysql.connector import Error

        mock_connect.side_effect = Error("persistent failure")
        result = app_module.get_db_connection()
        assert result is None
        assert mock_sleep.call_count == app_module.DB_CONNECT_MAX_RETRIES - 1

    @patch("app.time.sleep")
    @patch("app.mysql.connector.connect")
    def test_exponential_backoff_delays(self, mock_connect, mock_sleep):
        from mysql.connector import Error

        mock_connect.side_effect = Error("down")
        app_module.get_db_connection()
        delays = [c[0][0] for c in mock_sleep.call_args_list]
        # With base=1 and max_retries=3: delays should be [1, 2]
        assert delays == [1, 2]


# ── save_note (with mocked DB) ────────────────────────────────────────


class TestSaveNote:
    @patch("app.get_db_connection")
    def test_returns_note_id_on_success(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        mock_cursor = MagicMock()
        mock_cursor.lastrowid = 42
        mock_conn.cursor.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn

        result = app_module.save_note("U1", "alice", "hello world")
        assert result == 42
        mock_cursor.execute.assert_called_once()
        mock_conn.commit.assert_called_once()

    @patch("app.get_db_connection")
    def test_returns_false_when_no_connection(self, mock_get_conn):
        mock_get_conn.return_value = None
        result = app_module.save_note("U1", "alice", "hello")
        assert result is False

    @patch("app.get_db_connection")
    def test_returns_false_on_db_error(self, mock_get_conn):
        from mysql.connector import Error

        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = Error("insert failed")
        mock_conn.cursor.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn

        result = app_module.save_note("U1", "alice", "hello")
        assert result is False


# ── get_notes_page (with mocked DB) ───────────────────────────────────


class TestGetNotesPage:
    @patch("app.get_db_connection")
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

        notes, total = app_module.get_notes_page("U1", page=1, per_page=5)
        assert total == 3
        assert len(notes) == 2

    @patch("app.get_db_connection")
    def test_returns_none_on_no_connection(self, mock_get_conn):
        mock_get_conn.return_value = None
        notes, total = app_module.get_notes_page("U1", page=1, per_page=5)
        assert notes is None
        assert total == 0


# ── save_tags (with mocked DB) ────────────────────────────────────────


class TestSaveTags:
    @patch("app.get_db_connection")
    def test_inserts_tags(self, mock_get_conn):
        mock_conn = MagicMock()
        mock_conn.is_connected.return_value = True
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_get_conn.return_value = mock_conn

        app_module.save_tags(42, ["bug", "backend"])
        mock_cursor.executemany.assert_called_once()
        args = mock_cursor.executemany.call_args[0]
        assert args[1] == [(42, "bug"), (42, "backend")]
        mock_conn.commit.assert_called_once()

    @patch("app.get_db_connection")
    def test_noop_for_empty_tags(self, mock_get_conn):
        app_module.save_tags(42, [])
        mock_get_conn.assert_not_called()
