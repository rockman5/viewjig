"""VJ-CAPTURE-01: characterize native SOLIDWORKS raster capture.

This is intentionally a probe, not production ViewJig code. It records what
SOLIDWORKS actually does for a standard view + zoom-to-fit + fixed-size BMP.

Run on Windows with SOLIDWORKS open and a neutral part active:

    python probes/capture_01.py --out capture-01

The probe does not save or modify the model. It writes BMP captures plus a
JSON report beside them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pythoncom
import win32com.client
from PIL import Image


# Public SOLIDWORKS API enum value for an isometric standard view.
SW_ISOMETRIC_VIEW = 7


@dataclass(frozen=True)
class CaptureResult:
    requested_width: int
    requested_height: int
    actual_width: int
    actual_height: int
    mode: str
    sha256: str
    file: str


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _capture(model: Any, path: Path, width: int, height: int) -> CaptureResult:
    # IModelDocExtension.SaveBMP writes the graphics area at requested pixels.
    ok = bool(model.Extension.SaveBMP(str(path), width, height))
    if not ok or not path.exists():
        raise RuntimeError(f"SOLIDWORKS did not create capture: {path}")

    with Image.open(path) as image:
        actual_width, actual_height = image.size
        mode = image.mode

    return CaptureResult(
        requested_width=width,
        requested_height=height,
        actual_width=actual_width,
        actual_height=actual_height,
        mode=mode,
        sha256=_digest(path),
        file=path.name,
    )


def run(out_dir: Path) -> dict[str, Any]:
    pythoncom.CoInitialize()
    try:
        sw = win32com.client.Dispatch("SldWorks.Application")
        model = sw.ActiveDoc
        if model is None:
            raise RuntimeError("Open a neutral SOLIDWORKS part before running the probe.")

        out_dir.mkdir(parents=True, exist_ok=True)

        title = str(model.GetTitle())
        path_name = str(model.GetPathName() or "")
        was_dirty = int(model.GetSaveFlag())

        # Establish one deliberately ordinary camera state using only public API.
        model.ShowNamedView2("", SW_ISOMETRIC_VIEW)
        model.ViewZoomtofit2()
        model.GraphicsRedraw2()

        sizes = [
            (800, 600),
            (1200, 900),
            (1000, 1000),
            (1200, 675),
        ]
        captures: list[CaptureResult] = []
        for width, height in sizes:
            filename = f"iso_fit_{width}x{height}.bmp"
            captures.append(_capture(model, out_dir / filename, width, height))

        after_dirty = int(model.GetSaveFlag())
        report = {
            "probe": "VJ-CAPTURE-01",
            "purpose": "Characterize fixed-size raster capture after isometric + zoom-to-fit.",
            "document": {
                "title": title,
                "path": path_name,
                "dirty_before": was_dirty,
                "dirty_after": after_dirty,
            },
            "sequence": [
                "ShowNamedView2('', isometric)",
                "ViewZoomtofit2()",
                "GraphicsRedraw2()",
                "IModelDocExtension.SaveBMP(path, width, height)",
            ],
            "captures": [asdict(item) for item in captures],
        }

        report_path = out_dir / "report.json"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        return report
    finally:
        pythoncom.CoUninitialize()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("capture-01"))
    args = parser.parse_args()

    report = run(args.out)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
