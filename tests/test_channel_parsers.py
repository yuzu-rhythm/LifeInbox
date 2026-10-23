import pytest

from life_inbox.channel_parsers import (
    AccountInstructionKind,
    RhythmInstructionKind,
    parse_account_instruction,
    parse_card_input,
    parse_rhythm_instruction,
)


def test_card_input_supports_default_and_named_cards():
    default = parse_card_input("680 マック")
    named = parse_card_input("[楽天カード] 1,280 Amazon")

    assert default.account_name is None
    assert default.expense_text == "680 マック"
    assert named.account_name == "楽天カード"
    assert named.expense_text == "1,280 Amazon"


def test_account_income_expense_and_multiple_named_accounts():
    income = parse_account_instruction("[ゆうちょ] 入金 250,000 給料")
    expense = parse_account_instruction("[三菱UFJ] 支出 1,200 電気代")

    assert income.kind == AccountInstructionKind.INCOME
    assert income.source_account == "ゆうちょ"
    assert income.amount == 250_000
    assert income.note == "給料"
    assert expense.kind == AccountInstructionKind.EXPENSE
    assert expense.source_account == "三菱UFJ"
    assert expense.amount == 1_200


def test_transfer_and_card_payment_are_explicit_non_expense_movements():
    transfer = parse_account_instruction("[ゆうちょ] 振替 5,000 -> [現金] ATM")
    settlement = parse_account_instruction(
        "[ゆうちょ] カード引落 68,000 -> [楽天カード] 8月分"
    )

    assert transfer.kind == AccountInstructionKind.TRANSFER
    assert transfer.destination_account == "現金"
    assert transfer.card_payment is False
    assert settlement.kind == AccountInstructionKind.TRANSFER
    assert settlement.destination_account == "楽天カード"
    assert settlement.card_payment is True


def test_plain_withdrawal_is_rejected_to_prevent_false_expense():
    with pytest.raises(ValueError, match="振替"):
        parse_account_instruction("出金 5000 ATM")


@pytest.mark.parametrize(
    ("text", "kind", "game", "body"),
    [
        (
            "todo maimai でらっくす譜面 SSS+",
            RhythmInstructionKind.TODO,
            "maimai",
            "でらっくす譜面 SSS+",
        ),
        ("課題 オンゲキ ABFBを詰める", RhythmInstructionKind.TODO, "ongeki", "ABFBを詰める"),
        ("チュウニズム 14+を触る", RhythmInstructionKind.TODO, "chunithm", "14+を触る"),
        ("result maimai 100.5%", RhythmInstructionKind.RESULT, "maimai", "100.5%"),
        ("リザルト", RhythmInstructionKind.RESULT, None, ""),
    ],
)
def test_rhythm_instructions(text, kind, game, body):
    parsed = parse_rhythm_instruction(text)
    assert parsed.kind == kind
    assert parsed.game == game
    assert parsed.text == body


def test_rhythm_todo_requires_supported_game_and_body():
    with pytest.raises(ValueError, match="ゲーム名"):
        parse_rhythm_instruction("todo SDVX つまみ練習")
    with pytest.raises(ValueError, match="TODOの内容"):
        parse_rhythm_instruction("todo maimai")
