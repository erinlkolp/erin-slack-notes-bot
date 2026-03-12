# CLAUDE.md — Erin Slack Notes Bot

AI assistant guidance for this codebase.

## What This Project Does

A single-user personal note management Slack bot. One authorized Slack user can save, browse, edit, delete, search, and tag notes via slash commands. The bot connects to Slack via Socket Mode and persists notes in MySQL.

## Repository Structure

```
erin-slack-notes-bot/
├── app/
│   ├── __init__.py     # Package marker
│   ├── main.py         # Entry point: env validation, pool init, handler registration, Socket Mode startup (~137 lines)
│   ├── handlers.py     # Slack slash commands, actions, and modal submissions (~509 lines)
│   ├── database.py     # MySQL connection pooling + all CRUD operations (~282 lines)
│   ├── tags.py         # #hashtag regex parsing + tag DB operations (~141 lines)
│   ├── blocks.py       # Slack Block Kit UI component builders (~109 lines)
│   ├── middleware.py   # Authorization decorator + rate limiting (~79 lines)
│   ├── health.py       # HTTP health check endpoint on /healthz (~60 lines)
│   └── config.py       # Shared constants (limits, pool size, timeouts)
├── requirements.txt    # Python dependencies
├── tests/
│   └── test_app.py     # 75 unit tests with mocks (pytest)
├── migrations/
│   ├── 001-initial-schema.sql   # DB schema (notes + note_tags tables)
│   └── changelog-master.xml     # Liquibase changelog config
├── Dockerfile           # Python 3.12-slim app container
├── Dockerfile.liquibase # Liquibase migration container
└── docker-compose.yml   # Three services: slackbot, liquibase, db
```

## Key Conventions

### Module Responsibilities
- **`app/config.py`** is the single source of truth for all tunable constants. Edit constants there, not inline.
- **`app/handlers.py`** only orchestrates: it calls database, tag, and block functions. Business logic lives in other modules.
- **`app/database.py`** owns all SQL. No raw queries in other modules.
- **`app/blocks.py`** owns all Slack Block Kit construction. No inline block dicts in handlers.
- **`app/middleware.py`** owns authorization and rate limiting. All user-facing commands must go through `@require_allowed_user()`.

### Authorization
Authorization is single-user only, enforced via `ALLOWED_SLACK_USER_ID`. Every slash command handler must be decorated with `@require_allowed_user()` from `app/middleware.py`. Do not add multi-user logic without redesigning the middleware.

### Database
- All queries use parameterized statements (`%s` placeholders). Never use f-strings or string concatenation to build SQL.
- Always acquire connections via `get_db_connection()` (no arguments; it uses the module-level pool) and release them in a `finally` block.
- Connection retries use exponential backoff (configured in `app/config.py`: `DB_CONNECT_MAX_RETRIES`, `DB_CONNECT_BASE_DELAY`).
- Pool size is `DB_POOL_SIZE = 5`. Don't exceed this without updating `docker-compose.yml` accordingly.

### Rate Limiting
- `check_rate_limit(user_id, command)` in `app/middleware.py` enforces a per-user, per-command cooldown.
- The rate limit cache evicts stale entries when it exceeds `RATE_LIMIT_MAX_ENTRIES` (1000).
- `RATE_LIMIT_SECONDS = 1` is the cooldown window.

### Notes and Tags
- Notes are capped at `MAX_NOTE_LENGTH = 3000` characters (Slack modal character limit).
- Tags are parsed from note text using the regex `#[A-Za-z0-9_]+` in `app/tags.py`.
- Tags are stored separately in `note_tags` with a FK cascade on delete.
- When updating a note, always call `delete_tags_for_note()` then `save_tags()` — never update tags in place.

### Pagination
- Browse and search results page at `NOTES_PER_PAGE = 5` items.
- Page state (offset, query params) is serialized as JSON into Slack action `value` fields. Keep pagination payloads compact.

### Slack UI
- Use `app/blocks.py` functions to build all UI output. Return blocks, not plain text, for list responses.
- Modals (used by `/edit_note`) must call `ack()` before opening views.
- Use `respond()` for ephemeral replies to slash commands; use `client.views_open()` for modals.
- `app/blocks.py` also exports `escape_mrkdwn()` used by `handlers.py` to neutralise Slack mention/broadcast injection in user-supplied text.

### Event Handlers
`handlers.py` registers two non-command event listeners in addition to slash commands:
- **`@app.message(".*")`** — echoes a confirmation to the allowed user for any DM; silently ignores other users and bot messages.
- **`@app.event("app_mention")`** — responds to @-mentions from the allowed user; ignores unauthorized mentions.

## Development Workflow

### Running Tests
```bash
pytest tests/ -v
```
Tests use `unittest.mock` patches. No live DB or Slack connection is needed.

### Running Locally (Docker)
```bash
cp .env.example .env
# Fill in all required env vars in .env
docker compose up -d
```
The `liquibase` service runs migrations automatically on first start. The `slackbot` service starts after `liquibase` completes and `db` is healthy. The `db` service uses **Percona Server 8.0** (`percona/percona-server:8.0.39`) with the `SYS_NICE` capability to suppress mbind errors.

