from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from life_inbox.rhythm_chunirec_image_live import (
    BEST_IMAGE_SELECTOR,
    DEFAULT_CHUNIREC_BASE_URL,
    UNKNOWN_CONSTANT_ACKNOWLEDGEMENT,
    UNKNOWN_CONSTANT_NOTICE,
    CHUNIRECImageConfig,
    CHUNIRECImageFailureReason,
    CHUNIRECImageState,
    PlaywrightChunirecImageCollector,
    decode_png_bytes,
    decode_png_data_uri,
)

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
PNG_DATA_URI = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")


class FakeResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status


class FakeTimeout(Exception):
    pass


class FakeLocator:
    def __init__(
        self,
        page: FakePage,
        *,
        count: int = 1,
        visible: bool = True,
        action: str | None = None,
    ) -> None:
        self.page = page
        self._count = count
        self._visible = visible
        self.action = action

    def count(self) -> int:
        return self._count

    def nth(self, _index: int) -> FakeLocator:
        return self

    def is_visible(self) -> bool:
        return self._visible

    def click(self, **_kwargs: object) -> None:
        if self.action == "best_image":
            self.page.button_clicked = True
        elif self.action == "notice":
            self.page.notice_acknowledged = True

    def wait_for(self, *, state: str, timeout: int) -> None:
        assert state == "hidden"
        assert timeout == 1_000
        assert self.page.notice_acknowledged is True

    def text_content(self) -> str:
        return json.dumps(
            {
                "player_profile": {
                    "pn": self.page.profile_name,
                    "rt": self.page.profile_rating,
                }
            }
        )


class FakePage:
    def __init__(
        self,
        expected_url: str,
        *,
        artifact: object = None,
        notice: bool = False,
        dom_ok: bool = True,
        response_status: int = 200,
        wait_timeout: bool = False,
        profile_name: str = "Example Player",
        profile_rating: str = "16.72",
    ) -> None:
        self.url = expected_url
        self.artifact = artifact
        self.notice = notice
        self.notice_acknowledged = False
        self.dom_ok = dom_ok
        self.response_status = response_status
        self.wait_timeout = wait_timeout
        self.profile_name = profile_name
        self.profile_rating = profile_rating
        self.button_clicked = False
        self.wait_calls = 0
        self.evaluate_calls: list[str] = []
        self.goto_calls: list[tuple[str, dict[str, object]]] = []

    def goto(self, url: str, **kwargs: object) -> FakeResponse:
        self.goto_calls.append((url, kwargs))
        if self.response_status == -1:
            raise TimeoutError("simulated network timeout")
        return FakeResponse(self.response_status)

    def locator(self, selector: str) -> FakeLocator:
        if selector == BEST_IMAGE_SELECTOR:
            return FakeLocator(
                self,
                count=1 if self.dom_ok else 0,
                action="best_image",
            )
        return FakeLocator(self, count=1 if self.dom_ok else 0)

    def get_by_text(self, text: str, *, exact: bool) -> FakeLocator:
        assert exact is True
        return FakeLocator(
            self,
            count=1
            if self.notice
            and self.button_clicked
            and not self.notice_acknowledged
            and text == UNKNOWN_CONSTANT_NOTICE
            else 0,
        )

    def get_by_role(self, role: str, *, name: str, exact: bool) -> FakeLocator:
        assert role == "button"
        assert exact is True
        return FakeLocator(
            self,
            count=(
                1
                if self.notice
                and not self.notice_acknowledged
                and name == UNKNOWN_CONSTANT_ACKNOWLEDGEMENT
                else 0
            ),
            action="notice",
        )

    def evaluate(self, script: str) -> object:
        self.evaluate_calls.append(script)
        if "HTMLCanvasElement" in script or "capture.armed" in script:
            return None
        return self.artifact

    def wait_for_function(self, _script: str, **_kwargs: object) -> str:
        self.wait_calls += 1
        if self.wait_timeout:
            raise FakeTimeout("simulated generator timeout")
        if self.notice and not self.notice_acknowledged and self.wait_calls == 1:
            return "notice"
        return "generated_image"


