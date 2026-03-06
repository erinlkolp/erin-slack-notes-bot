# Security Review — Erin Slack Notes Bot

**Date:** 2026-03-06
**Scope:** Full codebase review (`app/`, `migrations/`, `Dockerfile`, `docker-compose.yml`)

---

## Executive Summary

The bot is reasonably well-built for a single-user personal tool. It uses parameterized SQL queries, enforces single-user authorization via middleware, runs as a non-root container user, and escapes user input in Slack mrkdwn output. However, there are several issues ranging from low to high severity that should be addressed.

---

## Findings

### 1. [HIGH] Sensitive Data Logged at Startup

**File:** `app/main.py:39-43`

The database host, port, and presence/absence of credentials are logged at INFO level during startup. While the actual password value is not logged, the host and port are logged in cleartext:

```python
logger.info(f"MYSQL_HOST: {mysql_host}")
logger.info(f"MYSQL_PORT: {mysql_port}")
```

This leaks infrastructure details into log aggregators, container stdout, and any log forwarding pipeline.

**Recommendation:**
- Remove the lines that log `MYSQL_HOST` and `MYSQL_PORT` values directly. The "Set/Missing" pattern used for secrets is fine — apply it to all variables, or remove the database-related log lines entirely. Infrastructure details belong in configuration auditing, not runtime logs.

---

### 2. [HIGH] Error Messages May Leak Internal State

**Files:** `app/database.py`, `app/tags.py`, `app/handlers.py` (throughout)

Database exceptions are logged with `f"Error saving note: {e}"`. The `mysql.connector.Error` exception can contain SQL fragments, table names, column names, and server version information. If logs are accessible to an attacker (e.g. via a log aggregation UI, misconfigured log endpoint, or container stdout in a shared cluster), this exposes the database schema.

**Recommendation:**
- Log exception types and generic messages at ERROR level. Log full exception details (including the message) only at DEBUG level, or use `logger.exception()` gated behind a debug check.

---

### 3. [HIGH] Health Check Endpoint Has No Access Control

**File:** `app/health.py:55`

The health check server binds to `0.0.0.0` and serves on an open HTTP port with no authentication. The `/healthz` endpoint reveals database connectivity status. An attacker with network access can:
- Determine whether the database is up or down.
- Observe the error message from the database driver on failure (line 21: `return False, str(e)`), which could leak connection strings, hostnames, or driver version info.

**Recommendation:**
- Bind the health check server to `127.0.0.1` instead of `0.0.0.0` if it only needs to be accessed locally or by Docker healthchecks. If external access is needed, place it behind a reverse proxy.
- Sanitize the error message returned in the unhealthy response — return a generic "database unavailable" instead of `str(e)`.

---

### 4. [MEDIUM] `private_metadata` in Edit Modal is Client-Controlled

**File:** `app/handlers.py:279-281`

When the edit modal is submitted, the handler trusts `note_id` from `private_metadata`:

```python
metadata = json.loads(view["private_metadata"])
note_id = metadata["note_id"]
```

While Slack signs requests and the note ownership is verified on line 297 (`get_note_by_id(note_id, user_id)`), the `channel_id` from metadata (line 281) is used directly in `client.chat_postEphemeral()` on line 324 without validation. A crafted `private_metadata` could potentially post ephemeral messages to arbitrary channels the bot has access to.

**Recommendation:**
- Validate that `channel_id` from `private_metadata` is a channel the user legitimately initiated the command from. Alternatively, fall back to the user's DM channel (`user_id`) when `channel_id` is absent, and validate any provided channel_id against a known-good value stored server-side (e.g. in a short-lived cache keyed by trigger_id).

---

### 5. [MEDIUM] Pagination Parameters are Client-Controlled

**Files:** `app/handlers.py:426-427, 444-445, 480-481`

Pagination action handlers deserialize `page` and `per_page` from the Slack action payload without validation:

```python
payload = json.loads(body["actions"][0]["value"])
notes, total_count = get_notes_page(user_id, payload["page"], payload["per_page"])
```

A malicious or crafted payload could supply:
- **Negative or zero `page`/`per_page`:** causing unexpected SQL `LIMIT`/`OFFSET` values.
- **Very large `per_page`:** forcing the database to return an unbounded number of rows, potentially causing memory exhaustion or a slow query.

**Recommendation:**
- Validate and clamp `page` (minimum 1) and `per_page` (minimum 1, maximum 20) in every pagination handler, the same way `handle_my_notes` already clamps `per_page` on line 149.

