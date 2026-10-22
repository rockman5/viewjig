"""VJ-CAPTURE-03: how the camera maps to the raster, and one camera across two documents.

This is an experimental probe, not production ViewJig code. It builds on
VJ-CAPTURE-02 (probes/capture_02_findings.md): a camera saved as plain numbers
(Orientation3, Translation3, Scale2) fully determines a fixed-size SaveBMP
raster, independent of the SOLIDWORKS window.

Two modes answer two separate questions and share no conclusions.

  mapping     At a fixed output size, how do changes to Scale2 and Translation3
              map to the model's raster scale and position? One document.
              Starting from a saved camera, sweep Scale2 (orientation and
              translation held), then sweep Translation3 one model axis at a
              time at two Scale2 values. Report fitted relationships and how
              well they fit.

  documents   With the same camera and output size, does identical model-space
              geometry in two separate documents project to identical pixel
              locations? Cameras C_A and C_B come from isometric + Zoom to Fit
              on each document. Both documents are captured with both cameras,
              opened one at a time.

The documents mode is generic and non-semantic. It never identifies or
interprets what differs between the documents. It reports raster measurements
for each image pair:
  - whole-model foreground bounds and clipping, per image;
  - differing-pixel count and bounding box;
  - an edge-overlap shift test: the share of dark edge-line pixels that
    coincide when one image is offset by each integer shift up to
    +/-SHIFT_RANGE px. Registered rasters overlap most at shift (0, 0), and a
    1 px misregistration moves or flattens that peak. No correspondence
    between features, faces, or geometry is computed.
Which geometry the documents share is known from the fixtures' construction
(fixtures/README.md), not inferred by the probe.

Safety, as in capture-01/02: attaches to a running SOLIDWORKS, refuses
documents that are already open, opens read-only, never saves, closes what it
opened. The window is left as it is (capture-02 showed it does not affect
fixed-size output) but its state is recorded.

Usage (Windows, SOLIDWORKS running, fixtures closed):

    .venv\\Scripts\\python probes\\capture_03.py mapping fixtures\\vj-f01.SLDPRT --tag mapping
    .venv\\Scripts\\python probes\\capture_03.py documents fixtures\\vj-f01.SLDPRT ^
        fixtures\\vj-f01b.SLDPRT --tag documents

Output goes to captures/capture-03/<tag>/ (git-ignored).
"""

from __future__ import annotations

import argparse
import ctypes
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    view_of,
    windows,
)
from capture_02 import assign_camera, fg, raster, read_camera
from PIL import Image, ImageChops

# mapping: sweeps around the starting camera.
MAPPING_SIZES = [(1600, 1200), (1000, 1000)]  # height-limited and width-limited
SCALE_FACTORS = [0.5, 0.7, 0.85, 1.0, 1.1]  # 1.1 keeps 0.8533 height fill inside the frame
# Metres added to one Translation3 component at a time. Kept small: if one model metre
# is ~8,700 px (VJ-F01's 0.12 m spans ~1041 px), 0.008 m moves the model ~70 px,
# inside the ~88 px margin at 1600x1200. Clipped captures are excluded from fits anyway.
TRANSLATION_STEPS = [-0.008, -0.004, 0.004, 0.008]
TRANSLATION_SCALES = [1.0, 0.7]  # Scale2 factors at which the translation sweep repeats
TRANSLATION_SIZE = (1600, 1200)
DEFAULT_CAMERA = Path("captures/capture-02/portable/camera.json")

# documents
DOCUMENT_SIZES = [(1600, 1200), (1000, 1000)]
EDGE_LUMA = (32, 64, 96)  # a pixel is an edge-line pixel at luminance <= value
SHIFT_RANGE = 3
DIFF_THRESHOLDS = (0, 8)


# --- Documents -------------------------------------------------------------------