### Running Without Docker
```bash
pip install -r requirements.txt
# Set all env vars from .env.example in your shell
python -m app.main
```
Requires a reachable MySQL instance and schema applied manually from `migrations/001-initial-schema.sql`.

### Health Check
```
GET http://localhost:8080/healthz
```
Returns `{"status": "healthy", "message": "ok"}` (200) or `{"status": "unhealthy", "message": "<reason>"}` (503). Port is controlled by `HEALTH_CHECK_PORT` env var.

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `SLACK_BOT_TOKEN` | Yes | — | Bot token (`xoxb-...`) |
| `SLACK_SIGNING_SECRET` | Yes | — | Request signing secret |
| `SLACK_APP_TOKEN` | Yes | — | App-level token (`xapp-...`) for Socket Mode |
| `ALLOWED_SLACK_USER_ID` | Yes | — | Slack user ID of the sole authorized user |
| `MYSQL_HOST` | No | `localhost` | MySQL hostname (set to `db` in `docker-compose.yml`) |
| `MYSQL_PORT` | No | `3306` | MySQL port |
| `MYSQL_USER` | Yes | — | MySQL username |
| `MYSQL_PASSWORD` | Yes | — | MySQL password |
| `MYSQL_DATABASE` | Yes | — | MySQL database name |
| `MYSQL_ROOT_PASSWORD` | Yes | — | MySQL root password (for Docker init) |
| `MYSQL_SSL_CA` | No | — | Path to SSL CA cert for MySQL |
| `HEALTH_CHECK_PORT` | No | `8080` | Port for health endpoint |
| `LOG_LEVEL` | No | `INFO` | Python log level |

Missing required variables cause `sys.exit(1)` at startup (validated in `app/main.py`).

## Database Schema

```sql
-- notes table
CREATE TABLE notes (
  id          INT AUTO_INCREMENT PRIMARY KEY,
  user_id     VARCHAR(50)  NOT NULL,
  username    VARCHAR(100) NOT NULL,
  note_text   TEXT         NOT NULL,
  created_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
  channel_id  VARCHAR(50),
  channel_name VARCHAR(100),
  INDEX idx_user_id (user_id),
  INDEX idx_created_at (created_at),
  INDEX idx_user_created (user_id, created_at)
);

-- note_tags table
CREATE TABLE note_tags (
  id      INT AUTO_INCREMENT PRIMARY KEY,
  note_id INT          NOT NULL,
  tag     VARCHAR(100) NOT NULL,
  FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE,
  INDEX idx_tag (tag),
  INDEX idx_note_id (note_id)
);
```

Migrations are managed by **Liquibase** via `migrations/changelog-master.xml`. Add new migrations as numbered SQL files and register them in the changelog — do not modify existing migration files.

## Slash Commands

| Command | Description |
|---|---|
| `/take_notes <text>` | Save a new note (supports `#tags`) |
| `/my_notes [per_page]` | Browse all notes with pagination (optional per_page 1–20, default 5) |
| `/edit_note <id>` | Open modal to edit a note |
| `/delete_note <id>` | Delete a note and its tags |
| `/search_notes <query>` | Full-text search notes (LIKE-based) |
| `/notes_by_tag [tag …]` | Filter notes by one or more tags; space/comma-separated = AND, `\|`-separated = OR; no args lists all your tags with counts |

## CI/CD

GitHub Actions runs on PRs and pushes to `main` with a matrix across Python versions:
1. Python 3.12, 3.13, and 3.14
2. `pip install -r requirements.txt`
3. `pytest tests/ -v`

All tests must pass on all three Python versions before merging. There is no linter configured — maintain consistent style manually.

## Testing Conventions

- Test classes are named `Test<FeatureName>` (e.g., `TestSaveNote`).
- Each test method patches external dependencies (`mysql.connector`, `get_db_connection`, `os.environ`) using `@patch` or `patch.object`. Patch targets use the `app.*` namespace (e.g., `app.database.mysql.connector`).
- Use `MagicMock` for DB cursors and connections.
- Tests are fully isolated — no shared state between test classes.
- `middleware.allowed_user_id` is set directly in the test module (not via env) to `"U_ALLOWED"` so middleware tests don't depend on the environment.
- When adding a new function, add a corresponding `Test<Function>` class in `tests/test_app.py`.

## Security Notes

- All SQL uses parameterized queries — maintain this strictly.
- The bot runs as a non-root `appuser` inside the Docker container.
- Rate limiting prevents command spam.
- Only `ALLOWED_SLACK_USER_ID` can interact with the bot — this is enforced in middleware, not in individual handlers.

## Common Pitfalls

- **Liquibase startup timing**: The `liquibase` container has a 10-second startup delay to avoid a race condition with MySQL initialization. If adding new migration steps, do not remove this delay.
- **Slack modal character limit**: Slack enforces a 3000-character limit on modal text inputs. `MAX_NOTE_LENGTH` must not exceed this.
- **Connection cleanup**: Always release DB connections in `finally` blocks. Missing a release will exhaust the pool (size 5).
- **Tag updates**: When editing a note, always delete-then-reinsert tags. There is no update path for tags.
- **Rate limit cache**: The cache grows without bound up to `RATE_LIMIT_MAX_ENTRIES`. Eviction only happens on overflow — keep this in mind for long-running deployments.
