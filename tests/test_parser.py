from life_inbox.constants import EventKind, MealType
from life_inbox.parser import parse_message


def test_parser_amount_and_merchant_examples():
    cases = {
        "680 マック": (680, "マック"),
        "マック 680": (680, "マック"),
        "電車220": (220, "電車"),
        "ゲーセン 2000": (2000, "ゲーセン"),
        "昼飯 850 魂心家": (850, "魂心家"),
        "680": (680, None),
    }
    for text, expected in cases.items():
        result = parse_message(text)
        assert (result.amount, result.merchant_raw) == expected
        assert not result.amount_invalid


def test_parser_meal_hint_and_event_kind():
    result = parse_message("昼飯 850 魂心家")
    assert result.meal_type == MealType.LUNCH
    assert result.has_meal_hint
    assert result.kind_hint == EventKind.EXPENSE


def test_parser_empty_and_merchant_only():
    empty = parse_message("")
    assert empty.amount is None
    assert empty.merchant_raw is None
    assert empty.kind_hint is None

    merchant = parse_message("マック")
    assert merchant.amount is None
    assert merchant.merchant_raw == "マック"
    assert not merchant.amount_invalid


def test_parser_rejects_invalid_zero_negative_extreme_and_malformed_amounts():
    for text in ("0 マック", "-680 マック", "999999999 マック", "12,34 マック", "680.5 マック"):
        result = parse_message(text)
        assert result.amount is None
        assert result.amount_invalid, text