---

### 6. [MEDIUM] Rate Limiting Uses In-Memory State Only

**File:** `app/middleware.py:11, 17-30`

The rate limit cache (`_last_command_time`) is a process-local `defaultdict`. This means:
- Rate limits reset on every restart or deployment.
- If the bot were ever scaled horizontally (multiple processes/containers), rate limiting would not be effective.
- The eviction strategy (line 26-29) only triggers when the dict exceeds 1000 entries, meaning a sustained attack below this threshold accumulates memory indefinitely.

**Recommendation:**
- For a single-user bot this is acceptable, but document the limitation. If the bot ever needs to handle multiple users or run multiple replicas, switch to Redis-backed rate limiting.
- Consider a time-based TTL eviction (e.g. periodic cleanup) rather than only evicting on overflow.

---

### 7. [MEDIUM] No Input Length Validation on Search Queries

**File:** `app/handlers.py:375`

The `/search_notes` command does not limit the length of the search keyword. A user could submit a very long string that gets passed to a `LIKE` query. While parameterized queries prevent SQL injection, an extremely long `LIKE` pattern can be computationally expensive for the database engine.

**Recommendation:**
- Add a maximum length check for the search keyword (e.g. 200 characters) before passing it to `search_notes()`.

---

### 8. [MEDIUM] `channel_name` is Not Escaped in Mrkdwn Output

**File:** `app/blocks.py:70`

In `build_notes_blocks`, `channel_name` from the database is inserted into mrkdwn output without escaping:

```python
channel_suffix = f" — #{channel_name}" if channel_name else ""
```

While `note_text` is properly escaped via `escape_mrkdwn()`, the `channel_name` is not. If a channel name contained mrkdwn-active characters (unlikely but possible with private channel naming), it could cause formatting issues or in theory be used for minor injection.

**Recommendation:**
- Apply `escape_mrkdwn()` to `channel_name` as well: `f" — #{escape_mrkdwn(channel_name)}"`.

---

### 9. [LOW] Mention Handler Reflects User Input

**File:** `app/handlers.py:65`

The `handle_mentions` handler echoes back the user's message text:

```python
say(f"👋 Hi there! I saw you mentioned me. Your message: '{clean_text}'")
```

This is a plain text response (not mrkdwn blocks), so Slack's built-in rendering applies. While the authorization check prevents unauthorized users from triggering this, the reflected content is not escaped. If the response were ever changed to use mrkdwn blocks, this would become a Slack mention/broadcast injection vector.

**Recommendation:**
- Apply `escape_mrkdwn()` to `clean_text` proactively, even though the current response surface is plain text.

---

### 10. [LOW] Dependencies Are Pinned to Exact Versions But Not Hash-Locked

**File:** `requirements.txt`

Dependencies are pinned (`slack-bolt==1.27.0`, `mysql-connector-python==9.4.0`, `pytest==8.4.2`) which is good, but there is no hash verification (`--hash` mode or a lockfile). A compromised PyPI package at the exact pinned version would be installed without detection.

**Recommendation:**
- Generate a `requirements.txt` with `--hash` flags using `pip-compile` from `pip-tools`, or use a lockfile-based tool like `pip-tools`, `poetry`, or `uv` to verify package integrity.
- Additionally, consider separating test dependencies (`pytest`) from production dependencies so that `pytest` is not installed in the production container.

---

### 11. [LOW] Dockerfile Copies Entire Build Context

**File:** `Dockerfile:8`

```dockerfile
COPY . .
```

This copies everything in the repo into the image, including `.git/`, `tests/`, `migrations/`, `CLAUDE.md`, and any `.env` files that may exist locally. A `.dockerignore` file does not appear to exist.

**Recommendation:**
- Create a `.dockerignore` file excluding at minimum: `.git`, `.env`, `.env.*`, `tests/`, `migrations/` (already handled by the liquibase container), `*.md`, `__pycache__`, `*.pyc`.

---

### 12. [LOW] No TLS Enforced on Database Connection by Default

**File:** `app/database.py:39-41`

SSL is only configured when `MYSQL_SSL_CA` is set. In the Docker Compose setup, the bot connects to the database over the Docker internal network without TLS. While Docker's internal network provides some isolation, traffic between containers is unencrypted.

**Recommendation:**
- For production deployments outside of Docker Compose (e.g. cloud-hosted MySQL), enforce `ssl_ca` and `ssl_verify_cert` or require `MYSQL_SSL_CA` to be set. Document that the Docker Compose setup relies on network isolation in lieu of TLS.

