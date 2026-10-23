from datetime import UTC, datetime, timedelta

import pytest

from life_inbox.schemas import (
    DomainValidationError,
    ReceiptAnalysisResult,
    TextAnalysisResult,
    validate_receipt_result,
    validate_text_result,
)


def test_receipt_allows_zero_tax_but_not_zero_total():
    result = ReceiptAnalysisResult(subtotal=100, tax=0, total=100)
    assert validate_receipt_result(result, max_amount_yen=1_000_000).tax == 0

    with pytest.raises(DomainValidationError, match="total"):
        validate_receipt_result(
            ReceiptAnalysisResult(total=0),
            max_amount_yen=1_000_000,
        )


def test_receipt_rejects_implausible_future_datetime():
    with pytest.raises(DomainValidationError, match="purchase_datetime"):
        validate_receipt_result(
            ReceiptAnalysisResult(
                total=100,
                purchase_datetime=datetime.now(UTC) + timedelta(days=3),
            ),
            max_amount_yen=1_000_000,
        )


def test_food_subcategory_requires_food_category():
    with pytest.raises(DomainValidationError, match="requires category"):
        validate_text_result(
            TextAnalysisResult(
                event_kinds=["expense"],
                amount=220,
                category="交通",
                subcategory="外食",
            ),
            max_amount_yen=1_000_000,
        )


def test_custom_category_is_accepted_when_configured():
    result = TextAnalysisResult(
        event_kinds=["expense"],
        amount=3_000,
        category="医療",
    )
    validated = validate_text_result(
        result,
        max_amount_yen=1_000_000,
        allowed_categories=("医療", "未分類"),
    )
    assert validated.category == "医療"
