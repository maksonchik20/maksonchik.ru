"""Read the media type from Telegram's versioned, zero-RLE encoded file IDs.

This is TDLib's internal format, not a Bot API field. Unknown versions and
malformed IDs deliberately stay unclassified. Protected content alone does
not mean that a photo is self-destructing.
"""

import base64
import binascii
import struct


SELF_DESTRUCTING_PHOTO_TYPE = 22
_LOCATION_FLAGS = (1 << 24) | (1 << 25)


def telegram_file_type(file_id: str | None) -> int | None:
    if not isinstance(file_id, str) or not file_id or len(file_id) > 1024:
        return None
    try:
        packed = base64.b64decode(
            file_id + "=" * (-len(file_id) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, binascii.Error):
        return None
    # Remote versions 2/4; version 3 identifies generated files, not photos.
    if not packed or packed[-1] not in (2, 4):
        return None
    trailer_size = 2 if packed[-1] == 4 else 1
    body = packed[:-trailer_size]
    decoded = bytearray()
    offset = 0
    while offset < len(body):
        value = body[offset]
        offset += 1
        if value == 0:
            if offset >= len(body) or body[offset] == 0:
                return None
            decoded.extend(b"\0" * body[offset])
            offset += 1
        else:
            decoded.append(value)
    if len(decoded) < 8:
        return None
    return struct.unpack_from("<I", decoded)[0] & ~_LOCATION_FLAGS


def is_self_destructing_photo(file_id: str | None) -> bool:
    return telegram_file_type(file_id) == SELF_DESTRUCTING_PHOTO_TYPE
