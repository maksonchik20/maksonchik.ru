import html

from .models import FileType


def format_message_html(message) -> str:
    text = (message.text or message.caption or "").strip()
    if message.file_type and message.file_type != FileType.UNKNOWN:
        media_label = dict(FileType.choices).get(message.file_type, message.file_type)
        if text:
            text = f"[{media_label}]\n{text}"
        else:
            text = f"[{media_label}]"

    safe = html.escape(text).replace("\n", "<br>") if text else "<i>(пусто)</i>"

    name_parts = []
    if message.first_name:
        name_parts.append(message.first_name)
    if message.username_from:
        name_parts.append(f"@{message.username_from}")
    who = " ".join(name_parts) or "Unknown"

    when = message.created_at.strftime("%d.%m.%Y %H:%M:%S") if message.created_at else "—"

    return (
        f'<div class="wu-chat-row wu-chat-msg">'
        f'<div class="wu-chat-meta">{html.escape(who)} · {when}</div>'
        f'<div class="wu-chat-text">{safe}</div>'
        f'</div>'
    )
