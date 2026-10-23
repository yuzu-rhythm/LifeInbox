"""Credential-free ONGEKI NET and image-generator browser runner.

The runner deliberately owns no login flow.  A caller supplies an ONGEKI-only
persistent browser directory that may already contain an authenticated
session, and a human completes login or additional verification in that
browser when necessary.  Credentials, cookies, storage state, page text, and
browser exception messages never become part of a result.

ScoreLog completion is also an explicit gate.  The repository has no stable,
known DOM event that proves the third-party ScoreLog update finished, so this
module stops at a manual-confirmation result unless the caller supplies that
confirmation.  Fixed waiting durations are used only as bounded Playwright
timeouts; they are never success evidence.
"""

from __future__ import annotations

import base64
import hashlib
import os
import struct
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .rhythm_collectors import (
    OngekiBrowserEvent,
    OngekiCollectionResult,
    OngekiCollector,
    OngekiConfig,
    OngekiEventType,
    OngekiFailureReason,
    OngekiState,
    parse_ongeki_data,
)
from .rhythm_images import RatingImageVerificationError, png_dimensions
from .rhythm_rating import RenderedImageMetadata

DEFAULT_ONGEKI_OFFICIAL_URL = "https://ongeki-net.com/ongeki-mobile/home/"
# OngekiScoreLog's published instructions require its bookmarklet to run on
# the official ONGEKI-NET home screen.  Keep a separately named constant for
# the state-machine vocabulary, but do not navigate to a guessed score-log
# path: that legacy path now returns the provider error page.
DEFAULT_ONGEKI_SCORE_LOG_URL = DEFAULT_ONGEKI_OFFICIAL_URL
DEFAULT_ONGEKI_GENERATOR_URL = "https://reiwa.f5.si/newbestimg/ongeki/"

# Short aliases keep the constants convenient for callers while the explicit
# names make their provider ownership clear in configuration and reviews.
ONGEKI_OFFICIAL_URL = DEFAULT_ONGEKI_OFFICIAL_URL
ONGEKI_SCORE_LOG_URL = DEFAULT_ONGEKI_SCORE_LOG_URL
ONGEKI_IMAGE_GENERATOR_URL = DEFAULT_ONGEKI_GENERATOR_URL
_CAPTURED_SCORE_LOG_KEY = "__lifeInboxOngekiScoreLogV1"

# These selectors and paths are part of the public generator's observed DOM
# contract.  They are used only to verify that non-copyright-mode generation
# has real jacket resources available; no jacket bytes are copied or retained.
_ONGEKI_JACKET_SELECTOR = ".img-song-block-img img"
_ONGEKI_JACKET_BASE_PATH = "/musicjackets/ongeki/"
_ONGEKI_JACKET_PLACEHOLDER_PATH = "/commonassets/images/ban2.png"
# The generator currently uses ``../commonassets`` from
# ``/newbestimg/ongeki/``, which resolves one directory above the generator
# path. Keep the original root-relative form for compatibility and recognize
# both observed resolutions when checking non-copyright captures.
_ONGEKI_JACKET_PLACEHOLDER_PATHS = (
    _ONGEKI_JACKET_PLACEHOLDER_PATH,
    "/newbestimg" + _ONGEKI_JACKET_PLACEHOLDER_PATH,
)
_JACKET_READINESS_KEY = "__lifeInboxOngekiJacketReadinessV1"

# The public generator appends jacket <img> nodes and calls html2canvas in the
# same generate() turn.  Waiting after the result image appears is too late:
# html2canvas may already have painted unloaded images.  In non-copyright mode
# wrap that capture boundary and let the image load/error events decide when
# capture may begin.  The state contains only fixed status/code/count values.
_INSTALL_JACKET_READINESS_SCRIPT = f"""
(() => {{
  const key = {_JACKET_READINESS_KEY!r};
  const selector = {_ONGEKI_JACKET_SELECTOR!r};
  const basePath = {_ONGEKI_JACKET_BASE_PATH!r};
  const placeholderPaths = {list(_ONGEKI_JACKET_PLACEHOLDER_PATHS)!r};
  const existing = window[key];
  if (existing && existing.installed) return true;
  const state = {{
    installed: false,
    status: "install_failed",
    code: null,
    count: 0,
    loaded: 0,
  }};
  window[key] = state;
  const original = window.html2canvas;
  if (typeof original !== "function") {{
    state.code = "capture_function_missing";
    return false;
  }}

  const sourcePath = (image) => {{
    const source = image.currentSrc || image.src || "";
    try {{
      return new URL(source, document.baseURI).pathname;
    }} catch (error) {{
      return source;
    }}
  }};
  const imageFailure = (code) => {{
    const error = new Error("required jacket image was not ready");
    error.code = code;
    return error;
  }};
  const waitForImage = (image) => new Promise((resolve, reject) => {{
    let settled = false;
    const finish = (success, code) => {{
      if (settled) return;
      settled = true;
      image.removeEventListener("load", onLoad);
      image.removeEventListener("error", onError);
      if (success) {{
        state.loaded += 1;
        resolve(true);
      }} else {{
        reject(imageFailure(code));
      }}
    }};
    const check = () => {{
      const path = sourcePath(image);
      if (placeholderPaths.some((placeholder) => path.endsWith(placeholder)))
        return finish(false, "placeholder_jacket");
      if (!path.includes(basePath)) return finish(false, "unexpected_jacket_source");
      if (!image.complete) return;
      finish(
        image.naturalWidth > 0 && image.naturalHeight > 0,
        "jacket_image_load_failed",
      );
    }};
    const onLoad = () => check();
    const onError = () => finish(false, "jacket_image_load_failed");
    image.addEventListener("load", onLoad);
    image.addEventListener("error", onError);
    check();
  }});
  const waitForJackets = () => {{
    const images = [...document.querySelectorAll(selector)];
    state.status = "waiting";
    state.code = null;
    state.count = images.length;
    state.loaded = 0;
    if (!images.length) return Promise.reject(imageFailure("jacket_elements_missing"));
    return Promise.all(images.map(waitForImage))
      .then(() => {{
        state.status = "ready";
        return true;
      }})
      .catch((error) => {{
        state.status = "failed";
        state.code = typeof error.code === "string"
          ? error.code : "jacket_readiness_failed";
        throw error;
      }});
  }};

  window.html2canvas = function(...args) {{
    const target = args[0];
    const computedDisplay = target instanceof HTMLElement
      ? getComputedStyle(target).display : "block";
    const captureDisplay = target instanceof HTMLElement
      ? (target.style.display && target.style.display !== "none"
          ? target.style.display
          : computedDisplay !== "none" ? computedDisplay : "block")
      : null;
    const hideTarget = () => {{
      if (target instanceof HTMLElement) target.style.display = "none";
    }};
    return waitForJackets()
      .then(() => {{
        // The provider hides #pre-render-area immediately after calling
        // html2canvas. Because this wrapper waits for jacket load events
        // first, restore the display state captured at invocation time and
        // keep it renderable until the canvas promise settles.
        if (target instanceof HTMLElement && captureDisplay) {{
          target.style.display = captureDisplay;
        }}
        state.status = "capturing";
        return Promise.resolve(Reflect.apply(original, this, args));
      }})
      .then((value) => {{
        hideTarget();
        state.status = "captured";
        return value;
      }})
      .catch((error) => {{
        hideTarget();
        if (state.status !== "failed") {{
          state.status = "capture_failed";
          state.code = "canvas_capture_failed";
        }}
        throw error;
      }});
  }};
  state.installed = true;
  state.status = "armed";
  return true;
}})()
"""

_JACKET_READINESS_TERMINAL_SCRIPT = f"""
() => {{
  const state = window[{_JACKET_READINESS_KEY!r}];
  return Boolean(state && ["captured", "failed", "capture_failed"].includes(state.status));
}}
"""

_JACKET_READINESS_STATUS_SCRIPT = f"""
() => {{
  const state = window[{_JACKET_READINESS_KEY!r}];
  if (!state) return null;
  return {{
    status: typeof state.status === "string" ? state.status : null,
    code: typeof state.code === "string" ? state.code : null,
  }};
}}
"""


class OngekiLiveOutcome(StrEnum):
    """Top-level outcomes with authentication/manual gates kept distinct."""

    SUCCESS = "success"
    AUTHENTICATION_REQUIRED = "authentication_required"
    MANUAL_ACTION_REQUIRED = "manual_action_required"
    PROVIDER_FAILURE = "provider_failure"
    DIAGNOSTIC_FAILURE = "diagnostic_failure"


class OngekiLiveStage(StrEnum):
    """Fixed internal stages safe to expose in diagnostics."""

    PLAYWRIGHT_IMPORT = "playwright_import"
    SESSION_DIRECTORY = "session_directory"
    BROWSER_LAUNCH = "browser_launch"
    OFFICIAL_NET = "official_net"
    SCORE_LOG_UPDATE = "score_log_update"
    SCORE_LOG_CONFIRMATION = "score_log_confirmation"
    SCORE_LOG_DATA = "score_log_data"
    GENERATOR = "generator"
    USER_MODE = "user_mode"
    IMAGE_GENERATION = "image_generation"
    IMAGE_DECODE = "image_decode"
    IMAGE_WRITE = "image_write"
    COLLECTOR_NORMALIZATION = "collector_normalization"