class FakeBrowser:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.closed = False

    def new_page(self) -> FakePage:
        return self.page

    def close(self) -> None:
        self.closed = True


class FakePlaywright:
    def __init__(self, browser: FakeBrowser) -> None:
        self.chromium = self
        self.browser = browser

    def __enter__(self) -> FakePlaywright:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def launch(self, **kwargs: object) -> FakeBrowser:
        assert kwargs == {"headless": True}
        return self.browser


def collector_for(
    tmp_path: Path,
    **kwargs: object,
) -> tuple[PlaywrightChunirecImageCollector, FakePage]:
    config = CHUNIRECImageConfig(
        user_name="example-user",
        output_path=tmp_path / "chunirec-best.png",
        timeout_seconds=1,
    )
    page = FakePage(config.user_url, **kwargs)
    return PlaywrightChunirecImageCollector(config), page


def test_config_builds_one_encoded_official_user_url_and_rejects_path_injection(
    tmp_path: Path,
) -> None:
    config = CHUNIRECImageConfig(user_name="user_01", output_path=tmp_path / "out.png")

    assert config.base_url == DEFAULT_CHUNIREC_BASE_URL
    assert config.user_url == "https://chunirec.net/users/user_01"

    for invalid in ("../other", "user/name", "user?x=1", "https://other.example/user"):
        with pytest.raises(ValueError, match="safe path segment"):
            CHUNIRECImageConfig(user_name=invalid, output_path=tmp_path / "out.png")


def test_expected_api_identity_is_normalized_and_mismatch_is_rejected(tmp_path: Path) -> None:
    config = CHUNIRECImageConfig(
        user_name="example-user",
        output_path=tmp_path / "out.png",
        expected_player_name="Example Player",
        expected_rating="16.72",
        timeout_seconds=1,
    )
    matching = FakePage(
        config.user_url,
        artifact={"kind": "generated_image", "data_url": PNG_DATA_URI},
        profile_name="ＥＸＡＭＰＬＥ　ＰＬＡＹＥＲ",
    )

    assert PlaywrightChunirecImageCollector(config).collect_page(matching).success is True

    mismatch = FakePage(
        config.user_url,
        artifact={"kind": "generated_image", "data_url": PNG_DATA_URI},
        profile_rating="16.71",
    )
    result = PlaywrightChunirecImageCollector(config).collect_page(mismatch)

    assert result.success is False
    assert result.failure_reason is CHUNIRECImageFailureReason.DOM_MISMATCH


def test_success_requires_explicit_generated_artifact_and_writes_atomically(tmp_path: Path) -> None:
    collector, page = collector_for(
        tmp_path,
        artifact={"kind": "canvas_blob", "data_url": PNG_DATA_URI},
    )

    result = collector.collect_page(
        page,
        collected_at=datetime(2026, 8, 30, 1, 2, 3, tzinfo=UTC),
    )

    output = tmp_path / "chunirec-best.png"
    assert result.success is True
    assert result.state is CHUNIRECImageState.SUCCESS
    assert result.failure_reason is None
    assert result.image is not None
    assert result.image.width == 1
    assert result.image.height == 1
    assert result.image.byte_size == len(PNG_BYTES)
    assert output.read_bytes() == PNG_BYTES
    assert page.button_clicked is True
    assert page.goto_calls[0][0] == "https://chunirec.net/users/example-user"


def test_identical_existing_image_is_reported_stale_without_replacing_output(
    tmp_path: Path,
) -> None:
    output = tmp_path / "chunirec-best.png"
    output.write_bytes(PNG_BYTES)
    collector, page = collector_for(
        tmp_path,
        artifact={"kind": "generated_image", "data_url": PNG_DATA_URI},
    )

    result = collector.collect_page(page)

    assert result.success is False
    assert result.state is CHUNIRECImageState.STALE_IMAGE
    assert result.failure_reason is CHUNIRECImageFailureReason.STALE_IMAGE
    assert output.read_bytes() == PNG_BYTES


