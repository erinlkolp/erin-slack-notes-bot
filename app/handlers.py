import json
import logging
from datetime import datetime

from .config import MAX_NOTE_LENGTH, NOTES_PER_PAGE
from .database import (
    delete_note,
    get_note_by_id,
    get_notes_page,
    save_note,
    search_notes,
    update_note,
)
from .tags import (
    delete_tags_for_note,
    get_notes_by_tag,
    get_user_tags,
    parse_tags,
    save_tags,
)
from .blocks import build_edit_note_modal, build_notes_blocks, escape_mrkdwn
from .middleware import allowed_user_id, require_allowed_user

logger = logging.getLogger(__name__)


def register_handlers(app):
    """Register all Slack event and command handlers with the Bolt app."""

    # ── Message / mention listeners ──────────────────────────────────────

    @app.message(".*")
    def handle_message_events(message, say, logger):
        """Echo a confirmation to the allowed user for any direct message."""
        try:
            user_id = message.get("user")
            channel = message.get("channel")

            if message.get("bot_id") or message.get("subtype") == "bot_message":
                return

            from . import middleware
            if user_id != middleware.allowed_user_id:
                return

            logger.debug(f"Message from user {user_id} in channel {channel}")
            say("✅ Message received!")
        except Exception as e:
            logger.error(f"Error handling message: {e}")

    @app.event("app_mention")
    def handle_mentions(event, say, logger):
        """Respond to @-mentions from the allowed user."""
        try:
            user = event.get("user")
            text = event.get("text", "")

            from . import middleware
            if user != middleware.allowed_user_id:
                logger.info(f"Ignoring mention from unauthorized user: {user}")
                return

            logger.info(f"Bot mentioned by user {user}")
            clean_text = text.split(">", 1)[-1].strip() if ">" in text else text
            say(f"👋 Hi there! I saw you mentioned me. Your message: '{clean_text}'")
        except Exception as e:
            logger.error(f"Error handling mention: {e}")

    # ── Slash commands ───────────────────────────────────────────────────

    @app.command("/take_notes")
    @require_allowed_user(command_name="take_notes")
    def handle_take_notes(ack, respond, command, client, logger):
        """Save a new note.  Usage: /take_notes <text>"""
        try:
            user_id = command.get("user_id")
            user_name = command.get("user_name", "Unknown")
            note_text = command.get("text", "").strip()
            channel_id = command.get("channel_id")

            channel_name = None
            try:
                if channel_id:
                    channel_info = client.conversations_info(channel=channel_id)
                    channel_name = channel_info["channel"]["name"]
            except Exception as e:
                logger.warning(f"Could not fetch channel name for {channel_id}: {e}")

            if not note_text:
                respond(
                    "❌ Please provide some text to save as a note.\n"
                    "Usage: `/take_notes Your note text here`"
                )
                return

            if len(note_text) > MAX_NOTE_LENGTH:
                respond(
                    f"❌ Note is too long ({len(note_text)} characters). "
                    f"Maximum is {MAX_NOTE_LENGTH} characters."
                )
                return

            note_id = save_note(user_id, user_name, note_text, channel_id, channel_name)

            if note_id:
                tags = parse_tags(note_text)
                if tags:
                    save_tags(note_id, tags)

                response = (
                    f"✅ Note saved successfully!\n"
                    f"📝 Note ID: {note_id}\n"
                    f"👤 User: {user_name}\n"
                    f"📄 Note: \"{escape_mrkdwn(note_text)}\"\n"
                    f"🕐 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                if channel_name:
                    response += f"\n📍 Channel: #{channel_name}"
                if tags:
                    response += f"\n🏷️ Tags: {', '.join('#' + t for t in tags)}"
            else:
                response = (
                    "❌ Sorry, there was an error saving your note. "
                    "Please check the database connection."
                )

            respond(response)
            logger.info(f"Note saved for user {user_name}")

        except Exception as e:
            logger.error(f"Error handling /take_notes command: {e}")
            respond("❌ An error occurred while saving your note. Please try again.")

    @app.command("/my_notes")
    @require_allowed_user(command_name="my_notes")
    def handle_my_notes(ack, respond, command, logger):
        """List notes with pagination.  Usage: /my_notes [per_page]"""
        try:
            user_id = command.get("user_id")
            user_name = command.get("user_name", "Unknown")
            text = command.get("text", "").strip()

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

            respond(blocks=build_notes_blocks(notes, page, per_page, total_count))

        except Exception as e:
            logger.error(f"Error handling /my_notes command: {e}")
            respond("An error occurred while retrieving your notes.")

    @app.command("/notes_by_tag")
    @require_allowed_user(command_name="notes_by_tag")
    def handle_notes_by_tag(ack, respond, command, logger):
        """List notes by tag.  Usage: /notes_by_tag [tag]  (no arg = list all tags)"""
        try:
            user_id = command.get("user_id")
            text = command.get("text", "").strip().lstrip("#").lower()

            if not text:
                user_tags = get_user_tags(user_id)
                if user_tags is None:
                    respond("❌ Database connection error.")
                    return
                if not user_tags:
                    respond(
                        "No tags found. Add tags to notes with `#tagname` in `/take_notes`."
                    )
                    return
                lines = [
                    f"• *#{tag}* — {count} note{'s' if count != 1 else ''}"
                    for tag, count in user_tags
                ]
                respond("🏷️ *Your tags:*\n" + "\n".join(lines))
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
            blocks[0] = {
                "type": "header",
                "text": {"type": "plain_text", "text": f"Notes tagged #{text}"},
            }
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

    @app.command("/edit_note")
    @require_allowed_user(command_name="edit_note")
    def handle_edit_note(ack, respond, command, client, logger):
        """Open a modal to edit a note.  Usage: /edit_note <note_id>"""
        try:
            user_id = command.get("user_id")
            text = command.get("text", "").strip()

            if not text:
                respond(
                    "❌ Please provide a note ID.\n"
                    "Usage: `/edit_note <note_id>`"
                )
                return

            try:
                note_id = int(text.split()[0])
            except ValueError:
                respond("❌ Invalid note ID. Usage: `/edit_note <note_id>`")
                return

            note = get_note_by_id(note_id, user_id)
            if note is None:
                respond(f"❌ Note #{note_id} not found or doesn't belong to you.")
                return

            current_text = note[1]
            channel_id = command.get("channel_id", "")

            client.views_open(
                trigger_id=command["trigger_id"],
                view=build_edit_note_modal(note_id, current_text, channel_id),
            )

        except Exception as e:
            logger.error(f"Error handling /edit_note command: {e}")
            respond("❌ An error occurred while opening the edit modal. Please try again.")

    @app.view("edit_note_modal")
    @require_allowed_user(is_view=True)
    def handle_edit_note_modal(ack, body, view, client, logger):
        """Handle submission of the edit-note modal."""
        try:
            user_id = body["user"]["id"]

            metadata = json.loads(view["private_metadata"])
            note_id = metadata["note_id"]
            channel_id = metadata.get("channel_id") or user_id

            new_text = view["state"]["values"]["note_text_block"]["note_text"]["value"]

            if len(new_text) > MAX_NOTE_LENGTH:
                ack(
                    response_action="errors",
                    errors={
                        "note_text_block": (
                            f"Note is too long ({len(new_text)} characters). "
                            f"Maximum is {MAX_NOTE_LENGTH} characters."
                        )
                    },
                )
                return

            if get_note_by_id(note_id, user_id) is None:
                ack(
                    response_action="errors",
                    errors={
                        "note_text_block": f"Note #{note_id} not found or doesn't belong to you."
                    },
                )
                return

            if not update_note(note_id, user_id, new_text):
                ack(
                    response_action="errors",
                    errors={"note_text_block": "Failed to update note. Please try again."},
                )
                return

            delete_tags_for_note(note_id)
            tags = parse_tags(new_text)
            if tags:
                save_tags(note_id, tags)

            ack()

            confirmation = f"✅ Note #{note_id} updated successfully!\n📄 New text: \"{escape_mrkdwn(new_text)}\""
            if tags:
                confirmation += f"\n🏷️ Tags: {', '.join('#' + t for t in tags)}"

            client.chat_postEphemeral(channel=channel_id, user=user_id, text=confirmation)
            logger.info(f"Note {note_id} updated by user {user_id} via modal")

        except Exception as e:
            logger.error(f"Error handling edit_note_modal submission: {e}")
            ack(
                response_action="errors",
                errors={"note_text_block": "An unexpected error occurred. Please try again."},
            )

    @app.command("/delete_note")
    @require_allowed_user(command_name="delete_note")
    def handle_delete_note(ack, respond, command, logger):
        """Delete a note.  Usage: /delete_note <note_id>"""
        try:
            user_id = command.get("user_id")
            text = command.get("text", "").strip()

            if not text:
                respond(
                    "❌ Usage: `/delete_note <note_id>`\nExample: `/delete_note 42`"
                )
                return

            try:
                note_id = int(text)
            except ValueError:
                respond("❌ Invalid note ID. Usage: `/delete_note <note_id>`")
                return

            if get_note_by_id(note_id, user_id) is None:
                respond(f"❌ Note #{note_id} not found or doesn't belong to you.")
                return

            if not delete_note(note_id, user_id):
                respond("❌ Failed to delete note. Please try again.")
                return

            respond(f"✅ Note #{note_id} has been deleted.")
            logger.info(f"Note {note_id} deleted by user {user_id}")

        except Exception as e:
            logger.error(f"Error handling /delete_note command: {e}")
            respond("❌ An error occurred while deleting your note. Please try again.")

    @app.command("/search_notes")
    @require_allowed_user(command_name="search_notes")
    def handle_search_notes(ack, respond, command, logger):
        """Search notes by keyword.  Usage: /search_notes <keyword>"""
        try:
            user_id = command.get("user_id")
            keyword = command.get("text", "").strip()

            if not keyword:
                respond(
                    "❌ Please provide a search term.\nUsage: `/search_notes <keyword>`"
                )
                return

            page = 1
            per_page = NOTES_PER_PAGE
            notes, total_count = search_notes(user_id, keyword, page, per_page)

            if notes is None:
                respond("❌ Database connection error.")
                return

            if not notes:
                respond(f"No notes found matching *\"{keyword}\"*.")
                return

            blocks = build_notes_blocks(notes, page, per_page, total_count)
            blocks[0] = {
                "type": "header",
                "text": {"type": "plain_text", "text": f"Search: {keyword}"},
            }
            for block in blocks:
                if block.get("type") == "actions":
                    for element in block["elements"]:
                        payload = json.loads(element["value"])
                        payload["keyword"] = keyword
                        element["value"] = json.dumps(payload)
                        if element["action_id"] == "notes_prev_page":
                            element["action_id"] = "search_notes_prev_page"
                        elif element["action_id"] == "notes_next_page":
                            element["action_id"] = "search_notes_next_page"

            respond(blocks=blocks)

        except Exception as e:
            logger.error(f"Error handling /search_notes command: {e}")
            respond("❌ An error occurred while searching your notes.")

    # ── Pagination action handlers ────────────────────────────────────────

    @app.action("notes_prev_page")
    @app.action("notes_next_page")
    @require_allowed_user()
    def handle_notes_pagination(ack, body, respond, logger):
        """Handle Previous / Next for the main notes list."""
        try:
            user_id = body["user"]["id"]
            payload = json.loads(body["actions"][0]["value"])
            notes, total_count = get_notes_page(user_id, payload["page"], payload["per_page"])
            if notes is None:
                return
            respond(
                blocks=build_notes_blocks(notes, payload["page"], payload["per_page"], total_count),
                replace_original=True,
            )
        except Exception as e:
            logger.error(f"Error handling notes pagination: {e}")

    @app.action("tag_notes_prev_page")
    @app.action("tag_notes_next_page")
    @require_allowed_user()
    def handle_tag_notes_pagination(ack, body, respond, logger):
        """Handle Previous / Next for tag-filtered notes."""
        try:
            user_id = body["user"]["id"]
            payload = json.loads(body["actions"][0]["value"])
            page, per_page, tag = payload["page"], payload["per_page"], payload["tag"]

            notes, total_count = get_notes_by_tag(user_id, tag, page, per_page)
            if notes is None:
                return

            blocks = build_notes_blocks(notes, page, per_page, total_count)
            blocks[0] = {
                "type": "header",
                "text": {"type": "plain_text", "text": f"Notes tagged #{tag}"},
            }
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

    @app.action("search_notes_prev_page")
    @app.action("search_notes_next_page")
    @require_allowed_user()
    def handle_search_notes_pagination(ack, body, respond, logger):
        """Handle Previous / Next for search results."""
        try:
            user_id = body["user"]["id"]
            payload = json.loads(body["actions"][0]["value"])
            page, per_page, keyword = payload["page"], payload["per_page"], payload["keyword"]

            notes, total_count = search_notes(user_id, keyword, page, per_page)
            if notes is None:
                return

            blocks = build_notes_blocks(notes, page, per_page, total_count)
            blocks[0] = {
                "type": "header",
                "text": {"type": "plain_text", "text": f"Search: {keyword}"},
            }
            for block in blocks:
                if block.get("type") == "actions":
                    for element in block["elements"]:
                        p = json.loads(element["value"])
                        p["keyword"] = keyword
                        element["value"] = json.dumps(p)
                        if element["action_id"] == "notes_prev_page":
                            element["action_id"] = "search_notes_prev_page"
                        elif element["action_id"] == "notes_next_page":
                            element["action_id"] = "search_notes_next_page"

            respond(blocks=blocks, replace_original=True)

        except Exception as e:
            logger.error(f"Error handling search notes pagination: {e}")

    # ── Global error handler ─────────────────────────────────────────────

    @app.error
    def global_error_handler(error, body, logger):
        logger.error(f"Global error: {error}")