class OngekiLiveDiagnosticCode(StrEnum):
    """Non-secret fixed diagnostic codes; no provider text is retained."""

    PLAYWRIGHT_UNAVAILABLE = "playwright_unavailable"
    SESSION_DIRECTORY_FAILURE = "session_directory_failure"
    BROWSER_LAUNCH_FAILURE = "browser_launch_failure"
    URL_MISMATCH = "url_mismatch"
    BROWSER_OPERATION_FAILED = "browser_operation_failed"
    PROVIDER_ERROR_SIGNAL = "provider_error_signal"
    BOOKMARKLET_LOAD_FAILURE = "bookmarklet_load_failure"
    SCORE_LOG_CONFIRMATION_REQUIRED = "score_log_confirmation_required"
    SCORE_LOG_DATA_MISSING = "score_log_data_missing"
    USER_MODE_CONFIGURATION_REQUIRED = "user_mode_configuration_required"
    GENERATOR_CONFIGURATION_REQUIRED = "generator_configuration_required"
    GENERATION_EVENT_MISSING = "generation_event_missing"
    JACKET_READINESS_INSTALL_FAILED = "jacket_readiness_install_failed"
    JACKET_IMAGES_NOT_READY = "jacket_images_not_ready"
    JACKET_ELEMENTS_MISSING = "jacket_elements_missing"
    JACKET_PLACEHOLDER_DETECTED = "jacket_placeholder_detected"
    JACKET_SOURCE_UNEXPECTED = "jacket_source_unexpected"
    JACKET_IMAGE_LOAD_FAILED = "jacket_image_load_failed"
    JACKET_READINESS_FAILED = "jacket_readiness_failed"
    IMAGE_DECODE_FAILURE = "image_decode_failure"
    IMAGE_WRITE_FAILURE = "image_write_failure"
    EVENT_ORDER_INVALID = "event_order_invalid"
    STALE_ARTIFACT = "stale_artifact"


class OngekiLiveFailureReason(StrEnum):
    """Runner-level reasons, including auth and external-generator failures."""

    AUTH_REQUIRED = "authentication_required"
    MANUAL_ACTION_REQUIRED = "manual_action_required"
    MANUAL_CONFIRMATION_REQUIRED = "manual_confirmation_required"
    USER_NOT_FOUND = "user_not_found"
    PRIVATE_MODE = "private_mode"
    PREMIUM_MISSING = "premium_missing"
    PROVIDER_NETWORK_OUTAGE = "provider_network_outage"
    EXTERNAL_SERVICE_FAILURE = "external_service_failure"
    IMAGE_MISSING = "image_missing"
    DOM_MISMATCH = "dom_mismatch"
    BOOKMARKLET_LOAD_FAILURE = "bookmarklet_load_failure"
    USER_ID_REQUIRED = "user_id_required"
    SCORE_LOG_DATA_MISSING = "score_log_data_missing"
    IMAGE_WAITING = "image_waiting"
    JACKET_IMAGES_NOT_READY = "jacket_images_not_ready"
    TIMEOUT = "timeout"
    STALE_IMAGE = "stale_image"
    PLAYWRIGHT_UNAVAILABLE = "playwright_unavailable"


@dataclass(frozen=True, slots=True)
class OngekiLiveDiagnostic:
    """Safe fixed-stage diagnostic without exception or page content."""

    stage: OngekiLiveStage
    code: OngekiLiveDiagnosticCode


@dataclass(frozen=True, slots=True)
class DecodedOngekiImage:
    """A complete PNG/JPEG artifact decoded from a generator event."""

    payload: bytes = field(repr=False)
    mime_type: str
    width: int
    height: int

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()


@dataclass(frozen=True, slots=True)
class OngekiLiveConfig:
    """Caller-owned, runtime-only ONGEKI browser configuration.

    There are intentionally no credential fields.  ``session_dir`` is an
    ONGEKI-specific persistent browser directory supplied by the caller; it is
    never inferred from a shared browser path and is never serialized by this
    module.  The optional selectors are explicit because silently guessing a
    changed generator DOM could select or publish the wrong user's image.
    """

    session_dir: str | Path
    output_path: str | Path
    user_id: str | None = field(default=None, repr=False)
    mode: str = "bestnew"
    copyright_mode: bool = True
    score_log_bookmarklet_path: str | Path | None = field(default=None, repr=False)
    official_url: str = DEFAULT_ONGEKI_OFFICIAL_URL
    score_log_url: str = DEFAULT_ONGEKI_SCORE_LOG_URL
    generator_url: str = DEFAULT_ONGEKI_GENERATOR_URL
    user_id_selector: str | None = "#osl_id"
    mode_selector: str | None = "#display-content"
    copyright_mode_selector: str | None = "#copyrightmode"
    generate_selector: str | None = "#generate"
    image_selector: str | None = "#result-img"
    headless: bool = True
    timeout_seconds: int = 120
    premium_required: bool = True

    def __post_init__(self) -> None:
        session = str(self.session_dir).strip()
        output = str(self.output_path).strip()
        if not session or not output:
            raise ValueError("session_dir and output_path are required")
        object.__setattr__(self, "session_dir", session)
        object.__setattr__(self, "output_path", output)

        if self.user_id is not None and not isinstance(self.user_id, str):
            raise ValueError("user_id must be a string or None")
        if self.user_id is not None:
            user_id = self.user_id.strip()
            object.__setattr__(self, "user_id", user_id or None)
        mode = self.mode.strip() if isinstance(self.mode, str) else ""
        if not mode:
            raise ValueError("mode is required")
        if mode not in {"bestnew", "pscore"}:
            raise ValueError("mode must be bestnew or pscore")
        object.__setattr__(self, "mode", mode)
        if type(self.copyright_mode) is not bool:
            raise ValueError("copyright_mode must be a boolean")

        if self.score_log_bookmarklet_path is not None:
            runtime = str(self.score_log_bookmarklet_path).strip()
            object.__setattr__(
                self,
                "score_log_bookmarklet_path",
                runtime or None,
            )

        object.__setattr__(
            self,
            "official_url",
            _require_exact_url(
                self.official_url,
                DEFAULT_ONGEKI_OFFICIAL_URL,
                "official ONGEKI NET URL",
            ),
        )
        object.__setattr__(
            self,
            "score_log_url",
            _require_exact_url(
                self.score_log_url,
                DEFAULT_ONGEKI_SCORE_LOG_URL,
                "OngekiScoreLog URL",
            ),
        )
        object.__setattr__(
            self,
            "generator_url",
            _require_exact_url(
                self.generator_url,
                DEFAULT_ONGEKI_GENERATOR_URL,
                "ONGEKI image generator URL",
            ),
        )

        for field_name in (
            "user_id_selector",
            "mode_selector",
            "copyright_mode_selector",
            "generate_selector",
            "image_selector",
        ):
            value = getattr(self, field_name)
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError(f"{field_name} must be a string or None")
                object.__setattr__(self, field_name, value.strip() or None)
        if type(self.headless) is not bool:
            raise ValueError("headless must be a boolean")
        if type(self.premium_required) is not bool:
            raise ValueError("premium_required must be a boolean")
        if type(self.timeout_seconds) is not int or not 10 <= self.timeout_seconds <= 300:
            raise ValueError("browser timeout must be between 10 and 300 seconds")

    @property
    def session_directory(self) -> str:
        return str(self.session_dir)

    @property
    def runtime_file(self) -> str | None:
        return (
            str(self.score_log_bookmarklet_path)
            if self.score_log_bookmarklet_path is not None
            else None
        )

    @property
    def score_log_user_id(self) -> str | None:
        """Compatibility vocabulary for the existing environment setting."""

        return self.user_id

    @property
    def image_mode(self) -> str:
        """Compatibility vocabulary for the existing environment setting."""

        return self.mode


@dataclass(frozen=True, slots=True)
class OngekiLiveResult:
    """Detached runner outcome with explicit auth/manual/provider classes."""

    outcome: OngekiLiveOutcome
    success: bool
    state: OngekiState | None = None
    failure_reason: OngekiLiveFailureReason | None = None
    collection: OngekiCollectionResult | None = field(default=None, repr=False)
    image: RenderedImageMetadata | None = None
    diagnostic: OngekiLiveDiagnostic | None = None
    events: tuple[OngekiBrowserEvent, ...] = field(default=(), repr=False)
    authentication_required: bool = False
    manual_action_required: bool = False
    message: str | None = None

    def __post_init__(self) -> None:
        if type(self.success) is not bool:
            raise ValueError("success must be a boolean")
        if self.success != (self.outcome is OngekiLiveOutcome.SUCCESS):
            raise ValueError("success must agree with outcome")
        if self.success and self.failure_reason is not None:
            raise ValueError("successful result cannot have a failure reason")

    @property
    def complete(self) -> bool:
        return (
            self.success
            and self.collection is not None
            and self.collection.complete
            and self.image is not None
        )

    @property
    def waiting_for_manual_action(self) -> bool:
        return self.manual_action_required or self.authentication_required

    @property
    def waitingForManualAction(self) -> bool:  # noqa: N802 - external event vocabulary
        return self.waiting_for_manual_action

    @property
    def reason(self) -> OngekiLiveFailureReason | None:
        return self.failure_reason


