"""Strict user, guild, channel, and bot-message scope checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _same_id(expected: int | None, actual: Any) -> bool:
    return expected is not None and actual is not None and str(expected) == str(actual)


@dataclass(frozen=True, slots=True)
class ScopeGuard:
    allowed_user_id: int | None
    guild_id: int | None
    channel_id: int | None
    records_channel_id: int | None = None
    card_channel_id: int | None = None
    account_channel_id: int | None = None
    memo_channel_id: int | None = None
    rhythm_channel_id: int | None = None
    meal_channel_id: int | None = None
    progress_channel_id: int | None = None
    social_draft_channel_id: int | None = None
    legacy_channel_id: int | None = None

    def allows_ids(self, *, user_id: Any, guild_id: Any, channel_id: Any) -> bool:
        return (
            _same_id(self.allowed_user_id, user_id)
            and _same_id(self.guild_id, guild_id)
            and _same_id(self.channel_id, channel_id)
        )

    def message_role(self, message: Any, *, self_user_id: Any = None) -> str | None:
        """Return the configured capture role after strict identity checks."""

        author = getattr(message, "author", None)
        user_id = getattr(author, "id", getattr(message, "author_id", None))
        if getattr(author, "bot", False) or getattr(message, "webhook_id", None) is not None:
            return None
        if self_user_id is not None and str(user_id) == str(self_user_id):
            return None
        guild_id = getattr(
            getattr(message, "guild", None), "id", getattr(message, "guild_id", None)
        )
        if not (
            _same_id(self.allowed_user_id, user_id)
            and _same_id(self.guild_id, guild_id)
        ):
            return None
        channel_id = getattr(
            getattr(message, "channel", None), "id", getattr(message, "channel_id", None)
        )
        for role, expected in (
            ("inbox", self.channel_id),
            ("inbox_compat", self.legacy_channel_id),
            ("card", self.card_channel_id),
            ("account", self.account_channel_id),
            ("memo", self.memo_channel_id),
            ("social_draft", self.social_draft_channel_id),
            ("rhythm", self.rhythm_channel_id),
            ("rhythm_progress", self.progress_channel_id),
            ("meal", self.meal_channel_id),
        ):
            if _same_id(expected, channel_id):
                return role
        return None

    def allows_message(self, message: Any, *, self_user_id: Any = None) -> bool:
        """Backward-compatible Inbox-only message check."""

        return self.message_role(message, self_user_id=self_user_id) in {
            "inbox",
            "inbox_compat",
        }

    def allows_inbox_ids(self, *, user_id: Any, guild_id: Any, channel_id: Any) -> bool:
        """Allow candidate actions by IDs in the primary or legacy Inbox."""

        return (
            _same_id(self.allowed_user_id, user_id)
            and _same_id(self.guild_id, guild_id)
            and (
                _same_id(self.channel_id, channel_id)
                or _same_id(self.legacy_channel_id, channel_id)
            )
        )

    def allows_inbox_interaction(self, interaction: Any) -> bool:
        """Allow candidate actions in the primary or compatibility inbox."""

        user = getattr(interaction, "user", None)
        return (
            not getattr(user, "bot", False)
            and self.allows_inbox_ids(
                user_id=getattr(user, "id", None),
                guild_id=getattr(interaction, "guild_id", None),
                channel_id=getattr(interaction, "channel_id", None),
            )
        )

    def allows_interaction(self, interaction: Any) -> bool:
        """Allow a mutating interaction only in the Inbox channel."""

        user = getattr(interaction, "user", None)
        return self.allows_ids(
            user_id=getattr(user, "id", None),
            guild_id=getattr(interaction, "guild_id", None),
            channel_id=getattr(interaction, "channel_id", None),
        )

    def allows_read_interaction(self, interaction: Any) -> bool:
        """Allow read-only interactions in Inbox or the optional records channel."""

        user = getattr(interaction, "user", None)
        channel_id = getattr(interaction, "channel_id", None)
        return (
            _same_id(self.allowed_user_id, getattr(user, "id", None))
            and _same_id(self.guild_id, getattr(interaction, "guild_id", None))
            and (
                _same_id(self.channel_id, channel_id)
                or _same_id(self.legacy_channel_id, channel_id)
                or _same_id(self.records_channel_id, channel_id)
            )
        )

    def allows_event_interaction(self, interaction: Any) -> bool:
        """Allow candidate actions in the Event-producing capture channels."""

        user = getattr(interaction, "user", None)
        channel_id = getattr(interaction, "channel_id", None)
        return (
            _same_id(self.allowed_user_id, getattr(user, "id", None))
            and _same_id(self.guild_id, getattr(interaction, "guild_id", None))
            and (
                _same_id(self.channel_id, channel_id)
                or _same_id(self.legacy_channel_id, channel_id)
                or _same_id(self.records_channel_id, channel_id)
                or _same_id(self.card_channel_id, channel_id)
                or _same_id(self.account_channel_id, channel_id)
                or _same_id(self.meal_channel_id, channel_id)
            )
        )

    def allows_account_interaction(self, interaction: Any) -> bool:
        """Allow account mutations only in the configured account channel."""

        user = getattr(interaction, "user", None)
        return (
            _same_id(self.allowed_user_id, getattr(user, "id", None))
            and _same_id(self.guild_id, getattr(interaction, "guild_id", None))
            and _same_id(self.account_channel_id, getattr(interaction, "channel_id", None))
        )

    def allows_finance_read_interaction(self, interaction: Any) -> bool:
        """Allow account/memo lists in their own channel or the read channel."""

        user = getattr(interaction, "user", None)
        channel_id = getattr(interaction, "channel_id", None)
        return (
            _same_id(self.allowed_user_id, getattr(user, "id", None))
            and _same_id(self.guild_id, getattr(interaction, "guild_id", None))
            and any(
                _same_id(expected, channel_id)
                for expected in (
                    self.channel_id,
                    self.legacy_channel_id,
                    self.records_channel_id,
                    self.account_channel_id,
                    self.memo_channel_id,
                )
            )
        )

    def allows_rhythm_interaction(self, interaction: Any, *, read_only: bool = False) -> bool:
        """Allow rhythm mutations in #音ゲー and reads there or in record channels."""

        user = getattr(interaction, "user", None)
        channel_id = getattr(interaction, "channel_id", None)
        allowed_channels = (
            (
                self.rhythm_channel_id,
                self.progress_channel_id,
                self.records_channel_id,
                self.channel_id,
                self.legacy_channel_id,
            )
            if read_only
            else (self.rhythm_channel_id, self.progress_channel_id)
        )
        return (
            _same_id(self.allowed_user_id, getattr(user, "id", None))
            and _same_id(self.guild_id, getattr(interaction, "guild_id", None))
            and any(_same_id(expected, channel_id) for expected in allowed_channels)
        )

    def allows_social_draft_interaction(
        self,
        interaction: Any,
        *,
        action_channel_id: Any = None,
    ) -> bool:
        """Allow social-draft target choices in their action-card channel."""

        user = getattr(interaction, "user", None)
        if getattr(user, "bot", False):
            return False
        expected_channel_id = (
            action_channel_id
            if action_channel_id is not None
            else self.social_draft_channel_id
        )
        return (
            _same_id(self.allowed_user_id, getattr(user, "id", None))
            and _same_id(self.guild_id, getattr(interaction, "guild_id", None))
            and _same_id(expected_channel_id, getattr(interaction, "channel_id", None))
        )
