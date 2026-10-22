from __future__ import annotations

from pathlib import Path

import pytest
from fakes import FakeSolidWorks

from viewjig.raster import RasterCaptureError, capture
from viewjig.sw import Com, open_document


def capture_with(solidworks: FakeSolidWorks, *args, **kwargs):
    with open_document(Com(solidworks.app), solidworks.path) as document:
        return capture(document, *args, **kwargs)


def test_capture_returns_an_rgb_image_of_the_requested_size(solidworks, tmp_path: Path) -> None:
    image = capture_with(solidworks, 1001, 751, workdir=tmp_path)
    assert image.size == (1001, 751)
    assert image.mode == "RGB"
    assert image.getpixel((0, 0)) == (255, 255, 255)


def test_savebmp_gets_a_bmp_path_and_the_exact_dimensions(solidworks, tmp_path: Path) -> None:
    capture_with(solidworks, 1600, 1200, workdir=tmp_path)
    calls = [args for who, name, _, args in solidworks.log if name == "SaveBMP"]
    assert len(calls) == 1
    path, width, height = calls[0]
    assert path.endswith(".bmp") and Path(path).parent == tmp_path
    assert (width, height) == (1600, 1200)


def test_temporary_bmp_is_removed_after_success(solidworks, tmp_path: Path) -> None:
    capture_with(solidworks, 800, 600, workdir=tmp_path)
    assert list(tmp_path.glob("*.bmp")) == []


def test_image_stays_usable_after_the_file_is_gone(solidworks, tmp_path: Path) -> None:
    image = capture_with(solidworks, 64, 48, workdir=tmp_path)
    assert image.copy().getpixel((63, 47)) == (255, 255, 255)


def test_savebmp_returning_false_raises_and_cleans_up(solidworks, tmp_path: Path) -> None:
    solidworks.save_bmp = lambda path, w, h: False
    with pytest.raises(RasterCaptureError, match="did not write"):
        capture_with(solidworks, 800, 600, workdir=tmp_path)
    assert list(tmp_path.glob("*.bmp")) == []


def test_true_without_a_file_is_still_a_failure(solidworks, tmp_path: Path) -> None:
    solidworks.save_bmp = lambda path, w, h: True
    with pytest.raises(RasterCaptureError, match="did not write"):
        capture_with(solidworks, 800, 600, workdir=tmp_path)


def test_partial_file_with_false_is_removed(solidworks, tmp_path: Path) -> None:
    def partial(path, w, h):
        Path(path).write_bytes(b"BM")
        return False

    solidworks.save_bmp = partial
    with pytest.raises(RasterCaptureError):
        capture_with(solidworks, 800, 600, workdir=tmp_path)
    assert list(tmp_path.glob("*.bmp")) == []


def test_wrong_output_size_raises_and_cleans_up(solidworks, tmp_path: Path) -> None:
    solidworks.bmp_size_override = (799, 600)
    with pytest.raises(RasterCaptureError, match="returned 799x600, requested 800x600"):
        capture_with(solidworks, 800, 600, workdir=tmp_path)
    assert list(tmp_path.glob("*.bmp")) == []


def test_unreadable_file_raises_and_cleans_up(solidworks, tmp_path: Path) -> None:
    def garbage(path, w, h):
        Path(path).write_bytes(b"not an image")
        return True

    solidworks.save_bmp = garbage
    with pytest.raises(RasterCaptureError, match="unreadable"):
        capture_with(solidworks, 800, 600, workdir=tmp_path)
    assert list(tmp_path.glob("*.bmp")) == []


@pytest.mark.parametrize("width, height", [(0, 0), (0, 600), (800, -1), (800.0, 600), (True, 600)])
def test_dimensions_must_be_explicit_positive_integers(solidworks, width, height) -> None:
    with pytest.raises(ValueError):
        capture_with(solidworks, width, height)
    assert "SaveBMP" not in solidworks.calls()