def test_final_url_mismatch_is_dom_mismatch_not_missing_network_page(tmp_path: Path) -> None:
    collector, page = collector_for(
        tmp_path,
        artifact={"kind": "generated_image", "data_url": PNG_DATA_URI},
    )
    page.url = "https://chunirec.net/users/another-user"

    result = collector.collect_page(page)

    assert result.success is False
    assert result.state is CHUNIRECImageState.DOM_MISMATCH
    assert result.failure_reason is CHUNIRECImageFailureReason.DOM_MISMATCH


def test_collect_uses_injected_browser_factory_without_importing_or_persisting_session(
    tmp_path: Path,
) -> None:
    collector, page = collector_for(
        tmp_path,
        artifact={"kind": "canvas_blob", "data_url": PNG_DATA_URI},
    )
    browser = FakeBrowser(page)
    collector = PlaywrightChunirecImageCollector(
        collector.config,
        playwright_factory=lambda: FakePlaywright(browser),
    )

    result = collector.collect()

    assert result.success is True
    assert browser.closed is True


def test_unknown_constant_notice_is_acknowledged_only_by_exact_button(tmp_path: Path) -> None:
    collector, page = collector_for(
        tmp_path,
        notice=True,
        artifact={"kind": "generated_image", "data_url": PNG_DATA_URI},
    )

    result = collector.collect_page(page)

    assert result.success is True
    assert page.notice_acknowledged is True
    assert page.wait_calls == 2


def test_dom_mismatch_is_a_failure_and_does_not_write_output(tmp_path: Path) -> None:
    collector, page = collector_for(
        tmp_path,
        dom_ok=False,
        artifact={"data_url": PNG_DATA_URI},
    )

    result = collector.collect_page(page)

    assert result.success is False
    assert result.state is CHUNIRECImageState.DOM_MISMATCH
    assert result.failure_reason is CHUNIRECImageFailureReason.DOM_MISMATCH
    assert not (tmp_path / "chunirec-best.png").exists()


def test_elapsed_timeout_cannot_be_reported_as_success(tmp_path: Path) -> None:
    collector, page = collector_for(
        tmp_path,
        wait_timeout=True,
        artifact={"data_url": PNG_DATA_URI},
    )

    result = collector.collect_page(page)

    assert result.success is False
    assert result.state is CHUNIRECImageState.GENERATOR_NOT_COMPLETED
    assert result.failure_reason is CHUNIRECImageFailureReason.GENERATOR_NOT_COMPLETED
    assert result.image is None
    assert not (tmp_path / "chunirec-best.png").exists()


def test_invalid_png_is_rejected_after_generator_event(tmp_path: Path) -> None:
    collector, page = collector_for(
        tmp_path,
        artifact={
            "kind": "generated_image",
            "data_url": "data:image/png;base64," + base64.b64encode(b"not-a-png").decode("ascii"),
        },
    )

    result = collector.collect_page(page)

    assert result.success is False
    assert result.state is CHUNIRECImageState.INVALID_IMAGE
    assert result.failure_reason is CHUNIRECImageFailureReason.INVALID_IMAGE
    assert not (tmp_path / "chunirec-best.png").exists()


def test_missing_public_page_and_network_timeout_have_distinct_safe_reasons(
    tmp_path: Path,
) -> None:
    missing_collector, missing_page = collector_for(tmp_path, response_status=404)
    missing = missing_collector.collect_page(missing_page)
    assert missing.failure_reason is CHUNIRECImageFailureReason.PUBLIC_PAGE_MISSING
    assert "https://" not in (missing.message or "")

    timeout_collector, timeout_page = collector_for(tmp_path, response_status=-1)
    timeout = timeout_collector.collect_page(timeout_page)
    assert timeout.failure_reason is CHUNIRECImageFailureReason.NETWORK_TIMEOUT


def test_png_helpers_require_actual_decodable_idat_data() -> None:
    assert decode_png_bytes(PNG_BYTES)[1:] == (1, 1)
    assert decode_png_data_uri(PNG_DATA_URI)[0] == PNG_BYTES

    with pytest.raises(ValueError):
        decode_png_bytes(b"\x89PNG\r\n\x1a\n")
