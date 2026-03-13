import re
import logging
from mysql.connector import Error

from .database import get_db_connection

logger = logging.getLogger(__name__)

TAG_PATTERN = re.compile(r"#(\w+)")


def parse_tags(text):
    """Extract #hashtags from note text. Returns a deduplicated list of lowercase strings."""
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


def delete_tags_for_note(note_id):
    """Delete all tags for a note (called before re-tagging on edit). Returns True on success."""
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        if connection is None:
            return False
        cursor = connection.cursor()
        cursor.execute("DELETE FROM note_tags WHERE note_id = %s", (note_id,))
        connection.commit()
        return True
    except Error as e:
        logger.error(f"Error deleting tags: {e}")
        return False
    finally:
        if cursor:
            cursor.close()
        if connection and connection.is_connected():
            connection.close()


def get_notes_by_tag(user_id, tags, page, per_page, mode="and"):
    """Fetch a page of notes filtered by tags.

    Args:
        tags: list of tag strings (without leading #). Must be non-empty.
        mode: "and" requires ALL tags to be present, "or" requires ANY.

    Returns (notes_list, total_count) or (None, 0) on error.
    """
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        if connection is None:
            return None, 0
        cursor = connection.cursor()

        lowered = [t.lower() for t in tags]
        placeholders = ", ".join(["%s"] * len(lowered))
        having_count = len(lowered) if mode == "and" else 1

        cursor.execute(
            "SELECT COUNT(*) FROM ("
            "SELECT n.id FROM notes n "
            "JOIN note_tags t ON n.id = t.note_id "
            f"WHERE n.user_id = %s AND t.tag IN ({placeholders}) "
            "GROUP BY n.id "
            "HAVING COUNT(DISTINCT t.tag) >= %s"
            ") AS matching_notes",
            [user_id] + lowered + [having_count],
        )
        total_count = cursor.fetchone()[0]

        offset = (page - 1) * per_page
        cursor.execute(
            "SELECT n.id, n.note_text, n.created_at, n.channel_name, n.pinned "
            "FROM notes n JOIN note_tags t ON n.id = t.note_id "
            f"WHERE n.user_id = %s AND t.tag IN ({placeholders}) "
            "GROUP BY n.id, n.note_text, n.created_at, n.channel_name, n.pinned "
            "HAVING COUNT(DISTINCT t.tag) >= %s "
            "ORDER BY n.created_at DESC LIMIT %s OFFSET %s",
            [user_id] + lowered + [having_count, per_page, offset],
        )
        return cursor.fetchall(), total_count

    except Error as e:
        logger.error(f"Database error in get_notes_by_tag: {e}")
        return None, 0
    finally:
        if cursor:
            cursor.close()
        if connection and connection.is_connected():
            connection.close()


def get_user_tags(user_id):
    """Return all tags used by a user with note counts, ordered by frequency."""
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
