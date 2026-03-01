import os
import logging

log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, log_level, logging.INFO))

MAX_NOTE_LENGTH = 4000
NOTES_PER_PAGE = 5
RATE_LIMIT_SECONDS = 5
RATE_LIMIT_MAX_ENTRIES = 1000
DB_CONNECT_MAX_RETRIES = 3
DB_CONNECT_BASE_DELAY = 1  # seconds, doubles each retry
DB_POOL_SIZE = 5
HEALTH_CHECK_PORT = int(os.environ.get("HEALTH_CHECK_PORT", "8080"))
