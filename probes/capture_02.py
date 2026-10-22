"""VJ-CAPTURE-02: does a camera restored in another window give the same raster?

This is an experimental probe, not production ViewJig code. It builds on
VJ-CAPTURE-01 (probes/capture_01_findings.md), which found that native Zoom to
Fit framing changes with the SOLIDWORKS window size, while a saved camera
restored in the *same* window reproduces the image exactly.

Primary question

  P. Portable camera  Save the camera (Orientation3, Scale2, Translation3) as
                      plain numbers in one window configuration, then assign it
                      in other configurations. Are the captured rasters
                      identical? If not, how does the model's size and position
                      in the image change?

Secondary questions, kept apart so they cannot confound P

  M. Minimized        With the camera assigned, minimize SOLIDWORKS and
                      capture again. Same pixels as the visible window?
  F. FeatureManager   Separate run: hide the FeatureManager panel by hand and
                      rerun with --camera-from (see below). Compare native Zoom
                      to Fit centring, and the portable camera, with the panel
                      shown and hidden.

Each configuration also captures 0x0 and output aspects just either side of
1.29, which tests hypothesis H1 from capture-01 without extra steps.

Window control: the probe resizes the SOLIDWORKS main window through Win32
(maximized, then fractions of the monitor work area) and restores the original
placement at the end, even on failure. Like capture-01, it refuses a fixture
that is already open, opens it read-only, never saves, and closes it.

Usage (Windows, SOLIDWORKS running, fixture closed):

    .venv\\Scripts\\python probes\\capture_02.py fixtures\\vj-f01.SLDPRT --tag portable

Panel test, after hiding the FeatureManager panel:

    .venv\\Scripts\\python probes\\capture_02.py fixtures\\vj-f01.SLDPRT --tag nopanel ^
        --camera-from captures\\capture-02\\portable\\camera.json --configs maximized --skip-minimized

Output goes to captures/capture-02/<tag>/ (git-ignored): report.json,
summary.txt, camera.json, and a PNG of every capture.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pythoncom
import win32api
import win32com.client
import win32con
import win32gui
from capture_01 import (
    THRESHOLDS,
    Com,
    Probe,
    attach,
    attempt,
    binding,
    ensure_active,
    environment,
    open_model,
    state,
    view_of,
    windows,
)

# Window configurations: None = maximized, otherwise (x, y, width, height) as
# fractions of the monitor work area. Shapes chosen to give the graphics area
# different aspect ratios: wide, about square, very wide, portrait.
CONFIGS: dict[str, tuple[float, float, float, float] | None] = {
    "maximized": None,
    "left-half": (0.0, 0.0, 0.5, 1.0),
    "landscape": (0.0, 0.0, 0.6, 0.55),
    "portrait": (0.0, 0.0, 0.32, 1.0),
}
DEFAULT_ORDER = ["maximized", "left-half", "landscape", "portrait", "maximized"]

# 1280x1000 (1.28) and 1300x1000 (1.30) sit either side of the ~1.2915 switch
# seen in capture-01 (hypothesis H1).
SIZES = [(1600, 1200), (1000, 1000), (1600, 900), (1280, 1000), (1300, 1000)]
REFERENCE_SIZE = (1600, 1200)

SETTLE_TIMEOUT = 5.0
SETTLE_POLL = 0.2
SETTLE_STABLE_POLLS = 3


# --- Window control -----------------------------------------------------------


def app_hwnd(sw: Com) -> int:
    return int(sw.call("Frame").call("GetHWndx64"))


def view_hwnd(model: Com) -> int:
    return int(view_of(model).call("GetViewHWndx64"))


def settle(model: Com) -> dict[str, Any]:
    """Wait until the view window's client size stops changing; report what happened."""
    started = time.monotonic()
    last, stable, polls = None, 0, 0
    while time.monotonic() - started < SETTLE_TIMEOUT:
        size = list(win32gui.GetClientRect(view_hwnd(model))[2:])
        polls += 1
        stable = stable + 1 if size == last else 0
        last = size
        if stable >= SETTLE_STABLE_POLLS:
            break
        time.sleep(SETTLE_POLL)
    redraw = attempt(lambda: model.call("GraphicsRedraw2"))
    return {
        "client_size": last,
        "settled": stable >= SETTLE_STABLE_POLLS,
        "seconds": round(time.monotonic() - started, 2),
        "polls": polls,
        "redraw": redraw,
    }


