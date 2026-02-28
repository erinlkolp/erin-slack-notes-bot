import os
import logging
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
_last_command_time = defaultdict(float)

# Print environment variables for debugging (without exposing tokens)
print("🔍 Checking environment variables...")
bot_token = os.environ.get("SLACK_BOT_TOKEN")
signing_secret = os.environ.get("SLACK_SIGNING_SECRET")
app_token = os.environ.get("SLACK_APP_TOKEN")

allowed_user_id = os.environ.get("ALLOWED_SLACK_USER_ID")

print(f"SLACK_BOT_TOKEN: {'✅ Set' if bot_token else '❌ Missing'}")
print(f"SLACK_SIGNING_SECRET: {'✅ Set' if signing_secret else '❌ Missing'}")
print(f"SLACK_APP_TOKEN: {'✅ Set' if app_token else '❌ Missing'}")
print(f"ALLOWED_SLACK_USER_ID: {'✅ Set' if allowed_user_id else '❌ Missing'}")

# Check MySQL environment variables
mysql_host = os.environ.get("MYSQL_HOST", "localhost")
mysql_port = os.environ.get("MYSQL_PORT", "3306")
mysql_database = os.environ.get("MYSQL_DATABASE")
mysql_user = os.environ.get("MYSQL_USER")
mysql_password = os.environ.get("MYSQL_PASSWORD")

print(f"MYSQL_HOST: {mysql_host}")
print(f"MYSQL_PORT: {mysql_port}")
print(f"MYSQL_DATABASE: {'✅ Set' if mysql_database else '❌ Missing'}")
print(f"MYSQL_USER: {'✅ Set' if mysql_user else '❌ Missing'}")
print(f"MYSQL_PASSWORD: {'✅ Set' if mysql_password else '❌ Missing'}")

if not all([bot_token, signing_secret, app_token, allowed_user_id]):
    print("❌ Missing required Slack environment variables!")
    print("\nPlease set:")
    print("export SLACK_BOT_TOKEN='xoxb-your-bot-token'")
    print("export SLACK_SIGNING_SECRET='your-signing-secret'")
    print("export SLACK_APP_TOKEN='xapp-your-app-token'")
    print("export ALLOWED_SLACK_USER_ID='U12345678'  # Your Slack user ID")
    exit(1)

if not all([mysql_database, mysql_user, mysql_password]):
    print("❌ Missing required MySQL environment variables!")
    print("\nPlease set:")
    print("export MYSQL_HOST='localhost'  # optional, defaults to localhost")
    print("export MYSQL_PORT='3306'       # optional, defaults to 3306")
    print("export MYSQL_DATABASE='slack_notes'")
    print("export MYSQL_USER='your_username'")
    print("export MYSQL_PASSWORD='your_password'")
    exit(1)

# Initialize the Slack app
try:
    app = App(
        token=bot_token,
        signing_secret=signing_secret
    )
    print("✅ Slack app initialized successfully")
except Exception as e:
    print(f"❌ Failed to initialize Slack app: {e}")
    exit(1)

# Test connection by trying to get bot info
try:
    auth_result = app.client.auth_test()
    print(f"✅ Bot authenticated as: {auth_result['user']} in team: {auth_result['team']}")
except Exception as e:
    print(f"❌ Authentication failed: {e}")
    print("Check your SLACK_BOT_TOKEN - it should start with 'xoxb-'")
    exit(1)

# Database connection and setup
def get_db_connection():
    """Create and return a MySQL database connection"""
    try:
        connection = mysql.connector.connect(
            host=mysql_host,
            port=mysql_port,
            database=mysql_database,
            user=mysql_user,
            password=mysql_password
        )
        return connection
    except Error as e:
        print(f"❌ MySQL connection error: {e}")
        return None

def setup_database():
    """Create the notes table if it doesn't exist"""
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
            channel_name VARCHAR(255)
        )
        """
        
        cursor.execute(create_table_query)
        connection.commit()
        print("✅ Database table 'notes' ready")
        return True
        
    except Error as e:
        print(f"❌ Database setup error: {e}")
        return False
    finally:
        if connection and connection.is_connected():
            cursor.close()
            connection.close()

def check_rate_limit(user_id, command_name):
    """Returns True if the user is rate-limited, False if allowed."""
    key = f"{user_id}:{command_name}"
    now = time.monotonic()
    last = _last_command_time[key]
    if now - last < RATE_LIMIT_SECONDS:
        return True
    _last_command_time[key] = now
    return False

def save_note(user_id, username, note_text, channel_id=None, channel_name=None):
    """Save a note to the database"""
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
        print(f"📝 Note saved with ID: {note_id}")
        return note_id
        
    except Error as e:
        print(f"❌ Error saving note: {e}")
        return False
    finally:
        if connection and connection.is_connected():
            cursor.close()
            connection.close()

# Test database connection and setup
print("🔍 Testing database connection...")
if setup_database():
    print("✅ Database connection successful")
else:
    print("❌ Database connection failed - notes feature will not work")
    print("Make sure MySQL is running and credentials are correct")

# Listen for messages (simplified approach)  
@app.message(".*")
def handle_message_events(message, say, logger):
    """Handle all message events"""
    try:
        print(f"🔔 MESSAGE RECEIVED: {message}")  # Debug print
        
        user_id = message.get('user')
        text = message.get('text', '')
        channel = message.get('channel')

        # Skip messages from bots (including this bot)
        if message.get('bot_id') or message.get('subtype') == 'bot_message':
            print("🤖 Skipping bot message")
            return

        # Only respond to the allowed user
        if user_id != allowed_user_id:
            print(f"🚫 Ignoring message from unauthorized user: {user_id}")
            return
        
        print(f"👤 User: {user_id}, 📝 Text: '{text}', 📍 Channel: {channel}")
        
        # Simple confirmation response
        response = f"✅ Message received! You said: '{text}'"
        say(response)
        
        print(f"📤 Sent response: {response}")
        
    except Exception as e:
        print(f"❌ Error handling message: {e}")
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

        logger.info(f"Bot mentioned by user {user}: {text}")

        # Clean up the mention from the text
        clean_text = text.split('>', 1)[-1].strip() if '>' in text else text
        
        response = f"👋 Hi there! I saw you mentioned me. Your message: '{clean_text}'"
        say(response)
        
        logger.info(f"Responded to mention: {response}")
        
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
        except:
            pass
        
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
        logger.info(f"Note saved for user {user_name}: {note_text}")
        
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
        except:
            limit = 5
        
        # Get user's recent notes
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
            if connection and connection.is_connected():
                cursor.close()
                connection.close()
                
    except Exception as e:
        logger.error(f"Error handling /my_notes command: {e}")
        respond("❌ An error occurred while retrieving your notes.")
@app.error
def global_error_handler(error, body, logger):
    logger.error(f"Global error: {error}")
    logger.debug(f"Request body: {body}")

def main():
    """Main function to start the bot"""
    try:
        # Create socket mode handler
        handler = SocketModeHandler(app, app_token)
        
        print("🚀 Starting Slack bot...")
        print("📡 Connecting to Slack...")
        
        # Start the handler
        handler.start()
        
    except KeyboardInterrupt:
        print("\n👋 Bot stopped by user")
    except Exception as e:
        print(f"❌ Error starting bot: {e}")
        logger.exception(e)

if __name__ == "__main__":
    main()