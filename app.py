import os
import re
import json
import logging
import signal
import sys
import time
import functools
import mysql.connector
from mysql.connector import Error
from collections import defaultdict
from datetime import datetime
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# Set up logging - configurable via environment variable
log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, log_level, logging.INFO))
logger = logging.getLogger(__name__)

MAX_NOTE_LENGTH = 4000
NOTES_PER_PAGE = 5
RATE_LIMIT_SECONDS = 5
RATE_LIMIT_MAX_ENTRIES = 1000
DB_CONNECT_MAX_RETRIES = 3
DB_CONNECT_BASE_DELAY = 1  # seconds, doubles each retry
_last_command_time = defaultdict(float)

TAG_PATTERN = re.compile(r"#(\w+)")

# Load environment variables
logger.info("Checking environment variables...")
bot_token = os.environ.get("SLACK_BOT_TOKEN")
signing_secret = os.environ.get("SLACK_SIGNING_SECRET")
app_token = os.environ.get("SLACK_APP_TOKEN")

allowed_user_id = os.environ.get("ALLOWED_SLACK_USER_ID")

logger.info(f"SLACK_BOT_TOKEN: {'Set' if bot_token else 'Missing'}")
logger.info(f"SLACK_SIGNING_SECRET: {'Set' if signing_secret else 'Missing'}")
logger.info(f"SLACK_APP_TOKEN: {'Set' if app_token else 'Missing'}")
logger.info(f"ALLOWED_SLACK_USER_ID: {'Set' if allowed_user_id else 'Missing'}")

# Check MySQL environment variables
mysql_host = os.environ.get("MYSQL_HOST", "localhost")
mysql_port = os.environ.get("MYSQL_PORT", "3306")
mysql_database = os.environ.get("MYSQL_DATABASE")
mysql_user = os.environ.get("MYSQL_USER")
mysql_password = os.environ.get("MYSQL_PASSWORD")

logger.info(f"MYSQL_HOST: {mysql_host}")
logger.info(f"MYSQL_PORT: {mysql_port}")
logger.info(f"MYSQL_DATABASE: {'Set' if mysql_database else 'Missing'}")
logger.info(f"MYSQL_USER: {'Set' if mysql_user else 'Missing'}")
logger.info(f"MYSQL_PASSWORD: {'Set' if mysql_password else 'Missing'}")

if not all([bot_token, signing_secret, app_token, allowed_user_id]):
    logger.error(
        "Missing required Slack environment variables. "
        "Please set SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET, SLACK_APP_TOKEN, and ALLOWED_SLACK_USER_ID."
    )
    exit(1)

if not all([mysql_database, mysql_user, mysql_password]):
    logger.error(
        "Missing required MySQL environment variables. "
        "Please set MYSQL_DATABASE, MYSQL_USER, and MYSQL_PASSWORD."
    )
    exit(1)

# Initialize the Slack app
try:
    app = App(
        token=bot_token,
        signing_secret=signing_secret
    )
    logger.info("Slack app initialized successfully")
except Exception as e:
    logger.error(f"Failed to initialize Slack app: {e}")
    exit(1)

# Test connection by trying to get bot info
try:
    auth_result = app.client.auth_test()
    logger.info(f"Bot authenticated as: {auth_result['user']} in team: {auth_result['team']}")
except Exception as e:
    logger.error(f"Authentication failed: {e}")
    logger.error("Check your SLACK_BOT_TOKEN - it should start with 'xoxb-'")
    exit(1)

# Database connection and setup
def get_db_connection():
    """Create and return a MySQL database connection with retry and exponential backoff.

    Retries up to DB_CONNECT_MAX_RETRIES times with exponential backoff
    (1s, 2s, 4s …) so the bot can recover from transient database outages
    without requiring a restart.
    """
    ssl_ca = os.environ.get("MYSQL_SSL_CA")
    connect_args = {
        "host": mysql_host,
        "port": mysql_port,
        "database": mysql_database,
        "user": mysql_user,
        "password": mysql_password,
    }
    if ssl_ca:
        connect_args["ssl_ca"] = ssl_ca
        connect_args["ssl_verify_cert"] = True

    last_error = None
    for attempt in range(DB_CONNECT_MAX_RETRIES):
        try:
            connection = mysql.connector.connect(**connect_args)
            if attempt > 0:
                logger.info(f"Database connection succeeded on attempt {attempt + 1}")
            return connection
        except Error as e:
            last_error = e
            if attempt < DB_CONNECT_MAX_RETRIES - 1:
                delay = DB_CONNECT_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    f"MySQL connection attempt {attempt + 1}/{DB_CONNECT_MAX_RETRIES} "
                    f"failed: {e}. Retrying in {delay}s..."
                )
                time.sleep(delay)

    logger.error(f"MySQL connection failed after {DB_CONNECT_MAX_RETRIES} attempts: {last_error}")
    return None