---

### 13. [LOW] Database Port Variable Not Validated

**File:** `app/database.py:34`

`MYSQL_PORT` is cast to `int` without validation:

```python
"port": int(mysql_port),
```

If `MYSQL_PORT` is set to a non-numeric value, this raises an unhandled `ValueError` that crashes the application during pool initialization. The outer `try/except` catches `Error` (MySQL-specific), not `ValueError`.

**Recommendation:**
- Validate `MYSQL_PORT` during the environment check in `app/main.py`, or wrap the cast in a try/except with a clear error message.

---

### 14. [LOW] `SYS_NICE` Capability on Database Container

**File:** `docker-compose.yml:57-58`

The `db` container is granted `SYS_NICE`, which allows adjusting process scheduling priorities. While this is used to suppress Percona mbind warnings, it is an additional Linux capability that widens the container's attack surface.

**Recommendation:**
- Document why `SYS_NICE` is needed. Consider whether the mbind warnings can be suppressed via MySQL configuration instead. If the capability is truly required, this is an acceptable trade-off for a development/personal deployment.

---

### 15. [LOW] No CSRF/Replay Protection Beyond Slack's Signing

**General**

The bot relies entirely on Slack's request signing (`signing_secret`) and Socket Mode's WebSocket authentication for request integrity. There is no additional CSRF token or nonce on modal submissions. This is standard for Slack bots and is acceptable, but worth noting:

- If `SLACK_SIGNING_SECRET` is compromised, an attacker could forge any command or action payload.
- Socket Mode mitigates many HTTP-based attacks since the bot does not expose a public HTTP endpoint for Slack events.

**Recommendation:**
- Ensure `SLACK_SIGNING_SECRET` is rotated periodically and stored securely (e.g. in a secrets manager, not in `.env` files committed to source control).

---

### 16. [INFO] Module-Level Code Execution in `main.py`

**File:** `app/main.py:21-98`

Environment validation, Slack app initialization, database pool creation, and handler registration all run at module import time (outside `main()`). This means importing `app.main` in a test or REPL triggers real network calls (`auth_test`, `init_db_pool`) and may `sys.exit(1)`.

**Recommendation:**
- Move all initialization logic inside `main()` or a dedicated `init()` function. This improves testability and prevents side effects on import.

---

## Summary Table

| # | Severity | Finding | File(s) |
|---|----------|---------|---------|
| 1 | HIGH | DB host/port logged in cleartext | `main.py` |
| 2 | HIGH | Exception messages may leak schema | `database.py`, `tags.py` |
| 3 | HIGH | Health endpoint exposes DB status + errors | `health.py` |
| 4 | MEDIUM | `channel_id` from modal metadata not validated | `handlers.py` |
| 5 | MEDIUM | Pagination params not validated/clamped | `handlers.py` |
| 6 | MEDIUM | Rate limiting is in-memory only | `middleware.py` |
| 7 | MEDIUM | No length limit on search queries | `handlers.py` |
| 8 | MEDIUM | `channel_name` not escaped in mrkdwn | `blocks.py` |
| 9 | LOW | Reflected user input in mention handler | `handlers.py` |
| 10 | LOW | No hash verification on dependencies | `requirements.txt` |
| 11 | LOW | `COPY .` with no `.dockerignore` | `Dockerfile` |
| 12 | LOW | No TLS on DB connection by default | `database.py` |
| 13 | LOW | `MYSQL_PORT` not validated as integer | `database.py` |
| 14 | LOW | `SYS_NICE` capability on DB container | `docker-compose.yml` |
| 15 | LOW | No CSRF beyond Slack signing | General |
| 16 | INFO | Module-level side effects | `main.py` |

---

## What the Code Does Well

- **Parameterized SQL everywhere** — no string interpolation in queries.
- **Single-user authorization** enforced at the middleware layer, not per-handler.
- **`escape_mrkdwn()`** properly neutralizes Slack mention/broadcast injection on note text.
- **Non-root container user** (`appuser`).
- **Connection cleanup in `finally` blocks** — prevents pool exhaustion.
- **LIKE wildcards escaped** in `search_notes()` (backslash, `%`, `_`).
- **Socket Mode** — no public HTTP ingress needed for Slack events, reducing attack surface.
- **Pinned dependency versions** — reproducible builds.
- **Graceful shutdown** with signal handlers.
