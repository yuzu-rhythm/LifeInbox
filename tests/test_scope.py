from types import SimpleNamespace

from life_inbox.scope import ScopeGuard


def test_message_scope_rejects_wrong_user_guild_channel_and_bots():
    guard = ScopeGuard(10, 20, 30)
    allowed = SimpleNamespace(
        author=SimpleNamespace(id=10, bot=False),
        guild=SimpleNamespace(id=20),
        channel=SimpleNamespace(id=30),
        webhook_id=None,
    )
    assert guard.allows_message(allowed)
    assert not guard.allows_message(SimpleNamespace(**{**allowed.__dict__, "webhook_id": 1}))
    assert not guard.allows_ids(user_id=11, guild_id=20, channel_id=30)
    assert not guard.allows_ids(user_id=10, guild_id=21, channel_id=30)
    assert not guard.allows_ids(user_id=10, guild_id=20, channel_id=31)
    assert not guard.allows_ids(user_id=10, guild_id=None, channel_id=30)


def test_interaction_scope():
    guard = ScopeGuard(10, 20, 30)
    interaction = SimpleNamespace(user=SimpleNamespace(id=10), guild_id=20, channel_id=30)
    assert guard.allows_interaction(interaction)
    interaction.user.id = 11
    assert not guard.allows_interaction(interaction)


def test_records_channel_is_read_only_and_inbox_fallback_remains_unchanged():
    guard = ScopeGuard(10, 20, 30, 40)
    records_interaction = SimpleNamespace(
        user=SimpleNamespace(id=10), guild_id=20, channel_id=40
    )
    assert guard.allows_read_interaction(records_interaction)
    assert not guard.allows_interaction(records_interaction)
    # Generic mutations stay Inbox-only, but event cards are deliberately
    # published to the records channel and their owner-scoped buttons must work.
    assert guard.allows_event_interaction(records_interaction)

    records_message = SimpleNamespace(
        author=SimpleNamespace(id=10, bot=False),
        guild=SimpleNamespace(id=20),
        channel=SimpleNamespace(id=40),
        webhook_id=None,
    )
    assert not guard.allows_message(records_message)

    without_records = ScopeGuard(10, 20, 30)
    assert not without_records.allows_read_interaction(records_interaction)
    assert not without_records.allows_event_interaction(records_interaction)
    inbox_interaction = SimpleNamespace(
        user=SimpleNamespace(id=10), guild_id=20, channel_id=30
    )
    assert without_records.allows_read_interaction(inbox_interaction)


def test_capture_channel_roles_and_specialized_interaction_scopes():
    guard = ScopeGuard(10, 20, 30, 40, 41, 42, 43, 44, 45)

    def message(channel_id):
        return SimpleNamespace(
            author=SimpleNamespace(id=10, bot=False),
            guild=SimpleNamespace(id=20),
            channel=SimpleNamespace(id=channel_id),
            webhook_id=None,
        )

    assert guard.message_role(message(30)) == "inbox"
    assert guard.message_role(message(41)) == "card"
    assert guard.message_role(message(42)) == "account"
    assert guard.message_role(message(43)) == "memo"
    assert guard.message_role(message(44)) == "rhythm"
    assert guard.message_role(message(45)) == "meal"
    assert guard.message_role(message(46)) is None
    assert guard.allows_message(message(30))
    assert not guard.allows_message(message(41))

    card = SimpleNamespace(user=SimpleNamespace(id=10), guild_id=20, channel_id=41)
    account = SimpleNamespace(user=SimpleNamespace(id=10), guild_id=20, channel_id=42)
    memo = SimpleNamespace(user=SimpleNamespace(id=10), guild_id=20, channel_id=43)
    rhythm = SimpleNamespace(user=SimpleNamespace(id=10), guild_id=20, channel_id=44)
    meal = SimpleNamespace(user=SimpleNamespace(id=10), guild_id=20, channel_id=45)
    assert guard.allows_event_interaction(card)
    assert guard.allows_event_interaction(account)
    assert guard.allows_event_interaction(meal)
    assert not guard.allows_interaction(card)
    assert guard.allows_account_interaction(account)
    assert not guard.allows_account_interaction(card)
    assert guard.allows_finance_read_interaction(account)
    assert guard.allows_finance_read_interaction(memo)
    assert guard.allows_rhythm_interaction(rhythm)
    assert guard.allows_rhythm_interaction(rhythm, read_only=True)
    assert not guard.allows_rhythm_interaction(account)


def test_progress_channel_has_rhythm_role_and_read_write_scopes():
    guard = ScopeGuard(
        10,
        20,
        30,
        records_channel_id=40,
        progress_channel_id=46,
    )

    message = SimpleNamespace(
        author=SimpleNamespace(id=10, bot=False),
        guild=SimpleNamespace(id=20),
        channel=SimpleNamespace(id=46),
        webhook_id=None,
    )
    assert guard.message_role(message) == "rhythm_progress"

    interaction = SimpleNamespace(user=SimpleNamespace(id=10), guild_id=20, channel_id=46)
    assert guard.allows_rhythm_interaction(interaction)
    assert guard.allows_rhythm_interaction(interaction, read_only=True)

    wrong_user = SimpleNamespace(user=SimpleNamespace(id=11), guild_id=20, channel_id=46)
    assert not guard.allows_rhythm_interaction(wrong_user)
    webhook = SimpleNamespace(**{**message.__dict__, "webhook_id": 999})
    assert guard.message_role(webhook) is None
    bot_message = SimpleNamespace(
        author=SimpleNamespace(id=10, bot=True),
        guild=SimpleNamespace(id=20),
        channel=SimpleNamespace(id=46),
        webhook_id=None,
    )
    assert guard.message_role(bot_message) is None


def test_social_draft_channel_role_and_selection_scope_are_restricted():
    guard = ScopeGuard(
        10,
        20,
        30,
        social_draft_channel_id=70,
    )
    message = SimpleNamespace(
        author=SimpleNamespace(id=10, bot=False),
        guild=SimpleNamespace(id=20),
        channel=SimpleNamespace(id=70),
        webhook_id=None,
    )
    assert guard.message_role(message) == "social_draft"

    allowed = SimpleNamespace(user=SimpleNamespace(id=10, bot=False), guild_id=20, channel_id=70)
    assert guard.allows_social_draft_interaction(allowed)
    assert not guard.allows_social_draft_interaction(
        SimpleNamespace(user=SimpleNamespace(id=11, bot=False), guild_id=20, channel_id=70)
    )
    assert not guard.allows_social_draft_interaction(
        SimpleNamespace(user=SimpleNamespace(id=10, bot=False), guild_id=21, channel_id=70)
    )
    assert not guard.allows_social_draft_interaction(
        SimpleNamespace(user=SimpleNamespace(id=10, bot=False), guild_id=20, channel_id=71)
    )
    assert not guard.allows_social_draft_interaction(
        SimpleNamespace(user=SimpleNamespace(id=10, bot=True), guild_id=20, channel_id=70)
    )
