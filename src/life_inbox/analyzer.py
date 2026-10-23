"""Mockable analyzer protocols and current OpenAI Responses API adapter."""

from __future__ import annotations

import base64
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol

from .constants import DEFAULT_CATEGORIES, FOOD_SUBCATEGORIES
from .schemas import (
    InboxRouteResult,
    MealAnalysisResult,
    ReceiptAnalysisResult,
    TextAnalysisResult,
)


class AnalyzerError(RuntimeError):
    """An analyzer could not produce a safe structured result."""


class AnalyzerUnavailable(AnalyzerError):
    """No configured provider is available for an operation."""


class AnalyzerProtocol(Protocol):
    def route_inbox(self, text: str) -> InboxRouteResult: ...

    def analyze_text(self, text: str) -> TextAnalysisResult: ...

    def analyze_receipt(
        self, path: str | Path, content_type: str, *, context_text: str = ""
    ) -> ReceiptAnalysisResult: ...

    def analyze_meal(
        self, path: str | Path, content_type: str, *, context_text: str = ""
    ) -> MealAnalysisResult: ...


class OpenAIAnalyzer:
    """OpenAI Responses API adapter.

    The client is injected in tests. Image bytes exist only while constructing
    the request data URL and are never included in an ``AIRun`` record.
    """

    provider = "openai"
    prompt_version = "mvp-2"
    routing_prompt_version = "inbox-router-2"

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-5.6-luna",
        *,
        client: Any | None = None,
        max_image_bytes: int = 10 * 1024 * 1024,
        categories: Iterable[str] = DEFAULT_CATEGORIES,
        food_subcategories: Iterable[str] = FOOD_SUBCATEGORIES,
    ) -> None:
        self.model = model
        self.max_image_bytes = max_image_bytes
        self.categories = tuple(categories)
        self.food_subcategories = tuple(food_subcategories)
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:  # pragma: no cover - dependency install issue
                raise AnalyzerUnavailable("openai package is not installed") from exc
            client = OpenAI(api_key=api_key)
        self.client = client

    def _request_model(
        self,
        *,
        schema_model: type,
        system_prompt: str,
        user_blocks: list[dict[str, Any]],
    ) -> Any:
        try:
            response = self.client.responses.parse(
                model=self.model,
                store=False,
                input=[
                    {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
                    {"role": "user", "content": user_blocks},
                ],
                text_format=schema_model,
            )
            parsed = getattr(response, "output_parsed", None)
            if parsed is None:
                raise AnalyzerError("OpenAI response did not contain parsed structured output")
            return schema_model.model_validate(parsed)
        except AnalyzerError:
            raise
        except Exception as exc:
            # Do not include request contents, credentials, or image data in
            # the exception text recorded by the service.
            raise AnalyzerError(f"OpenAI Responses request failed: {type(exc).__name__}") from exc

    @staticmethod
    def _image_block(path: str | Path, content_type: str, max_image_bytes: int) -> dict[str, str]:
        allowed_types = {"image/jpeg", "image/png", "image/webp", "image/gif"}
        if content_type.lower() not in allowed_types:
            raise AnalyzerError(f"unsupported image MIME type: {content_type}")
        image_path = Path(path)
        try:
            size = image_path.stat().st_size
        except OSError as exc:
            raise AnalyzerError("image file is not readable") from exc
        if size <= 0 or size > max_image_bytes:
            raise AnalyzerError("image file size is outside the configured limit")
        try:
            encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        except OSError as exc:
            raise AnalyzerError("image file could not be read") from exc
        return {"type": "input_image", "image_url": f"data:{content_type};base64,{encoded}"}

    def analyze_text(self, text: str) -> TextAnalysisResult:
        return self._request_model(
            schema_model=TextAnalysisResult,
            system_prompt=(
                "あなたは個人の生活Inboxを整理する補助です。入力原文を慎重に解析し、"
                "不明な値はnullまたはunknownにしてください。金額を推測・捏造せず、"
                "イベント種別はexpense/mealのうち根拠があるものだけ返してください。"
                f"categoryは次から選んでください: {', '.join(self.categories)}。"
                "categoryが食費の場合のsubcategoryは次から選んでください: "
                f"{', '.join(self.food_subcategories)}。"
            ),
            user_blocks=[{"type": "input_text", "text": text}],
        )

    def route_inbox(self, text: str) -> InboxRouteResult:
        """Classify text only; raw attachments stay local until domain analysis."""

        return self._request_model(
            schema_model=InboxRouteResult,
            system_prompt=(
                "あなたは個人用Life Inboxの安全な振り分け担当です。入力を次の1つへ分類して"
                "ください: finance_or_meal, memo, rhythm_todo, rhythm_result, life_task, "
                "career, unknown。"
                "支出・レシート・食事はfinance_or_mealです。保存したい考え・記録・文章はmemo、"
                "maimai・オンゲキ・チュウニズムで今後やる課題はrhythm_todo、プレイ済みの"
                "成果はrhythm_resultです。一般生活のやること・提出物・予定はlife_taskです。"
                "会社、求人、説明会・就活イベント、応募記録はcareerです。careerでは"
                "career_companyとcareer_titleを原文から安全に抽出し、kindはcompany/job/event/application"
                "のいずれか、応募ならstageもsaved/interested/applied/screening/interview/"
                "final_interview/offer/accepted/rejected/withdrawn/closedから選んでください。"
                "statusはopen/interested/applied/in_progress/accepted/closed/rejected/withdrawn/"
                "archivedから選んでください。場所、次の行動、メモ、URLも明示された場合だけ返します。"
                "確信がなければunknownにしてください。ゲーム名は本文に明示された場合だけ"
                "chunithm/maimai/ongekiを返し、推測せずnullにしてください。memoのcontentは"
                "意味のある本文を省略せず保持してください。音ゲーcontentは接頭辞を除いた簡潔な"
                "内容にしてください。life_taskではtask_titleを255文字以内で保持し、期限は"
                "ユーザーが西暦を含む明確な日付を示した場合だけtask_due_dateへYYYY-MM-DDで、"
                "時刻も明確な場合だけtask_due_timeへ入れてください。『明日』『来週』『午後』"
                "など相対的・曖昧な日時を日付へ変換してはいけません。その場合も安全なタイトルを"
                "保持し、期限をnullにしてください。careerのdeadline_dateとnext_action_dateも"
                "西暦を含む明確な日付だけ、時刻は同じ日付と明示された時だけ返してください。"
                "URLはhttp/httpsのみで、ユーザー名・パスワードを含めないでください。"
                "finance_or_mealとunknownではcontentをnull、career以外ではcareer fieldsを"
                "すべてnull、"
                "life_task以外ではtask fieldsをすべてnullにしてください。"
            ),
            user_blocks=[{"type": "input_text", "text": text}],
        )

    def _analyze_image(
        self,
        *,
        path: str | Path,
        content_type: str,
        context_text: str,
        schema_model: type,
        system_prompt: str,
    ) -> Any:
        blocks: list[dict[str, Any]] = []
        if context_text:
            blocks.append({"type": "input_text", "text": context_text})
        blocks.append(self._image_block(path, content_type, self.max_image_bytes))
        return self._request_model(
            schema_model=schema_model,
            system_prompt=system_prompt,
            user_blocks=blocks,
        )

    def analyze_receipt(
        self, path: str | Path, content_type: str, *, context_text: str = ""
    ) -> ReceiptAnalysisResult:
        return self._analyze_image(
            path=path,
            content_type=content_type,
            context_text=context_text,
            schema_model=ReceiptAnalysisResult,
            system_prompt=(
                "レシート画像を解析してください。支払総額totalの正確性を最優先し、"
                "画像で読めない値は必ずnullにしてください。商品合計、税、値引き、"
                "ポイント等が支払額と異なる場合があります。推測で補完しないでください。"
                "itemsには購入した商品・サービスの明細だけを印字順に一行ずつ返し、"
                "小計、税、合計、支払、釣銭、値引き見出しは商品として含めないでください。"
                "quantity、unit_price、amountは画像に明記されている場合だけ設定してください。"
                "商品名の一部しか読めない場合は、読めた文字だけをnameに入れてください。"
            ),
        )

    def analyze_meal(
        self, path: str | Path, content_type: str, *, context_text: str = ""
    ) -> MealAnalysisResult:
        return self._analyze_image(
            path=path,
            content_type=content_type,
            context_text=context_text,
            schema_model=MealAnalysisResult,
            system_prompt=(
                "食事画像を解析してください。料理や食事の簡潔な説明、食事区分、"
                "店内/自宅程度を返してください。カロリー、重量、栄養を画像だけから"
                "正確に断定してはいけません。不明な値はunknownまたはnullにしてください。"
            ),
        )


class NullAnalyzer:
    """Local-only fallback that keeps events pending without network access."""

    def _unavailable(self) -> None:
        raise AnalyzerUnavailable("AI analyzer is not configured")

    def analyze_text(self, text: str) -> TextAnalysisResult:
        self._unavailable()
        raise AssertionError  # pragma: no cover

    def route_inbox(self, text: str) -> InboxRouteResult:
        self._unavailable()
        raise AssertionError  # pragma: no cover

    def analyze_receipt(
        self, path: str | Path, content_type: str, *, context_text: str = ""
    ) -> ReceiptAnalysisResult:
        self._unavailable()
        raise AssertionError  # pragma: no cover

    def analyze_meal(
        self, path: str | Path, content_type: str, *, context_text: str = ""
    ) -> MealAnalysisResult:
        self._unavailable()
        raise AssertionError  # pragma: no cover
