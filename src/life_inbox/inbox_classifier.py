"""Safe multimodal classification for the free-form Discord Life Inbox.

The classifier is deliberately smaller than the domain analyzers.  It only
chooses reviewed destinations and extracts bounded handwritten planner items;
it does not create database rows, invoke tools, or fetch URLs.  The Discord
adapter performs destination sends after review or a high-confidence planner
split.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class InboxCategory(StrEnum):
    """Reviewed destination classes shown to the user as buttons."""

    EXPENSES = "expenses"
    MEALS = "meals"
    MEMO = "memo"
    SELF_TALK = "self_talk"
    DINING_OUT = "dining_out"
    MEMORIES = "memories"
    SOCIAL_DRAFT = "social_draft"
    TASK = "task"
    SHOPPING = "shopping"
    JOB_HUNTING = "job_hunting"
    SAVED_LINK = "saved_link"
    RHYTHM_SPENDING = "rhythm_spending"
    RHYTHM_PROGRESS = "rhythm_progress"
    FINANCE_ACCOUNT = "finance_account"
    FINANCE_CARD = "finance_card"
    UNKNOWN = "unknown"


CATEGORY_LABELS: dict[InboxCategory, str] = {
    InboxCategory.EXPENSES: "支出・レシート",
    InboxCategory.MEALS: "食事",
    InboxCategory.MEMO: "一言メモ",
    InboxCategory.SELF_TALK: "独り言",
    InboxCategory.DINING_OUT: "外食",
    InboxCategory.MEMORIES: "思い出",
    InboxCategory.SOCIAL_DRAFT: "毎日の投稿",
    InboxCategory.TASK: "タスク・期限",
    InboxCategory.SHOPPING: "買い物リスト",
    InboxCategory.JOB_HUNTING: "就活情報",
    InboxCategory.SAVED_LINK: "神リンク",
    InboxCategory.RHYTHM_SPENDING: "音ゲー利用額",
    InboxCategory.RHYTHM_PROGRESS: "音ゲー進捗・リザルト",
    InboxCategory.FINANCE_ACCOUNT: "口座・家計",
    InboxCategory.FINANCE_CARD: "クレカ・カード",
    InboxCategory.UNKNOWN: "保留",
}


class InboxCandidate(BaseModel):
    """One candidate selected from the closed category enum."""

    model_config = ConfigDict(extra="forbid")

    category: InboxCategory
    confidence: float = Field(ge=0, le=1)


InboxDocumentKind = Literal[
    "text",
    "planner",
    "receipt",
    "meal_photo",
    "rhythm_result",
    "photo",
    "unknown",
]


class InboxPlannerItem(BaseModel):
    """One handwritten planner item extracted from an attached image."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=1_000)
    candidates: list[InboxCandidate] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def validate_candidates(self) -> InboxPlannerItem:
        categories = [candidate.category for candidate in self.candidates]
        if len(categories) != len(set(categories)):
            raise ValueError("planner item candidates must be unique")
        if InboxCategory.UNKNOWN in categories and len(categories) != 1:
            raise ValueError("unknown cannot be combined with another candidate")
        return self


class InboxClassifierOutput(BaseModel):
    """Closed Responses API schema for text and bounded image classification."""

    model_config = ConfigDict(extra="forbid")

    candidates: list[InboxCandidate] = Field(min_length=1, max_length=3)
    document_kind: InboxDocumentKind = "text"
    planner_items: list[InboxPlannerItem] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_candidates(self) -> InboxClassifierOutput:
        categories = [candidate.category for candidate in self.candidates]
        if len(categories) != len(set(categories)):
            raise ValueError("classifier candidates must be unique")
        if InboxCategory.UNKNOWN in categories and len(categories) != 1:
            raise ValueError("unknown cannot be combined with another candidate")
        if self.document_kind != "planner" and self.planner_items:
            raise ValueError("planner_items require document_kind=planner")
        return self


@dataclass(frozen=True, slots=True)
class InboxImageInput:
    """One already stored local image supplied to the classifier."""

    path: Path
    content_type: str


@dataclass(frozen=True, slots=True)
class InboxClassification:
    """A safe classification result with no provider response text."""

    candidates: tuple[InboxCandidate, ...]
    source: str
    fallback_reason: str | None = None
    document_kind: InboxDocumentKind = "text"
    planner_items: tuple[InboxPlannerItem, ...] = ()

    @property
    def is_unknown(self) -> bool:
        return (
            not self.candidates
            or all(candidate.category is InboxCategory.UNKNOWN for candidate in self.candidates)
        )

    @property
    def primary(self) -> InboxCandidate | None:
        return self.candidates[0] if self.candidates else None


_URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
_CURRENCY_RE = re.compile(r"(?:[￥¥]\s*[0-9]|[0-9][0-9,]*\s*円)", re.IGNORECASE)
_NUMBER_AMOUNT_RE = re.compile(r"(?:^|\s)[0-9][0-9,]*(?:\s|$)")
_SELF_TALK_MARKER_RE = re.compile(
    r"(?:self[- _]?talk|独り言|ひとりごと|つぶやき)(?=$|\s|[:：、。,.!?！？])",
    re.IGNORECASE,
)
_DINING_OUT_MARKER_RE = re.compile(
    r"^(?:dining[- _]?out|外食)(?=$|\s|[:：、。,.!?！？])",
    re.IGNORECASE,
)
_MEMORIES_MARKER_RE = re.compile(
    r"(?:memories?|思い出(?:として残す|に残す|の写真)?|記念(?:として残す|日|写真)?)(?=$|\s|[:：、。,.!?！？])",
    re.IGNORECASE,
)
_SOCIAL_DRAFT_CUES = (
    "snsに投稿",
    "xに投稿",
    "twitterに投稿",
    "インスタに投稿",
    "instagramに投稿",
    "今日の投稿",
)


def _candidate(category: InboxCategory, confidence: float) -> InboxCandidate:
    return InboxCandidate(category=category, confidence=max(0.0, min(1.0, confidence)))


