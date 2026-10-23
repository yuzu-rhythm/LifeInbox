"""Verification helpers for Bot-owned rating image files.

Database paths are data, not authority.  Every boundary that can expose an
image (Discord attachment or HTTP response) re-resolves the path beneath the
configured image root and verifies the content hash before use.
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8"


class RatingImageVerificationError(ValueError):
    """The referenced image is missing, outside its root, or content-mismatched."""


def png_dimensions(payload: bytes, *, require_complete: bool = False) -> tuple[int, int] | None:
    """Read and validate the dimensions from a PNG's IHDR chunk.

    The project historically used signature-only byte fixtures in persistence
    tests.  Those remain accepted by :func:`verify_rating_image` for backwards
    compatibility, while renderers can request a complete PNG structure by
    passing ``require_complete=True``.  This parser deliberately does not
    decode pixels; it only checks the structural evidence needed before a
    generated file is persisted or served.
    """

    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise RatingImageVerificationError("rating image payload is not bytes")
    raw = bytes(payload)
    if not raw.startswith(PNG_SIGNATURE):
        raise RatingImageVerificationError("rating image has an invalid PNG signature")
    if len(raw) == len(PNG_SIGNATURE):
        if require_complete:
            raise RatingImageVerificationError("rating image has no PNG IHDR")
        return None

    offset = len(PNG_SIGNATURE)
    dimensions: tuple[int, int] | None = None
    saw_iend = False
    while offset + 12 <= len(raw):
        length = struct.unpack(">I", raw[offset : offset + 4])[0]
        chunk_type = raw[offset + 4 : offset + 8]
        chunk_end = offset + 12 + length
        if chunk_end > len(raw):
            if not require_complete and dimensions is None:
                return None
            raise RatingImageVerificationError("rating image has a truncated PNG chunk")
        chunk_data = raw[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", raw[offset + 8 + length : chunk_end])[0]
        actual_crc = zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            if not require_complete and dimensions is None:
                return None
            raise RatingImageVerificationError("rating image has an invalid PNG checksum")
        if chunk_type == b"IHDR":
            if length != 13 or dimensions is not None:
                raise RatingImageVerificationError("rating image has an invalid PNG IHDR")
            width, height = struct.unpack(">II", chunk_data[:8])
            if width < 1 or height < 1:
                raise RatingImageVerificationError("rating image dimensions are invalid")
            dimensions = (width, height)
        elif chunk_type == b"IEND":
            if length != 0:
                raise RatingImageVerificationError("rating image has an invalid PNG IEND")
            saw_iend = True
            break
        offset = chunk_end

    if dimensions is None and require_complete:
        raise RatingImageVerificationError("rating image has no PNG IHDR")
    if require_complete and not saw_iend:
        raise RatingImageVerificationError("rating image has no PNG IEND")
    return dimensions


def jpeg_dimensions(payload: bytes) -> tuple[int, int]:
    """Validate a complete JPEG envelope and return its frame dimensions."""

    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise RatingImageVerificationError("rating image payload is not bytes")
    raw = bytes(payload)
    if len(raw) < 4 or not raw.startswith(JPEG_SIGNATURE) or raw[-2:] != b"\xff\xd9":
        raise RatingImageVerificationError("rating image is not a complete JPEG")
    offset = 2
    dimensions: tuple[int, int] | None = None
    sof_markers = {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
    while offset < len(raw) - 2:
        if raw[offset] != 0xFF:
            # Entropy-coded bytes are valid only after a Start of Scan marker.
            raise RatingImageVerificationError("rating image has an invalid JPEG marker")
        while offset < len(raw) and raw[offset] == 0xFF:
            offset += 1
        if offset >= len(raw):
            raise RatingImageVerificationError("rating image has a truncated JPEG marker")
        marker = raw[offset]
        offset += 1
        if marker == 0xD9:
            break
        if marker == 0xDA:
            if dimensions is None:
                raise RatingImageVerificationError("rating image has no JPEG dimensions")
            # The outer EOI check proves the downloaded artifact is complete;
            # scan parsing is intentionally left to the browser decoder that
            # produced the already-validated dimensions.
            return dimensions
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(raw):
            raise RatingImageVerificationError("rating image has a truncated JPEG segment")
        length = struct.unpack(">H", raw[offset : offset + 2])[0]
        if length < 2 or offset + length > len(raw):
            raise RatingImageVerificationError("rating image has an invalid JPEG segment")
        if marker in sof_markers:
            if length < 7:
                raise RatingImageVerificationError("rating image has an invalid JPEG frame")
            height, width = struct.unpack(">HH", raw[offset + 3 : offset + 7])
            if width < 1 or height < 1:
                raise RatingImageVerificationError("rating image dimensions are invalid")
            dimensions = (width, height)
        offset += length
    if dimensions is None:
        raise RatingImageVerificationError("rating image has no JPEG dimensions")
    return dimensions


def verify_rating_image(
    *,
    image_root: str | Path,
    image_path: str | Path,
    expected_sha256: str | None = None,
    expected_byte_size: int | None = None,
    expected_width: int | None = None,
    expected_height: int | None = None,
    require_complete_png: bool = False,
) -> Path:
    """Return a verified absolute PNG/JPEG path contained by ``image_root``.

    ``Path.resolve`` follows symlinks before containment is checked, preventing
    a database row or symlink from turning a repost into an arbitrary-file
    upload.  The file is small enough for Discord, so hashing it at this trust
    boundary is intentional.
    """

    root = Path(image_root).resolve()
    candidate = Path(image_path)
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate).resolve()
    else:
        candidate = candidate.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise RatingImageVerificationError("rating image is outside its configured root") from exc
    suffix = candidate.suffix.casefold()
    if suffix not in {".png", ".jpg", ".jpeg"} or not candidate.is_file():
        raise RatingImageVerificationError("rating image is not an existing PNG/JPEG")
    try:
        payload = candidate.read_bytes()
    except OSError as exc:
        raise RatingImageVerificationError("rating image could not be read") from exc
    if suffix == ".png":
        dimensions = png_dimensions(payload, require_complete=require_complete_png)
    else:
        if not payload.startswith(JPEG_SIGNATURE):
            raise RatingImageVerificationError("rating image suffix does not match its bytes")
        dimensions = jpeg_dimensions(payload)
    if expected_byte_size is not None and len(payload) != expected_byte_size:
        raise RatingImageVerificationError("rating image byte size does not match metadata")
    if expected_width is not None and (type(expected_width) is not int or expected_width < 1):
        raise RatingImageVerificationError("rating image width metadata is invalid")
    if expected_height is not None and (type(expected_height) is not int or expected_height < 1):
        raise RatingImageVerificationError("rating image height metadata is invalid")
    if dimensions is not None:
        if expected_width is not None and dimensions[0] != expected_width:
            raise RatingImageVerificationError("rating image width does not match metadata")
        if expected_height is not None and dimensions[1] != expected_height:
            raise RatingImageVerificationError("rating image height does not match metadata")
    if expected_sha256 is not None:
        normalized_sha = expected_sha256.strip().casefold()
        if len(normalized_sha) != 64 or any(
            character not in "0123456789abcdef" for character in normalized_sha
        ):
            raise RatingImageVerificationError("rating image SHA-256 metadata is invalid")
        if hashlib.sha256(payload).hexdigest() != normalized_sha:
            raise RatingImageVerificationError("rating image SHA-256 does not match metadata")
    return candidate
