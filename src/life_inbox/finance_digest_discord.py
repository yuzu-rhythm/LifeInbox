"""Discord delivery adapter for the daily household-budget digest.

The finance snapshot and delivery claim live in :mod:`life_inbox.finance_digest`.
This module is the only layer that performs Discord I/O.  A create whose
response is ambiguous is deliberately blocked as ``unknown`` instead of being
blindly retried and potentially duplicated; editing a known Bot message is
idempotent and can be retried with bounded backoff.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

from .finance_digest import (
    DailyFinanceSnapshot,
    DeliveryAction,
    DeliveryClaimConflictError,
    FinanceDigestService,
)
from .rhythm_discord import DiscordSendError, DiscordTransport


class _ReplacementCreateSendError(DiscordSendError):
    """A failed create attempted after Discord reported the old message missing.

    The durable delivery row still points at the deleted message when the
    transport enters this path.  Carrying this distinction to ``publish`` lets
    it clear that stale target and, for an ambiguous create, block a second
    create instead of retrying through the deleted message ID.
    """

    def __init__(self, source: DiscordSendError) -> None:
        super().__init__(
            source.code,
            source.safe_message,
            retryable=source.retryable,
            retry_after=source.retry_after,
            outcome_unknown=source.outcome_unknown,
        )


@dataclass(frozen=True, slots=True)
class FinanceDigestPublishOutcome:
    """Safe result of one create/edit/skip decision."""

    local_date: date
    action: DeliveryAction
    posted: bool
    edited: bool
    dry_run: bool
    duplicate: bool
    in_progress: bool
    blocked: bool
    message_id: str | None
    content: str
    semantic_fingerprint: str
    attempt_count: int
    error_code: str | None = None
    uncertain: bool = False


class FinanceDigestPublisher:
    """Publish one local-date digest with durable idempotency state."""

    def __init__(
        self,
        service: FinanceDigestService,
        transport: DiscordTransport,
        *,
        dry_run: bool = True,
        post_zero_days: bool = True,
        max_attempts: int = 3,
        base_backoff_seconds: float = 1.0,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if base_backoff_seconds < 0:
            raise ValueError("base_backoff_seconds must not be negative")
        self.service = service
        self.transport = transport
        self.dry_run = dry_run
        self.post_zero_days = post_zero_days
        self.max_attempts = max_attempts
        self.base_backoff_seconds = base_backoff_seconds

    @staticmethod
    def _empty_day(snapshot: DailyFinanceSnapshot) -> bool:
        """Return true only when there is no spending or review-worthy state."""

        account_state = any(
            account.balance not in (None, 0) or account.delta not in (None, 0)
            for account in snapshot.accounts
        )
        return not any(
            (
                snapshot.daily_total,
                snapshot.pending_expense_count,
                snapshot.unclassified_confirmed_expense_count,
                snapshot.notable_spending,
                snapshot.monthly_budget is not None
                and snapshot.monthly_budget.is_over_budget,
                snapshot.warnings,
                snapshot.asset_total not in (None, 0),
                snapshot.liability_total not in (None, 0),
                snapshot.net_assets not in (None, 0),
                account_state,
            )
        )

    async def _deliver_with_retry(
        self,
        *,
        channel_id: int,
        content: str,
        existing_message_id: str | None,
        before_replacement_create: Callable[[], object] | None = None,
    ) -> tuple[str, bool, int]:
        replacement_create = False
        for attempt in range(1, self.max_attempts + 1):
            operation_was_edit = existing_message_id is not None
            try:
                if existing_message_id is not None:
                    message_id = await self.transport.edit(
                        channel_id=channel_id,
                        message_id=existing_message_id,
                        content=content,
                    )
                    return message_id, True, attempt
                message_id = await self.transport.send(
                    channel_id=channel_id,
                    content=content,
                )
                return message_id, False, attempt
            except DiscordSendError as exc:
                if existing_message_id is not None and exc.code == "discord_not_found":
                    # The prior Bot message was removed.  A fresh create is the
                    # only useful fallback, but its ambiguous outcome must not
                    # be retried as though it were an idempotent edit.
                    # Persist that the deleted target is no longer usable
                    # before beginning the non-idempotent replacement create.
                    if before_replacement_create is not None:
                        before_replacement_create()
                    existing_message_id = None
                    replacement_create = True
                    operation_was_edit = False
                    try:
                        message_id = await self.transport.send(
                            channel_id=channel_id,
                            content=content,
                        )
                        return message_id, False, attempt
                    except DiscordSendError as fallback_exc:
                        exc = fallback_exc
                    except Exception as fallback_exc:
                        # A generic failure around a non-idempotent create can
                        # have happened before or after Discord accepted it.
                        # Treat that outcome as unknown and never persist the
                        # exception text, which may contain response details.
                        safe_error = DiscordSendError(
                            f"discord_send_failed:{type(fallback_exc).__name__}"[:64],
                            "Discordへの再作成結果を確認できませんでした。",
                            retryable=False,
                            outcome_unknown=True,
                        )
                        raise _ReplacementCreateSendError(safe_error) from fallback_exc
                if exc.outcome_unknown and not operation_was_edit:
                    if replacement_create:
                        raise _ReplacementCreateSendError(exc) from exc
                    raise exc
                if not exc.retryable or attempt >= self.max_attempts:
                    if replacement_create:
                        raise _ReplacementCreateSendError(exc) from exc
                    raise exc
                delay = (
                    exc.retry_after
                    if exc.retry_after is not None
                    else self.base_backoff_seconds * (2 ** (attempt - 1))
                )
                # A delay protects a failed request.  Elapsed time is never
                # interpreted as delivery success.
                await asyncio.sleep(max(0.0, delay))
            except Exception as exc:
                if replacement_create:
                    safe_error = DiscordSendError(
                        f"discord_send_failed:{type(exc).__name__}"[:64],
                        "Discordへの再作成結果を確認できませんでした。",
                        retryable=False,
                        outcome_unknown=True,
                    )
                    raise _ReplacementCreateSendError(safe_error) from exc
                raise
        raise DiscordSendError(
            "delivery_exhausted",
            "Discord送信の再試行回数を超えました。",
            retryable=False,
        )

    def _outcome(
        self,
        *,
        snapshot: DailyFinanceSnapshot,
        action: DeliveryAction,
        content: str,
        attempt_count: int,
        posted: bool = False,
        edited: bool = False,
        dry_run: bool = False,
        duplicate: bool = False,
        in_progress: bool = False,
        blocked: bool = False,
        message_id: str | None = None,
        error_code: str | None = None,
        uncertain: bool = False,
    ) -> FinanceDigestPublishOutcome:
        return FinanceDigestPublishOutcome(
            local_date=snapshot.local_date,
            action=action,
            posted=posted,
            edited=edited,
            dry_run=dry_run,
            duplicate=duplicate,
            in_progress=in_progress,
            blocked=blocked,
            message_id=message_id,
            content=content,
            semantic_fingerprint=snapshot.semantic_fingerprint,
            attempt_count=attempt_count,
            error_code=error_code,
            uncertain=uncertain,
        )

    async def publish(
        self,
        *,
        channel_id: int,
        local_date: date | datetime | None = None,
        now: datetime | None = None,
        force: bool = False,
    ) -> FinanceDigestPublishOutcome:
        """Build and, unless dry-run, deliver one local-date snapshot."""

        snapshot = self.service.build_snapshot(local_date, now=now)
        if not self.post_zero_days and self._empty_day(snapshot):
            # A genuinely new zero day may be omitted.  If a message already
            # exists, however, a correction that reduced the day to zero must
            # edit that message instead of leaving stale spending visible.
            existing = self.service.get_delivery(snapshot.local_date, channel_id)
            if existing is None or (
                existing.status == "failed" and existing.discord_message_id is None
            ):
                return self._outcome(
                    snapshot=snapshot,
                    action=DeliveryAction.SKIP,
                    content="",
                    attempt_count=0,
                    duplicate=True,
                )

        plan = self.service.prepare_delivery(
            snapshot,
            channel_id,
            now=now,
            dry_run=self.dry_run,
            force=force,
        )
        if plan.dry_run:
            return self._outcome(
                snapshot=snapshot,
                action=plan.action,
                content=plan.content,
                attempt_count=plan.attempt_count,
                dry_run=True,
                duplicate=plan.action == DeliveryAction.SKIP,
                in_progress=plan.action == DeliveryAction.IN_PROGRESS,
                blocked=plan.action == DeliveryAction.BLOCKED,
                message_id=plan.message_id,
                uncertain=plan.action == DeliveryAction.BLOCKED,
            )
        if not plan.claimed:
            return self._outcome(
                snapshot=snapshot,
                action=plan.action,
                content=plan.content,
                attempt_count=plan.attempt_count,
                duplicate=plan.action == DeliveryAction.SKIP,
                in_progress=plan.action == DeliveryAction.IN_PROGRESS,
                blocked=plan.action == DeliveryAction.BLOCKED,
                message_id=plan.message_id,
                error_code=(
                    "delivery_outcome_unknown"
                    if plan.action == DeliveryAction.BLOCKED
                    else None
                ),
                uncertain=plan.action == DeliveryAction.BLOCKED,
            )

        try:
            message_id, edited, attempts = await self._deliver_with_retry(
                channel_id=channel_id,
                content=plan.content,
                existing_message_id=plan.message_id,
                before_replacement_create=(
                    lambda: self.service.mark_replacement_create_started(plan, now=now)
                    if plan.message_id is not None
                    else None
                ),
            )
        except DiscordSendError as exc:
            replacement_create = isinstance(exc, _ReplacementCreateSendError)
            marker = self.service.mark_unknown if exc.outcome_unknown else self.service.mark_failed
            marker(
                plan,
                error_code=exc.code,
                safe_message=exc.safe_message,
                now=now,
                clear_message_target=replacement_create,
            )
            return self._outcome(
                snapshot=snapshot,
                action=plan.action,
                content=plan.content,
                attempt_count=plan.attempt_count,
                message_id=None if replacement_create else plan.message_id,
                error_code=exc.code,
                uncertain=exc.outcome_unknown,
                blocked=exc.outcome_unknown,
            )
        except Exception as exc:
            # Never persist arbitrary exception text: transport/library errors
            # may include response bodies.  The type name is enough to debug.
            error_code = f"discord_send_failed:{type(exc).__name__}"[:64]
            self.service.mark_failed(
                plan,
                error_code=error_code,
                safe_message="Discordへの送信に失敗しました。",
                now=now,
            )
            return self._outcome(
                snapshot=snapshot,
                action=plan.action,
                content=plan.content,
                attempt_count=plan.attempt_count,
                message_id=plan.message_id,
                error_code=error_code,
            )

        try:
            self.service.mark_sent(plan, discord_message_id=message_id, posted_at=now)
        except DeliveryClaimConflictError:
            # Discord accepted the operation, but the local claim was lost.
            # The stale-claim recovery path will keep a create blocked rather
            # than risking a second message.
            return self._outcome(
                snapshot=snapshot,
                action=plan.action,
                content=plan.content,
                attempt_count=plan.attempt_count,
                edited=edited,
                message_id=message_id,
                error_code="delivery_finalize_conflict",
                uncertain=True,
                blocked=not edited,
            )
        return self._outcome(
            snapshot=snapshot,
            action=plan.action,
            content=plan.content,
            attempt_count=max(plan.attempt_count, attempts),
            posted=True,
            edited=edited,
            message_id=message_id,
        )


__all__ = [
    "FinanceDigestPublishOutcome",
    "FinanceDigestPublisher",
]
