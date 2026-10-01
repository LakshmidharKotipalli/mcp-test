"""Small shared helpers."""
from __future__ import annotations

import re
import struct


def slugify(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-").lower() or "test"


def image_mime(data: bytes) -> str:
    return "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"


def image_size(data: bytes) -> tuple[int, int]:
    """(width, height) of a PNG or JPEG, or (0, 0) if it cannot be determined."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", data[16:24])
        i = 2  # JPEG: walk segments until a start-of-frame marker
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    except struct.error:
        pass
    return 0, 0
