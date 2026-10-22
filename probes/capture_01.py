"""VJ-CAPTURE-01: characterize native SOLIDWORKS view + fixed-size raster capture.

This is an experimental probe, not production ViewJig code. It observes what
SOLIDWORKS actually does and records it; it does not try to correct anything.

Questions it is designed to answer:

  A. Fixed sizes   Does SaveBMP produce exactly the requested raster? How do
                   requested size and aspect ratio change where the model
                   lands (foreground bounds, fill, centering)? Does capture
                   change the camera? Are back-to-back captures identical?
  B. Start views   Does standard view + Zoom to Fit reach the same camera and
                   the same pixels regardless of the starting view?
  C. Camera state  Can Orientation3 / Translation3 / Scale2 be saved, the view
                   disturbed, and the state restored to reproduce the pixels?
  D. Viewport      (across runs) Does any of the above depend on the
                   SOLIDWORKS window size? Run with different --tag values.

Safety: the probe attaches to an already running SOLIDWORKS (it never launches
one), opens the given model read-only unless it is already open, touches only
that document, and never saves. It refuses to run if the model is already open,
so every run starts from a fresh read-only document, which it closes afterwards.

COM binding: pywin32's dynamic dispatch decides from type information whether a
name is a property or a method, and invokes unknown names on attribute access.
SOLIDWORKS document objects do not describe every documented member that way
(live run: GetPathName came back as a str before it could be called). The probe
therefore never uses attribute access on SOLIDWORKS objects; it goes through
Com, which invokes each member explicitly as the method or property the public
API documents.

Usage (Windows, SOLIDWORKS running):

    .venv\\Scripts\\python probes\\capture_01.py fixtures\\vj-f01.SLDPRT --tag max

Output goes to captures/capture-01/<tag>/ (git-ignored): report.json plus a
lossless PNG of every capture.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import struct
import sys
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pythoncom
import pywintypes
import win32com.client
from PIL import Image, ImageChops

# Public SOLIDWORKS API enum values.
VIEW_FRONT = 1  # swStandardViews_e
VIEW_TOP = 5
VIEW_ISOMETRIC = 7
VIEW_TRIMETRIC = 8
DOC_TYPES = {".sldprt": 1, ".sldasm": 2}  # swDocumentTypes_e
OPEN_SILENT = 1  # swOpenDocOptions_e
OPEN_READ_ONLY = 2
DONT_REBUILD = 1  # swRebuildOnActivation_e

# (0, 0) asks SaveBMP for the current window size. The rest vary scale at a fixed
# aspect (800x600 vs 1600x1200), aspect ratio, odd dimensions, and an oversize request.
SIZES = [
    (0, 0),
    (800, 600),
    (1600, 1200),
    (1000, 1000),
    (1600, 900),
    (600, 1000),
    (1001, 751),
    (3840, 2160),
]
REFERENCE_SIZE = (1600, 1200)

# Orders in which the saved camera properties are assigned back to the view.
RESTORE_ORDERS = [
    ("Orientation3", "Translation3", "Scale2"),
    ("Orientation3", "Scale2", "Translation3"),
    ("Scale2", "Orientation3", "Translation3"),
]

# A pixel is foreground when its largest channel difference from the background
# exceeds the threshold. Several thresholds are reported so the real one can be
# chosen from evidence (anti-aliasing, edge lines, shading) rather than assumed.
THRESHOLDS = (0, 8, 32, 96)


def attempt(fn: Callable[[], Any]) -> dict[str, Any]:
    """Run one observation; record its value or error instead of aborting."""
    try:
        return {"ok": True, "value": plain(fn())}
    except Exception as exc:  # noqa: BLE001 - every failure is a probe result
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def plain(value: Any) -> Any:
    """Make a value JSON-ready, keeping nested structure; only opaque objects become repr."""
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return repr(value)


def same_file(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


# --- SOLIDWORKS session -----------------------------------------------------


CALL = pythoncom.DISPATCH_METHOD | pythoncom.DISPATCH_PROPERTYGET  # standard late-bound call
GET = pythoncom.DISPATCH_PROPERTYGET
PUT = pythoncom.DISPATCH_PROPERTYPUT
PUTREF = pythoncom.DISPATCH_PROPERTYPUTREF
IDISPATCH_TYPE = pythoncom.TypeIIDs[pythoncom.IID_IDispatch]


class Com:
    """Explicit IDispatch access: nothing is invoked by attribute lookup.

    call() is for members the SOLIDWORKS API documents as methods, get()/put() for
    documented properties. Returned dispatch objects are wrapped as Com too.
    """

    def __init__(self, dispatch: Any) -> None:
        self.dispatch = dispatch

    def _invoke(self, name: str, flags: int, result: bool, *args: Any) -> Any:
        dispid = self.dispatch.GetIDsOfNames(name)
        args = tuple(a.dispatch if isinstance(a, Com) else a for a in args)
        return wrap(self.dispatch.Invoke(dispid, 0, flags, result, *args))

    def call(self, name: str, *args: Any) -> Any:
        return self._invoke(name, CALL, True, *args)

    def get(self, name: str) -> Any:
        return self._invoke(name, GET, True)

    def put(self, name: str, value: Any, flags: int = PUT) -> None:
        self._invoke(name, flags, False, value)

    def interface(self) -> str:
        """Name of the interface this object's own type information describes."""
        return self.dispatch.GetTypeInfo().GetDocumentation(-1)[0]