def _require_exact_url(value: object, expected: str, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be the exact HTTPS URL")
    candidate = value.strip()
    parsed = urlsplit(candidate)
    if (
        candidate != expected
        or parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.hostname is None
    ):
        raise ValueError(f"{label} must be the exact HTTPS URL")
    return candidate


def _safe_url(value: object, fallback: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


def _url_matches(observed: str, expected: str) -> bool:
    observed_parts = urlsplit(observed)
    expected_parts = urlsplit(expected)
    return (
        observed_parts.scheme == "https"
        and observed_parts.hostname == expected_parts.hostname
        and observed_parts.port == expected_parts.port
        and observed_parts.path.rstrip("/") == expected_parts.path.rstrip("/")
        and not observed_parts.username
        and not observed_parts.password
    )


def _is_timeout_error(error: object) -> bool:
    """Classify a timeout internally without exposing its concrete text."""

    return (
        isinstance(error, TimeoutError)
        or "timeout" in type(error).__name__.casefold()
        or "timed out" in str(error).casefold()
    )


def _is_network_error(error: object) -> bool:
    """Classify bounded browser/network failures without exposing their text."""

    try:
        text = str(error).casefold()
    except Exception:
        text = ""
    text = f"{type(error).__name__.casefold()} {text}"
    return any(
        marker in text
        for marker in (
            "network",
            "connection",
            "dns",
            "unreachable",
            "refused",
            "reset",
            "failed to fetch",
            "net::err_",
            "err_failed",
            "err_name_not_resolved",
            "err_internet_disconnected",
            "502",
            "503",
            "504",
        )
    )


def _hash_existing(path: str | Path | None) -> str | None:
    if path is None:
        return None
    try:
        candidate = Path(path)
        if candidate.is_file():
            return hashlib.sha256(candidate.read_bytes()).hexdigest()
    except OSError:
        return None
    return None


def _jpeg_dimensions(payload: bytes) -> tuple[int, int]:
    """Validate a complete JPEG stream and return its SOF dimensions."""

    raw = bytes(payload)
    if len(raw) < 4 or raw[:2] != b"\xff\xd8":
        raise RatingImageVerificationError("rating image has an invalid JPEG signature")
    offset = 2
    dimensions: tuple[int, int] | None = None
    saw_scan = False
    standalone = {0x01, *range(0xD0, 0xD8)}
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
    while offset < len(raw):
        if raw[offset] != 0xFF:
            raise RatingImageVerificationError("rating image has an invalid JPEG marker")
        while offset < len(raw) and raw[offset] == 0xFF:
            offset += 1
        if offset >= len(raw):
            raise RatingImageVerificationError("rating image has a truncated JPEG marker")
        marker = raw[offset]
        offset += 1
        if marker == 0xD9:
            if dimensions is None or not saw_scan:
                raise RatingImageVerificationError("rating image has no JPEG dimensions")
            if offset != len(raw):
                raise RatingImageVerificationError("rating image has trailing JPEG data")
            return dimensions
        if marker in standalone:
            continue
        if offset + 2 > len(raw):
            raise RatingImageVerificationError("rating image has a truncated JPEG segment")
        segment_length = struct.unpack(">H", raw[offset : offset + 2])[0]
        if segment_length < 2:
            raise RatingImageVerificationError("rating image has an invalid JPEG segment")
        segment_end = offset + segment_length
        if segment_end > len(raw):
            raise RatingImageVerificationError("rating image has a truncated JPEG segment")
        if marker in sof_markers:
            if segment_length < 7:
                raise RatingImageVerificationError("rating image has an invalid JPEG frame")
            height, width = struct.unpack(">HH", raw[offset + 3 : offset + 7])
            if width < 1 or height < 1:
                raise RatingImageVerificationError("rating image dimensions are invalid")
            dimensions = (width, height)
        offset = segment_end
        if marker == 0xDA:
            saw_scan = True
            # The entropy-coded scan is not a length-delimited segment.  Walk
            # stuffed bytes and restart markers until the mandatory EOI.
            while offset < len(raw):
                if raw[offset] != 0xFF:
                    offset += 1
                    continue
                while offset < len(raw) and raw[offset] == 0xFF:
                    offset += 1
                if offset >= len(raw):
                    raise RatingImageVerificationError("rating image has no JPEG EOI")
                scan_marker = raw[offset]
                if scan_marker == 0x00 or 0xD0 <= scan_marker <= 0xD7:
                    offset += 1
                    continue
                if scan_marker != 0xD9:
                    # A second marker inside the scan requires a more
                    # specific JPEG parser; refusing it is safer than
                    # accepting a partial or ambiguous artifact.
                    raise RatingImageVerificationError(
                        "rating image has an unexpected JPEG scan marker"
                    )
                offset += 1
                if dimensions is None or offset != len(raw):
                    raise RatingImageVerificationError("rating image has an incomplete JPEG")
                return dimensions
            raise RatingImageVerificationError("rating image has no JPEG EOI")
    raise RatingImageVerificationError("rating image has no JPEG EOI")


def decode_image_bytes(payload: object, *, mime_type: str | None = None) -> DecodedOngekiImage:
    """Validate a complete PNG/JPEG artifact and return detached metadata."""

    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise RatingImageVerificationError("rating image payload is not bytes")
    raw = bytes(payload)
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        dimensions = png_dimensions(raw, require_complete=True)
        if dimensions is None:
            raise RatingImageVerificationError("rating image has no PNG dimensions")
        actual_mime = "image/png"
    elif raw.startswith(b"\xff\xd8"):
        dimensions = _jpeg_dimensions(raw)
        actual_mime = "image/jpeg"
    else:
        raise RatingImageVerificationError("rating image is neither PNG nor JPEG")
    if mime_type is not None and mime_type.casefold() != actual_mime:
        raise RatingImageVerificationError("rating image MIME type does not match bytes")
    return DecodedOngekiImage(raw, actual_mime, dimensions[0], dimensions[1])


def decode_image_data_uri(value: object) -> DecodedOngekiImage:
    """Decode a base64 PNG/JPEG data URI emitted by the generator DOM."""

    if not isinstance(value, str) or "," not in value:
        raise RatingImageVerificationError("rating image is not a data URI")
    header, encoded = value.split(",", 1)
    parts = header.casefold().split(";")
    if len(parts) < 2 or parts[0] not in {"data:image/png", "data:image/jpeg", "data:image/jpg"}:
        raise RatingImageVerificationError("rating image data URI MIME type is unsupported")
    if "base64" not in parts[1:]:
        raise RatingImageVerificationError("rating image data URI is not base64")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise RatingImageVerificationError("rating image base64 is invalid") from exc
    expected_mime = "image/jpeg" if parts[0] == "data:image/jpg" else parts[0][5:]
    return decode_image_bytes(payload, mime_type=expected_mime)


def decode_ongeki_image_data_uri(value: object) -> DecodedOngekiImage:
    """ASCII-friendly compatibility alias for the public decoder."""

    return decode_image_data_uri(value)


def decode_png_data_uri(value: object) -> tuple[bytes, int, int]:
    """Compatibility helper matching the existing maimai runner contract."""

    image = decode_image_data_uri(value)
    if image.mime_type != "image/png":
        raise RatingImageVerificationError("rating image is not a PNG data URI")
    return image.payload, image.width, image.height


def write_image_atomically(output_path: str | Path, image: DecodedOngekiImage | bytes) -> Path:
    """Write one already-validated artifact with an atomic replacement."""

    decoded = image if isinstance(image, DecodedOngekiImage) else decode_image_bytes(image)
    destination = Path(output_path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(decoded.payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return destination


def _collector_config(config: OngekiLiveConfig) -> OngekiConfig:
    runtime_path = config.score_log_bookmarklet_path
    if runtime_path is None:
        # The reducer requires a runtime path even for event-only tests.  This
        # placeholder is never read or created; the live runner returns a
        # manual/diagnostic result before attempting to use it.
        runtime_path = Path(config.session_dir) / ".ongeki-runtime-not-configured.js"
    return OngekiConfig(
        persistent_session_dir=config.session_dir,
        bookmarklet_runtime_path=runtime_path,
        official_url=config.official_url,
        score_log_url=config.score_log_url,
        generator_url=config.generator_url,
        user_id_selector=config.user_id_selector,
        mode_selector=config.mode_selector,
        premium_required=config.premium_required,
    )


def _new_collector(
    config: OngekiLiveConfig,
    *,
    last_successful_image: str | Path | None,
) -> OngekiCollector:
    return OngekiCollector(
        _collector_config(config),
        last_successful_image=last_successful_image,
    )


def _map_reason(reason: OngekiFailureReason | None) -> OngekiLiveFailureReason | None:
    if reason is None:
        return None
    if reason is OngekiFailureReason.MANUAL_CONFIRMATION_REQUIRED:
        return OngekiLiveFailureReason.MANUAL_CONFIRMATION_REQUIRED
    try:
        return OngekiLiveFailureReason(reason.value)
    except ValueError:
        return OngekiLiveFailureReason.DOM_MISMATCH


def _outcome_for_reason(reason: OngekiLiveFailureReason | None) -> OngekiLiveOutcome:
    if reason in {
        OngekiLiveFailureReason.AUTH_REQUIRED,
    }:
        return OngekiLiveOutcome.AUTHENTICATION_REQUIRED
    if reason in {
        OngekiLiveFailureReason.MANUAL_ACTION_REQUIRED,
        OngekiLiveFailureReason.MANUAL_CONFIRMATION_REQUIRED,
        OngekiLiveFailureReason.USER_ID_REQUIRED,
        OngekiLiveFailureReason.IMAGE_WAITING,
    }:
        return OngekiLiveOutcome.MANUAL_ACTION_REQUIRED
    if reason is not None:
        return OngekiLiveOutcome.PROVIDER_FAILURE
    return OngekiLiveOutcome.DIAGNOSTIC_FAILURE


def _message(outcome: OngekiLiveOutcome, stage: OngekiLiveStage) -> str:
    """Build a fixed diagnostic message with no exception/page text."""

    return f"ongeki_live:{outcome.value}:{stage.value}"


def _metadata_from_collection(
    collection: OngekiCollectionResult,
) -> RenderedImageMetadata | None:
    """Detach image metadata for both path-backed and synthetic image events."""

    payload = collection.image_bytes
    path = collection.image_path
    if payload is None and path is not None:
        try:
            payload = Path(path).read_bytes()
        except OSError:
            return None
    if payload is None:
        return None
    try:
        decoded = decode_image_bytes(payload)
    except RatingImageVerificationError:
        return None
    return RenderedImageMetadata(
        path=path or "(in-memory)",
        sha256=hashlib.sha256(payload).hexdigest(),
        mime_type=decoded.mime_type,
        width=decoded.width,
        height=decoded.height,
        byte_size=len(payload),
    )


def _result(
    collection: OngekiCollectionResult | None,
    *,
    stage: OngekiLiveStage,
    reason: OngekiLiveFailureReason | None = None,
    diagnostic: OngekiLiveDiagnostic | None = None,
    image: RenderedImageMetadata | None = None,
    authentication_required: bool = False,
    manual_action_required: bool = False,
) -> OngekiLiveResult:
    if collection is not None and reason is None:
        reason = _map_reason(collection.failure_reason)
        if reason is None and collection.state in {
            OngekiState.SCORE_LOG_REFRESH_REQUIRED,
            OngekiState.ADMIN_CONFIRMATION_REQUIRED,
        }:
            reason = OngekiLiveFailureReason.MANUAL_CONFIRMATION_REQUIRED
            manual_action_required = True
        elif reason is None and collection.state is OngekiState.USER_ID_REQUIRED:
            reason = OngekiLiveFailureReason.USER_ID_REQUIRED
            manual_action_required = True
        elif reason is None and not collection.complete:
            reason = OngekiLiveFailureReason.SCORE_LOG_DATA_MISSING

    if collection is not None and collection.complete and reason is None:
        outcome = OngekiLiveOutcome.SUCCESS
        success = True
        authentication_required = False
        manual_action_required = False
    else:
        outcome = (
            OngekiLiveOutcome.AUTHENTICATION_REQUIRED
            if authentication_required
            else _outcome_for_reason(reason)
        )
        success = False
    if outcome is OngekiLiveOutcome.MANUAL_ACTION_REQUIRED:
        manual_action_required = True
    if outcome is OngekiLiveOutcome.AUTHENTICATION_REQUIRED:
        authentication_required = True
        manual_action_required = True
    return OngekiLiveResult(
        outcome=outcome,
        success=success,
        state=collection.state if collection is not None else None,
        failure_reason=reason,
        collection=collection,
        image=image,
        diagnostic=diagnostic,
        events=collection.events if collection is not None else (),
        authentication_required=authentication_required,
        manual_action_required=manual_action_required,
        message=_message(outcome, stage),
    )


def _event_order_diagnostic(
    collector: OngekiCollector,
    stage: OngekiLiveStage,
) -> OngekiLiveResult:
    return _result(
        collector.result(),
        stage=stage,
        reason=OngekiLiveFailureReason.MANUAL_ACTION_REQUIRED,
        diagnostic=OngekiLiveDiagnostic(
            stage,
            OngekiLiveDiagnosticCode.EVENT_ORDER_INVALID,
        ),
        manual_action_required=True,
    )


def collect_ongeki_events(
    config: OngekiLiveConfig,
    events: Iterable[OngekiBrowserEvent],
    *,
    last_successful_image: str | Path | None = None,
) -> OngekiLiveResult:
    """Reduce explicit synthetic/browser events through the existing collector.

    This is the pure seam used by tests and by the live runner after each
    observable operation.  It rejects generator/image events before their
    ScoreLog completion, manual confirmation, data, and user/mode gates.
    """

    collector = _new_collector(config, last_successful_image=last_successful_image)
    official = False
    manual_external_refresh = False
    refreshed = False
    confirmed = False
    data_available = False
    generator = False
    user_mode = False
    generated = False

    for event in events:
        event_type = event.event_type
        if event_type in {
            OngekiEventType.USER_NOT_FOUND,
            OngekiEventType.PRIVATE_MODE,
            OngekiEventType.PREMIUM_MISSING,
            OngekiEventType.PROVIDER_NETWORK_OUTAGE,
            OngekiEventType.BOOKMARKLET_LOAD_FAILED,
            OngekiEventType.IMAGE_MISSING,
            OngekiEventType.JACKET_IMAGES_NOT_READY,
            OngekiEventType.DOM_MISMATCH,
            OngekiEventType.TIMEOUT,
            OngekiEventType.SCORE_LOG_DATA_FAILED,
        }:
            collector.observe(event)
            return _result(collector.result(), stage=OngekiLiveStage.COLLECTOR_NORMALIZATION)
        if event_type in {
            OngekiEventType.OFFICIAL_NET_OPENED,
            OngekiEventType.URL_OBSERVED,
        }:
            if official or refreshed or generator or user_mode or generated:
                return _event_order_diagnostic(collector, OngekiLiveStage.OFFICIAL_NET)
            official = True
        elif event_type is OngekiEventType.MANUAL_SCORE_LOG_REFRESH_CONFIRMED:
            if (
                official
                or manual_external_refresh
                or refreshed
                or generator
                or not event.completed
                or not event.explicit_confirmation
            ):
                return _event_order_diagnostic(
                    collector,
                    OngekiLiveStage.SCORE_LOG_CONFIRMATION,
                )
            manual_external_refresh = True
            refreshed = True
            confirmed = True
        elif event_type is OngekiEventType.SCORE_LOG_REFRESH_STARTED:
            if not official or refreshed or generator:
                return _event_order_diagnostic(collector, OngekiLiveStage.SCORE_LOG_UPDATE)
        elif event_type is OngekiEventType.BOOKMARKLET_LOADED:
            if not official or refreshed or generator:
                return _event_order_diagnostic(collector, OngekiLiveStage.SCORE_LOG_UPDATE)
        elif event_type is OngekiEventType.SCORE_LOG_REFRESH_COMPLETED:
            if not official or refreshed or generator or not event.completed:
                return _event_order_diagnostic(collector, OngekiLiveStage.SCORE_LOG_UPDATE)
            refreshed = True
            confirmed = event.explicit_confirmation
            if event.payload is not None:
                # The current generator can expose its parsed ScoreLog as an
                # explicit payload on the refresh-completed event. Mirror the
                # collector's embedded-data handling before applying the
                # remaining event-order gates; otherwise a valid payload is
                # incorrectly reported as missing at image generation.
                try:
                    parse_ongeki_data(event.payload)
                except (TypeError, ValueError):
                    data_available = False
                else:
                    data_available = True
        elif event_type is OngekiEventType.SCORE_LOG_DATA_AVAILABLE:
            # The public generator exposes its normalized ScoreLog payload
            # from parsePage while preparing the image.  Therefore live
            # evidence can legitimately arrive after GENERATOR_OPENED and
            # USER_MODE_SELECTED, but it must still arrive before the image
            # generation event is accepted.
            if not refreshed or generated or not event.completed:
                return _event_order_diagnostic(collector, OngekiLiveStage.SCORE_LOG_DATA)
            try:
                parse_ongeki_data(event.payload)
            except (TypeError, ValueError):
                collector.observe(event)
                return _result(
                    collector.result(),
                    stage=OngekiLiveStage.SCORE_LOG_DATA,
                    diagnostic=OngekiLiveDiagnostic(
                        OngekiLiveStage.SCORE_LOG_DATA,
                        OngekiLiveDiagnosticCode.SCORE_LOG_DATA_MISSING,
                    ),
                )
            data_available = True
        elif event_type is OngekiEventType.ADMIN_CONFIRMATION:
            if not refreshed or generator or not event.completed or not event.explicit_confirmation:
                return _event_order_diagnostic(
                    collector,
                    OngekiLiveStage.SCORE_LOG_CONFIRMATION,
                )
            confirmed = True
        elif event_type is OngekiEventType.GENERATOR_OPENED:
            if (
                not (official or manual_external_refresh)
                or not refreshed
                or not confirmed
                or generator
            ):
                return _event_order_diagnostic(collector, OngekiLiveStage.GENERATOR)
            generator = True
        elif event_type is OngekiEventType.USER_MODE_SELECTED:
            if not generator or user_mode or not event.completed:
                return _event_order_diagnostic(collector, OngekiLiveStage.USER_MODE)
            if (
                config.user_id is None
                or event.user_id != config.user_id
                or not config.mode
                or event.mode != config.mode
            ):
                collector.observe(
                    OngekiBrowserEvent(OngekiEventType.DOM_MISMATCH, completed=True)
                )
                return _result(
                    collector.result(),
                    stage=OngekiLiveStage.USER_MODE,
                    reason=OngekiLiveFailureReason.USER_ID_REQUIRED,
                    diagnostic=OngekiLiveDiagnostic(
                        OngekiLiveStage.USER_MODE,
                        OngekiLiveDiagnosticCode.USER_MODE_CONFIGURATION_REQUIRED,
                    ),
                    manual_action_required=True,
                )
            user_mode = True
        elif event_type is OngekiEventType.IMAGE_GENERATED:
            if (
                not user_mode
                or not data_available
                or generated
                or not event.completed
                or not event.generation_id
            ):
                return _event_order_diagnostic(collector, OngekiLiveStage.IMAGE_GENERATION)
            generated = True
        elif event_type is OngekiEventType.IMAGE_SAVED:
            if not generated or not event.completed:
                return _event_order_diagnostic(collector, OngekiLiveStage.IMAGE_DECODE)
        elif event_type is OngekiEventType.IMAGE_WAITING:
            if not user_mode:
                return _event_order_diagnostic(collector, OngekiLiveStage.IMAGE_GENERATION)

        collector.observe(event)
        if collector.state in {
            OngekiState.USER_NOT_FOUND,
            OngekiState.PRIVATE_MODE,
            OngekiState.PREMIUM_MISSING,
            OngekiState.PROVIDER_NETWORK_OUTAGE,
            OngekiState.DOM_MISMATCH,
            OngekiState.JACKET_IMAGES_NOT_READY,
            OngekiState.BOOKMARKLET_LOAD_FAILURE,
            OngekiState.TIMEOUT,
            OngekiState.SCORE_LOG_DATA_MISSING,
        }:
            return _result(collector.result(), stage=OngekiLiveStage.COLLECTOR_NORMALIZATION)

    if not generated:
        stage = (
            OngekiLiveStage.OFFICIAL_NET
            if not (official or manual_external_refresh)
            else OngekiLiveStage.SCORE_LOG_CONFIRMATION
            if not refreshed or not confirmed
            else OngekiLiveStage.GENERATOR
            if not generator
            else OngekiLiveStage.USER_MODE
            if not user_mode
            else OngekiLiveStage.IMAGE_GENERATION
        )
        return _result(
            collector.result(),
            stage=stage,
            reason=(
                OngekiLiveFailureReason.MANUAL_CONFIRMATION_REQUIRED
                if (official or manual_external_refresh) and (not refreshed or not confirmed)
                else OngekiLiveFailureReason.MANUAL_ACTION_REQUIRED
            ),
            diagnostic=OngekiLiveDiagnostic(
                stage,
                (
                    OngekiLiveDiagnosticCode.SCORE_LOG_CONFIRMATION_REQUIRED
                    if stage is OngekiLiveStage.SCORE_LOG_CONFIRMATION
                    else OngekiLiveDiagnosticCode.EVENT_ORDER_INVALID
                ),
            ),
            manual_action_required=True,
        )
    final_collection = collector.result()
    return _result(
        final_collection,
        stage=OngekiLiveStage.COLLECTOR_NORMALIZATION,
        image=_metadata_from_collection(final_collection),
    )


def _locator_count(page: object, selector: str) -> int | None:
    try:
        locator = page.locator(selector)
        count = locator.count()
    except Exception:
        return None
    return count if type(count) is int else None


def _login_form_observed(page: object) -> bool:
    """Detect login without reading or filling credentials."""

    for selector in (
        'input[name="segaId"]',
        'input[name="password"]',
        'form[action*="submit"] input[type="password"]',
    ):
        count = _locator_count(page, selector)
        if count is not None and count > 0:
            return True
    return False


def _auth_redirect_observed(page: object, requested_url: str) -> bool:
    try:
        observed = getattr(page, "url", None)
    except Exception:
        observed = None
    if not isinstance(observed, str) or not observed.strip():
        observed = requested_url
    path = urlsplit(observed).path.casefold()
    return any(marker in path for marker in ("/login", "/signin", "/auth", "/error"))


def _observed_url(page: object, requested_url: str) -> str:
    try:
        observed = getattr(page, "url", None)
    except Exception:
        observed = None
    return _safe_url(observed, requested_url)


def _status_reason(value: object) -> OngekiLiveFailureReason | None:
    if isinstance(value, OngekiLiveFailureReason):
        return value
    if isinstance(value, OngekiFailureReason):
        return _map_reason(value)
    if isinstance(value, str):
        try:
            return OngekiLiveFailureReason(value.strip().casefold())
        except ValueError:
            return _classify_ongeki_provider_message(value)
    return None


def _classify_ongeki_provider_message(value: object) -> OngekiLiveFailureReason | None:
    """Map only known generator status text to fixed, non-secret reasons.

    The current public generator reports provider failures through Japanese
    alerts rather than a stable DOM status node.  We inspect the text only in
    memory and return a fixed enum; the text itself never enters diagnostics.
    Ambiguous ``Score not fetched`` is kept as missing ScoreLog data unless
    the alert explicitly mentions private mode.
    """

    if not isinstance(value, str):
        return None
    normalized = " ".join(value.casefold().split())
    if "user not found" in normalized or "ユーザーが見つかりません" in normalized:
        return OngekiLiveFailureReason.USER_NOT_FOUND
    if "premium user only" in normalized or "プレミアムコース" in normalized:
        return OngekiLiveFailureReason.PREMIUM_MISSING
    if "private mode" in normalized or "プライベートモード" in normalized:
        return OngekiLiveFailureReason.PRIVATE_MODE
    if "score not fetched" in normalized or "スコアを登録していません" in normalized:
        return OngekiLiveFailureReason.SCORE_LOG_DATA_MISSING
    if any(
        marker in normalized
        for marker in (
            "failed to fetch",
            "networkerror",
            "network error",
            "ネットワークエラー",
            "通信エラー",
        )
    ):
        return OngekiLiveFailureReason.PROVIDER_NETWORK_OUTAGE
    return None


def _failure_event(reason: OngekiLiveFailureReason) -> OngekiBrowserEvent | None:
    event_map = {
        OngekiLiveFailureReason.USER_NOT_FOUND: OngekiEventType.USER_NOT_FOUND,
        OngekiLiveFailureReason.PRIVATE_MODE: OngekiEventType.PRIVATE_MODE,
        OngekiLiveFailureReason.PREMIUM_MISSING: OngekiEventType.PREMIUM_MISSING,
        OngekiLiveFailureReason.PROVIDER_NETWORK_OUTAGE: OngekiEventType.PROVIDER_NETWORK_OUTAGE,
        OngekiLiveFailureReason.SCORE_LOG_DATA_MISSING: OngekiEventType.SCORE_LOG_DATA_FAILED,
        OngekiLiveFailureReason.IMAGE_MISSING: OngekiEventType.IMAGE_MISSING,
        OngekiLiveFailureReason.JACKET_IMAGES_NOT_READY:
            OngekiEventType.JACKET_IMAGES_NOT_READY,
        OngekiLiveFailureReason.DOM_MISMATCH: OngekiEventType.DOM_MISMATCH,
        OngekiLiveFailureReason.TIMEOUT: OngekiEventType.TIMEOUT,
    }
    event_type = event_map.get(reason)
    return OngekiBrowserEvent(event_type, completed=True) if event_type is not None else None


def _jacket_readiness_status(page: object) -> tuple[str | None, str | None]:
    """Read only fixed jacket-readiness fields from the browser state."""

    try:
        value = page.evaluate(_JACKET_READINESS_STATUS_SCRIPT)
    except Exception:
        return None, None
    if not isinstance(value, dict):
        return None, None
    status = value.get("status")
    code = value.get("code")
    return (
        status if isinstance(status, str) else None,
        code if isinstance(code, str) else None,
    )


def _wait_for_new_result_image(
    page: object,
    *,
    selector: str,
    previous_source: str,
    timeout_ms: int,
) -> None:
    """Wait for a decoded replacement image using Playwright's keyword-only arg."""

    page.wait_for_function(
        """([selector, previous]) => {
          const image = document.querySelector(selector);
          return Boolean(image && image.complete
            && image.naturalWidth > 0 && image.naturalHeight > 0
            && image.src && image.src !== previous);
        }""",
        arg=[selector, previous_source],
        timeout=timeout_ms,
    )


def _jacket_diagnostic_code(code: str | None) -> OngekiLiveDiagnosticCode:
    """Map fixed browser state codes without retaining provider/page text."""

    return {
        "jacket_elements_missing": OngekiLiveDiagnosticCode.JACKET_ELEMENTS_MISSING,
        "placeholder_jacket": OngekiLiveDiagnosticCode.JACKET_PLACEHOLDER_DETECTED,
        "unexpected_jacket_source": OngekiLiveDiagnosticCode.JACKET_SOURCE_UNEXPECTED,
        "jacket_image_load_failed": OngekiLiveDiagnosticCode.JACKET_IMAGE_LOAD_FAILED,
        "jacket_readiness_failed": OngekiLiveDiagnosticCode.JACKET_READINESS_FAILED,
    }.get(code, OngekiLiveDiagnosticCode.JACKET_IMAGES_NOT_READY)


class PlaywrightOngekiLiveRunner:
    """Run the bounded ONGEKI browser sequence with no credential handling."""

    def __init__(
        self,
        config: OngekiLiveConfig,
        *,
        last_successful_image: str | Path | None = None,
        score_log_data_capture: Callable[[], object] | None = None,
        status_reader: Callable[[object], object] | None = None,
    ) -> None:
        self.config = config
        self.last_successful_image = last_successful_image
        self.score_log_data_capture = score_log_data_capture
        self.status_reader = status_reader

    @property
    def timeout_ms(self) -> int:
        return self.config.timeout_seconds * 1_000

    def collect(
        self,
        events: Iterable[OngekiBrowserEvent] | None = None,
        *,
        collected_at: datetime | None = None,
        score_log_confirmed: bool = False,
        score_log_data: object | None = None,
    ) -> OngekiLiveResult:
        """Run live Playwright collection or reduce an explicit event stream."""

        if events is not None:
            return collect_ongeki_events(
                self.config,
                events,
                last_successful_image=self.last_successful_image,
            )
        return self._collect_browser(
            collected_at=collected_at,
            score_log_confirmed=score_log_confirmed,
            score_log_data=score_log_data,
        )

    run = collect

    def _partial(
        self,
        events: list[OngekiBrowserEvent],
        *,
        stage: OngekiLiveStage,
        reason: OngekiLiveFailureReason | None = None,
        diagnostic: OngekiLiveDiagnostic | None = None,
        image: RenderedImageMetadata | None = None,
        authentication_required: bool = False,
        manual_action_required: bool = False,
    ) -> OngekiLiveResult:
        return _result(
            collect_ongeki_events(
                self.config,
                events,
                last_successful_image=self.last_successful_image,
            ).collection,
            stage=stage,
            reason=reason,
            diagnostic=diagnostic,
            image=image,
            authentication_required=authentication_required,
            manual_action_required=manual_action_required,
        )

    def _jacket_images_not_ready(
        self,
        events: list[OngekiBrowserEvent],
        *,
        diagnostic_code: OngekiLiveDiagnosticCode = (
            OngekiLiveDiagnosticCode.JACKET_IMAGES_NOT_READY
        ),
    ) -> OngekiLiveResult:
        """Fail closed when required non-copyright jackets are not verified."""

        events.append(OngekiBrowserEvent.jacket_images_not_ready())
        return self._partial(
            events,
            stage=OngekiLiveStage.IMAGE_GENERATION,
            reason=OngekiLiveFailureReason.JACKET_IMAGES_NOT_READY,
            diagnostic=OngekiLiveDiagnostic(
                OngekiLiveStage.IMAGE_GENERATION,
                diagnostic_code,
            ),
        )

    def _collect_browser(
        self,
        *,
        collected_at: datetime | None,
        score_log_confirmed: bool,
        score_log_data: object | None,
    ) -> OngekiLiveResult:
        observed_at = collected_at or datetime.now(UTC)
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
            from playwright.sync_api import sync_playwright
        except ImportError:
            return _result(
                None,
                stage=OngekiLiveStage.PLAYWRIGHT_IMPORT,
                reason=OngekiLiveFailureReason.PLAYWRIGHT_UNAVAILABLE,
                diagnostic=OngekiLiveDiagnostic(
                    OngekiLiveStage.PLAYWRIGHT_IMPORT,
                    OngekiLiveDiagnosticCode.PLAYWRIGHT_UNAVAILABLE,
                ),
            )

        baseline_path = self.last_successful_image
        if baseline_path is None and Path(self.config.output_path).is_file():
            baseline_path = self.config.output_path
        events: list[OngekiBrowserEvent] = []
        context: Any = None
        stage = OngekiLiveStage.SESSION_DIRECTORY
        try:
            session_dir = Path(self.config.session_dir).resolve()
            session_dir.mkdir(parents=True, exist_ok=True)
            with sync_playwright() as playwright:
                stage = OngekiLiveStage.BROWSER_LAUNCH
                context = playwright.chromium.launch_persistent_context(
                    str(session_dir),
                    headless=self.config.headless,
                    accept_downloads=True,
                )
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(self.timeout_ms)
                page.set_default_navigation_timeout(self.timeout_ms)

                manual_external_refresh = (
                    self.config.score_log_bookmarklet_path is None
                    and score_log_confirmed
                )
                if manual_external_refresh:
                    # The administrator explicitly confirms that they ran the
                    # ScoreLog bookmarklet in their own authenticated browser.
                    # Do not claim that this Playwright context observed the
                    # official page, and do not require its cookies merely to
                    # consume the now-refreshed public generator data.
                    stage = OngekiLiveStage.SCORE_LOG_CONFIRMATION
                    events.append(
                        OngekiBrowserEvent.manual_score_log_refresh_confirmed()
                    )
                else:
                    stage = OngekiLiveStage.OFFICIAL_NET
                    page.goto(
                        self.config.official_url,
                        wait_until="domcontentloaded",
                        timeout=self.timeout_ms,
                    )
                    if _login_form_observed(page) or _auth_redirect_observed(
                        page,
                        self.config.official_url,
                    ):
                        return self._partial(
                            events,
                            stage=stage,
                            reason=OngekiLiveFailureReason.AUTH_REQUIRED,
                            authentication_required=True,
                            manual_action_required=True,
                        )
                    official_url = _observed_url(page, self.config.official_url)
                    if not _url_matches(official_url, self.config.official_url):
                        return self._partial(
                            events,
                            stage=stage,
                            reason=OngekiLiveFailureReason.DOM_MISMATCH,
                            diagnostic=OngekiLiveDiagnostic(
                                stage,
                                OngekiLiveDiagnosticCode.URL_MISMATCH,
                            ),
                        )
                    events.append(OngekiBrowserEvent.official_net_opened(official_url))

                    stage = OngekiLiveStage.SCORE_LOG_UPDATE
                    # The refresh bookmarklet is defined to run on the
                    # official home page already verified above.
                    score_log_url = official_url
                    events.append(
                        OngekiBrowserEvent(
                            OngekiEventType.SCORE_LOG_REFRESH_STARTED,
                            completed=True,
                            url=score_log_url,
                        )
                    )

                    if self.config.score_log_bookmarklet_path is None:
                        return self._partial(
                            events,
                            stage=OngekiLiveStage.SCORE_LOG_CONFIRMATION,
                            reason=OngekiLiveFailureReason.MANUAL_CONFIRMATION_REQUIRED,
                            diagnostic=OngekiLiveDiagnostic(
                                OngekiLiveStage.SCORE_LOG_CONFIRMATION,
                                OngekiLiveDiagnosticCode.SCORE_LOG_CONFIRMATION_REQUIRED,
                            ),
                            manual_action_required=True,
                        )

                    from .rhythm_collectors.common import load_runtime_source

                    loaded = load_runtime_source(
                        self.config.score_log_bookmarklet_path
                    )
                    if not loaded.success or loaded.source is None:
                        return self._partial(
                            events,
                            stage=stage,
                            reason=OngekiLiveFailureReason.BOOKMARKLET_LOAD_FAILURE,
                            diagnostic=OngekiLiveDiagnostic(
                                stage,
                                OngekiLiveDiagnosticCode.BOOKMARKLET_LOAD_FAILURE,
                            ),
                        )
                    try:
                        page.evaluate(loaded.source)
                    except Exception:
                        return self._partial(
                            events,
                            stage=stage,
                            reason=OngekiLiveFailureReason.BOOKMARKLET_LOAD_FAILURE,
                            diagnostic=OngekiLiveDiagnostic(
                                stage,
                                OngekiLiveDiagnosticCode.BOOKMARKLET_LOAD_FAILURE,
                            ),
                        )
                    events.append(
                        OngekiBrowserEvent(
                            OngekiEventType.BOOKMARKLET_LOADED,
                            completed=True,
                        )
                    )

                    # No stable ScoreLog completion DOM is known.  Elapsed
                    # time alone never crosses this gate.
                    if not score_log_confirmed:
                        return self._partial(
                            events,
                            stage=OngekiLiveStage.SCORE_LOG_CONFIRMATION,
                            reason=OngekiLiveFailureReason.MANUAL_CONFIRMATION_REQUIRED,
                            diagnostic=OngekiLiveDiagnostic(
                                OngekiLiveStage.SCORE_LOG_CONFIRMATION,
                                OngekiLiveDiagnosticCode.SCORE_LOG_CONFIRMATION_REQUIRED,
                            ),
                            manual_action_required=True,
                        )
                    events.append(
                        OngekiBrowserEvent.score_log_refresh_completed(url=score_log_url)
                    )
                    events.append(OngekiBrowserEvent.admin_confirmation())

                payload = score_log_data
                if payload is None and callable(self.score_log_data_capture):
                    try:
                        payload = self.score_log_data_capture()
                    except Exception:
                        payload = None

                stage = OngekiLiveStage.GENERATOR
                page.goto(
                    self.config.generator_url,
                    wait_until="domcontentloaded",
                    timeout=self.timeout_ms,
                )
                generator_url = _observed_url(page, self.config.generator_url)
                if not _url_matches(generator_url, self.config.generator_url):
                    return self._partial(
                        events,
                        stage=stage,
                        reason=OngekiLiveFailureReason.EXTERNAL_SERVICE_FAILURE,
                        diagnostic=OngekiLiveDiagnostic(
                            stage,
                            OngekiLiveDiagnosticCode.URL_MISMATCH,
                        ),
                    )
                events.append(OngekiBrowserEvent.generator_opened(generator_url))
                page.wait_for_function(
                    """() => typeof parsePage === "function"
                      && typeof musicRecord !== "undefined"
                      && Array.isArray(musicRecord)
                      && musicRecord.length > 0""",
                    timeout=self.timeout_ms,
                )
                capture_installed = page.evaluate(
                    f"""(() => {{
                      const original = window.parsePage;
                      if (typeof original !== "function") return false;
                      window[{_CAPTURED_SCORE_LOG_KEY!r}] = null;
                      window.parsePage = async function(...args) {{
                        const result = await original.apply(this, args);
                        window[{_CAPTURED_SCORE_LOG_KEY!r}] = result;
                        return result;
                      }};
                      return true;
                    }})()"""
                )
                if capture_installed is not True:
                    return self._partial(
                        events,
                        stage=stage,
                        reason=OngekiLiveFailureReason.DOM_MISMATCH,
                        diagnostic=OngekiLiveDiagnostic(
                            stage,
                            OngekiLiveDiagnosticCode.BROWSER_OPERATION_FAILED,
                        ),
                    )
                if callable(self.status_reader):
                    try:
                        provider_status = _status_reason(self.status_reader(page))
                    except Exception:
                        return self._partial(
                            events,
                            stage=stage,
                            reason=OngekiLiveFailureReason.DOM_MISMATCH,
                            diagnostic=OngekiLiveDiagnostic(
                                stage,
                                OngekiLiveDiagnosticCode.BROWSER_OPERATION_FAILED,
                            ),
                        )
                    if provider_status is not None:
                        failure_event = _failure_event(provider_status)
                        if failure_event is not None:
                            events.append(failure_event)
                        return self._partial(
                            events,
                            stage=stage,
                            reason=provider_status,
                            authentication_required=provider_status
                            is OngekiLiveFailureReason.AUTH_REQUIRED,
                            manual_action_required=provider_status
                            in {
                                OngekiLiveFailureReason.AUTH_REQUIRED,
                                OngekiLiveFailureReason.USER_NOT_FOUND,
                                OngekiLiveFailureReason.PRIVATE_MODE,
                                OngekiLiveFailureReason.PREMIUM_MISSING,
                            },
                        )

                provider_dialog_reasons: list[OngekiLiveFailureReason] = []

                def handle_generator_dialog(dialog: object) -> None:
                    # The current generator reports User not found, private
                    # mode, premium-only, and fetch failures with alert().
                    # Classify the fixed known phrases in memory and retain
                    # only the enum; arbitrary provider text is never copied
                    # into a result or diagnostic.
                    message = getattr(dialog, "message", None)
                    reason = _classify_ongeki_provider_message(message)
                    provider_dialog_reasons.append(
                        reason or OngekiLiveFailureReason.EXTERNAL_SERVICE_FAILURE
                    )
                    dismiss = getattr(dialog, "dismiss", None)
                    if callable(dismiss):
                        try:
                            dismiss()
                        except Exception:
                            pass

                page_on = getattr(page, "on", None)
                if callable(page_on):
                    try:
                        page_on("dialog", handle_generator_dialog)
                    except Exception:
                        # A page seam without dialog events remains bounded by
                        # the explicit image wait below.
                        pass

                if (
                    self.config.user_id is None
                    or self.config.user_id_selector is None
                    or self.config.mode_selector is None
                ):
                    return self._partial(
                        events,
                        stage=OngekiLiveStage.USER_MODE,
                        reason=OngekiLiveFailureReason.USER_ID_REQUIRED,
                        diagnostic=OngekiLiveDiagnostic(
                            OngekiLiveStage.USER_MODE,
                            OngekiLiveDiagnosticCode.USER_MODE_CONFIGURATION_REQUIRED,
                        ),
                        manual_action_required=True,
                    )
                try:
                    self._set_control(page, self.config.user_id_selector, self.config.user_id)
                    self._set_control(page, self.config.mode_selector, self.config.mode)
                    if self.config.copyright_mode_selector is not None:
                        self._set_checkbox(
                            page,
                            self.config.copyright_mode_selector,
                            self.config.copyright_mode,
                        )
                except Exception:
                    return self._partial(
                        events,
                        stage=OngekiLiveStage.USER_MODE,
                        reason=OngekiLiveFailureReason.DOM_MISMATCH,
                        diagnostic=OngekiLiveDiagnostic(
                            OngekiLiveStage.USER_MODE,
                            OngekiLiveDiagnosticCode.BROWSER_OPERATION_FAILED,
                        ),
                    )
                events.append(
                    OngekiBrowserEvent.user_mode_selected(
                        self.config.user_id,
                        self.config.mode,
                    )
                )

                if (
                    self.config.generate_selector is None
                    or self.config.image_selector is None
                ):
                    return self._partial(
                        events,
                        stage=OngekiLiveStage.IMAGE_GENERATION,
                        reason=OngekiLiveFailureReason.MANUAL_ACTION_REQUIRED,
                        diagnostic=OngekiLiveDiagnostic(
                            OngekiLiveStage.IMAGE_GENERATION,
                            OngekiLiveDiagnosticCode.GENERATOR_CONFIGURATION_REQUIRED,
                        ),
                        manual_action_required=True,
                    )

                image_locator = page.locator(self.config.image_selector)
                generate_locator = page.locator(self.config.generate_selector)
                image_count = image_locator.count()
                if image_count > 1 or generate_locator.count() != 1:
                    return self._partial(
                        events,
                        stage=OngekiLiveStage.IMAGE_GENERATION,
                        reason=OngekiLiveFailureReason.DOM_MISMATCH,
                        diagnostic=OngekiLiveDiagnostic(
                            OngekiLiveStage.IMAGE_GENERATION,
                            OngekiLiveDiagnosticCode.GENERATOR_CONFIGURATION_REQUIRED,
                        ),
                    )
                if not self.config.copyright_mode:
                    try:
                        jacket_readiness_installed = page.evaluate(
                            _INSTALL_JACKET_READINESS_SCRIPT
                        )
                    except Exception:
                        return self._jacket_images_not_ready(
                            events,
                            diagnostic_code=OngekiLiveDiagnosticCode.JACKET_READINESS_INSTALL_FAILED,
                        )
                    if jacket_readiness_installed is not True:
                        return self._jacket_images_not_ready(
                            events,
                            diagnostic_code=OngekiLiveDiagnosticCode.JACKET_READINESS_INSTALL_FAILED,
                        )
                # The public generator creates #result-img only after a
                # successful html2canvas event.  Zero pre-existing images is
                # therefore the normal initial state, not a DOM mismatch.
                before_src = image_locator.get_attribute("src") or "" if image_count else ""
                generate_locator.click(timeout=self.timeout_ms)
                if provider_dialog_reasons:
                    provider_reason = provider_dialog_reasons[-1]
                    failure_event = _failure_event(provider_reason)
                    if failure_event is not None:
                        events.append(failure_event)
                    return self._partial(
                        events,
                        stage=OngekiLiveStage.IMAGE_GENERATION,
                        reason=provider_reason,
                        diagnostic=OngekiLiveDiagnostic(
                            OngekiLiveStage.IMAGE_GENERATION,
                            OngekiLiveDiagnosticCode.PROVIDER_ERROR_SIGNAL,
                        ),
                        manual_action_required=provider_reason
                        in {
                            OngekiLiveFailureReason.USER_NOT_FOUND,
                            OngekiLiveFailureReason.PRIVATE_MODE,
                            OngekiLiveFailureReason.PREMIUM_MISSING,
                        },
                    )
                if not self.config.copyright_mode:
                    try:
                        page.wait_for_function(
                            _JACKET_READINESS_TERMINAL_SCRIPT,
                            timeout=self.timeout_ms,
                        )
                    except PlaywrightTimeoutError:
                        jacket_status, jacket_code = _jacket_readiness_status(page)
                        if jacket_status in {"waiting", "failed", None}:
                            return self._jacket_images_not_ready(
                                events,
                                diagnostic_code=_jacket_diagnostic_code(jacket_code),
                            )
                        raise
                    jacket_status, jacket_code = _jacket_readiness_status(page)
                    if jacket_status == "failed":
                        return self._jacket_images_not_ready(
                            events,
                            diagnostic_code=_jacket_diagnostic_code(jacket_code),
                        )
                    if jacket_status != "captured":
                        if jacket_status != "capture_failed":
                            return self._jacket_images_not_ready(
                                events,
                                diagnostic_code=_jacket_diagnostic_code(jacket_code),
                            )
                        return self._partial(
                            events,
                            stage=OngekiLiveStage.IMAGE_GENERATION,
                            reason=OngekiLiveFailureReason.IMAGE_MISSING,
                            diagnostic=OngekiLiveDiagnostic(
                                OngekiLiveStage.IMAGE_GENERATION,
                                OngekiLiveDiagnosticCode.GENERATION_EVENT_MISSING,
                            ),
                        )
                _wait_for_new_result_image(
                    page,
                    selector=self.config.image_selector,
                    previous_source=before_src,
                    timeout_ms=self.timeout_ms,
                )
                source = page.locator(self.config.image_selector).get_attribute("src") or ""
                if not source or source == before_src:
                    return self._partial(
                        events,
                        stage=OngekiLiveStage.IMAGE_GENERATION,
                        reason=OngekiLiveFailureReason.IMAGE_MISSING,
                        diagnostic=OngekiLiveDiagnostic(
                            OngekiLiveStage.IMAGE_GENERATION,
                            OngekiLiveDiagnosticCode.GENERATION_EVENT_MISSING,
                        ),
                    )
                if payload is None:
                    try:
                        payload = page.evaluate(
                            f"window[{_CAPTURED_SCORE_LOG_KEY!r}] || null"
                        )
                    except Exception:
                        payload = None
                payload_event = (
                    payload
                    if isinstance(payload, OngekiBrowserEvent)
                    else OngekiBrowserEvent.score_log_data(payload)
                    if payload is not None
                    else OngekiBrowserEvent(
                        OngekiEventType.SCORE_LOG_DATA_FAILED,
                        completed=False,
                    )
                )
                events.append(payload_event)
                if (
                    payload_event.event_type is not OngekiEventType.SCORE_LOG_DATA_AVAILABLE
                    or not payload_event.completed
                ):
                    return self._partial(
                        events,
                        stage=OngekiLiveStage.SCORE_LOG_DATA,
                        reason=OngekiLiveFailureReason.SCORE_LOG_DATA_MISSING,
                        diagnostic=OngekiLiveDiagnostic(
                            OngekiLiveStage.SCORE_LOG_DATA,
                            OngekiLiveDiagnosticCode.SCORE_LOG_DATA_MISSING,
                        ),
                        manual_action_required=True,
                    )
                try:
                    parse_ongeki_data(payload_event.payload)
                except (TypeError, ValueError):
                    return self._partial(
                        events,
                        stage=OngekiLiveStage.SCORE_LOG_DATA,
                        reason=OngekiLiveFailureReason.SCORE_LOG_DATA_MISSING,
                        diagnostic=OngekiLiveDiagnostic(
                            OngekiLiveStage.SCORE_LOG_DATA,
                            OngekiLiveDiagnosticCode.SCORE_LOG_DATA_MISSING,
                        ),
                    )
                generation_id = hashlib.sha256(source.encode("utf-8")).hexdigest()
                events.append(OngekiBrowserEvent.image_generated(generation_id=generation_id))

                stage = OngekiLiveStage.IMAGE_DECODE
                decoded = decode_image_data_uri(source)
                candidate_sha = decoded.sha256
                previous_sha = _hash_existing(baseline_path)
                if previous_sha is not None and candidate_sha == previous_sha:
                    return self._partial(
                        events,
                        stage=stage,
                        reason=OngekiLiveFailureReason.STALE_IMAGE,
                        diagnostic=OngekiLiveDiagnostic(
                            stage,
                            OngekiLiveDiagnosticCode.STALE_ARTIFACT,
                        ),
                    )

                stage = OngekiLiveStage.IMAGE_WRITE
                output = write_image_atomically(self.config.output_path, decoded)
                events.append(
                    OngekiBrowserEvent.image_saved(
                        output,
                        generation_id=generation_id,
                    )
                )
                stage = OngekiLiveStage.COLLECTOR_NORMALIZATION
                result = collect_ongeki_events(
                    self.config,
                    events,
                    last_successful_image=baseline_path,
                )
                if not result.complete or result.collection is None:
                    return result
                metadata = RenderedImageMetadata(
                    path=output,
                    sha256=candidate_sha,
                    mime_type=decoded.mime_type,
                    width=decoded.width,
                    height=decoded.height,
                    byte_size=len(decoded.payload),
                    rendered_at=observed_at,
                )
                return _result(
                    result.collection,
                    stage=stage,
                    image=metadata,
                )
        except PlaywrightTimeoutError:
            return self._partial(
                events,
                stage=stage,
                reason=OngekiLiveFailureReason.TIMEOUT,
                diagnostic=OngekiLiveDiagnostic(
                    stage,
                    OngekiLiveDiagnosticCode.BROWSER_OPERATION_FAILED,
                ),
                manual_action_required=True,
            )
        except RatingImageVerificationError:
            return self._partial(
                events,
                stage=OngekiLiveStage.IMAGE_DECODE,
                reason=OngekiLiveFailureReason.IMAGE_MISSING,
                diagnostic=OngekiLiveDiagnostic(
                    OngekiLiveStage.IMAGE_DECODE,
                    OngekiLiveDiagnosticCode.IMAGE_DECODE_FAILURE,
                ),
            )
        except OSError:
            if stage is OngekiLiveStage.SESSION_DIRECTORY:
                return self._partial(
                    events,
                    stage=stage,
                    reason=OngekiLiveFailureReason.DOM_MISMATCH,
                    diagnostic=OngekiLiveDiagnostic(
                        stage,
                        OngekiLiveDiagnosticCode.SESSION_DIRECTORY_FAILURE,
                    ),
                )
            return self._partial(
                events,
                stage=stage,
                reason=OngekiLiveFailureReason.IMAGE_MISSING,
                diagnostic=OngekiLiveDiagnostic(
                    stage,
                    OngekiLiveDiagnosticCode.IMAGE_WRITE_FAILURE,
                ),
            )
        except Exception as exc:
            reason = (
                OngekiLiveFailureReason.PROVIDER_NETWORK_OUTAGE
                if _is_network_error(exc)
                else OngekiLiveFailureReason.EXTERNAL_SERVICE_FAILURE
            )
            return self._partial(
                events,
                stage=stage,
                reason=reason,
                diagnostic=OngekiLiveDiagnostic(
                    stage,
                    OngekiLiveDiagnosticCode.BROWSER_OPERATION_FAILED,
                ),
            )
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass

    def _set_control(self, page: object, selector: str, value: str) -> None:
        locator = page.locator(selector)
        if locator.count() != 1:
            raise ValueError("configured control was not unique")
        try:
            locator.select_option(value)
        except Exception:
            locator.fill(value)

    @staticmethod
    def _set_checkbox(page: object, selector: str, checked: bool) -> None:
        locator = page.locator(selector)
        if locator.count() != 1:
            raise ValueError("configured checkbox was not unique")
        if checked:
            locator.check()
        else:
            locator.uncheck()


# Naming follows the existing maimai runner while retaining a runner alias
# for callers that prefer the operation-oriented name.
PlaywrightOngekiLiveCollector = PlaywrightOngekiLiveRunner
OngekiLiveCollector = PlaywrightOngekiLiveRunner
OngekiLiveRunner = PlaywrightOngekiLiveRunner
OngekiLiveReason = OngekiLiveFailureReason


__all__ = [
    "DEFAULT_ONGEKI_GENERATOR_URL",
    "DEFAULT_ONGEKI_OFFICIAL_URL",
    "DEFAULT_ONGEKI_SCORE_LOG_URL",
    "DecodedOngekiImage",
    "ONGEKI_IMAGE_GENERATOR_URL",
    "ONGEKI_OFFICIAL_URL",
    "ONGEKI_SCORE_LOG_URL",
    "OngekiLiveCollector",
    "OngekiLiveConfig",
    "OngekiLiveDiagnostic",
    "OngekiLiveDiagnosticCode",
    "OngekiLiveFailureReason",
    "OngekiLiveOutcome",
    "OngekiLiveReason",
    "OngekiLiveResult",
    "OngekiLiveRunner",
    "OngekiLiveStage",
    "PlaywrightOngekiLiveCollector",
    "PlaywrightOngekiLiveRunner",
    "collect_ongeki_events",
    "decode_image_bytes",
    "decode_image_data_uri",
    "decode_ongeki_image_data_uri",
    "decode_png_data_uri",
    "write_image_atomically",
]