def with_document(
    sw: Com, path: Path, out_dir: Path, keep_bmp: bool, body: Callable[[Com, Probe], Any]
) -> dict[str, Any]:
    """Open read-only, run body, always close; record safety state around it."""
    model = open_model(sw, path)
    title = str(model.call("GetTitle"))
    record: dict[str, Any] = {"file_name": path.name, "title": title}
    try:
        ensure_active(sw, title, path)
        record["dirty_start"] = int(model.call("GetSaveFlag"))
        record["binding"] = binding(sw, model)
        record["environment"] = environment(sw, model)
        record["result"] = body(model, Probe(model, out_dir, keep_bmp))
        record["dirty_end"] = int(model.call("GetSaveFlag"))
    finally:
        record["closed"] = attempt(lambda: sw.call("CloseDoc", title))
    return record


def clipped(cap: dict[str, Any]) -> bool | None:
    bounds, size = fg(cap).get("bounds"), raster(cap).get("size")
    if not bounds or not size:
        return None
    left, top, right, bottom = bounds
    return left == 0 or top == 0 or right == size[0] - 1 or bottom == size[1] - 1


def centre(cap: dict[str, Any]) -> list[float] | None:
    """Foreground bounds centre in image pixels (x right, y down)."""
    bounds = fg(cap).get("bounds")
    if not bounds:
        return None
    left, top, right, bottom = bounds
    return [(left + right) / 2, (top + bottom) / 2]


def with_camera(camera: dict[str, Any], scale: float | None = None, delta=None) -> dict[str, Any]:
    out = {k: list(v) if isinstance(v, list) else v for k, v in camera.items()}
    if scale is not None:
        out["scale2"] = camera["scale2"] * scale
    if delta is not None:
        out["translation3"] = [t + d for t, d in zip(camera["translation3"], delta, strict=True)]
    return out


def shot(sw: Com, model: Com, probe: Probe, camera: dict[str, Any], name: str, sizes) -> dict:
    assigned = assign_camera(sw, model, camera)
    caps = [probe.capture(f"{name}_{w}x{h}", w, h) for w, h in sizes]
    for cap in caps:
        cap["clipped"] = clipped(cap)
    return {"camera": camera, "assign": assigned, "captures": caps}


# --- mapping ---------------------------------------------------------------------


def run_mapping(sw: Com, model: Com, probe: Probe, camera: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"windows": windows(sw, view_of(model))}
    result["base_start"] = shot(sw, model, probe, camera, "base-start", MAPPING_SIZES)
    result["scale"] = [
        {
            "factor": f,
            **shot(sw, model, probe, with_camera(camera, scale=f), f"S{f}", MAPPING_SIZES),
        }
        for f in SCALE_FACTORS
    ]
    result["translation"] = []
    for s in TRANSLATION_SCALES:
        for axis in range(3):
            for step in TRANSLATION_STEPS:
                delta = [step if i == axis else 0.0 for i in range(3)]
                cam = with_camera(camera, scale=s, delta=delta)
                name = f"T_s{s}_axis{axis}_{step:+.2f}"
                entry = shot(sw, model, probe, cam, name, [TRANSLATION_SIZE])
                result["translation"].append({"scale": s, "axis": axis, "step": step, **entry})
        result["translation"].append(
            {
                "scale": s,
                "axis": None,
                "step": 0.0,
                **shot(
                    sw,
                    model,
                    probe,
                    with_camera(camera, scale=s),
                    f"T_s{s}_zero",
                    [TRANSLATION_SIZE],
                ),
            }
        )
    result["base_end"] = shot(sw, model, probe, camera, "base-end", MAPPING_SIZES)
    return result


def fit_line(xs: list[float], ys: list[float]) -> dict[str, float]:
    """Least-squares y = slope * x + intercept, with the largest absolute residual."""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
    intercept = my - slope * mx
    residual = max(abs(y - (slope * x + intercept)) for x, y in zip(xs, ys, strict=True))
    return {"slope": slope, "intercept": intercept, "max_residual": residual}


