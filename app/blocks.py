import json

from .config import MAX_NOTE_LENGTH


def escape_mrkdwn(text):
    """Escape Slack mrkdwn special characters to prevent mention/link injection.

    Slack treats <@U...>, <!here>, <!channel>, <!everyone>, and <URL|text>
    as active elements in mrkdwn surfaces.  Escaping & < > neutralises all of
    them so user-supplied note text is rendered literally.
    Order matters: & must be replaced first to avoid double-escaping.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_edit_note_modal(note_id, current_text, channel_id=""):
    """Build the Slack modal view for editing an existing note."""
    return {
        "type": "modal",
        "callback_id": "edit_note_modal",
        "private_metadata": json.dumps({"note_id": note_id, "channel_id": channel_id}),
        "title": {"type": "plain_text", "text": f"Edit Note #{note_id}"},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "note_text_block",
                "label": {"type": "plain_text", "text": "Note text"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "note_text",
                    "multiline": True,
                    "initial_value": current_text,
                    "max_length": MAX_NOTE_LENGTH,
                },
            }
        ],
    }


def build_notes_blocks(notes, page, per_page, total_count):
    """Build Slack Block Kit blocks for a page of notes with prev/next navigation."""
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
                    "text": (
                        f"Page {page} of {total_pages}  |  "
                        f"{total_count} notes total  |  {per_page} per page"
                    ),
                }
            ],
        },
        {"type": "divider"},
    ]

    for note_id, note_text, created_at, channel_name in notes:
        display_text = note_text if len(note_text) <= 200 else note_text[:197] + "..."
        display_text = escape_mrkdwn(display_text)
        channel_info = f"  #{channel_name}" if channel_name else ""
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*#{note_id}* — {created_at.strftime('%m/%d/%Y %H:%M')}"
                        f"{channel_info}\n{display_text}"
                    ),
                },
            }
        )
        blocks.append({"type": "divider"})

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
