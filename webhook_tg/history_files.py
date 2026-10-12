"""Private durable history spool and bounded-memory multipart uploads."""

import hashlib
import os
import re
import stat
from pathlib import Path
from uuid import uuid4

from django.conf import settings


HISTORY_FILE_LIMIT = 40 * 1024 * 1024


def history_directory():
    directory = Path(getattr(settings, "WHO_UPDATE_HISTORY_EXPORT_DIR", "/var/lib/who-update-history"))
    if directory.is_symlink():
        raise ValueError("History spool must not be a symlink")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory.resolve()


def history_document_path(key, part):
    name = hashlib.sha256(f"{key}:{part}".encode()).hexdigest() + ".txt"
    return history_directory() / name


def validate_history_path(path):
    candidate = Path(path)
    if (
        not candidate.is_absolute()
        or candidate.parent != history_directory()
        or not re.fullmatch(r"[a-f0-9]{64}\.txt", candidate.name)
        or candidate.is_symlink()
    ):
        raise ValueError("Invalid history spool path")
    return candidate


def open_history_document(path):
    candidate = validate_history_path(path)
    descriptor = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("History document must be a private regular file")
        if not 0 < info.st_size <= HISTORY_FILE_LIMIT:
            raise ValueError("Invalid history document size")
        return os.fdopen(descriptor, "rb")
    except Exception:
        os.close(descriptor)
        raise


def remove_history_document(path):
    validate_history_path(path).unlink(missing_ok=True)


class DocumentMultipart:
    """requests reads this file-like body incrementally, including on IP retry."""

    def __init__(self, document, *, chat_id, filename, caption, content_type):
        if any(character in filename + content_type for character in '\r\n"'):
            raise ValueError("Invalid document filename or content type")
        boundary = uuid4().hex
        self.content_type = f"multipart/form-data; boundary={boundary}"
        fieldToValue = {"chat_id": str(chat_id), "caption": caption, "parse_mode": "HTML"}
        parts = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"\r\n\r\n{value}\r\n'
            for field, value in fieldToValue.items()
        ]
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        )
        self.header = "".join(parts).encode("utf-8")
        self.footer = f"\r\n--{boundary}--\r\n".encode()
        self.document = document
        self.document_size = os.fstat(document.fileno()).st_size
        self.position = 0

    def __len__(self):
        return len(self.header) + self.document_size + len(self.footer)

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        if offset != 0 or whence != 0:
            raise ValueError("Multipart can only rewind")
        self.document.seek(0)
        self.position = 0
        return 0

    def read(self, size=-1):
        # Even a caller without a size must not materialize the entire export.
        remaining = min(size if size is not None and size >= 0 else 65536, 65536, len(self) - self.position)
        chunks = []
        while remaining > 0:
            if self.position < len(self.header):
                chunk = self.header[self.position:self.position + remaining]
            elif self.position < len(self.header) + self.document_size:
                chunk = self.document.read(min(remaining, len(self.header) + self.document_size - self.position))
                if not chunk:
                    raise OSError("History document was truncated during upload")
            else:
                start = self.position - len(self.header) - self.document_size
                chunk = self.footer[start:start + remaining]
            chunks.append(chunk)
            self.position += len(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
