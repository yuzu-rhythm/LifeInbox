from __future__ import annotations

import base64
import hashlib

import pytest

from life_inbox.rhythm_images import (
    RatingImageVerificationError,
    jpeg_dimensions,
    png_dimensions,
    verify_rating_image,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\nfixture"
COMPLETE_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
JPEG_BYTES = bytes.fromhex(
    "ffd8ffc0000b080001000101011100ffda0008010100003f0000ffd9"
)


def test_png_dimensions_validates_complete_structure_and_expected_dimensions():
    assert png_dimensions(COMPLETE_PNG_BYTES, require_complete=True) == (1, 1)

    with pytest.raises(RatingImageVerificationError):
        png_dimensions(PNG_BYTES, require_complete=True)

    with pytest.raises(RatingImageVerificationError):
        png_dimensions(COMPLETE_PNG_BYTES[:-1], require_complete=True)


def test_rating_image_verification_accepts_only_matching_png_under_root(tmp_path):
    root = tmp_path / "images"
    root.mkdir()
    image = root / "rating.png"
    image.write_bytes(PNG_BYTES)

    verified = verify_rating_image(
        image_root=root,
        image_path=image,
        expected_sha256=hashlib.sha256(PNG_BYTES).hexdigest(),
        expected_byte_size=len(PNG_BYTES),
    )

    assert verified == image.resolve()


def test_rating_image_verification_checks_png_dimensions_when_metadata_is_present(tmp_path):
    root = tmp_path / "images"
    root.mkdir()
    image = root / "rating.png"
    image.write_bytes(COMPLETE_PNG_BYTES)

    verified = verify_rating_image(
        image_root=root,
        image_path=image,
        expected_sha256=hashlib.sha256(COMPLETE_PNG_BYTES).hexdigest(),
        expected_byte_size=len(COMPLETE_PNG_BYTES),
        expected_width=1,
        expected_height=1,
        require_complete_png=True,
    )
    assert verified == image.resolve()

    with pytest.raises(RatingImageVerificationError, match="width"):
        verify_rating_image(
            image_root=root,
            image_path=image,
            expected_width=2,
            require_complete_png=True,
        )


def test_rating_image_verification_accepts_complete_jpeg_under_root(tmp_path):
    root = tmp_path / "images"
    root.mkdir()
    image = root / "rating.jpg"
    image.write_bytes(JPEG_BYTES)

    assert jpeg_dimensions(JPEG_BYTES) == (1, 1)
    verified = verify_rating_image(
        image_root=root,
        image_path=image,
        expected_sha256=hashlib.sha256(JPEG_BYTES).hexdigest(),
        expected_byte_size=len(JPEG_BYTES),
        expected_width=1,
        expected_height=1,
    )

    assert verified == image.resolve()


@pytest.mark.parametrize("mismatch", ["hash", "size", "signature"])
def test_rating_image_verification_rejects_metadata_or_content_mismatch(
    tmp_path,
    mismatch,
):
    root = tmp_path / "images"
    root.mkdir()
    image = root / "rating.png"
    image.write_bytes(b"not-png" if mismatch == "signature" else PNG_BYTES)
    expected_hash = (
        "0" * 64 if mismatch == "hash" else hashlib.sha256(image.read_bytes()).hexdigest()
    )
    expected_size = len(image.read_bytes()) + (1 if mismatch == "size" else 0)

    with pytest.raises(RatingImageVerificationError):
        verify_rating_image(
            image_root=root,
            image_path=image,
            expected_sha256=expected_hash,
            expected_byte_size=expected_size,
        )


def test_rating_image_verification_rejects_outside_and_symlink_escape(tmp_path):
    root = tmp_path / "images"
    root.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(PNG_BYTES)
    link = root / "linked.png"
    link.symlink_to(outside)

    for candidate in (outside, link):
        with pytest.raises(RatingImageVerificationError):
            verify_rating_image(
                image_root=root,
                image_path=candidate,
                expected_sha256=hashlib.sha256(PNG_BYTES).hexdigest(),
                expected_byte_size=len(PNG_BYTES),
            )