def wrap(value: Any) -> Any:
    return Com(value) if isinstance(value, IDISPATCH_TYPE) else value


def byref_long() -> win32com.client.VARIANT:
    return win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)


def attach() -> Com:
    clsid = pywintypes.IID("SldWorks.Application")
    try:
        unknown = pythoncom.GetActiveObject(clsid)
    except pythoncom.com_error as exc:
        raise SystemExit("SOLIDWORKS is not running. Start it, then rerun the probe.") from exc
    return Com(unknown.QueryInterface(pythoncom.IID_IDispatch))


def open_model(sw: Com, path: Path) -> Com:
    """Open the model read-only. Refuses documents that are already open."""
    if sw.call("GetOpenDocumentByName", str(path)) is not None:
        raise SystemExit(f"{path.name} is already open in SOLIDWORKS. Close it and rerun.")

    doc_type = DOC_TYPES.get(path.suffix.lower())
    if doc_type is None:
        raise SystemExit(f"Unsupported document type: {path.suffix}")
    errors, warnings = byref_long(), byref_long()
    options = OPEN_SILENT | OPEN_READ_ONLY
    model = sw.call("OpenDoc6", str(path), doc_type, options, "", errors, warnings)
    if model is None:
        raise SystemExit(f"OpenDoc6 failed (errors={errors.value}, warnings={warnings.value})")
    if not model.call("IsOpenedReadOnly"):
        # OpenDoc6 handed back an existing editable document; never use or close it.
        raise SystemExit(f"{path.name} is open for editing in SOLIDWORKS. Close it and rerun.")
    return model


def ensure_active(sw: Com, title: str, path: Path) -> None:
    def active_path() -> str | None:
        active = sw.get("ActiveDoc")
        return None if active is None else str(active.call("GetPathName"))

    if (current := active_path()) is not None and same_file(current, str(path)):
        return
    sw.call("ActivateDoc3", title, False, DONT_REBUILD, byref_long())
    if (current := active_path()) is None or not same_file(current, str(path)):
        raise SystemExit("Could not make the probe document the active document.")


def view_of(model: Com) -> Com:
    return model.get("ActiveView")


def state(model: Com) -> dict[str, Any]:
    """Generic camera/view state plus the document's dirty flag."""
    view = view_of(model)
    return {
        "scale2": attempt(lambda: float(view.get("Scale2"))),
        "translation3": attempt(lambda: view.get("Translation3").get("ArrayData")),
        "orientation3": attempt(lambda: view.get("Orientation3").get("ArrayData")),
        "visible_box": attempt(lambda: view.call("GetVisibleBox")),
        "dirty": attempt(lambda: int(model.call("GetSaveFlag"))),
    }


