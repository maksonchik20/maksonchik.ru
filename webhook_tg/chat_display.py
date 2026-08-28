import html

from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from .models import FileType


def format_message_html(message) -> str:
    text = (message.text or message.caption or "").strip()
    has_file = bool(
        message.file_id
        and message.file_type
        and message.file_type != FileType.UNKNOWN
    )

    if has_file:
        media_label = dict(FileType.choices).get(message.file_type, message.file_type)
        header = f"[{media_label}] file_id: {message.file_id}"
        if text:
            text = f"{header}\n{text}"
        else:
            text = header
    elif message.file_type and message.file_type != FileType.UNKNOWN:
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

    if has_file:
        preview_url = reverse("admin:webhook_tg_message_preview_file")
        return format_html(
            '<div class="wu-chat-row wu-chat-msg">'
            '<div class="wu-chat-meta">{} · {}</div>'
            '<div class="wu-chat-text">{}</div>'
            '<button type="button" class="wu-show-file" '
            'data-preview-url="{}" data-file-id="{}" data-file-type="{}">'
            "показать</button>"
            '<div class="wu-file-preview" hidden></div>'
            "</div>",
            who,
            when,
            mark_safe(safe),
            preview_url,
            message.file_id,
            message.file_type,
        )

    return format_html(
        '<div class="wu-chat-row wu-chat-msg">'
        '<div class="wu-chat-meta">{} · {}</div>'
        '<div class="wu-chat-text">{}</div>'
        "</div>",
        who,
        when,
        mark_safe(safe),
    )