def setup_database():
    """Create the notes table if it doesn't exist"""
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        if connection is None:
            return False

        cursor = connection.cursor()

        create_notes_query = """
        CREATE TABLE IF NOT EXISTS notes (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id VARCHAR(255) NOT NULL,
            username VARCHAR(255),
            note_text TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            channel_id VARCHAR(255),
            channel_name VARCHAR(255),
            INDEX idx_user_created (user_id, created_at)
        )
        """

        create_tags_query = """
        CREATE TABLE IF NOT EXISTS note_tags (
            id INT AUTO_INCREMENT PRIMARY KEY,
            note_id INT NOT NULL,
            tag VARCHAR(255) NOT NULL,
            INDEX idx_tag (tag),
            INDEX idx_note_id (note_id),
            FOREIGN KEY (note_id) REFERENCES notes(id) ON DELETE CASCADE
        )
        """

        cursor.execute(create_notes_query)
        cursor.execute(create_tags_query)
        connection.commit()
        logger.info("Database tables 'notes' and 'note_tags' ready")
        return True

    except Error as e:
        logger.error(f"Database setup error: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if connection and connection.is_connected():
            connection.close()

def check_rate_limit(user_id, command_name):
    """Returns True if the user is rate-limited, False if allowed."""
    key = f"{user_id}:{command_name}"
    now = time.monotonic()
    last = _last_command_time[key]
    if now - last < RATE_LIMIT_SECONDS:
        return True
    _last_command_time[key] = now
    # Evict stale entries to prevent unbounded memory growth
    if len(_last_command_time) > RATE_LIMIT_MAX_ENTRIES:
        stale_keys = [
            k for k, v in _last_command_time.items()
            if now - v > RATE_LIMIT_SECONDS
        ]
        for k in stale_keys:
            del _last_command_time[k]
    return False


def require_allowed_user(command_name=None):
    """Decorator that enforces single-user authorization and optional rate limiting.

    For slash-command handlers (ack/respond/command style), it extracts user_id
    from the ``command`` dict, calls ``ack()``, checks authorization, and
    optionally checks the per-command rate limit before calling the wrapped
    handler.

    Args:
        command_name: If provided, rate-limiting is applied using this key.
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            # Slack Bolt injects these as keyword arguments
            ack = kwargs.get("ack")
            respond = kwargs.get("respond")
            command = kwargs.get("command")
            body = kwargs.get("body")

            # Determine user_id from command dict or body dict
            if command:
                user_id = command.get("user_id")
            elif body:
                user_id = body.get("user", {}).get("id")
            else:
                user_id = None

            # Acknowledge the request if ack is available
            if ack:
                ack()

            # Authorization check
            if user_id != allowed_user_id:
                if respond:
                    respond("🚫 Sorry, this bot is restricted to a specific user.")
                return

            # Rate-limit check (only when command_name is specified)
            if command_name and check_rate_limit(user_id, command_name):
                if respond:
                    respond("⏳ Please wait a few seconds before sending another command.")
                return

            return fn(*args, **kwargs)
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Tag helpers
# ---------------------------------------------------------------------------

def parse_tags(text):
    """Extract #hashtags from note text. Returns a list of lowercase tag strings."""
    return list(dict.fromkeys(tag.lower() for tag in TAG_PATTERN.findall(text)))


def save_tags(note_id, tags):
    """Persist a list of tags for the given note_id."""
    if not tags:
        return
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        if connection is None:
            return
        cursor = connection.cursor()
        cursor.executemany(
            "INSERT INTO note_tags (note_id, tag) VALUES (%s, %s)",
            [(note_id, tag) for tag in tags],
        )
        connection.commit()
    except Error as e:
        logger.error(f"Error saving tags: {e}")
    finally:
        if cursor:
            cursor.close()
        if connection and connection.is_connected():
            connection.close()


def get_notes_by_tag(user_id, tag, page, per_page):
    """Fetch a page of notes that have a given tag. Returns (notes_list, total_count)."""
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        if connection is None:
            return None, 0
        cursor = connection.cursor()

        cursor.execute(
            "SELECT COUNT(*) FROM notes n "
            "JOIN note_tags t ON n.id = t.note_id "
            "WHERE n.user_id = %s AND t.tag = %s",
            (user_id, tag.lower()),
        )
        total_count = cursor.fetchone()[0]

        offset = (page - 1) * per_page
        cursor.execute(
            "SELECT n.id, n.note_text, n.created_at, n.channel_name "
            "FROM notes n JOIN note_tags t ON n.id = t.note_id "
            "WHERE n.user_id = %s AND t.tag = %s "
            "ORDER BY n.created_at DESC LIMIT %s OFFSET %s",
            (user_id, tag.lower(), per_page, offset),
        )
        notes = cursor.fetchall()
        return notes, total_count
    except Error as e:
        logger.error(f"Database error in get_notes_by_tag: {e}")
        return None, 0
    finally:
        if cursor:
            cursor.close()
        if connection and connection.is_connected():
            connection.close()


def get_user_tags(user_id):
    """Return all tags used by a user with their note counts, ordered by frequency."""
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        if connection is None:
            return None
        cursor = connection.cursor()
        cursor.execute(
            "SELECT t.tag, COUNT(*) as cnt FROM note_tags t "
            "JOIN notes n ON n.id = t.note_id "
            "WHERE n.user_id = %s GROUP BY t.tag ORDER BY cnt DESC",
            (user_id,),
        )
        return cursor.fetchall()
    except Error as e:
        logger.error(f"Database error in get_user_tags: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if connection and connection.is_connected():
            connection.close()


def save_note(user_id, username, note_text, channel_id=None, channel_name=None):
    """Save a note to the database"""
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        if connection is None:
            return False

        cursor = connection.cursor()

        insert_query = """
        INSERT INTO notes (user_id, username, note_text, channel_id, channel_name)
        VALUES (%s, %s, %s, %s, %s)
        """

        cursor.execute(insert_query, (user_id, username, note_text, channel_id, channel_name))
        connection.commit()

        note_id = cursor.lastrowid
        logger.info(f"Note saved with ID: {note_id}")
        return note_id

    except Error as e:
        logger.error(f"Error saving note: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if connection and connection.is_connected():
            connection.close()

def get_notes_page(user_id, page, per_page):
    """Fetch a single page of notes for a user. Returns (notes_list, total_count)."""
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        if connection is None:
            return None, 0

        cursor = connection.cursor()

        # Get total count
        cursor.execute("SELECT COUNT(*) FROM notes WHERE user_id = %s", (user_id,))
        total_count = cursor.fetchone()[0]

        # Get the requested page
        offset = (page - 1) * per_page
        cursor.execute(
            "SELECT id, note_text, created_at, channel_name "
            "FROM notes WHERE user_id = %s ORDER BY created_at DESC LIMIT %s OFFSET %s",
            (user_id, per_page, offset),
        )
        notes = cursor.fetchall()

        return notes, total_count

    except Error as e:
        logger.error(f"Database error in get_notes_page: {e}")
        return None, 0
    finally:
        if cursor:
            cursor.close()
        if connection and connection.is_connected():
            connection.close()


def build_notes_blocks(notes, page, per_page, total_count):
    """Build Slack Block Kit blocks for a page of notes with navigation."""
    total_pages = max(1, (total_count + per_page - 1) // per_page)

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Your Notes"},
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Page {page} of {total_pages}  |  {total_count} notes total  |  {per_page} per page",
                }
            ],
        },
        {"type": "divider"},
    ]

    for note_id, note_text, created_at, channel_name in notes:
        display_text = note_text if len(note_text) <= 200 else note_text[:197] + "..."
        channel_info = f"  #{channel_name}" if channel_name else ""
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*#{note_id}* — {created_at.strftime('%m/%d/%Y %H:%M')}{channel_info}\n{display_text}",
                },
            }
        )
        blocks.append({"type": "divider"})

    # Navigation buttons
    nav_buttons = []
    if page > 1:
        nav_buttons.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "< Previous"},
                "action_id": "notes_prev_page",
                "value": json.dumps({"page": page - 1, "per_page": per_page}),
            }
        )
    if page < total_pages:
        nav_buttons.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Next >"},
                "action_id": "notes_next_page",
                "value": json.dumps({"page": page + 1, "per_page": per_page}),
            }
        )

    if nav_buttons:
        blocks.append({"type": "actions", "elements": nav_buttons})

    return blocks


