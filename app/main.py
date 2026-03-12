import os
import sys
import signal
import logging

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from .database import init_db_pool, verify_connection, close_db_pool
from .handlers import register_handlers
from .health import start_health_check_server

log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, log_level, logging.INFO))
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment validation
# ---------------------------------------------------------------------------

logger.info("Checking environment variables...")

bot_token = os.environ.get("SLACK_BOT_TOKEN")
signing_secret = os.environ.get("SLACK_SIGNING_SECRET")
app_token = os.environ.get("SLACK_APP_TOKEN")
allowed_user_id = os.environ.get("ALLOWED_SLACK_USER_ID")

logger.info(f"SLACK_BOT_TOKEN: {'Set' if bot_token else 'Missing'}")
logger.info(f"SLACK_SIGNING_SECRET: {'Set' if signing_secret else 'Missing'}")
logger.info(f"SLACK_APP_TOKEN: {'Set' if app_token else 'Missing'}")
logger.info(f"ALLOWED_SLACK_USER_ID: {'Set' if allowed_user_id else 'Missing'}")

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
        "Please set SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET, SLACK_APP_TOKEN, "
        "and ALLOWED_SLACK_USER_ID."
    )
    sys.exit(1)

if not all([mysql_database, mysql_user, mysql_password]):
    logger.error(
        "Missing required MySQL environment variables. "
        "Please set MYSQL_DATABASE, MYSQL_USER, and MYSQL_PASSWORD."
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Slack app initialisation
# ---------------------------------------------------------------------------

try:
    app = App(token=bot_token, signing_secret=signing_secret)
    logger.info("Slack app initialized successfully")
except Exception as e:
    logger.error(f"Failed to initialize Slack app: {e}")
    sys.exit(1)

try:
    auth_result = app.client.auth_test()
    logger.info(
        f"Bot authenticated as: {auth_result['user']} in team: {auth_result['team']}"
    )
except Exception as e:
    logger.error(f"Authentication failed: {e}")
    logger.error("Check your SLACK_BOT_TOKEN - it should start with 'xoxb-'")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Database initialisation
# ---------------------------------------------------------------------------

logger.info("Initializing database connection pool...")
if not init_db_pool():
    logger.error("Failed to create database connection pool. Check MySQL configuration.")
    sys.exit(1)

logger.info("Testing database connection...")
if verify_connection():
    logger.info("Database connection successful")
else:
    logger.error(
        "Database connection failed. Make sure MySQL is running and credentials are correct."
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Register handlers
# ---------------------------------------------------------------------------

register_handlers(app)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    """Start the health check server and the Slack Socket Mode handler."""
    try:
        health_server = start_health_check_server()
        handler = SocketModeHandler(app, app_token)

        def shutdown_handler(signum, frame):
            logger.info("Received shutdown signal, stopping bot...")
            health_server.shutdown()
            handler.close()
            close_db_pool()
            sys.exit(0)

        signal.signal(signal.SIGTERM, shutdown_handler)
        signal.signal(signal.SIGINT, shutdown_handler)

        logger.info("Starting Slack bot...")
        handler.start()

    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Error starting bot: {e}")
        logger.exception(e)


if __name__ == "__main__":
    main()