def camera_key(snapshot: dict[str, Any]) -> Any:
    return [snapshot[k].get("value") for k in ("scale2", "translation3", "orientation3")]


def environment(sw: Com, model: Com) -> dict[str, Any]:
    view = view_of(model)
    user32 = ctypes.windll.user32
    frame_members = ("FrameLeft", "FrameTop", "FrameWidth", "FrameHeight", "FrameState")
    return {
        "solidworks_revision": attempt(lambda: sw.call("RevisionNumber")),
        "python": sys.version,
        "platform": platform.platform(),
        "system_dpi": attempt(lambda: user32.GetDpiForSystem()),
        "primary_screen": attempt(lambda: [user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)]),
        "app_frame": {m: attempt(lambda m=m: sw.get(m)) for m in frame_members},
        "view_frame": {m: attempt(lambda m=m: view.get(m)) for m in frame_members},
        "display_mode": attempt(lambda: view.get("DisplayMode")),
        "has_perspective": attempt(lambda: view.call("HasPerspective")),
        "part_box": attempt(lambda: model.call("GetPartBox", True)),
        "read_only": attempt(lambda: model.call("IsOpenedReadOnly")),
    }


def binding(sw: Com, model: Com) -> dict[str, Any]:
    """Interface each object's own type information claims; pywin32 binds by this."""
    view = view_of(model)
    return {
        "application": attempt(sw.interface),
        "document": attempt(model.interface),
        "view": attempt(view.interface),
        "math_vector": attempt(lambda: view.get("Translation3").interface()),
        "math_transform": attempt(lambda: view.get("Orientation3").interface()),
    }


# --- Raster analysis (pure pixels) ------------------------------------------


def bmp_header(raw: bytes) -> dict[str, Any]:
    if raw[:2] != b"BM" or len(raw) < 30:
        return {"signature": raw[:2].hex()}
    (pixel_offset,) = struct.unpack_from("<I", raw, 10)
    dib_size, width, height, _planes, bits = struct.unpack_from("<IiiHH", raw, 14)
    return {
        "width": width,
        "height": height,
        "top_down": height < 0,
        "bits_per_pixel": bits,
        "dib_header_size": dib_size,
        "pixel_offset": pixel_offset,
        "file_bytes": len(raw),
    }


def border_background(rgb: Image.Image) -> tuple[tuple[int, int, int], float, int]:
    """Dominant colour of the outer 1px ring, its share of the ring, distinct colours."""
    w, h = rgb.size
    strips = [
        rgb.crop((0, 0, w, 1)),
        rgb.crop((0, h - 1, w, h)),
        rgb.crop((0, 1, 1, h - 1)),
        rgb.crop((w - 1, 1, w, h - 1)),
    ]
    counts: Counter[tuple[int, int, int]] = Counter()
    for strip in strips:
        for n, colour in strip.getcolors(strip.width * strip.height) or []:
            counts[colour] += n
    colour, n = counts.most_common(1)[0]
    return colour, n / sum(counts.values()), len(counts)


def describe_foreground(box: tuple[int, int, int, int] | None, pixels: int, w: int, h: int):
    if box is None:
        return {"pixels": 0, "bounds": None}
    left, top, right_ex, bottom_ex = box
    fg_w, fg_h = right_ex - left, bottom_ex - top
    return {
        "pixels": pixels,
        "bounds": [left, top, right_ex - 1, bottom_ex - 1],  # inclusive, like viewjig.measure
        "size": [fg_w, fg_h],
        "fill": [round(fg_w / w, 5), round(fg_h / h, 5)],
        "margins": [left, top, w - right_ex, h - bottom_ex],  # left, top, right, bottom
        "center_offset": [
            (left + right_ex - 1) / 2 - (w - 1) / 2,
            (top + bottom_ex - 1) / 2 - (h - 1) / 2,
        ],
    }