# Test database connection and setup
logger.info("Testing database connection...")
if setup_database():
    logger.info("Database connection successful")
else:
    logger.error("Database connection failed. Make sure MySQL is running and credentials are correct.")
    exit(1)

# Listen for messages (simplified approach)
@app.message(".*")
def handle_message_events(message, say, logger):
    """Handle all message events"""
    try:
        user_id = message.get('user')
        text = message.get('text', '')
        channel = message.get('channel')

        # Skip messages from bots (including this bot)
        if message.get('bot_id') or message.get('subtype') == 'bot_message':
            return

        # Only respond to the allowed user
        if user_id != allowed_user_id:
            return

        logger.debug(f"Message from user {user_id} in channel {channel}")

        # Simple confirmation response
        say("✅ Message received!")

    except Exception as e:
        logger.error(f"Error handling message: {e}")

# Listen for app mentions
@app.event("app_mention")
def handle_mentions(event, say, logger):
    """Handle app mentions"""
    try:
        user = event.get('user')
        text = event.get('text', '')

        # Only respond to the allowed user
        if user != allowed_user_id:
            logger.info(f"Ignoring mention from unauthorized user: {user}")
            return

        logger.info(f"Bot mentioned by user {user}")

        # Clean up the mention from the text
        clean_text = text.split('>', 1)[-1].strip() if '>' in text else text

        say(f"👋 Hi there! I saw you mentioned me. Your message: '{clean_text}'")

    except Exception as e:
        logger.error(f"Error handling mention: {e}")