def _local_candidates(text: str, *, attachment_count: int = 0) -> tuple[InboxCandidate, ...]:
    """Classify common Japanese Inbox forms without network access.

    This intentionally returns ``unknown`` for an uninformative sentence.  A
    false positive would send a user's original post to the wrong channel,
    while an unknown result remains available for a later explicit retry.
    """

    raw = (text or "").strip()
    lowered = raw.casefold()
    scores: dict[InboxCategory, float] = {}

    def add(category: InboxCategory, confidence: float) -> None:
        scores[category] = max(scores.get(category, 0.0), confidence)

    has_url = _URL_RE.search(raw) is not None
    has_amount = _CURRENCY_RE.search(raw) is not None or _NUMBER_AMOUNT_RE.search(raw) is not None

    if not raw and attachment_count:
        add(InboxCategory.EXPENSES, 0.68)
        add(InboxCategory.MEALS, 0.52)

    if _SELF_TALK_MARKER_RE.search(raw):
        add(InboxCategory.SELF_TALK, 0.99)
    if _DINING_OUT_MARKER_RE.search(raw):
        add(InboxCategory.DINING_OUT, 0.99)
    if _MEMORIES_MARKER_RE.search(raw):
        add(InboxCategory.MEMORIES, 0.99)
    if any(cue in lowered for cue in _SOCIAL_DRAFT_CUES):
        add(InboxCategory.SOCIAL_DRAFT, 0.96)
    if re.search(r"(?:^|\s)(?:memo|メモ|一言メモ)(?:\s*[:：]|\s|$)", lowered):
        add(InboxCategory.MEMO, 0.99)
    if re.search(
        r"(?:^|\s)(?:task|todo|タスク|予定|やること|deadline|due)"
        r"(?:\s*[:：]|\s|$)",
        lowered,
    ):
        add(InboxCategory.TASK, 0.99)

    if any(word in lowered for word in ("締切", "期限", "提出", "までに", "リマインド")):
        add(InboxCategory.TASK, 0.88)
    rhythm_spending_game = any(
        word in lowered
        for word in (
            "maimai",
            "マイマイ",
            "オンゲキ",
            "ongeki",
            "チュウニズム",
            "chunithm",
            "ウニ",
        )
    ) or re.search(
        r"(?:^|\s)(?:マイ|ゲキ)(?=\s|[:：]|[0-9￥¥]|$)", raw, re.IGNORECASE
    ) is not None
    if rhythm_spending_game and has_amount:
        add(InboxCategory.RHYTHM_SPENDING, 0.99)

    if any(
        word in lowered
        for word in ("買い物", "買う", "購入", "欲しい", "買って", "リスト", "shopping")
    ):
        add(InboxCategory.SHOPPING, 0.86)
    if any(
        word in lowered
        for word in (
            "就活",
            "求人",
            "応募",
            "面接",
            "インターン",
            "会社説明会",
            "職種",
            "job",
            "career",
        )
    ):
        add(InboxCategory.JOB_HUNTING, 0.92)
    if any(
        word in lowered
        for word in (
            "maimai",
            "オンゲキ",
            "チュウニズム",
            "音ゲー",
            "リザルト",
            "結果",
            "スコア",
            "自己ベスト",
            "進捗",
            "aj",
            "sss+",
        )
    ):
        add(InboxCategory.RHYTHM_PROGRESS, 0.92)

    if any(word in lowered for word in ("口座", "銀行", "入金", "振替", "残高", "引落")):
        add(InboxCategory.FINANCE_ACCOUNT, 0.9)
    if any(word in lowered for word in ("クレカ", "カード", "カード払い", "クレジット")):
        add(InboxCategory.FINANCE_CARD, 0.9)
    if any(
        word in lowered
        for word in ("レシート", "領収書", "支出", "家計", "会計", "買った", "利用額")
    ) or has_amount:
        add(InboxCategory.EXPENSES, 0.9 if has_amount else 0.84)

    if any(
        word in lowered
        for word in (
            "朝食",
            "昼食",
            "夕食",
            "朝ご飯",
            "昼ご飯",
            "夜ご飯",
            "ごはん",
            "食事",
            "食べた",
            "ラーメン",
            "自炊",
            "弁当",
            "meal",
            "breakfast",
            "lunch",
            "dinner",
        )
    ):
        add(InboxCategory.MEALS, 0.91)

    if any(
        phrase in lowered
        for phrase in (
            "外食",
            "レストラン",
            "飲食店",
            "店で食べ",
            "店での食事",
            "外で食べ",
            "外での食事",
            "食べに行",
            "食事に行",
            "テイクアウト",
            "持ち帰り",
            "持ち帰っ",
            "カフェ",
            "喫茶店",
        )
    ):
        add(InboxCategory.DINING_OUT, 0.94)

    if has_url:
        # A job-related URL is already represented above; a generic URL is a
        # saved link.  Keep both when the text contains an explicit job hint so
        # the user can correct an ambiguous local guess.
        add(InboxCategory.SAVED_LINK, 0.78)
    if any(word in lowered for word in ("神リンク", "保存リンク", "あとで読む", "参考リンク")):
        add(InboxCategory.SAVED_LINK, 0.84)
    if any(word in lowered for word in ("覚えて", "残して", "記録して", "メモして", "考え")):
        add(InboxCategory.MEMO, 0.79)

    if not scores:
        return (_candidate(InboxCategory.UNKNOWN, 1.0),)

    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0].value))
    best_score = ordered[0][1]
    if best_score < 0.65:
        return (_candidate(InboxCategory.UNKNOWN, 1.0),)

    selected = [item for item in ordered if item[1] >= 0.45 and item[1] >= best_score - 0.25]
    selected = selected[:3]
    return tuple(_candidate(category, confidence) for category, confidence in selected)