def apply_config(sw: Com, model: Com, name: str) -> dict[str, Any]:
    hwnd = app_hwnd(sw)
    frac = CONFIGS[name]
    if frac is None:
        win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
        target = None
    else:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        monitor = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
        left, top, right, bottom = win32api.GetMonitorInfo(monitor)["Work"]
        width, height = right - left, bottom - top
        x, y, w, h = frac
        target = [left + int(x * width), top + int(y * height), int(w * width), int(h * height)]
        flags = win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE
        win32gui.SetWindowPos(hwnd, 0, *target, flags)
    return {"config": name, "target_rect": target, "settle": settle(model)}


# --- Camera as plain numbers ----------------------------------------------------


def read_camera(model: Com) -> dict[str, Any]:
    view = view_of(model)
    return {
        "orientation3": list(view.get("Orientation3").get("ArrayData")),
        "translation3": list(view.get("Translation3").get("ArrayData")),
        "scale2": float(view.get("Scale2")),
    }


def doubles(values: list[float]) -> win32com.client.VARIANT:
    return win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [float(v) for v in values])


def assign_camera(sw: Com, model: Com, camera: dict[str, Any]) -> dict[str, Any]:
    """Assign a saved camera from numbers; Translation3 last, per capture-01 (O9)."""
    result: dict[str, Any] = {}
    try:
        math = sw.call("GetMathUtility")
        xform = math.call("CreateTransform", doubles(camera["orientation3"]))
        vec = math.call("CreateVector", doubles(camera["translation3"]))
    except Exception as exc:  # noqa: BLE001 - recorded; without the objects P cannot run
        result["create"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return result
    result["create"] = {"ok": True}
    view = view_of(model)
    result["Orientation3"] = attempt(lambda: view.put("Orientation3", xform))
    result["Scale2"] = attempt(lambda: view.put("Scale2", camera["scale2"]))
    result["Translation3"] = attempt(lambda: view.put("Translation3", vec))
    result["redraw"] = attempt(lambda: model.call("GraphicsRedraw2"))
    readback = read_camera(model)
    result["readback"] = readback
    result["readback_exact"] = readback == camera
    return result


# --- Experiment -------------------------------------------------------------------


def capture_set(probe: Probe, prefix: str, sizes: list[tuple[int, int]]) -> list[dict[str, Any]]:
    return [probe.capture(f"{prefix}_{w}x{h}", w, h) for w, h in sizes]


def run(
    sw: Com,
    model: Com,
    out_dir: Path,
    order: list[str],
    camera: dict[str, Any] | None,
    skip_minimized: bool,
    keep_bmp: bool,
    report: dict[str, Any],
) -> None:
    probe = Probe(model, out_dir, keep_bmp)
    rw, rh = REFERENCE_SIZE

    # P. Establish (or load) the camera in the first configuration.
    first = order[0]
    setup: dict[str, Any] = {"window": apply_config(sw, model, first)}
    setup["establish"] = probe.establish()
    if camera is None:
        camera = read_camera(model)
        camera_source = {"tag": report["tag"], "config": first}
        setup["native"] = probe.capture(f"P0_{first}_native_{rw}x{rh}", rw, rh)
    else:
        camera_source = report["camera_from"]
    # Every configuration starts from the saved camera, so a resize is the only change.
    setup["assign"] = assign_camera(sw, model, camera)
    camera_record = {
        "source": camera_source,
        "graphics_area": setup["window"]["settle"]["client_size"],
        **camera,
    }
    (out_dir / "camera.json").write_text(json.dumps(camera_record, indent=2), encoding="utf-8")
    report["camera"] = camera_record
    report["setup"] = setup

    report["configs"] = []
    for index, name in enumerate(order, start=1):
        prefix = f"P{index}_{name}"
        entry: dict[str, Any] = {"config": name}
        if index > 1:
            entry["window"] = apply_config(sw, model, name)
        entry["windows"] = windows(sw, view_of(model))
        # The view held the saved camera before the resize: does resizing alone change it?
        entry["state_after_resize"] = state(model)
        entry["camera_kept_through_resize"] = attempt(lambda: read_camera(model) == camera)
        entry["as_resized"] = probe.capture(f"{prefix}_as-resized_{rw}x{rh}", rw, rh)
        # Native Zoom to Fit in this window, for contrast.
        entry["native_establish"] = probe.establish()
        entry["native"] = probe.capture(f"{prefix}_native_{rw}x{rh}", rw, rh)
        # The portable camera itself; it also stays in place for the next resize.
        entry["assign"] = assign_camera(sw, model, camera)
        entry["state_after_assign"] = state(model)
        entry["restored"] = capture_set(probe, f"{prefix}_restored", [(0, 0), *SIZES])
        report["configs"].append(entry)

    # M. Minimized: same camera, SOLIDWORKS minimized, same sizes.
    if not skip_minimized:
        hwnd = app_hwnd(sw)
        minimized: dict[str, Any] = {"config": order[-1]}
        minimized["assign_visible"] = assign_camera(sw, model, camera)
        minimized["visible"] = capture_set(probe, "M_visible", [(0, 0), *SIZES])
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
        time.sleep(1.0)
        minimized["windows_minimized"] = windows(sw, view_of(model))
        minimized["state_minimized"] = state(model)
        minimized["minimized"] = capture_set(probe, "M_minimized", [(0, 0), *SIZES])
        minimized["camera_kept_while_minimized"] = attempt(lambda: read_camera(model) == camera)
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        minimized["window_restored"] = apply_config(sw, model, order[-1])
        report["minimized"] = minimized


# --- Summary ------------------------------------------------------------------------


def raster(cap: dict[str, Any] | None) -> dict[str, Any]:
    return ((cap or {}).get("raster") or {}).get("value") or {}


def fg(cap: dict[str, Any] | None) -> dict[str, Any]:
    return raster(cap).get("foreground", {}).get("8", {})


def compare(cap: dict[str, Any], ref: dict[str, Any] | None) -> str:
    got, want = raster(cap), raster(ref)
    if not got:
        return f"FAILED {cap.get('save_bmp')}"
    f, r = fg(cap), fg(ref)
    same = (got.get("pixel_sha256") == want.get("pixel_sha256")) if want else "n/a"
    text = f"identical={same!s:<5} fg={f.get('size')} offset={f.get('center_offset')}"
    if want and not same and f.get("size") and r.get("size"):
        ratio = [round(a / b, 4) for a, b in zip(f["size"], r["size"], strict=True)]
        text += f" size ratio vs reference={ratio}"
    return text


def summarize(report: dict[str, Any]) -> str:
    lines: list[str] = []
    configs = report.get("configs", [])
    camera = report.get("camera", {})
    lines.append("P. portable camera")
    lines.append(
        f"  camera from {camera.get('source')} graphics area {camera.get('graphics_area')} "
        f"scale2={camera.get('scale2')!r}"
    )
    native0 = report.get("setup", {}).get("native")
    reference = {tuple(c["requested"]): c for c in configs[0]["restored"]} if configs else {}
    if native0 and reference:
        lines.append(
            "  control: restored-from-numbers vs native in first window: "
            + compare(reference.get(REFERENCE_SIZE), native0)
        )
    for entry in configs:
        settle_info = (entry.get("window") or report.get("setup", {}).get("window", {})).get(
            "settle", {}
        )
        assign = entry.get("assign", {})
        visible = entry["state_after_assign"]["visible_box"].get("value")
        lines.append("")
        lines.append(
            f"  [{entry['config']}] graphics area {settle_info.get('client_size')} "
            f"settled={settle_info.get('settled')} visible_box={visible} "
            f"camera kept through resize={entry['camera_kept_through_resize'].get('value')} "
            f"readback exact={assign.get('readback_exact')}"
        )
        lines.append(
            f"    as resized {REFERENCE_SIZE}: {compare(entry['as_resized'], reference.get(REFERENCE_SIZE))}"
        )
        for cap in entry["restored"]:
            size = tuple(cap["requested"])
            ref = reference.get(size) if size != (0, 0) else None
            lines.append(f"    restored {cap['requested']}: {compare(cap, ref)}")
        scale = entry["native_establish"]["after_redraw"]["scale2"].get("value")
        lines.append(
            f"    native fit (scale2={scale!r}): {compare(entry['native'], reference.get(REFERENCE_SIZE))}"
        )

    minimized = report.get("minimized")
    if minimized:
        lines.append("")
        chain = minimized["windows_minimized"].get("view_chain", {}).get("value") or []
        lines.append(
            f"M. minimized ({minimized['config']}): any window minimized="
            f"{any(w['minimized'] for w in chain)} camera kept={minimized['camera_kept_while_minimized'].get('value')}"
        )
        visible = {tuple(c["requested"]): c for c in minimized["visible"]}
        for cap in minimized["minimized"]:
            lines.append(
                f"    {cap['requested']}: {compare(cap, visible.get(tuple(cap['requested'])))}"
            )

    doc = report.get("document", {})
    lines.append("")
    lines.append(f"window placement restored: {report.get('placement_restored')}")
    lines.append(f"dirty: start={doc.get('dirty_start')} end={doc.get('dirty_end')}")
    if "fatal" in report:
        lines.append(f"FATAL: {report['fatal']}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("model", type=Path, help="neutral fixture, e.g. fixtures/vj-f01.SLDPRT")
    parser.add_argument("--tag", required=True, help="label for this run, e.g. portable")
    parser.add_argument("--out", type=Path, default=Path("captures/capture-02"))
    parser.add_argument("--camera-from", type=Path, help="camera.json from an earlier run")
    parser.add_argument(
        "--configs",
        nargs="+",
        choices=sorted(CONFIGS),
        default=DEFAULT_ORDER,
        help="window configurations to visit, in order (default: %(default)s)",
    )
    parser.add_argument("--skip-minimized", action="store_true", help="skip phase M")
    parser.add_argument("--keep-bmp", action="store_true", help="keep the raw BMP files")
    args = parser.parse_args()

    model_path = args.model.resolve()
    if not model_path.is_file():
        raise SystemExit(f"Model not found: {model_path}")
    camera = None
    if args.camera_from:
        loaded = json.loads(args.camera_from.read_text(encoding="utf-8"))
        camera = {k: loaded[k] for k in ("orientation3", "translation3", "scale2")}
    out_dir = (args.out / args.tag).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    attempt(lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2))  # physical pixels

    report: dict[str, Any] = {
        "probe": "VJ-CAPTURE-02",
        "tag": args.tag,
        "started": datetime.now(UTC).isoformat(timespec="seconds"),
        "thresholds": list(THRESHOLDS),
        "configs_requested": args.configs,
        "camera_from": str(args.camera_from) if args.camera_from else None,
    }
    sw = attach()
    hwnd = app_hwnd(sw)
    placement = win32gui.GetWindowPlacement(hwnd)
    report["original_placement"] = list(placement)
    model = open_model(sw, model_path)
    title = str(model.call("GetTitle"))
    report["document"] = {"file_name": model_path.name, "title": title}
    try:
        ensure_active(sw, title, model_path)
        report["binding"] = binding(sw, model)
        report["document"]["dirty_start"] = int(model.call("GetSaveFlag"))
        report["environment"] = environment(sw, model)
        run(sw, model, out_dir, args.configs, camera, args.skip_minimized, args.keep_bmp, report)
        report["environment_end"] = environment(sw, model)
        report["document"]["dirty_end"] = int(model.call("GetSaveFlag"))
    except BaseException as exc:
        report["fatal"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        report["placement_restored"] = attempt(lambda: restore_placement(hwnd, placement))
        report["document"]["closed"] = attempt(lambda: sw.call("CloseDoc", title))
        report["finished"] = datetime.now(UTC).isoformat(timespec="seconds")
        (out_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        summary = summarize(report)
        (out_dir / "summary.txt").write_text(summary + "\n", encoding="utf-8")
        print(summary)
        print(f"\nReport: {out_dir / 'report.json'}")
    return 0


def restore_placement(hwnd: int, placement: tuple[Any, ...]) -> dict[str, Any]:
    win32gui.SetWindowPlacement(hwnd, placement)
    after = win32gui.GetWindowPlacement(hwnd)
    # (flags, showCmd, ptMin, ptMax, rcNormal); flags can legitimately differ.
    return {
        "show_cmd_matches": after[1] == placement[1],
        "normal_rect_matches": after[4] == placement[4],
    }


if __name__ == "__main__":
    raise SystemExit(main())