def analyze(bmp: Path, keep_bmp: bool) -> dict[str, Any]:
    raw = bmp.read_bytes()
    with Image.open(bmp) as image:
        mode = image.mode
        rgb = image.convert("RGB")
    w, h = rgb.size
    background, share, distinct = border_background(rgb)
    diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, background))
    r, g, b = diff.split()
    delta = ImageChops.lighter(ImageChops.lighter(r, g), b)

    foreground = {}
    for t in THRESHOLDS:
        mask = delta.point(lambda v, t=t: 255 if v > t else 0)
        foreground[str(t)] = describe_foreground(mask.getbbox(), mask.histogram()[255], w, h)

    rgb.save(bmp.with_suffix(".png"))
    if not keep_bmp:
        bmp.unlink()
    return {
        "size": [w, h],
        "mode": mode,
        "bmp_header": bmp_header(raw),
        "pixel_sha256": hashlib.sha256(rgb.tobytes()).hexdigest()[:16],
        "background": {
            "rgb": list(background),
            "border_share": round(share, 5),
            "border_colours": distinct,
        },
        "foreground": foreground,
    }


# --- Experiment steps ---------------------------------------------------------


class Probe:
    def __init__(self, model: Com, out_dir: Path, keep_bmp: bool) -> None:
        self.model = model
        self.out_dir = out_dir
        self.keep_bmp = keep_bmp

    def redraw(self) -> dict[str, Any]:
        return attempt(lambda: self.model.call("GraphicsRedraw2"))

    def establish(self, view_id: int = VIEW_ISOMETRIC) -> dict[str, Any]:
        """Standard view, then Zoom to Fit; record state after each step."""
        steps: dict[str, Any] = {
            "show_named_view": attempt(lambda: self.model.call("ShowNamedView2", "", view_id))
        }
        steps["after_orient"] = state(self.model)
        steps["zoom_to_fit"] = attempt(lambda: self.model.call("ViewZoomtofit2"))
        steps["after_fit"] = state(self.model)
        steps["redraw"] = self.redraw()
        steps["after_redraw"] = state(self.model)
        return steps

    def capture(self, name: str, width: int, height: int) -> dict[str, Any]:
        bmp = self.out_dir / f"{name}.bmp"
        bmp.unlink(missing_ok=True)
        bmp.with_suffix(".png").unlink(missing_ok=True)
        before = state(self.model)
        started = time.perf_counter()
        result = attempt(lambda: bool(self.model.call("SaveBMP", str(bmp), width, height)))
        seconds = round(time.perf_counter() - started, 3)
        after = state(self.model)
        return {
            "name": name,
            "requested": [width, height],
            "save_bmp": result,
            "seconds": seconds,
            "camera_changed_by_capture": camera_key(before) != camera_key(after),
            "state_before": before,
            "state_after": after,
            "raster": attempt(lambda: analyze(bmp, self.keep_bmp)) if bmp.exists() else None,
        }

    def perturb(self, steps: list[Callable[[], Any]]) -> dict[str, Any]:
        results = [attempt(step) for step in steps]
        self.redraw()
        return {"steps": results, "state": state(self.model)}


def put_object(view: Com, name: str, value: Com) -> dict[str, Any]:
    """Assign an object-valued property, recording which COM put form SOLIDWORKS accepts."""
    outcome = {"propertyput": attempt(lambda: view.put(name, value, PUT))}
    if not outcome["propertyput"]["ok"]:
        outcome["propertyputref"] = attempt(lambda: view.put(name, value, PUTREF))
    return outcome