# Add a slash command for taking notes
@app.command("/take_notes")
@require_allowed_user(command_name="take_notes")
def handle_take_notes(ack, respond, command, client, logger):
    """Handle /take_notes command"""
    try:
        user_id = command.get('user_id')
        user_name = command.get('user_name', 'Unknown')
        note_text = command.get('text', '').strip()
        channel_id = command.get('channel_id')

        # Get channel name if possible
        channel_name = None
        try:
            if channel_id:
                channel_info = client.conversations_info(channel=channel_id)
                channel_name = channel_info['channel']['name']
        except Exception as e:
            logger.warning(f"Could not fetch channel name for {channel_id}: {e}")

        if not note_text:
            respond("❌ Please provide some text to save as a note.\nUsage: `/take_notes Your note text here`")
            return

        if len(note_text) > MAX_NOTE_LENGTH:
            respond(f"❌ Note is too long ({len(note_text)} characters). Maximum is {MAX_NOTE_LENGTH} characters.")
            return

        # Save the note to database
        note_id = save_note(user_id, user_name, note_text, channel_id, channel_name)

        if note_id:
            # Extract and persist tags from the note text
            tags = parse_tags(note_text)
            if tags:
                save_tags(note_id, tags)

            response = f"✅ Note saved successfully!\n📝 Note ID: {note_id}\n👤 User: {user_name}\n📄 Note: \"{note_text}\"\n🕐 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            if channel_name:
                response += f"\n📍 Channel: #{channel_name}"
            if tags:
                response += f"\n🏷️ Tags: {', '.join('#' + t for t in tags)}"
        else:
            response = "❌ Sorry, there was an error saving your note. Please check the database connection."

        respond(response)
        logger.info(f"Note saved for user {user_name}")

    except Exception as e:
        logger.error(f"Error handling /take_notes command: {e}")
        respond("❌ An error occurred while saving your note. Please try again.")

# Add a command to retrieve recent notes
@app.command("/my_notes")
@require_allowed_user(command_name="my_notes")
def handle_my_notes(ack, respond, command, logger):
    """Handle /my_notes command to retrieve user's notes with pagination.

    Usage:
        /my_notes          - show page 1 (default 5 per page)
        /my_notes 10       - show page 1 with 10 per page
    """
    try:
        user_id = command.get('user_id')
        user_name = command.get('user_name', 'Unknown')
        text = command.get('text', '').strip()

        # Parse optional per_page from command text (default NOTES_PER_PAGE)
        try:
            per_page = int(text) if text.isdigit() else NOTES_PER_PAGE
            per_page = max(1, min(per_page, 20))
        except (ValueError, TypeError):
            per_page = NOTES_PER_PAGE

        page = 1
        notes, total_count = get_notes_page(user_id, page, per_page)

        if notes is None:
            respond("Database connection error.")
            return

        if not notes:
            respond(f"No notes found for {user_name}.")
            return

        blocks = build_notes_blocks(notes, page, per_page, total_count)
        respond(blocks=blocks)

    except Exception as e:
        logger.error(f"Error handling /my_notes command: {e}")
        respond("An error occurred while retrieving your notes.")


