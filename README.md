# Erin Slack Notes Bot

A Slack bot that lets you save, retrieve, and browse personal notes directly from Slack using slash commands. Built with Python, Slack Bolt, and MySQL, and fully containerized with Docker Compose.

## Features

- **Save notes** with `/take_notes` — stores text along with timestamp and channel context
- **Browse notes** with `/my_notes` — paginated view with interactive Previous/Next buttons
- **Edit notes** with `/edit_note` — update any note's text (tags are re-parsed automatically)
- **Delete notes** with `/delete_note` — remove a note and its tags
- **Search notes** with `/search_notes` — find notes by keyword with paginated results
- **Tag support** with `/notes_by_tag` — organize and filter notes using `#hashtags`
- **Connection pooling** — reuses MySQL connections for lower latency and fewer connections
- **Health check endpoint** — HTTP `/healthz` endpoint for container orchestration
- **Single-user mode** — restricts access to one authorized Slack user
- **Rate limiting** — 1-second cooldown between commands to prevent spam
- **Persistent storage** — all notes stored in a MySQL database
- **Configurable logging** — adjustable log levels (DEBUG, INFO, WARNING, ERROR, CRITICAL)

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/)
- A Slack workspace where you can create apps
- Slack app credentials (Bot Token, Signing Secret, App Token)

## Slack App Setup

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and create a new app
2. Enable **Socket Mode** and generate an App-Level Token (`xapp-...`)
3. Under **OAuth & Permissions**, add these Bot Token Scopes:
   - `chat:write`
   - `commands`
   - `app_mentions:read`
4. Install the app to your workspace and copy the **Bot User OAuth Token** (`xoxb-...`)
5. Copy the **Signing Secret** from the Basic Information page
6. Register these slash commands:
   - `/take_notes` — Save a new note
   - `/my_notes` — View your saved notes
   - `/edit_note` — Edit an existing note
   - `/delete_note` — Delete a note
   - `/search_notes` — Search notes by keyword
   - `/notes_by_tag` — Browse notes by tag
7. Find your Slack User ID (click your profile > three dots > "Copy member ID")

## Installation

1. **Clone the repository:**

   ```bash
   git clone https://github.com/erinlkolp/erin-slack-notes-bot.git
   cd erin-slack-notes-bot
   ```

2. **Create your environment file:**

   ```bash
   cp .env.example .env
   ```

3. **Edit `.env`** with your actual credentials:

   ```env
   # Slack credentials
   SLACK_BOT_TOKEN=xoxb-your-bot-token
   SLACK_SIGNING_SECRET=your-signing-secret
   SLACK_APP_TOKEN=xapp-your-app-token
   ALLOWED_SLACK_USER_ID=U12345678

   # MySQL credentials
   MYSQL_HOST=db
   MYSQL_PORT=3306
   MYSQL_USER=slackbot
   MYSQL_PASSWORD=change-me-to-a-strong-password
   MYSQL_DATABASE=slack_notes
   MYSQL_ROOT_PASSWORD=change-me-to-a-strong-root-password
   ```

## Running the Bot

**Start all services:**

```bash
docker compose up -d
```

Docker Compose will:
1. Build the bot container from the Dockerfile (Python 3.12-slim)
2. Start a Percona MySQL 8.0 database
3. Wait for the database health check to pass
4. Automatically initialize the database schema
5. Launch the bot and connect to Slack via Socket Mode

**View logs:**

```bash
docker compose logs -f slackbot
```

**Stop all services:**

```bash
docker compose down
```

**Restart the bot only:**

```bash
docker compose restart slackbot
```

## Building

To rebuild the bot image after making code changes:

```bash
docker compose build
docker compose up -d
```

Or in a single step:

```bash
docker compose up -d --build
```

### Running Without Docker

If you prefer to run without Docker, you'll need Python 3.12+ and a running MySQL instance.

1. **Install dependencies:**

   ```bash
   pip install -r requirements.txt
   ```

