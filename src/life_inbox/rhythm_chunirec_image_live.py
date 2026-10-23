"""Live capture of the public CHUNIREC rating-board image.

CHUNIREC's public user page owns the table-to-image implementation.  This
adapter only opens that page, clicks its exact ``#best_image`` control, and
captures the PNG produced by the page.  It does not copy the page's
JavaScript or image assets and it does not use CHUNIREC credentials.

The browser boundary is deliberately event driven.  The collector observes
the page's exact warning text, the canvas ``toBlob`` callback, or a completed
generated image/download.  A timeout is only a bounded failure guard; it can
never turn an unchanged or initial blank image into a success.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import struct
import tempfile
import unicodedata
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from .rhythm_collectors.common import safe_message
from .rhythm_images import PNG_SIGNATURE, RatingImageVerificationError
from .rhythm_rating import RenderedImageMetadata

DEFAULT_CHUNIREC_BASE_URL = "https://chunirec.net"
CHUNIREC_BASE_URL = DEFAULT_CHUNIREC_BASE_URL
BEST_IMAGE_SELECTOR = 'button#best_image[title="Table to image"]'
CHUNIREC_BEST_IMAGE_SELECTOR = BEST_IMAGE_SELECTOR
PROFILE_SELECTOR = ".player-profile-container"
PROFILE_DATA_SELECTOR = "script#urec_pd"
BEST_TABLE_SELECTOR = "#table_best"
RECENT_TABLE_SELECTOR = "#table_recent"

# These strings are part of the small public-page contract.  They are used
# only for exact recognition; no broad text/role guessing is performed.
UNKNOWN_CONSTANT_NOTICE = (
    "現在、一部の譜面定数は未確定のため不正確なデータが表示されることがあります。ご注意ください。"
)
UNKNOWN_CONSTANT_ACKNOWLEDGEMENT = "わかった"

_CAPTURE_KEY = "__lifeInboxChunirecImageCaptureV1"
_MAX_PNG_BYTES = 64 * 1024 * 1024
_MAX_PNG_DIMENSION = 16_384
_MAX_DECODED_PNG_BYTES = 256 * 1024 * 1024
_USER_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


class CHUNIRECImageState(StrEnum):
    """Stable states for one public-image collection attempt."""

    INITIAL = "initial"
    OPENING_PAGE = "opening_page"
    PUBLIC_PAGE_MISSING = "public_page_missing"
    DOM_MISMATCH = "dom_mismatch"
    GENERATING = "generating"
    GENERATOR_NOT_COMPLETED = "generator_not_completed"
    INVALID_IMAGE = "invalid_image"
    SAVING = "saving"
    SUCCESS = "success"
    NETWORK_TIMEOUT = "network_timeout"
    NETWORK_ERROR = "network_error"
    PLAYWRIGHT_UNAVAILABLE = "playwright_unavailable"
    OUTPUT_WRITE_FAILURE = "output_write_failure"
    STALE_IMAGE = "stale_image"


class CHUNIRECImageFailureReason(StrEnum):
    """Non-secret reason codes returned by the live image adapter."""

    PUBLIC_PAGE_MISSING = "public_page_missing"
    DOM_MISMATCH = "dom_mismatch"
    GENERATOR_NOT_COMPLETED = "generator_not_completed"
    INVALID_IMAGE = "invalid_image"
    NETWORK_TIMEOUT = "network_timeout"
    NETWORK_ERROR = "network_error"
    PLAYWRIGHT_UNAVAILABLE = "playwright_unavailable"
    OUTPUT_WRITE_FAILURE = "output_write_failure"
    STALE_IMAGE = "stale_image"


class CHUNIRECImageError(ValueError):
    """Base error for local PNG/data-URI validation failures."""


class CHUNIRECPNGDecodeError(CHUNIRECImageError, RatingImageVerificationError):
    """Raised when a generated payload is not a bounded, decodable PNG."""


@dataclass(frozen=True, slots=True)
class CHUNIRECImageConfig:
    """Non-secret inputs for one public CHUNIREC image capture.

    ``base_url`` is intentionally restricted to the official origin.  A
    caller cannot turn a validated user name into an arbitrary URL, and this
    collector has no session, token, cookie, or credential fields.
    """

    user_name: str
    output_path: str | Path
    expected_player_name: str | None = None
    expected_rating: str | Decimal | None = None
    base_url: str = DEFAULT_CHUNIREC_BASE_URL
    headless: bool = True
    timeout_seconds: int = 120

    def __post_init__(self) -> None:
        if not isinstance(self.user_name, str):
            raise ValueError("CHUNIREC user name must be a string")
        user_name = self.user_name.strip()
        if not _USER_NAME_PATTERN.fullmatch(user_name) or ".." in user_name:
            raise ValueError("CHUNIREC user name is not a safe path segment")

        if not isinstance(self.base_url, str):
            raise ValueError("CHUNIREC base URL must be a string")
        base_url = self.base_url.strip().rstrip("/")
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "chunirec.net"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("CHUNIREC base URL must be the official origin")

        if type(self.headless) is not bool:
            raise ValueError("headless must be a boolean")
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 300:
            raise ValueError("CHUNIREC browser timeout must be between 1 and 300 seconds")

        output = Path(self.output_path)
        if not str(output).strip() or output.name in {".", ".."}:
            raise ValueError("CHUNIREC output path is required")
        if output.suffix.casefold() != ".png":
            raise ValueError("CHUNIREC output path must end with .png")

        if self.expected_player_name is not None:
            if not isinstance(self.expected_player_name, str):
                raise ValueError("expected CHUNIREC player name must be a string")
            expected_player_name = self.expected_player_name.strip()
            if not expected_player_name:
                raise ValueError("expected CHUNIREC player name cannot be blank")
            object.__setattr__(self, "expected_player_name", expected_player_name)
        if self.expected_rating is not None:
            try:
                expected_rating = Decimal(str(self.expected_rating))
            except (InvalidOperation, ValueError) as exc:
                raise ValueError("expected CHUNIREC rating is invalid") from exc
            if not expected_rating.is_finite() or expected_rating < 0:
                raise ValueError("expected CHUNIREC rating is invalid")
            object.__setattr__(self, "expected_rating", expected_rating)

        object.__setattr__(self, "user_name", user_name)
        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(self, "output_path", str(output))

    @property
    def user_url(self) -> str:
        """Return the only URL this configuration can request."""

        return f"{self.base_url}/users/{quote(self.user_name, safe='')}"

    @property
    def timeout_ms(self) -> int:
        return self.timeout_seconds * 1_000


@dataclass(frozen=True, slots=True)
class CHUNIRECImageResult:
    """Secret-safe detached result from one public image capture."""

    success: bool
    state: CHUNIRECImageState
    failure_reason: CHUNIRECImageFailureReason | None = None
    image: RenderedImageMetadata | None = field(default=None, repr=False)
    message: str | None = None

    def __post_init__(self) -> None:
        if type(self.success) is not bool:
            raise ValueError("success must be a boolean")
        if not isinstance(self.state, CHUNIRECImageState):
            try:
                object.__setattr__(self, "state", CHUNIRECImageState(str(self.state)))
            except ValueError as exc:
                raise ValueError("unsupported CHUNIREC image state") from exc
        if self.failure_reason is not None and not isinstance(
            self.failure_reason, CHUNIRECImageFailureReason
        ):
            try:
                object.__setattr__(
                    self,
                    "failure_reason",
                    CHUNIRECImageFailureReason(str(self.failure_reason)),
                )
            except ValueError as exc:
                raise ValueError("unsupported CHUNIREC image failure reason") from exc
        if self.success:
            if self.image is None:
                raise ValueError("successful CHUNIREC image result requires image metadata")
            if self.failure_reason is not None:
                raise ValueError("successful CHUNIREC image result cannot have a failure reason")
            if self.state is not CHUNIRECImageState.SUCCESS:
                raise ValueError("successful CHUNIREC image result must use success state")
        elif self.image is not None:
            raise ValueError("failed CHUNIREC image result cannot contain image metadata")

        if self.message is not None:
            # Internal callers use only fixed ``chunirec_image:...`` messages.
            # Coerce externally supplied diagnostics to a short safe marker so
            # page HTML, URLs, cookies, or exception text cannot cross this API.
            normalized = safe_message(str(self.message)) or ""
            if not re.fullmatch(r"[a-z0-9_:-]{1,160}", normalized):
                normalized = f"chunirec_image:{self.state.value}"
            object.__setattr__(self, "message", normalized)

    @property
    def reason(self) -> CHUNIRECImageFailureReason | None:
        return self.failure_reason

    @property
    def metadata(self) -> RenderedImageMetadata | None:
        return self.image

    @property
    def image_path(self) -> str | None:
        return self.image.image_path if self.image is not None else None

    @property
    def image_sha256(self) -> str | None:
        return self.image.image_sha256 if self.image is not None else None


@dataclass(frozen=True, slots=True)
class _LiveFailure(Exception):
    reason: CHUNIRECImageFailureReason
    state: CHUNIRECImageState
    stage: str


def _failure_state(reason: CHUNIRECImageFailureReason) -> CHUNIRECImageState:
    return CHUNIRECImageState(reason.value)


def _is_timeout_exception(error: BaseException) -> bool:
    return (
        isinstance(error, TimeoutError)
        or "timeout" in type(error).__name__.casefold()
        or "timed out" in str(error).casefold()
    )


def _response_status(response: object) -> int | None:
    if response is None:
        return None
    status = getattr(response, "status", None)
    if callable(status):
        try:
            status = status()
        except Exception:
            return None
    return status if type(status) is int else None


def _safe_url_matches(actual: object, expected: str) -> bool:
    if not isinstance(actual, str):
        return False
    try:
        actual_parts = urlsplit(actual)
        expected_parts = urlsplit(expected)
    except ValueError:
        return False
    return (
        actual_parts.scheme == expected_parts.scheme
        and actual_parts.hostname == expected_parts.hostname
        and actual_parts.port == expected_parts.port
        and actual_parts.path == expected_parts.path
        and not actual_parts.username
        and not actual_parts.password
        and not actual_parts.query
        and not actual_parts.fragment
    )


def _paeth(left: int, above: int, upper_left: int) -> int:
    estimate = left + above - upper_left
    left_distance = abs(estimate - left)
    above_distance = abs(estimate - above)
    upper_left_distance = abs(estimate - upper_left)
    if left_distance <= above_distance and left_distance <= upper_left_distance:
        return left
    if above_distance <= upper_left_distance:
        return above
    return upper_left


def _decode_png_payload(payload: object) -> tuple[bytes, int, int]:
    """Decode and structurally verify a bounded non-interlaced PNG.

    ``png_dimensions`` in the existing image boundary intentionally performs
    a lighter structural check for backwards-compatible stored fixtures.  A
    live browser artifact needs stronger evidence: this routine validates all
    chunk CRCs, decompresses IDAT data, checks scanline/filter structure, and
    bounds both encoded and decoded sizes.
    """

    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise CHUNIRECPNGDecodeError("CHUNIREC image payload is not bytes")
    raw = bytes(payload)
    if len(raw) > _MAX_PNG_BYTES or len(raw) <= len(PNG_SIGNATURE):
        raise CHUNIRECPNGDecodeError("CHUNIREC image byte size is invalid")
    if not raw.startswith(PNG_SIGNATURE):
        raise CHUNIRECPNGDecodeError("CHUNIREC image has an invalid PNG signature")

    offset = len(PNG_SIGNATURE)
    width: int | None = None
    height: int | None = None
    bit_depth: int | None = None
    color_type: int | None = None
    channels: int | None = None
    idat_parts: list[bytes] = []
    saw_iend = False
    palette_entries: int | None = None

    allowed_bit_depths = {
        0: {1, 2, 4, 8, 16},
        2: {8, 16},
        3: {1, 2, 4, 8},
        4: {8, 16},
        6: {8, 16},
    }
    channel_counts = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}

    while offset < len(raw):
        if offset + 12 > len(raw):
            raise CHUNIRECPNGDecodeError("CHUNIREC image has a truncated PNG chunk")
        chunk_length = struct.unpack(">I", raw[offset : offset + 4])[0]
        chunk_end = offset + 12 + chunk_length
        if chunk_end > len(raw):
            raise CHUNIRECPNGDecodeError("CHUNIREC image has a truncated PNG chunk")
        chunk_type = raw[offset + 4 : offset + 8]
        chunk_data = raw[offset + 8 : offset + 8 + chunk_length]
        expected_crc = struct.unpack(">I", raw[offset + 8 + chunk_length : chunk_end])[0]
        if zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF != expected_crc:
            raise CHUNIRECPNGDecodeError("CHUNIREC image has an invalid PNG checksum")

        if chunk_type == b"IHDR":
            if offset != len(PNG_SIGNATURE) or chunk_length != 13 or width is not None:
                raise CHUNIRECPNGDecodeError("CHUNIREC image has an invalid PNG IHDR")
            (
                width,
                height,
                bit_depth,
                color_type,
                compression_method,
                filter_method,
                interlace_method,
            ) = struct.unpack(">IIBBBBB", chunk_data)
            if (
                width < 1
                or height < 1
                or width > _MAX_PNG_DIMENSION
                or height > _MAX_PNG_DIMENSION
                or color_type not in allowed_bit_depths
                or bit_depth not in allowed_bit_depths.get(color_type, set())
                or compression_method != 0
                or filter_method != 0
                or interlace_method != 0
            ):
                raise CHUNIRECPNGDecodeError("CHUNIREC image dimensions or format are invalid")
            channels = channel_counts[color_type]
        elif chunk_type == b"PLTE":
            if width is None or chunk_length == 0 or chunk_length % 3:
                raise CHUNIRECPNGDecodeError("CHUNIREC image has an invalid PNG palette")
            palette_entries = chunk_length // 3
            if palette_entries > 256:
                raise CHUNIRECPNGDecodeError("CHUNIREC image palette is too large")
        elif chunk_type == b"IDAT":
            if width is None:
                raise CHUNIRECPNGDecodeError("CHUNIREC image has IDAT before IHDR")
            idat_parts.append(chunk_data)
        elif chunk_type == b"IEND":
            if chunk_length != 0 or width is None or not idat_parts:
                raise CHUNIRECPNGDecodeError("CHUNIREC image has an incomplete PNG")
            saw_iend = True
            offset = chunk_end
            break
        offset = chunk_end

    if not saw_iend or offset != len(raw):
        raise CHUNIRECPNGDecodeError("CHUNIREC image has no final PNG IEND")
    if (
        width is None
        or height is None
        or bit_depth is None
        or color_type is None
        or channels is None
    ):
        raise CHUNIRECPNGDecodeError("CHUNIREC image has no valid PNG IHDR")
    if color_type == 3 and palette_entries is None:
        raise CHUNIRECPNGDecodeError("CHUNIREC image has no indexed-color palette")

    row_bits = width * channels * bit_depth
    row_bytes = (row_bits + 7) // 8
    inflated_size = (row_bytes + 1) * height
    if inflated_size > _MAX_DECODED_PNG_BYTES:
        raise CHUNIRECPNGDecodeError("CHUNIREC image decoded size is too large")

    decompressor = zlib.decompressobj()
    try:
        inflated = decompressor.decompress(b"".join(idat_parts), inflated_size + 1)
        inflated += decompressor.flush()
    except zlib.error as exc:
        raise CHUNIRECPNGDecodeError("CHUNIREC image IDAT data is not decodable") from exc
    if decompressor.unused_data or decompressor.unconsumed_tail or len(inflated) != inflated_size:
        raise CHUNIRECPNGDecodeError("CHUNIREC image scanlines are incomplete")

    bytes_per_pixel = max(1, (channels * bit_depth + 7) // 8)
    previous = bytearray(row_bytes)
    cursor = 0
    for _ in range(height):
        filter_type = inflated[cursor]
        cursor += 1
        if filter_type > 4:
            raise CHUNIRECPNGDecodeError("CHUNIREC image has an invalid PNG filter")
        current = bytearray(inflated[cursor : cursor + row_bytes])
        cursor += row_bytes
        for index, value in enumerate(current):
            left = current[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            above = previous[index]
            upper_left = previous[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
            if filter_type == 1:
                current[index] = (value + left) & 0xFF
            elif filter_type == 2:
                current[index] = (value + above) & 0xFF
            elif filter_type == 3:
                current[index] = (value + ((left + above) // 2)) & 0xFF
            elif filter_type == 4:
                current[index] = (value + _paeth(left, above, upper_left)) & 0xFF
        previous = current

    if cursor != len(inflated):
        raise CHUNIRECPNGDecodeError("CHUNIREC image scanlines contain trailing data")
    return raw, width, height


def decode_png_bytes(payload: object) -> tuple[bytes, int, int]:
    """Public helper for decoding a completed PNG artifact."""

    return _decode_png_payload(payload)


def decode_png_data_uri(value: object) -> tuple[bytes, int, int]:
    """Decode one ``data:image/png;base64`` artifact from the page."""

    if not isinstance(value, str):
        raise CHUNIRECPNGDecodeError("CHUNIREC image is not a PNG data URI")
    header, separator, encoded = value.partition(",")
    if separator != "," or header.casefold() != "data:image/png;base64":
        raise CHUNIRECPNGDecodeError("CHUNIREC image is not a PNG data URI")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise CHUNIRECPNGDecodeError("CHUNIREC image base64 is invalid") from exc
    return decode_png_bytes(payload)


def _atomic_write(path: str | Path, payload: bytes) -> Path:
    output = Path(path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    descriptor: int | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output.name}.",
            suffix=".tmp",
            dir=str(output.parent),
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, output)
        temporary_name = None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
    return output


_INSTALL_CAPTURE_SCRIPT = f"""
(() => {{
  const key = {(_CAPTURE_KEY)!r};
  const current = window[key];
  if (current && current.installed) return;
  const capture = {{installed: true, armed: false, ready: false, blob: null, canvas: null}};
  const original = HTMLCanvasElement.prototype.toBlob;
  if (typeof original === "function") {{
    HTMLCanvasElement.prototype.toBlob = function(callback, type, quality) {{
      return original.call(this, (blob) => {{
        if (capture.armed && blob && (!type || String(type).toLowerCase() === "image/png")) {{
          capture.blob = blob;
          capture.canvas = this;
          capture.ready = true;
        }}
        callback(blob);
      }}, type, quality);
    }};
  }}
  window[key] = capture;
}})()
"""
_ARM_CAPTURE_SCRIPT = f"""
(() => {{
  const capture = window[{(_CAPTURE_KEY)!r}];
  if (capture) capture.armed = true;
}})()
"""

_GENERATOR_OR_NOTICE_SCRIPT = f"""
() => {{
  const noticeText = {UNKNOWN_CONSTANT_NOTICE!r};
  const visible = (element) => {{
    if (!element || !element.textContent || !element.getClientRects().length) return false;
    const style = window.getComputedStyle(element);
    return style.display !== "none" && style.visibility !== "hidden" && style.opacity !== "0";
  }};
  for (const element of document.querySelectorAll("body *")) {{
    if (visible(element) && element.textContent.trim() === noticeText) return "notice";
  }}
  const capture = window[{(_CAPTURE_KEY)!r}];
  if (capture && capture.ready && capture.blob) return "canvas_blob";
  for (const image of document.images) {{
    const source = image.currentSrc || image.src || "";
    if (source.startsWith("blob:") && image.complete && image.naturalWidth > 0
        && image.naturalHeight > 0) {{
      return "generated_image";
    }}
  }}
  for (const link of document.querySelectorAll("a[download]")) {{
    const source = link.href || "";
    if ((source.startsWith("blob:") || source.startsWith("data:image/png"))
        && visible(link)) return "download";
  }}
  return false;
}}
"""

_EXTRACT_ARTIFACT_SCRIPT = f"""
async () => {{
  const key = {(_CAPTURE_KEY)!r};
  const toDataURL = (blob) => new Promise((resolve) => {{
    const reader = new FileReader();
    reader.onload = () => resolve(typeof reader.result === "string" ? reader.result : null);
    reader.onerror = () => resolve(null);
    reader.readAsDataURL(blob);
  }});
  const capture = window[key];
  if (capture && capture.ready && capture.blob) {{
    const dataURL = await toDataURL(capture.blob);
    if (dataURL) return {{kind: "canvas_blob", data_url: dataURL}};
  }}
  const images = [...document.images].filter((image) => {{
    const source = image.currentSrc || image.src || "";
    return (source.startsWith("blob:") || source.startsWith("data:image/png"))
      && image.complete && image.naturalWidth > 0 && image.naturalHeight > 0;
  }});
  for (const image of images.reverse()) {{
    try {{
      const response = await fetch(image.currentSrc || image.src);
      const dataURL = await toDataURL(await response.blob());
      if (dataURL) return {{kind: "generated_image", data_url: dataURL,
        width: image.naturalWidth, height: image.naturalHeight}};
    }} catch (error) {{}}
  }}
  const links = [...document.querySelectorAll("a[download]")].filter((link) =>
    (link.href || "").startsWith("blob:") || (link.href || "").startsWith("data:image/png"));
  for (const link of links.reverse()) {{
    try {{
      const response = await fetch(link.href);
      const dataURL = await toDataURL(await response.blob());
      if (dataURL) return {{kind: "download", data_url: dataURL}};
    }} catch (error) {{}}
  }}
  return null;
}}
"""


class PlaywrightChunirecImageCollector:
    """Capture the official public CHUNIREC BEST/NEW board PNG."""

    def __init__(
        self,
        config: CHUNIRECImageConfig,
        *,
        playwright_factory: Callable[[], Any] | None = None,
        last_successful_image: str | Path | None = None,
    ) -> None:
        self.config = config
        self._playwright_factory = playwright_factory
        self.last_successful_image = last_successful_image

    def _failure(self, failure: _LiveFailure) -> CHUNIRECImageResult:
        return CHUNIRECImageResult(
            success=False,
            state=failure.state,
            failure_reason=failure.reason,
            message=f"chunirec_image:{failure.reason.value}:{failure.stage}",
        )

    @staticmethod
    def _existing_image_sha256(path: str | Path | None) -> str | None:
        if path is None:
            return None
        try:
            candidate = Path(path)
            if candidate.is_file():
                return hashlib.sha256(candidate.read_bytes()).hexdigest()
        except OSError:
            return None
        return None

    @staticmethod
    def _locator_count(page: Any, selector: str) -> int:
        locator = page.locator(selector)
        count = locator.count()
        return count if type(count) is int else 0

    def _verify_page(self, page: Any, response: object) -> None:
        status = _response_status(response)
        if status == 404:
            raise _LiveFailure(
                CHUNIRECImageFailureReason.PUBLIC_PAGE_MISSING,
                CHUNIRECImageState.PUBLIC_PAGE_MISSING,
                "response",
            )
        if status is None or not 200 <= status < 300:
            raise _LiveFailure(
                CHUNIRECImageFailureReason.NETWORK_ERROR,
                CHUNIRECImageState.NETWORK_ERROR,
                "response",
            )
        if not _safe_url_matches(getattr(page, "url", None), self.config.user_url):
            raise _LiveFailure(
                CHUNIRECImageFailureReason.DOM_MISMATCH,
                CHUNIRECImageState.DOM_MISMATCH,
                "url",
            )

        expected_selectors = (
            PROFILE_SELECTOR,
            PROFILE_DATA_SELECTOR,
            BEST_TABLE_SELECTOR,
            RECENT_TABLE_SELECTOR,
        )
        if any(self._locator_count(page, selector) != 1 for selector in expected_selectors):
            raise _LiveFailure(
                CHUNIRECImageFailureReason.DOM_MISMATCH,
                CHUNIRECImageState.DOM_MISMATCH,
                "profile",
            )
        if self._locator_count(page, BEST_IMAGE_SELECTOR) != 1:
            raise _LiveFailure(
                CHUNIRECImageFailureReason.DOM_MISMATCH,
                CHUNIRECImageState.DOM_MISMATCH,
                "best_image_button",
            )

        if self.config.expected_player_name is None and self.config.expected_rating is None:
            return
        try:
            profile_text = page.locator(PROFILE_DATA_SELECTOR).text_content()
            profile_data = json.loads(profile_text)
            player_profile = profile_data["player_profile"]
            observed_name = str(player_profile["pn"])
            observed_rating = Decimal(str(player_profile["rt"]))
        except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
            raise _LiveFailure(
                CHUNIRECImageFailureReason.DOM_MISMATCH,
                CHUNIRECImageState.DOM_MISMATCH,
                "profile_data",
            ) from exc
        if self.config.expected_player_name is not None:
            expected_name = unicodedata.normalize(
                "NFKC", self.config.expected_player_name
            ).strip().casefold()
            observed_normalized = unicodedata.normalize(
                "NFKC", observed_name
            ).strip().casefold()
            if observed_normalized != expected_name:
                raise _LiveFailure(
                    CHUNIRECImageFailureReason.DOM_MISMATCH,
                    CHUNIRECImageState.DOM_MISMATCH,
                    "player_mismatch",
                )
        if (
            self.config.expected_rating is not None
            and observed_rating != self.config.expected_rating
        ):
            raise _LiveFailure(
                CHUNIRECImageFailureReason.DOM_MISMATCH,
                CHUNIRECImageState.DOM_MISMATCH,
                "rating_mismatch",
            )

    @staticmethod
    def _wait_value(handle: object) -> object:
        json_value = getattr(handle, "json_value", None)
        if callable(json_value):
            return json_value()
        return handle

    @staticmethod
    def _notice_visible(page: Any) -> bool:
        try:
            notice = page.get_by_text(UNKNOWN_CONSTANT_NOTICE, exact=True)
            count = notice.count()
            if count < 1:
                return False
            return any(notice.nth(index).is_visible() for index in range(count))
        except Exception:
            return False

    def _acknowledge_notice(self, page: Any) -> None:
        try:
            notice = page.get_by_text(UNKNOWN_CONSTANT_NOTICE, exact=True)
            acknowledgement = page.get_by_role(
                "button",
                name=UNKNOWN_CONSTANT_ACKNOWLEDGEMENT,
                exact=True,
            )
            if acknowledgement.count() != 1 or not acknowledgement.is_visible():
                raise _LiveFailure(
                    CHUNIRECImageFailureReason.DOM_MISMATCH,
                    CHUNIRECImageState.DOM_MISMATCH,
                    "notice_button",
                )
            acknowledgement.click()
            # Closing the modal is the page's explicit signal that its
            # awaited notice flow may continue into image generation.  Do not
            # let a still-visible notice satisfy the next generator wait.
            notice.wait_for(state="hidden", timeout=self.config.timeout_ms)
        except _LiveFailure:
            raise
        except Exception as exc:
            if _is_timeout_exception(exc):
                raise _LiveFailure(
                    CHUNIRECImageFailureReason.GENERATOR_NOT_COMPLETED,
                    CHUNIRECImageState.GENERATOR_NOT_COMPLETED,
                    "notice_button",
                ) from exc
            raise _LiveFailure(
                CHUNIRECImageFailureReason.DOM_MISMATCH,
                CHUNIRECImageState.DOM_MISMATCH,
                "notice_button",
            ) from exc

    def _wait_for_generator(self, page: Any) -> str:
        try:
            handle = page.wait_for_function(
                _GENERATOR_OR_NOTICE_SCRIPT,
                timeout=self.config.timeout_ms,
            )
            value = self._wait_value(handle)
        except Exception as exc:
            raise _LiveFailure(
                CHUNIRECImageFailureReason.GENERATOR_NOT_COMPLETED,
                CHUNIRECImageState.GENERATOR_NOT_COMPLETED,
                "generator",
            ) from exc
        if value == "notice":
            return "notice"
        if value in {"canvas_blob", "generated_image", "download"}:
            return str(value)
        raise _LiveFailure(
            CHUNIRECImageFailureReason.GENERATOR_NOT_COMPLETED,
            CHUNIRECImageState.GENERATOR_NOT_COMPLETED,
            "generator",
        )

    @staticmethod
    def _artifact_data_url(artifact: object) -> tuple[str, int | None, int | None]:
        if not isinstance(artifact, dict):
            raise CHUNIRECPNGDecodeError("CHUNIREC generated image was not observed")
        data_url = artifact.get("data_url")
        if not isinstance(data_url, str):
            raise CHUNIRECPNGDecodeError("CHUNIREC generated PNG data was not observed")
        width = artifact.get("width")
        height = artifact.get("height")
        if width is not None and (type(width) is not int or width < 1):
            raise CHUNIRECPNGDecodeError("CHUNIREC generated image width is invalid")
        if height is not None and (type(height) is not int or height < 1):
            raise CHUNIRECPNGDecodeError("CHUNIREC generated image height is invalid")
        return data_url, width, height

    def _extract_png(self, page: Any) -> tuple[bytes, int, int]:
        try:
            artifact = page.evaluate(_EXTRACT_ARTIFACT_SCRIPT)
            data_url, observed_width, observed_height = self._artifact_data_url(artifact)
            payload, width, height = decode_png_data_uri(data_url)
        except CHUNIRECPNGDecodeError:
            raise
        except RatingImageVerificationError as exc:
            raise CHUNIRECPNGDecodeError(str(exc)) from exc
        except Exception as exc:
            raise CHUNIRECPNGDecodeError("CHUNIREC generated PNG could not be extracted") from exc
        if observed_width is not None and observed_width != width:
            raise CHUNIRECPNGDecodeError("CHUNIREC generated image width changed during capture")
        if observed_height is not None and observed_height != height:
            raise CHUNIRECPNGDecodeError("CHUNIREC generated image height changed during capture")
        return payload, width, height

    def collect_page(
        self,
        page: Any,
        *,
        collected_at: datetime | None = None,
    ) -> CHUNIRECImageResult:
        """Navigate a supplied Playwright-like page and capture one PNG.

        This narrow seam is public so tests and a caller-owned browser harness
        can provide a fake/page object without making a real network request.
        The normal production entry point is :meth:`collect`.
        """

        observed_at = collected_at or datetime.now(UTC)
        stage = "target_page"
        try:
            response = page.goto(
                self.config.user_url,
                wait_until="domcontentloaded",
                timeout=self.config.timeout_ms,
            )
            self._verify_page(page, response)
            stage = "best_image_button"
            button = page.locator(BEST_IMAGE_SELECTOR)

            stage = "capture_observer"
            try:
                page.evaluate(_INSTALL_CAPTURE_SCRIPT)
                page.evaluate(_ARM_CAPTURE_SCRIPT)
            except Exception as exc:
                raise _LiveFailure(
                    CHUNIRECImageFailureReason.DOM_MISMATCH,
                    CHUNIRECImageState.DOM_MISMATCH,
                    "capture_observer",
                ) from exc

            if self._notice_visible(page):
                stage = "notice"
                self._acknowledge_notice(page)

            stage = "generator_click"
            button.click()
            stage = "generator"
            outcome = self._wait_for_generator(page)
            if outcome == "notice":
                self._acknowledge_notice(page)
                outcome = self._wait_for_generator(page)
            if outcome not in {"canvas_blob", "generated_image", "download"}:
                raise _LiveFailure(
                    CHUNIRECImageFailureReason.GENERATOR_NOT_COMPLETED,
                    CHUNIRECImageState.GENERATOR_NOT_COMPLETED,
                    "generator",
                )

            stage = "image_decode"
            payload, width, height = self._extract_png(page)
            if not payload or width < 1 or height < 1 or len(payload) > _MAX_PNG_BYTES:
                raise CHUNIRECPNGDecodeError("CHUNIREC generated PNG size is invalid")

            baseline_path = self.last_successful_image
            output = Path(self.config.output_path).resolve()
            if baseline_path is None and output.is_file():
                baseline_path = output
            candidate_sha256 = hashlib.sha256(payload).hexdigest()
            baseline_sha256 = self._existing_image_sha256(baseline_path)
            if baseline_sha256 is not None and candidate_sha256 == baseline_sha256:
                raise _LiveFailure(
                    CHUNIRECImageFailureReason.STALE_IMAGE,
                    CHUNIRECImageState.STALE_IMAGE,
                    "image_compare",
                )

            stage = "image_write"
            output = _atomic_write(self.config.output_path, payload)
            metadata = RenderedImageMetadata(
                path=output,
                sha256=candidate_sha256,
                width=width,
                height=height,
                byte_size=len(payload),
                rendered_at=observed_at,
            )
            return CHUNIRECImageResult(
                success=True,
                state=CHUNIRECImageState.SUCCESS,
                image=metadata,
            )
        except _LiveFailure as exc:
            return self._failure(exc)
        except CHUNIRECPNGDecodeError:
            return self._failure(
                _LiveFailure(
                    CHUNIRECImageFailureReason.INVALID_IMAGE,
                    CHUNIRECImageState.INVALID_IMAGE,
                    "image_decode",
                )
            )
        except OSError as exc:
            if stage == "target_page":
                reason = (
                    CHUNIRECImageFailureReason.NETWORK_TIMEOUT
                    if _is_timeout_exception(exc)
                    else CHUNIRECImageFailureReason.NETWORK_ERROR
                )
                return self._failure(_LiveFailure(reason, _failure_state(reason), stage))
            return self._failure(
                _LiveFailure(
                    CHUNIRECImageFailureReason.OUTPUT_WRITE_FAILURE,
                    CHUNIRECImageState.OUTPUT_WRITE_FAILURE,
                    "image_write",
                )
            )
        except Exception as exc:
            if _is_timeout_exception(exc) and stage == "target_page":
                return self._failure(
                    _LiveFailure(
                        CHUNIRECImageFailureReason.NETWORK_TIMEOUT,
                        CHUNIRECImageState.NETWORK_TIMEOUT,
                        "target_page",
                    )
                )
            reason = (
                CHUNIRECImageFailureReason.GENERATOR_NOT_COMPLETED
                if stage == "generator"
                else CHUNIRECImageFailureReason.NETWORK_ERROR
                if stage == "target_page"
                else CHUNIRECImageFailureReason.DOM_MISMATCH
            )
            return self._failure(_LiveFailure(reason, _failure_state(reason), stage))

    def collect(self, *, collected_at: datetime | None = None) -> CHUNIRECImageResult:
        """Launch an ephemeral Chromium session and run one bounded capture."""

        try:
            if self._playwright_factory is None:
                from playwright.sync_api import sync_playwright

                factory = sync_playwright
            else:
                factory = self._playwright_factory
        except ImportError:
            return self._failure(
                _LiveFailure(
                    CHUNIRECImageFailureReason.PLAYWRIGHT_UNAVAILABLE,
                    CHUNIRECImageState.PLAYWRIGHT_UNAVAILABLE,
                    "playwright_import",
                )
            )

        browser = None
        try:
            with factory() as playwright:
                browser = playwright.chromium.launch(headless=self.config.headless)
                page = browser.new_page()
                if hasattr(page, "set_default_timeout"):
                    page.set_default_timeout(self.config.timeout_ms)
                if hasattr(page, "set_default_navigation_timeout"):
                    page.set_default_navigation_timeout(self.config.timeout_ms)
                return self.collect_page(page, collected_at=collected_at)
        except Exception as exc:
            reason = (
                CHUNIRECImageFailureReason.NETWORK_TIMEOUT
                if _is_timeout_exception(exc)
                else CHUNIRECImageFailureReason.NETWORK_ERROR
            )
            return self._failure(_LiveFailure(reason, _failure_state(reason), "browser"))
        finally:
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    pass


# Naming aliases keep this adapter discoverable beside the existing maimai
# live collector without forcing a package-level import or runtime integration.
CHUNIRECImageLiveConfig = CHUNIRECImageConfig
CHUNIRECImageLiveResult = CHUNIRECImageResult
ChunirecImageConfig = CHUNIRECImageConfig
ChunirecImageResult = CHUNIRECImageResult
ChunirecImageState = CHUNIRECImageState
ChunirecImageFailureReason = CHUNIRECImageFailureReason
PlaywrightCHUNIRECImageCollector = PlaywrightChunirecImageCollector
CHUNIRECImageLiveCollector = PlaywrightChunirecImageCollector


__all__ = [
    "BEST_IMAGE_SELECTOR",
    "BEST_TABLE_SELECTOR",
    "CHUNIREC_BASE_URL",
    "CHUNIREC_BEST_IMAGE_SELECTOR",
    "CHUNIRECImageConfig",
    "CHUNIRECImageError",
    "CHUNIRECImageFailureReason",
    "CHUNIRECImageLiveCollector",
    "CHUNIRECImageLiveConfig",
    "CHUNIRECImageLiveResult",
    "CHUNIRECImageResult",
    "CHUNIRECImageState",
    "CHUNIRECPNGDecodeError",
    "ChunirecImageConfig",
    "ChunirecImageFailureReason",
    "ChunirecImageResult",
    "ChunirecImageState",
    "DEFAULT_CHUNIREC_BASE_URL",
    "PlaywrightCHUNIRECImageCollector",
    "PlaywrightChunirecImageCollector",
    "decode_png_bytes",
    "decode_png_data_uri",
]