# Tag-based note retrieval
@app.command("/notes_by_tag")
@require_allowed_user(command_name="notes_by_tag")
def handle_notes_by_tag(ack, respond, command, logger):
    """Handle /notes_by_tag command.

    Usage:
        /notes_by_tag              - list all your tags
        /notes_by_tag meeting      - show notes tagged #meeting
        /notes_by_tag #meeting     - same (leading # is stripped)
    """
    try:
        user_id = command.get('user_id')
        text = command.get('text', '').strip().lstrip('#').lower()

        # No argument → show all tags the user has used
        if not text:
            tags = get_user_tags(user_id)
            if tags is None:
                respond("❌ Database connection error.")
                return
            if not tags:
                respond("No tags found. Add tags to notes with `#tagname` in `/take_notes`.")
                return
            lines = [f"• *#{tag}* — {count} note{'s' if count != 1 else ''}" for tag, count in tags]
            respond(f"🏷️ *Your tags:*\n" + "\n".join(lines))
            return

        page = 1
        per_page = NOTES_PER_PAGE
        notes, total_count = get_notes_by_tag(user_id, text, page, per_page)

        if notes is None:
            respond("❌ Database connection error.")
            return

        if not notes:
            respond(f"No notes found with tag *#{text}*.")
            return

        blocks = build_notes_blocks(notes, page, per_page, total_count)
        # Override header to show the tag filter
        blocks[0] = {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Notes tagged #{text}"},
        }
        # Swap action_ids so pagination stays within the tag filter
        for block in blocks:
            if block.get("type") == "actions":
                for element in block["elements"]:
                    payload = json.loads(element["value"])
                    payload["tag"] = text
                    element["value"] = json.dumps(payload)
                    if element["action_id"] == "notes_prev_page":
                        element["action_id"] = "tag_notes_prev_page"
                    elif element["action_id"] == "notes_next_page":
                        element["action_id"] = "tag_notes_next_page"

        respond(blocks=blocks)

    except Exception as e:
        logger.error(f"Error handling /notes_by_tag command: {e}")
        respond("❌ An error occurred while retrieving your notes.")


@app.action("notes_prev_page")
@app.action("notes_next_page")
@require_allowed_user()
def handle_notes_pagination(ack, body, respond, logger):
    """Handle Previous / Next button clicks for note pagination."""
    try:
        user_id = body["user"]["id"]
        action = body["actions"][0]
        payload = json.loads(action["value"])
        page = payload["page"]
        per_page = payload["per_page"]

        notes, total_count = get_notes_page(user_id, page, per_page)

        if notes is None:
            return

        blocks = build_notes_blocks(notes, page, per_page, total_count)

        # Use respond with replace_original to update the ephemeral message
        respond(blocks=blocks, replace_original=True)

    except Exception as e:
        logger.error(f"Error handling notes pagination: {e}")


@app.action("tag_notes_prev_page")
@app.action("tag_notes_next_page")
@require_allowed_user()
def handle_tag_notes_pagination(ack, body, respond, logger):
    """Handle Previous / Next button clicks for tag-filtered pagination."""
    try:
        user_id = body["user"]["id"]
        action = body["actions"][0]
        payload = json.loads(action["value"])
        page = payload["page"]
        per_page = payload["per_page"]
        tag = payload["tag"]

        notes, total_count = get_notes_by_tag(user_id, tag, page, per_page)

        if notes is None:
            return

        blocks = build_notes_blocks(notes, page, per_page, total_count)
        blocks[0] = {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Notes tagged #{tag}"},
        }
        # Ensure pagination buttons keep the tag context
        for block in blocks:
            if block.get("type") == "actions":
                for element in block["elements"]:
                    p = json.loads(element["value"])
                    p["tag"] = tag
                    element["value"] = json.dumps(p)
                    if element["action_id"] == "notes_prev_page":
                        element["action_id"] = "tag_notes_prev_page"
                    elif element["action_id"] == "notes_next_page":
                        element["action_id"] = "tag_notes_next_page"

        respond(blocks=blocks, replace_original=True)

    except Exception as e:
        logger.error(f"Error handling tag notes pagination: {e}")


@app.error
def global_error_handler(error, body, logger):
    logger.error(f"Global error: {error}")

def main():
    """Main function to start the bot"""
    try:
        # Create socket mode handler
        handler = SocketModeHandler(app, app_token)

        def shutdown_handler(signum, frame):
            logger.info("Received shutdown signal, stopping bot...")
            handler.close()
            sys.exit(0)

        signal.signal(signal.SIGTERM, shutdown_handler)
        signal.signal(signal.SIGINT, shutdown_handler)

        logger.info("Starting Slack bot...")

        # Start the handler
        handler.start()

    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Error starting bot: {e}")
        logger.exception(e)

if __name__ == "__main__":
    main()