def run(model: Com, out_dir: Path, keep_bmp: bool, report: dict[str, Any]) -> None:
    probe = Probe(model, out_dir, keep_bmp)

    def view() -> Com:  # always re-read the active view
        return view_of(model)

    # A. Fixed sizes from one established view, each captured twice back to back.
    baseline = probe.establish()
    second_fit = attempt(lambda: model.call("ViewZoomtofit2"))
    report["fixed_sizes"] = {
        "establish": baseline,
        "second_zoom_to_fit": second_fit,
        "after_second_fit": state(model),
        "captures": [probe.capture(f"A_{w}x{h}_{n}", w, h) for w, h in SIZES for n in (1, 2)],
    }

    # B. Different starting views, then the same standard view + Zoom to Fit.
    rw, rh = REFERENCE_SIZE
    starts: dict[str, list[Callable[[], Any]]] = {
        "unchanged": [],
        "front": [lambda: model.call("ShowNamedView2", "", VIEW_FRONT)],
        "top_zoom_3": [
            lambda: model.call("ShowNamedView2", "", VIEW_TOP),
            lambda: view().call("ZoomByFactor", 3.0),
        ],
        "trimetric_zoom_0.4": [
            lambda: model.call("ShowNamedView2", "", VIEW_TRIMETRIC),
            lambda: view().call("ZoomByFactor", 0.4),
        ],
        "iso_panned": [
            lambda: model.call("ShowNamedView2", "", VIEW_ISOMETRIC),
            lambda: view().call("TranslateBy", 0.03, 0.02),
        ],
        "iso_rotated": [
            lambda: model.call("ShowNamedView2", "", VIEW_ISOMETRIC),
            lambda: view().call("RotateAboutCenter", 0.5, 0.8),
        ],
    }
    report["start_views"] = []
    for name, steps in starts.items():
        entry = {"start": name, "perturb": probe.perturb(steps), "establish": probe.establish()}
        entry["capture"] = probe.capture(f"B_{name}", rw, rh)
        report["start_views"].append(entry)

    # C. Save raw camera state, disturb the view, restore it by direct assignment. The
    # first live run restored Translation3 multiplied by Scale2, so the assignment order
    # is varied and the state is read back after every single assignment.
    probe.establish()
    saved = {name: view().get(name) for name in ("Orientation3", "Translation3", "Scale2")}

    def saved_values() -> list[Any]:
        return [
            saved["Scale2"],
            list(saved["Translation3"].get("ArrayData")),
            list(saved["Orientation3"].get("ArrayData")),
        ]

    before_disturb = saved_values()
    report["camera_restore"] = {
        "saved": before_disturb,
        "reference": probe.capture("C_reference", rw, rh),
        "orders": [],
    }
    for order in RESTORE_ORDERS:
        disturbed = probe.perturb(
            starts["trimetric_zoom_0.4"] + [lambda: view().call("TranslateBy", 0.02, -0.01)]
        )
        steps = []
        for name in order:
            value = saved[name]
            if isinstance(value, Com):
                result = put_object(view(), name, value)
            else:
                result = attempt(lambda name=name, value=value: view().put(name, value))
            steps.append({"set": name, "result": result, "state": state(model)})
        report["camera_restore"]["orders"].append(
            {
                "order": list(order),
                "disturbed": disturbed,
                # False would mean the saved objects are live views of the camera.
                "saved_objects_unchanged": attempt(lambda: saved_values() == before_disturb),
                "steps": steps,
                "redraw": probe.redraw(),
                "state": state(model),
                "restored": probe.capture("C_restored_" + "-".join(order), rw, rh),
            }
        )


# --- Summary -----------------------------------------------------------------


