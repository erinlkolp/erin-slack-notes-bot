import os
import time
import logging
from mysql.connector import Error
from mysql.connector.pooling import MySQLConnectionPool

from .config import DB_CONNECT_MAX_RETRIES, DB_CONNECT_BASE_DELAY, DB_POOL_SIZE

logger = logging.getLogger(__name__)

_db_pool = None


def init_db_pool():
    """Create the MySQL connection pool with retry and exponential backoff.

    Reads DB credentials from environment at call time so they can be patched
    in tests.  Retries up to DB_CONNECT_MAX_RETRIES times with exponential
    backoff (1s, 2s, 4s ...).
    """
    global _db_pool
    mysql_host = os.environ.get("MYSQL_HOST", "localhost")
    mysql_port = os.environ.get("MYSQL_PORT", "3306")
    mysql_database = os.environ.get("MYSQL_DATABASE")
    mysql_user = os.environ.get("MYSQL_USER")
    mysql_password = os.environ.get("MYSQL_PASSWORD")
    ssl_ca = os.environ.get("MYSQL_SSL_CA")

    pool_args = {
        "pool_name": "slackbot_pool",
        "pool_size": DB_POOL_SIZE,
        "pool_reset_session": True,
        "host": mysql_host,
        "port": int(mysql_port),
        "database": mysql_database,
        "user": mysql_user,
        "password": mysql_password,
    }
    if ssl_ca:
        pool_args["ssl_ca"] = ssl_ca
        pool_args["ssl_verify_cert"] = True

    last_error = None
    for attempt in range(DB_CONNECT_MAX_RETRIES):
        try:
            _db_pool = MySQLConnectionPool(**pool_args)
            if attempt > 0:
                logger.info(f"Connection pool created on attempt {attempt + 1}")
            logger.info(f"Database connection pool created (size={DB_POOL_SIZE})")
            return True
        except Error as e:
            last_error = e
            if attempt < DB_CONNECT_MAX_RETRIES - 1:
                delay = DB_CONNECT_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    f"Pool creation attempt {attempt + 1}/{DB_CONNECT_MAX_RETRIES} "
                    f"failed: {e}. Retrying in {delay}s..."
                )
                time.sleep(delay)

    logger.error(
        f"Failed to create connection pool after {DB_CONNECT_MAX_RETRIES} attempts: {last_error}"
    )
    return False


def get_db_connection():
    """Get a connection from the pool with retry and exponential backoff.

    Returns a pooled connection on success, or None after all retries are
    exhausted.  Callers must close the connection in a finally block so it
    is returned to the pool.
    """
    if _db_pool is None:
        logger.error("Database connection pool not initialized")
        return None

    last_error = None
    for attempt in range(DB_CONNECT_MAX_RETRIES):
        try:
            connection = _db_pool.get_connection()
            if attempt > 0:
                logger.info(f"Got pooled connection on attempt {attempt + 1}")
            return connection
        except Error as e:
            last_error = e
            if attempt < DB_CONNECT_MAX_RETRIES - 1:
                delay = DB_CONNECT_BASE_DELAY * (2 ** attempt)
                logger.warning(
                    f"Get connection attempt {attempt + 1}/{DB_CONNECT_MAX_RETRIES} "
                    f"failed: {e}. Retrying in {delay}s..."
                )
                time.sleep(delay)

    logger.error(
        f"Failed to get connection from pool after {DB_CONNECT_MAX_RETRIES} attempts: {last_error}"
    )
    return None


def verify_connection():
    """Verify database connectivity by obtaining and releasing a pooled connection."""
    connection = get_db_connection()
    if connection is None:
        return False
    connection.close()
    return True


def close_db_pool():
    """Close all connections in the pool during graceful shutdown."""
    global _db_pool
    if _db_pool is None:
        return
    try:
        # Drain pooled connections by acquiring and closing them.
        # MySQLConnectionPool doesn't expose a close-all method, so we
        # pull connections until the pool is empty.
        for _ in range(DB_POOL_SIZE):
            try:
                conn = _db_pool.get_connection()
                conn.close()
            except Exception:
                break
        logger.info("Database connection pool closed")
    except Exception as e:
        logger.warning(f"Error closing database pool: {e}")
    finally:
        _db_pool = None


def save_note(user_id, username, note_text, channel_id=None, channel_name=None):
    """Insert a note row and return its new ID, or False on error."""
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        if connection is None:
            return False

        cursor = connection.cursor()
        cursor.execute(
            "INSERT INTO notes (user_id, username, note_text, channel_id, channel_name) "
            "VALUES (%s, %s, %s, %s, %s)",
            (user_id, username, note_text, channel_id, channel_name),
        )
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


