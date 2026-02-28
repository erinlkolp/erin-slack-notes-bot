import os
import logging
import signal
import sys
import time
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
RATE_LIMIT_SECONDS = 5
RATE_LIMIT_MAX_ENTRIES = 1000
_last_command_time = defaultdict(float)

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
    """Create and return a MySQL database connection"""
    try:
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
        connection = mysql.connector.connect(**connect_args)
        return connection
    except Error as e:
        logger.error(f"MySQL connection error: {e}")
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

        create_table_query = """
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

        cursor.execute(create_table_query)
        connection.commit()
        logger.info("Database table 'notes' ready")
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
def handle_take_notes(ack, respond, command, client, logger):
    """Handle /take_notes command"""
    try:
        ack()  # Must acknowledge the command

        user_id = command.get('user_id')

        # Only respond to the allowed user
        if user_id != allowed_user_id:
            respond("🚫 Sorry, this bot is restricted to a specific user.")
            return

        if check_rate_limit(user_id, "take_notes"):
            respond("⏳ Please wait a few seconds before sending another command.")
            return

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
            response = f"✅ Note saved successfully!\n📝 Note ID: {note_id}\n👤 User: {user_name}\n📄 Note: \"{note_text}\"\n🕐 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            if channel_name:
                response += f"\n📍 Channel: #{channel_name}"
        else:
            response = "❌ Sorry, there was an error saving your note. Please check the database connection."
        
        respond(response)
        logger.info(f"Note saved for user {user_name}")
        
    except Exception as e:
        logger.error(f"Error handling /take_notes command: {e}")
        respond("❌ An error occurred while saving your note. Please try again.")

# Add a command to retrieve recent notes
@app.command("/my_notes")  
def handle_my_notes(ack, respond, command, logger):
    """Handle /my_notes command to retrieve user's recent notes"""
    try:
        ack()

        user_id = command.get('user_id')

        # Only respond to the allowed user
        if user_id != allowed_user_id:
            respond("🚫 Sorry, this bot is restricted to a specific user.")
            return

        if check_rate_limit(user_id, "my_notes"):
            respond("⏳ Please wait a few seconds before sending another command.")
            return

        user_name = command.get('user_name', 'Unknown')
        limit_text = command.get('text', '5').strip()
        
        # Parse limit (default to 5)
        try:
            limit = int(limit_text) if limit_text.isdigit() else 5
            limit = min(limit, 20)  # Cap at 20 notes
        except (ValueError, TypeError):
            limit = 5
        
        # Get user's recent notes
        connection = None
        cursor = None
        try:
            connection = get_db_connection()
            if connection is None:
                respond("❌ Database connection error")
                return

            cursor = connection.cursor()

            query = """
            SELECT id, note_text, created_at, channel_name
            FROM notes
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT %s
            """

            cursor.execute(query, (user_id, limit))
            notes = cursor.fetchall()

            if not notes:
                respond(f"📝 No notes found for {user_name}")
                return

            response = f"📚 Your last {len(notes)} notes:\n\n"

            for note_id, note_text, created_at, channel_name in notes:
                # Truncate long notes
                display_text = note_text if len(note_text) <= 100 else note_text[:97] + "..."
                channel_info = f" (#{channel_name})" if channel_name else ""
                response += f"**#{note_id}** - {created_at.strftime('%m/%d %H:%M')}{channel_info}\n{display_text}\n\n"

            respond(response)

        except Error as e:
            logger.error(f"Database error retrieving notes: {e}")
            respond("❌ Error retrieving notes from database")
        finally:
            if cursor:
                cursor.close()
            if connection and connection.is_connected():
                connection.close()
                
    except Exception as e:
        logger.error(f"Error handling /my_notes command: {e}")
        respond("❌ An error occurred while retrieving your notes.")
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