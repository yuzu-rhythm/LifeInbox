"""Deterministic parsers for the specialized low-friction Discord channels."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

_ACCOUNT_PREFIX_RE = re.compile(r"^\[(?P<account>[^\[\]]{1,80})\]\s*(?P<body>.*)$")
_AMOUNT_PATTERN = r"(?P<amount>\d[\d,]*)"
_INCOME_RE = re.compile(rf"^(?:入金|収入|income)\s+{_AMOUNT_PATTERN}(?:\s+(?P<note>.*))?$", re.I)
_EXPENSE_RE = re.compile(rf"^(?:支出|expense)\s+{_AMOUNT_PATTERN}(?:\s+(?P<note>.*))?$", re.I)
_TRANSFER_RE = re.compile(
    rf"^(?:振替|移動|transfer)\s+{_AMOUNT_PATTERN}\s*->\s*"
    r"\[(?P<destination>[^\[\]]{1,80})\](?:\s+(?P<note>.*))?$",
    re.I,
)
_CARD_PAYMENT_RE = re.compile(
    rf"^(?:カード引落|カード支払|card\s*payment)\s+{_AMOUNT_PATTERN}\s*->\s*"
    r"\[(?P<destination>[^\[\]]{1,80})\](?:\s+(?P<note>.*))?$",
    re.I,
)


class AccountInstructionKind(StrEnum):
    INCOME = "income"
    EXPENSE = "expense"
    TRANSFER = "transfer"


@dataclass(frozen=True, slots=True)
class CardInput:
    account_name: str | None
    expense_text: str


@dataclass(frozen=True, slots=True)
class AccountInstruction:
    kind: AccountInstructionKind
    amount: int
    source_account: str | None
    destination_account: str | None
    note: str | None
    card_payment: bool = False


class RhythmInstructionKind(StrEnum):
    TODO = "todo"
    RESULT = "result"


@dataclass(frozen=True, slots=True)
class RhythmInstruction:
    kind: RhythmInstructionKind
    game: str | None
    text: str


_GAME_ALIASES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^(?:maimai|マイマイ|舞)(?:\s+|$)", re.I), "maimai"),
    (re.compile(r"^(?:ongeki|オンゲキ)(?:\s+|$)", re.I), "ongeki"),
    (re.compile(r"^(?:chunithm|チュウニズム|ウニ)(?:\s+|$)", re.I), "chunithm"),
)


def _clean_account(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def split_account_prefix(text: str | None) -> tuple[str | None, str]:
    """Split optional ``[口座名]`` while preserving the remaining literal text."""

    raw = (text or "").strip()
    match = _ACCOUNT_PREFIX_RE.fullmatch(raw)
    if match is None:
        return None, raw
    return _clean_account(match.group("account")), match.group("body").strip()


def parse_card_input(text: str | None) -> CardInput:
    account_name, expense_text = split_account_prefix(text)
    if not expense_text:
        raise ValueError("クレカの利用内容を入力してください")
    return CardInput(account_name=account_name, expense_text=expense_text)


def _positive_amount(value: str, *, maximum: int) -> int:
    if not re.fullmatch(r"\d{1,3}(?:,\d{3})*|\d+", value):
        raise ValueError("金額の形式が不正です")
    amount = int(value.replace(",", ""))
    if amount <= 0 or amount > maximum:
        raise ValueError(f"金額は1〜{maximum:,}円で入力してください")
    return amount


def parse_account_instruction(
    text: str | None, *, maximum: int = 10_000_000
) -> AccountInstruction:
    """Parse explicit income, expense, transfer, or card-settlement directives.

    Plain ``出金`` is intentionally unsupported: an ATM withdrawal is normally
    a bank-to-cash transfer, not consumption. The caller can show the concrete
    ``振替 ... -> [現金]`` hint without silently double-counting spending.
    """

    source_account, body = split_account_prefix(text)
    if not body:
        raise ValueError("口座の操作内容を入力してください")

    for pattern, kind, card_payment in (
        (_INCOME_RE, AccountInstructionKind.INCOME, False),
        (_EXPENSE_RE, AccountInstructionKind.EXPENSE, False),
        (_TRANSFER_RE, AccountInstructionKind.TRANSFER, False),
        (_CARD_PAYMENT_RE, AccountInstructionKind.TRANSFER, True),
    ):
        match = pattern.fullmatch(body)
        if match is None:
            continue
        destination = _clean_account(match.groupdict().get("destination"))
        return AccountInstruction(
            kind=kind,
            amount=_positive_amount(match.group("amount"), maximum=maximum),
            source_account=source_account,
            destination_account=destination,
            note=_clean_account(match.groupdict().get("note")),
            card_payment=card_payment,
        )

    if re.match(r"^(?:出金|withdrawal)(?:\s|$)", body, re.I):
        raise ValueError("ATM出金は『振替 5000 -> [現金] ATM』として入力してください")
    raise ValueError(
        "『入金 250000 給料』『支出 1200 電気代』または"
        "『振替 5000 -> [現金] ATM』の形式で入力してください"
    )


def _extract_game(text: str) -> tuple[str | None, str]:
    for pattern, canonical in _GAME_ALIASES:
        match = pattern.match(text)
        if match:
            return canonical, text[match.end() :].strip()
    return None, text.strip()


def parse_rhythm_instruction(text: str | None) -> RhythmInstruction:
    """Parse a rhythm-game TODO/result header without interpreting score images."""

    raw = (text or "").strip()
    lowered = raw.casefold()
    kind: RhythmInstructionKind
    for prefix in ("todo", "課題", "やること"):
        if lowered == prefix.casefold() or lowered.startswith(prefix.casefold() + " "):
            kind = RhythmInstructionKind.TODO
            raw = raw[len(prefix) :].strip()
            break
    else:
        for prefix in ("result", "リザルト", "結果"):
            if lowered == prefix.casefold() or lowered.startswith(prefix.casefold() + " "):
                kind = RhythmInstructionKind.RESULT
                raw = raw[len(prefix) :].strip()
                break
        else:
            # A game-prefixed plain sentence is the fastest TODO input.
            kind = RhythmInstructionKind.TODO

    game, body = _extract_game(raw)
    if kind == RhythmInstructionKind.TODO and game is None:
        raise ValueError("ゲーム名は maimai・オンゲキ・チュウニズムから指定してください")
    if kind == RhythmInstructionKind.TODO and not body:
        raise ValueError("TODOの内容を入力してください")
    return RhythmInstruction(kind=kind, game=game, text=body)