def get_note_by_id(note_id, user_id):
    """Fetch a single note by ID, only if it belongs to user_id. Returns row or None."""
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        if connection is None:
            return None
        cursor = connection.cursor()
        cursor.execute(
            "SELECT id, note_text, created_at, channel_name "
            "FROM notes WHERE id = %s AND user_id = %s",
            (note_id, user_id),
        )
        return cursor.fetchone()
    except Error as e:
        logger.error(f"Database error in get_note_by_id: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if connection and connection.is_connected():
            connection.close()


def update_note(note_id, user_id, new_text):
    """Update a note's text. Returns True on success, False otherwise."""
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        if connection is None:
            return False
        cursor = connection.cursor()
        cursor.execute(
            "UPDATE notes SET note_text = %s WHERE id = %s AND user_id = %s",
            (new_text, note_id, user_id),
        )
        connection.commit()
        return cursor.rowcount > 0
    except Error as e:
        logger.error(f"Error updating note: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if connection and connection.is_connected():
            connection.close()


def delete_note(note_id, user_id):
    """Delete a note (tags auto-deleted via ON DELETE CASCADE). Returns True on success."""
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        if connection is None:
            return False
        cursor = connection.cursor()
        cursor.execute(
            "DELETE FROM notes WHERE id = %s AND user_id = %s",
            (note_id, user_id),
        )
        connection.commit()
        return cursor.rowcount > 0
    except Error as e:
        logger.error(f"Error deleting note: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if connection and connection.is_connected():
            connection.close()


def get_notes_page(user_id, page, per_page, sort="newest"):
    """Fetch one page of notes for a user. Returns (notes_list, total_count).

    sort: 'newest' (default) or 'oldest'. Pinned notes always sort first.
    """
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        if connection is None:
            return None, 0

        cursor = connection.cursor()
        cursor.execute("SELECT COUNT(*) FROM notes WHERE user_id = %s", (user_id,))
        total_count = cursor.fetchone()[0]

        order_dir = "ASC" if sort == "oldest" else "DESC"
        offset = (page - 1) * per_page
        cursor.execute(
            "SELECT id, note_text, created_at, channel_name, pinned "
            "FROM notes WHERE user_id = %s "
            f"ORDER BY pinned DESC, created_at {order_dir} LIMIT %s OFFSET %s",
            (user_id, per_page, offset),
        )
        return cursor.fetchall(), total_count

    except Error as e:
        logger.error(f"Database error in get_notes_page: {e}")
        return None, 0
    finally:
        if cursor:
            cursor.close()
        if connection and connection.is_connected():
            connection.close()


def search_notes(user_id, keyword, page, per_page):
    """Search notes by keyword (LIKE). Returns (notes_list, total_count)."""
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        if connection is None:
            return None, 0

        cursor = connection.cursor()
        escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like_pattern = f"%{escaped}%"

        cursor.execute(
            "SELECT COUNT(*) FROM notes WHERE user_id = %s AND note_text LIKE %s ESCAPE '\\\\'",
            (user_id, like_pattern),
        )
        total_count = cursor.fetchone()[0]

        offset = (page - 1) * per_page
        cursor.execute(
            "SELECT id, note_text, created_at, channel_name, pinned "
            "FROM notes WHERE user_id = %s AND note_text LIKE %s ESCAPE '\\\\' "
            "ORDER BY created_at DESC LIMIT %s OFFSET %s",
            (user_id, like_pattern, per_page, offset),
        )
        return cursor.fetchall(), total_count

    except Error as e:
        logger.error(f"Database error in search_notes: {e}")
        return None, 0
    finally:
        if cursor:
            cursor.close()
        if connection and connection.is_connected():
            connection.close()


def toggle_pin_note(note_id, user_id):
    """Toggle the pinned state of a note.

    Returns True (now pinned), False (now unpinned), or None if not found / on error.
    """
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        if connection is None:
            return None
        cursor = connection.cursor()
        cursor.execute(
            "UPDATE notes SET pinned = 1 - pinned WHERE id = %s AND user_id = %s",
            (note_id, user_id),
        )
        connection.commit()
        if cursor.rowcount == 0:
            return None
        cursor.execute(
            "SELECT pinned FROM notes WHERE id = %s AND user_id = %s",
            (note_id, user_id),
        )
        row = cursor.fetchone()
        return bool(row[0]) if row else None
    except Error as e:
        logger.error(f"Error toggling pin for note {note_id}: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if connection and connection.is_connected():
            connection.close()


def get_note_stats(user_id):
    """Fetch usage statistics for a user.

    Returns a dict with keys: total_notes, pinned_count, oldest, newest,
    total_tags, top_tags, top_channels. Returns None on error.
    """
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        if connection is None:
            return None
        cursor = connection.cursor()

        cursor.execute(
            "SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM notes WHERE user_id = %s",
            (user_id,),
        )
        total_notes, oldest, newest = cursor.fetchone()

        cursor.execute(
            "SELECT COUNT(*) FROM notes WHERE user_id = %s AND pinned = 1",
            (user_id,),
        )
        pinned_count = cursor.fetchone()[0]

        cursor.execute(
            "SELECT COUNT(DISTINCT t.tag) FROM note_tags t "
            "JOIN notes n ON n.id = t.note_id WHERE n.user_id = %s",
            (user_id,),
        )
        total_tags = cursor.fetchone()[0]

        cursor.execute(
            "SELECT t.tag, COUNT(*) as cnt FROM note_tags t "
            "JOIN notes n ON n.id = t.note_id "
            "WHERE n.user_id = %s GROUP BY t.tag ORDER BY cnt DESC LIMIT 5",
            (user_id,),
        )
        top_tags = cursor.fetchall()

        cursor.execute(
            "SELECT channel_name, COUNT(*) as cnt FROM notes "
            "WHERE user_id = %s AND channel_name IS NOT NULL "
            "GROUP BY channel_name ORDER BY cnt DESC LIMIT 5",
            (user_id,),
        )
        top_channels = cursor.fetchall()

        return {
            "total_notes": total_notes,
            "pinned_count": pinned_count,
            "oldest": oldest,
            "newest": newest,
            "total_tags": total_tags,
            "top_tags": top_tags,
            "top_channels": top_channels,
        }
    except Error as e:
        logger.error(f"Database error in get_note_stats: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if connection and connection.is_connected():
            connection.close()
