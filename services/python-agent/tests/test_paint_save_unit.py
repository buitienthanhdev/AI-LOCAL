"""Saving a Paint picture: the file is only 'saved' when it was read back and matches (SPEC_PAINT section 14)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from lga_worker.errors import ErrorCode, ToolError
from lga_worker.paint import saving


def picture(size=(200, 120)) -> Image.Image:
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    d.ellipse((20, 20, 120, 100), fill=(0, 150, 224), outline=(0, 0, 0))
    d.rectangle((130, 30, 180, 60), fill=(230, 0, 18))
    return img


def checks(report: saving.SavedReport) -> dict[str, bool]:
    return {c.name: c.passed for c in report.checks}


def test_a_correct_png_passes_every_check(tmp_path: Path) -> None:
    img = picture()
    path = tmp_path / "a.png"
    img.save(path)
    report = saving.verify_saved_file(path, img.size, img, started_at=time.time() - 1)
    assert report.ok, report.failed()
    assert {"file_exists", "not_empty", "opens_as_image", "format_matches_extension", "pixel_size", "content_matches_canvas"} <= set(checks(report))


def test_a_missing_empty_or_garbage_file_is_not_a_save(tmp_path: Path) -> None:
    assert not saving.verify_saved_file(tmp_path / "none.png", (200, 120)).ok
    empty = tmp_path / "e.png"
    empty.write_bytes(b"")
    r = saving.verify_saved_file(empty, (200, 120))
    assert not r.ok and not checks(r)["not_empty"]
    junk = tmp_path / "j.png"
    junk.write_bytes(b"this is not an image")
    r = saving.verify_saved_file(junk, (200, 120))
    assert not r.ok and checks(r)["opens_as_image"] is False


def test_wrong_size_wrong_format_and_wrong_content_are_caught(tmp_path: Path) -> None:
    img = picture()
    small = tmp_path / "small.png"
    img.resize((100, 60)).save(small)
    assert checks(saving.verify_saved_file(small, img.size, img))["pixel_size"] is False
    disguised = tmp_path / "x.png"
    img.save(disguised, format="JPEG")  # a JPEG with a .png name
    assert checks(saving.verify_saved_file(disguised, img.size, img))["format_matches_extension"] is False
    other = tmp_path / "other.png"
    Image.new("RGB", img.size, (10, 10, 10)).save(other)
    assert checks(saving.verify_saved_file(other, img.size, img))["content_matches_canvas"] is False


def test_jpeg_gets_a_lossy_tolerance(tmp_path: Path) -> None:
    img = picture()
    path = tmp_path / "a.jpg"
    img.save(path, quality=92)
    report = saving.verify_saved_file(path, img.size, img)
    assert report.ok, report.failed()


def test_a_stale_file_left_in_place_is_not_this_save(tmp_path: Path) -> None:
    img = picture()
    path = tmp_path / "old.png"
    img.save(path)
    old = time.time() - 3600
    os.utime(path, (old, old))
    report = saving.verify_saved_file(path, img.size, img, started_at=time.time())
    assert not report.ok and checks(report)["written_by_this_save"] is False


def test_only_picture_extensions_are_accepted() -> None:
    assert saving.format_of(Path("a.PNG")) == "PNG" and saving.format_of(Path("b.jpeg")) == "JPEG"
    for bad in ("a.docx", "noext", "a.exe"):
        with pytest.raises(ToolError) as err:
            saving.format_of(Path(bad))
        assert err.value.code == ErrorCode.INVALID_ARGUMENT


def test_overwriting_makes_a_backup_first(tmp_path: Path) -> None:
    path = tmp_path / "keep.png"
    assert saving.backup_existing(path) is None
    picture().save(path)
    backup = saving.backup_existing(path)
    assert backup is not None and backup.is_file() and backup != path
    assert backup.read_bytes() == path.read_bytes()