class InboxClassifier:
    """Closed-schema OpenAI classifier with deterministic local fallback."""

    provider = "openai-inbox-classifier"
    prompt_version = "inbox-classifier-3"

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "gpt-5.6-luna",
        *,
        enabled: bool = True,
        timeout_seconds: float = 8.0,
        max_image_bytes: int = 10 * 1024 * 1024,
        client: Any | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self.model = model
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds
        self.max_image_bytes = max_image_bytes
        self.client = client
        if self.client is None and self.enabled and api_key:
            try:
                from openai import OpenAI

                # max_retries=0 prevents an unavailable provider from holding
                # the Discord message for repeated network attempts.
                self.client = OpenAI(
                    api_key=api_key,
                    timeout=timeout_seconds,
                    max_retries=0,
                )
            except Exception:
                # Import/configuration failure is a normal local-fallback
                # condition. Never expose the key or provider exception text.
                self.client = None

    @staticmethod
    def local(text: str, *, attachment_count: int = 0) -> InboxClassification:
        return InboxClassification(
            candidates=_local_candidates(text, attachment_count=attachment_count),
            source="local",
        )

    def _image_block(self, image: InboxImageInput) -> dict[str, str]:
        content_type = image.content_type.casefold().split(";", 1)[0].strip()
        if content_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
            raise ValueError("unsupported classifier image type")
        size = image.path.stat().st_size
        if size <= 0 or size > self.max_image_bytes:
            raise ValueError("classifier image size is outside the configured limit")
        encoded = base64.b64encode(image.path.read_bytes()).decode("ascii")
        return {
            "type": "input_image",
            "image_url": f"data:{content_type};base64,{encoded}",
        }

    def _request_openai(
        self,
        text: str,
        *,
        attachment_count: int,
        images: Sequence[InboxImageInput],
    ) -> InboxClassifierOutput:
        if self.client is None:
            raise RuntimeError("classifier provider is unavailable")
        bounded_text = (text or "")[:4_000]
        response = self.client.responses.parse(
            model=self.model,
            store=False,
            timeout=self.timeout_seconds,
            max_output_tokens=1_600,
            input=[
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "あなたは個人用Discord Life Inboxの分類器です。"
                                "入力を次の閉じたカテゴリから最大3件、確信度の高い順に"
                                "選んでください: "
                                + ", ".join(category.value for category in InboxCategory)
                                + "。"
                                "支出・レシートはexpenses、"
                                "保存して後で読み返す短いメモはmemo、期限・提出・やることはtask、"
                                "独り言・ひとりごと・つぶやきなど、構造化メモやタスクとして"
                                "扱う必要のない気軽な考え・感情はself_talk、"
                                "日常の食事・料理・食べたものの記録はmeals、外食・レストラン・"
                                "カフェ・テイクアウトなどレビューしたい外出の食事はdining_out、"
                                "思い出・記念として残したい出来事はmemories、"
                                "X・Twitter・Instagramなど外部SNSへ投稿する下書きはsocial_draft、"
                                "買う物はshopping、求人・会社・応募はjob_hunting、"
                                "後で使う一般Webリンクはsaved_link、"
                                "ウニ・マイ・ゲキのいずれかと円単位の金額が明示された"
                                "音ゲー利用額はrhythm_spending、"
                                "maimai・オンゲキ・チュウニズムの進捗・結果はrhythm_progress、"
                                "口座・銀行・入金・振替はfinance_account、"
                                "クレカ・カード払いはfinance_cardです。"
                                "根拠が弱い場合はunknownだけを返してください。"
                                "URLへアクセスせず、画像データが入力されていない添付の内容は推測せず、"
                                "画像が入力されている場合は実際に読める内容だけを使ってください。"
                                "レシートはdocument_kind=receiptかつexpensesを第一候補、"
                                "食事写真はmeal_photo、音ゲーのリザルト画面はrhythm_resultにします。"
                                "手帳・ノート・予定表の画像はdocument_kind=plannerとし、"
                                "読み取れた独立した記録をplanner_itemsへ分割してください。"
                                "各planner itemは原文の意味を保った短いtextと"
                                "最大3件の候補だけを返し、"
                                "読めない文字、金額、期限、場所を推測しないでください。"
                                "通常文はdocument_kind=text、判定不能な画像はunknownです。"
                                "説明文や自由な補足を出さず、閉じたスキーマだけを返してください。"
                            ),
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                f"添付件数: {max(0, attachment_count)}\n"
                                f"入力本文:\n{bounded_text}"
                            ),
                        },
                        *(self._image_block(image) for image in tuple(images)[:4]),
                    ],
                },
            ],
            text_format=InboxClassifierOutput,
        )
        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            raise RuntimeError("classifier response was not parsed")
        return InboxClassifierOutput.model_validate(parsed)

    def classify(
        self,
        text: str,
        *,
        attachment_count: int = 0,
        images: Sequence[InboxImageInput] = (),
    ) -> InboxClassification:
        """Return OpenAI candidates or a local result without raising provider errors."""

        local = self.local(text, attachment_count=attachment_count)
        if not self.enabled or self.client is None:
            return local
        try:
            output = self._request_openai(
                text,
                attachment_count=attachment_count,
                images=images,
            )
        except Exception as exc:
            return InboxClassification(
                candidates=local.candidates,
                source="local_fallback",
                fallback_reason=type(exc).__name__,
            )
        return InboxClassification(
            candidates=tuple(output.candidates),
            source="openai",
            document_kind=output.document_kind,
            planner_items=tuple(output.planner_items),
        )


__all__ = [
    "CATEGORY_LABELS",
    "InboxCandidate",
    "InboxClassification",
    "InboxClassifier",
    "InboxClassifierOutput",
    "InboxCategory",
    "InboxDocumentKind",
    "InboxImageInput",
    "InboxPlannerItem",
]