def summarize(report: dict[str, Any]) -> str:
    lines = []

    def fg(cap: dict[str, Any], t: str = "8") -> tuple[dict[str, Any], dict[str, Any]]:
        raster = (cap.get("raster") or {}).get("value") or {}
        return raster.get("foreground", {}).get(t, {}), raster

    lines.append("A. fixed sizes (foreground at threshold 8)")
    lines.append(
        f"{'requested':>11} {'actual':>11} {'bg share':>8} {'fg size':>11} {'fill w,h':>17} "
        f"{'center off':>15} {'cam chg':>7} hash"
    )
    for cap in report.get("fixed_sizes", {}).get("captures", []):
        f, raster = fg(cap)
        if not raster:
            lines.append(f"{'x'.join(map(str, cap['requested'])):>11}  FAILED {cap['save_bmp']}")
            continue
        lines.append(
            f"{'x'.join(map(str, cap['requested'])):>11} {'x'.join(map(str, raster['size'])):>11} "
            f"{raster['background']['border_share']:>8} {'x'.join(map(str, f.get('size', []))):>11} "
            f"{f.get('fill')!s:>17} {f.get('center_offset')!s:>15} "
            f"{cap['camera_changed_by_capture']!s:>7} {raster['pixel_sha256']}"
        )

    lines.append("")
    lines.append("B. start views -> isometric + Zoom to Fit at reference size")
    for entry in report.get("start_views", []):
        f, raster = fg(entry["capture"])
        scale = entry["establish"]["after_redraw"]["scale2"].get("value")
        lines.append(
            f"{entry['start']:>20} scale2={scale!r:<24} bounds={f.get('bounds')} "
            f"hash={raster.get('pixel_sha256')}"
        )

    restore = report.get("camera_restore")
    if restore:
        lines.append("")
        lines.append("C. camera restore")
        ref = fg(restore["reference"])[1].get("pixel_sha256")
        scale, translation, orientation = restore["saved"]
        lines.append(f"  reference hash={ref} saved scale2={scale!r} translation3={translation}")
        for entry in restore.get("orders", []):
            got = fg(entry["restored"])[1].get("pixel_sha256")
            final = entry["state"]
            lines.append(
                f"  {'>'.join(entry['order']):<34} identical={ref == got and ref is not None} "
                f"scale2={final['scale2'].get('value') == scale} "
                f"orientation3={final['orientation3'].get('value') == orientation} "
                f"translation3={final['translation3'].get('value')}"
            )
            for step in entry["steps"]:
                lines.append(
                    f"    after {step['set']:<13} translation3="
                    f"{step['state']['translation3'].get('value')} result={step['result']}"
                )

    doc = report.get("document", {})
    lines.append("")
    lines.append(f"dirty: start={doc.get('dirty_start')} end={doc.get('dirty_end')}")
    if "fatal" in report:
        lines.append(f"FATAL: {report['fatal']}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "model", type=Path, help="neutral fixture to capture, e.g. fixtures/vj-f01.SLDPRT"
    )
    parser.add_argument("--tag", required=True, help="label for this run, e.g. max or half")
    parser.add_argument("--out", type=Path, default=Path("captures/capture-01"))
    parser.add_argument("--keep-bmp", action="store_true", help="keep the raw BMP files")
    args = parser.parse_args()

    model_path = args.model.resolve()
    if not model_path.is_file():
        raise SystemExit(f"Model not found: {model_path}")
    out_dir = (args.out / args.tag).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    attempt(lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2))  # physical-pixel screen metrics

    report: dict[str, Any] = {
        "probe": "VJ-CAPTURE-01",
        "tag": args.tag,
        "started": datetime.now(UTC).isoformat(timespec="seconds"),
        "thresholds": list(THRESHOLDS),
    }
    sw = attach()
    model = open_model(sw, model_path)
    title = str(model.call("GetTitle"))
    report["document"] = {"file_name": model_path.name, "title": title}
    try:
        ensure_active(sw, title, model_path)
        report["binding"] = binding(sw, model)
        report["document"]["dirty_start"] = int(model.call("GetSaveFlag"))
        report["environment"] = environment(sw, model)
        run(model, out_dir, args.keep_bmp, report)
        report["environment_end"] = environment(sw, model)
        report["document"]["dirty_end"] = int(model.call("GetSaveFlag"))
    except BaseException as exc:
        report["fatal"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        report["document"]["closed"] = attempt(lambda: sw.call("CloseDoc", title))
        report["finished"] = datetime.now(UTC).isoformat(timespec="seconds")
        (out_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        summary = summarize(report)
        (out_dir / "summary.txt").write_text(summary + "\n", encoding="utf-8")
        print(summary)
        print(f"\nReport: {out_dir / 'report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