2. **Set environment variables** (export them or use a tool like `direnv`):

   ```bash
   export SLACK_BOT_TOKEN=xoxb-...
   export SLACK_SIGNING_SECRET=...
   export SLACK_APP_TOKEN=xapp-...
   export ALLOWED_SLACK_USER_ID=U...
   export MYSQL_HOST=localhost
   export MYSQL_PORT=3306
   export MYSQL_USER=slackbot
   export MYSQL_PASSWORD=your-password
   export MYSQL_DATABASE=slack_notes
   ```

3. **Initialize the database** by running the migration SQL against your MySQL server:

   ```bash
   mysql -u root -p < migrations/001-initial-schema.sql
   ```

4. **Start the bot:**

   ```bash
   python -m app.main
   ```

## Usage

### Saving a Note

```
/take_notes Buy groceries after work
```

The bot responds with a confirmation showing the note ID, text, timestamp, and channel.

### Viewing Notes

```
/my_notes
```

Shows your most recent notes (5 per page by default) with Previous/Next buttons for pagination.

You can specify a custom page size (1-20):

```
/my_notes 10
```

### Editing a Note

```
/edit_note 42
```

Opens a modal pre-filled with the current text of note #42. Edit and click **Save** to update. Tags are automatically re-parsed from the new text.

### Deleting a Note

```
/delete_note 42
```

Permanently deletes note #42 and all its associated tags.

### Searching Notes

```
/search_notes groceries
```

Finds all notes containing "groceries" with paginated results.

### Browsing by Tag

```
/notes_by_tag
```

Lists all your tags with note counts.

```
/notes_by_tag work
```

Shows all notes tagged `#work`.

```
/notes_by_tag work important
```

Shows notes that carry **both** `#work` and `#important` (AND semantics).

```
/notes_by_tag work|personal
```

Shows notes tagged with **either** `#work` or `#personal` (OR semantics).

### Limits

| Constraint         | Value              |
| ------------------ | ------------------ |
| Max note length    | 3,000 characters   |
| Default page size  | 5 notes            |
| Max page size      | 20 notes           |
| Rate limit         | 1 second between commands |

## Configuration

| Variable               | Required | Default | Description                                      |
| ---------------------- | -------- | ------- | ------------------------------------------------ |
| `SLACK_BOT_TOKEN`      | Yes      | —       | Bot token (`xoxb-...`)                           |
| `SLACK_SIGNING_SECRET` | Yes      | —       | Request signing secret                           |
| `SLACK_APP_TOKEN`      | Yes      | —       | App-level token for Socket Mode (`xapp-...`)     |
| `ALLOWED_SLACK_USER_ID`| Yes      | —       | Slack user ID authorized to use the bot          |
| `MYSQL_HOST`           | No       | `localhost` | MySQL hostname (hardcoded to `db` in `docker-compose.yml`) |
| `MYSQL_PORT`           | No       | `3306`  | MySQL port                                       |
| `MYSQL_USER`           | Yes      | —       | MySQL username                                   |
| `MYSQL_PASSWORD`       | Yes      | —       | MySQL password                                   |
| `MYSQL_DATABASE`       | Yes      | —       | MySQL database name                              |
| `MYSQL_ROOT_PASSWORD`  | Yes      | —       | MySQL root password (Docker setup)               |
| `MYSQL_SSL_CA`         | No       | —       | Path to SSL CA certificate for encrypted DB connections |
| `HEALTH_CHECK_PORT`    | No       | `8080`  | Port for the `/healthz` HTTP endpoint            |
| `LOG_LEVEL`            | No       | `INFO`  | Logging verbosity                                |

## Tech Stack

- **Python 3.14** — application runtime
- **[Slack Bolt](https://slack.dev/bolt-python/)** — Slack app framework
- **MySQL (Percona Server 8.0)** — note storage
- **Liquibase** — automated db schema migrations
- **Docker & Docker Compose** — containerization and orchestration

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.
