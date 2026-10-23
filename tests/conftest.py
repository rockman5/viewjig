from __future__ import annotations

from pathlib import Path

import pytest
from fakes import FakeSolidWorks, FakeVariant


@pytest.fixture(autouse=True)
def no_live_solidworks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that would attach to a real running SOLIDWORKS."""
    try:
        import pythoncom
    except ImportError:  # pywin32 absent: nothing could attach anyway
        return

    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to attach to a live SOLIDWORKS")

    monkeypatch.setattr(pythoncom, "GetActiveObject", refuse)


@pytest.fixture(autouse=True)
def offline_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the pywin32 VARIANT builders so no test needs pywin32 or SOLIDWORKS."""
    import viewjig.camera
    import viewjig.sw

    monkeypatch.setattr(viewjig.sw, "byref_long", lambda: FakeVariant(0))
    monkeypatch.setattr(viewjig.camera, "double_array", lambda values: FakeVariant(list(values)))


@pytest.fixture
def part(tmp_path: Path) -> Path:
    path = tmp_path / "vj-f01.SLDPRT"
    path.write_bytes(b"not a real part")
    return path


@pytest.fixture
def solidworks(part: Path) -> FakeSolidWorks:
    return FakeSolidWorks(part)