def analyse_mapping(result: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    start = {tuple(c["requested"]): c for c in result["base_start"]["captures"]}
    end = {tuple(c["requested"]): c for c in result["base_end"]["captures"]}
    out["base_repeatable"] = {
        f"{w}x{h}": raster(start[(w, h)]).get("pixel_sha256")
        == raster(end[(w, h)]).get("pixel_sha256")
        for w, h in MAPPING_SIZES
    }
    out["any_clipped"] = any(
        c.get("clipped") for e in result["scale"] + result["translation"] for c in e["captures"]
    )
    out["readback_exact"] = all(
        e["assign"].get("readback_exact") for e in result["scale"] + result["translation"]
    )

    # Scale: foreground size relative to factor 1.0, divided by the factor (1.0 = proportional).
    scale_rows = []
    for w, h in MAPPING_SIZES:
        base = next(
            c
            for e in result["scale"]
            if e["factor"] == 1.0
            for c in e["captures"]
            if tuple(c["requested"]) == (w, h)
        )
        bw, bh = fg(base)["size"]
        bc = centre(base)
        for e in result["scale"]:
            cap = next(c for c in e["captures"] if tuple(c["requested"]) == (w, h))
            fw, fh = fg(cap)["size"]
            c = centre(cap)
            scale_rows.append(
                {
                    "size": f"{w}x{h}",
                    "factor": e["factor"],
                    "fg_size": [fw, fh],
                    "size_ratio_over_factor": [
                        round(fw / bw / e["factor"], 4),
                        round(fh / bh / e["factor"], 4),
                    ],
                    "centre": c,
                    "centre_shift_vs_factor_1": [round(c[0] - bc[0], 2), round(c[1] - bc[1], 2)],
                    "clipped": cap.get("clipped"),
                }
            )
    out["scale"] = scale_rows

    # Translation: centre shift (px) per metre for each scale and model axis.
    fits = []
    for s in TRANSLATION_SCALES:
        zero = next(e for e in result["translation"] if e["scale"] == s and e["axis"] is None)
        z = centre(zero["captures"][0])
        for axis in range(3):
            rows = [e for e in result["translation"] if e["scale"] == s and e["axis"] == axis]
            usable = [e for e in rows if e["captures"][0].get("clipped") is False]
            steps = [0.0] + [e["step"] for e in usable]
            cx = [0.0] + [centre(e["captures"][0])[0] - z[0] for e in usable]
            cy = [0.0] + [centre(e["captures"][0])[1] - z[1] for e in usable]
            fit: dict[str, Any] = {
                "scale": s,
                "axis": "xyz"[axis],
                "excluded_clipped": len(rows) - len(usable),
            }
            if len(usable) >= 2:
                fit["px_per_m_x"], fit["px_per_m_y"] = fit_line(steps, cx), fit_line(steps, cy)
            fits.append(fit)
    out["translation"] = fits
    return out


# --- documents ---------------------------------------------------------------------


def run_document_a_first(sw: Com, model: Com, probe: Probe) -> dict[str, Any]:
    est = probe.establish()
    camera = read_camera(model)
    return {
        "establish": est,
        "camera": camera,
        "windows": windows(sw, view_of(model)),
        "native": shot(sw, model, probe, camera, "A_cam-A", DOCUMENT_SIZES),
    }


def run_document_b(sw: Com, model: Com, probe: Probe, camera_a: dict[str, Any]) -> dict[str, Any]:
    est = probe.establish()
    camera_b = read_camera(model)
    return {
        "establish": est,
        "camera": camera_b,
        "windows": windows(sw, view_of(model)),
        "native": shot(sw, model, probe, camera_b, "B_cam-B", DOCUMENT_SIZES),
        "with_a": shot(sw, model, probe, camera_a, "B_cam-A", DOCUMENT_SIZES),
    }


def run_document_a_again(sw, model, probe, camera_a, camera_b) -> dict[str, Any]:
    return {
        "with_b": shot(sw, model, probe, camera_b, "A_cam-B", DOCUMENT_SIZES),
        "with_a_reopened": shot(sw, model, probe, camera_a, "A_cam-A_reopened", DOCUMENT_SIZES),
    }


def luma_edges(path: Path, limit: int) -> Image.Image:
    with Image.open(path) as image:
        luma = image.convert("L")
    return luma.point(lambda v, limit=limit: 255 if v <= limit else 0)


def count(mask: Image.Image) -> int:
    return mask.histogram()[255]


def shifted(mask: Image.Image, dx: int, dy: int) -> Image.Image:
    out = Image.new("L", mask.size, 0)
    out.paste(mask, (dx, dy))
    return out


def edge_overlap(a: Path, b: Path, limit: int) -> dict[str, Any]:
    """Coincident edge-line pixels between a and b shifted by (dx, dy)."""
    ea, eb = luma_edges(a, limit), luma_edges(b, limit)
    na, nb = count(ea), count(eb)
    grid = {
        (dx, dy): count(ImageChops.multiply(ea, shifted(eb, dx, dy)))
        for dy in range(-SHIFT_RANGE, SHIFT_RANGE + 1)
        for dx in range(-SHIFT_RANGE, SHIFT_RANGE + 1)
    }
    best = max(grid, key=grid.get)
    zero = grid[(0, 0)]
    runner_up = max(v for k, v in grid.items() if k != best)
    return {
        "edge_pixels": [na, nb],
        "zero_shift_overlap": zero,
        "zero_shift_share": [
            round(zero / na, 4) if na else None,
            round(zero / nb, 4) if nb else None,
        ],
        "best_shift": list(best),
        "best_overlap": grid[best],
        "best_over_runner_up": round(grid[best] / runner_up, 4) if runner_up else None,
        # A best shift on the edge of the searched range means no peak was found in it.
        "peak_inside_range": max(abs(best[0]), abs(best[1])) < SHIFT_RANGE,
        "grid": {f"{dx},{dy}": v for (dx, dy), v in grid.items()},
    }


def pixel_difference(a: Path, b: Path) -> dict[str, Any]:
    with Image.open(a) as ia, Image.open(b) as ib:
        diff = ImageChops.difference(ia.convert("RGB"), ib.convert("RGB"))
    r, g, bl = diff.split()
    delta = ImageChops.lighter(ImageChops.lighter(r, g), bl)
    out = {}
    for t in DIFF_THRESHOLDS:
        mask = delta.point(lambda v, t=t: 255 if v > t else 0)
        box = mask.getbbox()
        out[str(t)] = {
            "pixels": count(mask),
            "bbox": [box[0], box[1], box[2] - 1, box[3] - 1] if box else None,
        }
    return out


def compare_pair(name: str, a: dict[str, Any], b: dict[str, Any], out_dir: Path) -> list[dict]:
    rows = []
    for ca, cb in zip(a["captures"], b["captures"], strict=True):
        pa, pb = out_dir / f"{ca['name']}.png", out_dir / f"{cb['name']}.png"
        rows.append(
            {
                "pair": name,
                "size": f"{ca['requested'][0]}x{ca['requested'][1]}",
                "a": ca["name"],
                "b": cb["name"],
                "identical": raster(ca).get("pixel_sha256") == raster(cb).get("pixel_sha256"),
                "bounds": [fg(ca).get("bounds"), fg(cb).get("bounds")],
                "clipped": [ca.get("clipped"), cb.get("clipped")],
                "difference": attempt(lambda pa=pa, pb=pb: pixel_difference(pa, pb)),
                "edge_overlap": {
                    str(limit): attempt(
                        lambda pa=pa, pb=pb, limit=limit: edge_overlap(pa, pb, limit)
                    )
                    for limit in EDGE_LUMA
                },
            }
        )
    return rows


def analyse_documents(docs: dict[str, Any], out_dir: Path) -> list[dict[str, Any]]:
    a1, b, a2 = docs["a_first"]["result"], docs["b"]["result"], docs["a_again"]["result"]
    return (
        compare_pair(
            "control: A cam-A vs A cam-A after reopening",
            a1["native"],
            a2["with_a_reopened"],
            out_dir,
        )
        + compare_pair("shared camera C_A: A vs B", a1["native"], b["with_a"], out_dir)
        + compare_pair("shared camera C_B: A vs B", a2["with_b"], b["native"], out_dir)
        + compare_pair(
            "native per-document: A cam-A vs B cam-B", a1["native"], b["native"], out_dir
        )
    )


# --- Summaries ---------------------------------------------------------------------


def summarize_mapping(report: dict[str, Any]) -> list[str]:
    a = report.get("analysis", {})
    lines = [
        f"base camera repeatable start/end: {a.get('base_repeatable')}",
        f"camera readback exact everywhere: {a.get('readback_exact')}  any capture clipped: {a.get('any_clipped')}",
        "",
        "Scale2 sweep (orientation, translation held): size ratio / factor  (1.0 = proportional)",
    ]
    for row in a.get("scale", []):
        lines.append(
            f"  {row['size']:>9} x{row['factor']:<5} fg={row['fg_size']} ratio/factor={row['size_ratio_over_factor']}"
            f" centre shift vs x1.0={row['centre_shift_vs_factor_1']} clipped={row['clipped']}"
        )
    lines.append("")
    lines.append(
        f"Translation3 sweep at {TRANSLATION_SIZE}: centre shift in px per metre added to one component"
    )
    for row in a.get("translation", []):
        head = f"  Scale2 x{row['scale']:<4} axis {row['axis']} (clipped excluded: {row['excluded_clipped']}): "
        if "px_per_m_x" not in row:
            lines.append(head + "too few unclipped captures to fit")
            continue
        fx, fy = row["px_per_m_x"], row["px_per_m_y"]
        lines.append(
            head + f"dx/dm={fx['slope']:9.1f} (resid {fx['max_residual']:.2f}px)"
            f"  dy/dm={fy['slope']:9.1f} (resid {fy['max_residual']:.2f}px)"
        )
    return lines


def summarize_documents(report: dict[str, Any]) -> list[str]:
    lines = []
    cams = report.get("cameras", {})
    for key in ("A", "B"):
        cam = cams.get(key) or {}
        lines.append(
            f"camera C_{key} (isometric + Zoom to Fit): scale2={cam.get('scale2')!r} translation3={cam.get('translation3')}"
        )
    for row in report.get("pairs", []):
        lines.append("")
        lines.append(f"{row['pair']}  [{row['size']}]")
        diff = row["difference"].get("value", {})
        lines.append(
            f"  identical={row['identical']} clipped={row['clipped']} differing px (>0)={diff.get('0', {}).get('pixels')}"
            f" bbox={diff.get('0', {}).get('bbox')}"
        )
        lines.append(f"  bounds A={row['bounds'][0]} B={row['bounds'][1]}")
        for limit, result in row["edge_overlap"].items():
            e = result.get("value") or {}
            lines.append(
                f"  edges luma<={limit:>3}: best shift={e.get('best_shift')}"
                f" peak inside +/-{SHIFT_RANGE}px={e.get('peak_inside_range')}"
                f" best/runner-up={e.get('best_over_runner_up')}"
                f" zero-shift share of A,B={e.get('zero_shift_share')} edge px={e.get('edge_pixels')}"
            )
    return lines


def documents_safe(report: dict[str, Any]) -> list[str]:
    lines = []
    for key, doc in report.get("documents", {}).items():
        lines.append(
            f"{key}: {doc.get('file_name')} dirty start={doc.get('dirty_start')} end={doc.get('dirty_end')}"
            f" closed={doc.get('closed', {}).get('ok')}"
        )
    return lines


def summarize(report: dict[str, Any]) -> str:
    lines = [f"VJ-CAPTURE-03 {report.get('mode')}"]
    lines += (
        summarize_mapping(report)
        if report.get("mode") == "mapping"
        else summarize_documents(report)
    )
    lines.append("")
    lines += documents_safe(report)
    if "fatal" in report:
        lines.append(f"FATAL: {report['fatal']}")
    return "\n".join(lines)


# --- main ----------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    modes = parser.add_subparsers(dest="mode", required=True)
    mapping = modes.add_parser("mapping", help="Scale2 / Translation3 to raster mapping")
    mapping.add_argument("model", type=Path)
    mapping.add_argument("--camera", type=Path, default=DEFAULT_CAMERA, help="starting camera.json")
    documents = modes.add_parser("documents", help="one camera across two documents")
    documents.add_argument("model_a", type=Path)
    documents.add_argument("model_b", type=Path)
    for sub in (mapping, documents):
        sub.add_argument("--tag", required=True)
        sub.add_argument("--out", type=Path, default=Path("captures/capture-03"))
        sub.add_argument("--keep-bmp", action="store_true")
    args = parser.parse_args()

    paths = [args.model] if args.mode == "mapping" else [args.model_a, args.model_b]
    paths = [p.resolve() for p in paths]
    for p in paths:
        if not p.is_file():
            raise SystemExit(f"Model not found: {p}")
    out_dir = (args.out / args.tag).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    attempt(lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2))

    report: dict[str, Any] = {
        "probe": "VJ-CAPTURE-03",
        "mode": args.mode,
        "tag": args.tag,
        "started": datetime.now(UTC).isoformat(timespec="seconds"),
        "thresholds": list(THRESHOLDS),
        "documents": {},
    }
    sw = attach()
    try:
        if args.mode == "mapping":
            camera_file = json.loads(args.camera.read_text(encoding="utf-8"))
            camera = {k: camera_file[k] for k in ("orientation3", "translation3", "scale2")}
            report["camera_from"] = str(args.camera)
            doc = with_document(
                sw,
                paths[0],
                out_dir,
                args.keep_bmp,
                lambda model, probe: run_mapping(sw, model, probe, camera),
            )
            report["documents"]["model"] = doc
            report["mapping"] = doc.pop("result")
            report["analysis"] = analyse_mapping(report["mapping"])
        else:
            a, b = paths
            first = with_document(
                sw,
                a,
                out_dir,
                args.keep_bmp,
                lambda model, probe: run_document_a_first(sw, model, probe),
            )
            report["documents"]["a_first"] = first
            camera_a = first["result"]["camera"]
            second = with_document(
                sw,
                b,
                out_dir,
                args.keep_bmp,
                lambda model, probe: run_document_b(sw, model, probe, camera_a),
            )
            report["documents"]["b"] = second
            camera_b = second["result"]["camera"]
            third = with_document(
                sw,
                a,
                out_dir,
                args.keep_bmp,
                lambda model, probe: run_document_a_again(sw, model, probe, camera_a, camera_b),
            )
            report["documents"]["a_again"] = third
            report["cameras"] = {"A": camera_a, "B": camera_b}
            (out_dir / "cameras.json").write_text(
                json.dumps(report["cameras"], indent=2), encoding="utf-8"
            )
            report["pairs"] = analyse_documents(report["documents"], out_dir)
    except BaseException as exc:
        report["fatal"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        report["finished"] = datetime.now(UTC).isoformat(timespec="seconds")
        (out_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        summary = summarize(report)
        (out_dir / "summary.txt").write_text(summary + "\n", encoding="utf-8")
        print(summary)
        print(f"\nReport: {out_dir / 'report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
