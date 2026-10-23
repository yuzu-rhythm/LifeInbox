"""Pure local planning for Notion pages and safe image attachments."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .attachments import SUPPORTED_IMAGE_TYPES
from .constants import EventKind
from .models import Event
from .notion_policy import EligibilityDecision, NotionEligibilityPolicy, event_policy_tokens
from .notion_transport import NotionPageRequest

_ROUTE_CATEGORY_LABELS = {
    "expenses": "Expenses",
    "meals": "Meals",
    "memo": "Memo",
    "self_talk": "Self talk",
    "dining_out": "Dining out",
    "memories": "Memory",
    "social_draft": "Social draft",
    "task": "Task",
    "shopping": "Shopping",
    "job_hunting": "Career",
    "saved_link": "Saved link",
    "rhythm_progress": "Rhythm progress",
    "rhythm_spending": "Rhythm spending",
    "planner_multi": "Planner",
}


def _bounded_text(value: object, maximum: int) -> str:
    return str(value or "").replace("\x00", " ").strip()[:maximum]


def _iso_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _safe_attachment_root(data_dir: str | Path) -> Path:
    return (Path(data_dir).expanduser() / "attachments").resolve(strict=False)


def resolve_safe_attachment_path(data_dir: str | Path, local_path: str | Path) -> Path | None:
    """Resolve a stored path only when it remains under ``DATA_DIR/attachments``."""

    root = _safe_attachment_root(data_dir)
    raw_path = Path(local_path).expanduser()
    candidates = (raw_path,) if raw_path.is_absolute() else (raw_path, Path(data_dir) / raw_path)
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        return resolved
    return None


def _sha256_file(path: Path) -> str | None:
    """Hash one already-resolved local file without loading it all at once."""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _stored_attachment_fingerprint(attachment: object) -> str:
    return _bounded_text(getattr(attachment, "sha256", None), 64).casefold()


def _attachment_content_fingerprint(
    attachment: object,
    data_dir: str | Path | None,
) -> str:
    """Prefer the bytes on disk while retaining the stored hash as a fallback."""

    stored = _stored_attachment_fingerprint(attachment)
    if data_dir is None:
        return stored
    content_type = str(getattr(attachment, "content_type", "") or "")
    content_type = content_type.lower().split(";", 1)[0].strip()
    if content_type not in SUPPORTED_IMAGE_TYPES:
        return stored
    local_path = resolve_safe_attachment_path(
        data_dir,
        getattr(attachment, "local_path", ""),
    )
    if local_path is None:
        return stored
    try:
        if not local_path.is_file():
            return stored
    except OSError:
        return stored
    return _sha256_file(local_path) or stored


@dataclass(frozen=True, slots=True)
class NotionAttachmentWarning:
    attachment_id: int
    reason_code: str
    message: str


@dataclass(frozen=True, slots=True)
class NotionAttachmentPlan:
    attachment_id: int
    path: Path | None
    filename: str
    content_type: str
    size_bytes: int | None
    sha256: str | None
    skip_reason: str | None = None

    @property
    def uploadable(self) -> bool:
        return self.skip_reason is None and self.path is not None and self.size_bytes is not None

    @property
    def warning(self) -> NotionAttachmentWarning | None:
        if self.skip_reason is None:
            return None
        messages = {
            "attachment_path_outside_data_dir": (
                "attachment path is outside the local attachment store"
            ),
            "attachment_missing": "local attachment is missing",
            "attachment_unreadable": "local attachment could not be read",
            "attachment_oversized": "attachment exceeds workspace max upload bytes",
            "unsupported_attachment_type": "attachment MIME type is not an image",
        }
        return NotionAttachmentWarning(
            self.attachment_id,
            self.skip_reason,
            messages.get(self.skip_reason, "attachment was skipped by the safety policy"),
        )


def plan_safe_image_attachments(
    event: Event,
    data_dir: str | Path,
    *,
    max_upload_bytes: int | None = None,
) -> tuple[NotionAttachmentPlan, ...]:
    """Plan only local image uploads; never turn a private file into a URL."""

    planned: list[NotionAttachmentPlan] = []
    for attachment in event.attachments or []:
        attachment_id = int(attachment.id)
        content_type = str(attachment.content_type or "").lower().split(";", 1)[0].strip()
        stored_fingerprint = _stored_attachment_fingerprint(attachment) or None
        path = resolve_safe_attachment_path(data_dir, attachment.local_path)
        if path is None:
            planned.append(
                NotionAttachmentPlan(
                    attachment_id,
                    None,
                    _bounded_text(attachment.original_filename, 255),
                    content_type,
                    None,
                    stored_fingerprint,
                    "attachment_path_outside_data_dir",
                )
            )
            continue
        if content_type not in SUPPORTED_IMAGE_TYPES:
            planned.append(
                NotionAttachmentPlan(
                    attachment_id,
                    path,
                    _bounded_text(attachment.original_filename, 255),
                    content_type,
                    None,
                    stored_fingerprint,
                    "unsupported_attachment_type",
                )
            )
            continue
        try:
            if not path.is_file():
                raise FileNotFoundError(path)
            size_bytes = path.stat().st_size
            content_fingerprint = _sha256_file(path)
            if content_fingerprint is None:
                raise OSError(path)
        except FileNotFoundError:
            skip_reason = "attachment_missing"
            size_bytes = None
            content_fingerprint = stored_fingerprint
        except OSError:
            skip_reason = "attachment_unreadable"
            size_bytes = None
            content_fingerprint = stored_fingerprint
        else:
            skip_reason = None
            if max_upload_bytes is not None and size_bytes > max_upload_bytes:
                skip_reason = "attachment_oversized"
        planned.append(
            NotionAttachmentPlan(
                attachment_id,
                path,
                _bounded_text(attachment.original_filename, 255),
                content_type,
                size_bytes,
                content_fingerprint,
                skip_reason,
            )
        )
    return tuple(planned)


def _structured_content(event: Event) -> dict[str, Any]:
    """Copy reviewed Discord fields into a queryable Notion page body."""

    context = event.capture_context
    content: dict[str, Any] = {
        "discord_capture": {
            "message_id": _bounded_text(event.discord_message_id, 64),
            "channel_id": _bounded_text(event.discord_channel_id, 64),
            "captured_at": _iso_datetime(event.captured_at),
            "occurred_at": _iso_datetime(event.occurred_at),
            "capture_type": _bounded_text(
                context.capture_type if context is not None else None,
                32,
            ),
            "capture_source": _bounded_text(
                context.source_label if context is not None else event.source,
                32,
            ),
            "route_category": _bounded_text(
                context.route_category if context is not None else event.event_type,
                32,
            ),
            "destination_channel_id": _bounded_text(
                context.destination_channel_id if context is not None else None,
                64,
            ),
            "destination_message_id": _bounded_text(
                context.destination_message_id if context is not None else None,
                64,
            ),
        }
    }
    if (
        context is not None
        and context.latitude is not None
        and context.longitude is not None
    ):
        content["explicit_capture_location"] = {
            "label": _bounded_text(context.location_label or "現在地", 255),
            "latitude": round(float(context.latitude), 6),
            "longitude": round(float(context.longitude), 6),
            "horizontal_accuracy_meters": context.horizontal_accuracy_meters,
            "shared_at": _iso_datetime(context.location_shared_at),
            "map_url": (
                "https://maps.apple.com/?ll="
                f"{context.latitude:.6f},{context.longitude:.6f}"
            ),
            "scope": "explicit single capture; not continuous tracking",
        }
    if event.planner_items:
        content["planner_items"] = [
            {
                "index": item.item_index,
                "text": _bounded_text(item.raw_text, 4_000),
                "category": _bounded_text(item.category, 32),
                "confidence": item.confidence,
                "status": _bounded_text(item.status, 16),
                "destination_channel_id": _bounded_text(
                    item.destination_channel_id,
                    64,
                ),
            }
            for item in event.planner_items
        ]
    if event.expenses:
        content["expenses"] = [
            {
                "amount_yen": expense.amount,
                "merchant": _bounded_text(
                    expense.merchant_normalized or expense.merchant_raw,
                    255,
                ),
                "category": _bounded_text(expense.category, 64),
                "subcategory": _bounded_text(expense.subcategory, 64),
                "description": _bounded_text(expense.description, 2_000),
                "confidence": expense.confidence,
                "receipt_items": [
                    {
                        "name": _bounded_text(item.name, 255),
                        "quantity": item.quantity,
                        "unit_price_yen": item.unit_price,
                        "amount_yen": item.amount,
                    }
                    for item in expense.receipt_items
                ],
            }
            for expense in event.expenses
        ]
    if event.memo is not None:
        content["memo"] = {"body": _bounded_text(event.memo.body, 20_000)}
    if event.meals:
        content["meals"] = [
            {
                "meal_type": _bounded_text(meal.meal_type, 32),
                "restaurant": _bounded_text(
                    meal.restaurant_normalized or meal.restaurant_raw, 255
                ),
                "description": _bounded_text(meal.description, 2_000),
                "outside_home": _bounded_text(meal.outside_home, 32),
            }
            for meal in event.meals
        ]
    if event.rhythm_result is not None:
        result = event.rhythm_result
        content["rhythm_result"] = {
            "game": _bounded_text(result.game, 32),
            "note": _bounded_text(result.note, 1_000),
        }
        if result.todo is not None:
            content["rhythm_result"]["todo"] = {
                "title": _bounded_text(result.todo.title, 255),
                "status": _bounded_text(result.todo.status, 32),
            }
    if event.rhythm_todo is not None and event.rhythm_result is None:
        todo = event.rhythm_todo
        content["rhythm_todo"] = {
            "game": _bounded_text(todo.game, 32),
            "title": _bounded_text(todo.title, 255),
            "status": _bounded_text(todo.status, 32),
            "completed_at": _iso_datetime(todo.completed_at),
        }
    if event.rhythm_spending_link is not None:
        spending = event.rhythm_spending_link.spending_event
        content["rhythm_spending"] = {
            "game": _bounded_text(spending.game, 16),
            "amount_yen": spending.amount_yen,
            "local_date": spending.local_date.isoformat(),
            "time_zone": _bounded_text(spending.time_zone, 64),
            "revision": spending.revision,
            "source": _bounded_text(spending.source, 16),
            "deleted": spending.deleted_at is not None,
        }
    if event.career_opportunity is not None:
        opportunity = event.career_opportunity
        content["career_opportunity"] = {
            "company": _bounded_text(opportunity.company, 255),
            "title": _bounded_text(opportunity.title, 255),
            "kind": _bounded_text(opportunity.kind, 32),
            "location": _bounded_text(opportunity.location, 255),
            "url": _bounded_text(opportunity.url, 2_048),
            "notes": _bounded_text(opportunity.notes, 4_000),
            "status": _bounded_text(opportunity.status, 32),
            "deadline_at": _iso_datetime(opportunity.deadline_at),
            "next_action": _bounded_text(opportunity.next_action, 255),
        }
    if event.career_application is not None:
        application = event.career_application
        content["career_application"] = {
            "stage": _bounded_text(application.stage, 32),
            "status": _bounded_text(application.status, 32),
            "notes": _bounded_text(application.notes, 4_000),
            "deadline_at": _iso_datetime(application.deadline_at),
            "next_action": _bounded_text(application.next_action, 255),
        }
    return content


def _content_fingerprint_from_attachments(
    event: Event,
    attachment_fingerprints: Mapping[int, str],
) -> str:
    """Hash the safe page source without persisting the source payload locally."""

    payload = {
        "schema": 1,
        "status": _bounded_text(event.status, 32),
        "source": _bounded_text(event.source, 64),
        "event_type": _bounded_text(event.event_type, 64),
        "event_kinds": sorted(_bounded_text(kind, 64) for kind in event.event_kinds or []),
        "occurred_at": _iso_datetime(event.occurred_at),
        "raw_text": _bounded_text(event.raw_text, 20_000),
        "structured": _structured_content(event),
        "attachments": [
            {
                "id": int(attachment.id),
                "filename": _bounded_text(attachment.original_filename, 255),
                "content_type": _bounded_text(attachment.content_type, 128),
                "sha256": attachment_fingerprints.get(
                    int(attachment.id), _stored_attachment_fingerprint(attachment)
                ),
            }
            for attachment in event.attachments or []
        ],
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def compute_content_fingerprint(
    event: Event,
    data_dir: str | Path | None = None,
) -> str:
    """Hash the safe page source, including the current local image bytes."""

    return _content_fingerprint_from_attachments(
        event,
        {
            int(attachment.id): _attachment_content_fingerprint(attachment, data_dir)
            for attachment in event.attachments or []
        },
    )


def _event_label(event: Event) -> str:
    context = event.capture_context
    if context is not None and context.route_category:
        routed = _ROUTE_CATEGORY_LABELS.get(context.route_category)
        if routed is not None:
            return routed
    tokens = set(event_policy_tokens(event))
    labels = (
        ("career", "Career"),
        (EventKind.EXPENSE.value, "Expenses"),
        ("dining_out", "Dining out"),
        ("life_task", "Task"),
        ("memories", "Memory"),
        ("self_talk", "Self talk"),
        ("monologue", "Self talk"),
        ("shopping", "Shopping"),
        ("social_draft", "Social draft"),
        ("saved_link", "Saved link"),
        ("link", "Saved link"),
        ("links", "Saved link"),
        (EventKind.MEAL.value, "Meal"),
        (EventKind.RHYTHM_TODO.value, "Rhythm todo"),
        (EventKind.RHYTHM_RESULT.value, "Rhythm result"),
        ("rhythm_spending", "Rhythm spending"),
        (EventKind.MEMO.value, "Memo"),
    )
    for token, label in labels:
        if token in tokens:
            return label
    return "Life archive"


def _render_structured_lines(event: Event) -> list[str]:
    structured = _structured_content(event)
    lines: list[str] = []
    for name, value in structured.items():
        lines.append(f"### {name.replace('_', ' ').title()}")
        lines.append("```json")
        lines.append(json.dumps(value, ensure_ascii=False, indent=2))
        lines.append("```")
    return lines


@dataclass(frozen=True, slots=True)
class NotionPageDraft:
    event_id: int
    content_fingerprint: str
    title: str
    body_markdown: str
    properties: tuple[tuple[str, str], ...]
    attachments: tuple[NotionAttachmentPlan, ...] = ()

    @property
    def warnings(self) -> tuple[NotionAttachmentWarning, ...]:
        return tuple(
            warning
            for attachment in self.attachments
            if (warning := attachment.warning) is not None
        )

    def request(self, file_ids: Sequence[str] = ()) -> NotionPageRequest:
        return NotionPageRequest(
            event_id=self.event_id,
            title=self.title,
            body_markdown=self.body_markdown,
            properties=self.properties,
            file_ids=tuple(file_ids),
        )


@dataclass(frozen=True, slots=True)
class NotionEventPlan:
    event_id: int
    decision: EligibilityDecision
    content_fingerprint: str
    draft: NotionPageDraft | None = None

    @property
    def eligible(self) -> bool:
        return self.decision.eligible

    @property
    def reason_code(self) -> str:
        return self.decision.reason_code


def build_notion_event_plan(
    event: Event,
    data_dir: str | Path = "data",
    *,
    max_upload_bytes: int | None = None,
    policy: NotionEligibilityPolicy | None = None,
) -> NotionEventPlan:
    """Build a local-only draft after applying the safety policy."""

    decision = (policy or NotionEligibilityPolicy()).evaluate(event)
    if not decision.eligible:
        return NotionEventPlan(event.id, decision, compute_content_fingerprint(event, data_dir))

    title = f"{_event_label(event)} · {event.occurred_at:%Y-%m-%d}"
    raw_text = _bounded_text(event.raw_text, 20_000) or "(empty)"
    lines = [
        f"# {title}",
        "",
        f"- Local Event ID: `{event.id}`",
        f"- Occurred at: `{event.occurred_at.isoformat()}`",
        f"- Source: `{_bounded_text(event.source, 64)}`",
        "",
        "## Original Discord text (read-only)",
        "",
        raw_text,
    ]
    structured_lines = _render_structured_lines(event)
    if structured_lines:
        lines.extend(["", "## Structured local fields", "", *structured_lines])
    attachments = plan_safe_image_attachments(
        event,
        data_dir,
        max_upload_bytes=max_upload_bytes,
    )
    fingerprint = _content_fingerprint_from_attachments(
        event,
        {
            item.attachment_id: item.sha256 or ""
            for item in attachments
        },
    )
    return NotionEventPlan(
        event.id,
        decision,
        fingerprint,
        NotionPageDraft(
            event_id=event.id,
            content_fingerprint=fingerprint,
            title=title,
            body_markdown="\n".join(lines),
            properties=(
                ("Local Event ID", str(event.id)),
                ("Source", _bounded_text(event.source, 64)),
                ("Category", _event_label(event)),
                ("State", "Promoted"),
                ("Occurred At", event.occurred_at.isoformat()),
                ("Summary", raw_text[:1_000]),
                ("Has Media", "true" if any(item.uploadable for item in attachments) else "false"),
                ("Original text policy", "read-only local source"),
            ),
            attachments=attachments,
        ),
    )


__all__ = [
    "NotionAttachmentPlan",
    "NotionAttachmentWarning",
    "NotionEventPlan",
    "NotionPageDraft",
    "build_notion_event_plan",
    "compute_content_fingerprint",
    "plan_safe_image_attachments",
    "resolve_safe_attachment_path",
]
