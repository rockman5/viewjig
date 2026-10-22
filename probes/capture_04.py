"""VJ-CAPTURE-04: how close does one predicted camera correction land?

This is an experimental probe, not production ViewJig code. It tests the
camera-to-raster model from VJ-CAPTURE-03 (probes/capture_03_findings.md, I1)
predictively. From measured rasters, it calculates one shared camera that
should frame a two-document capture set to a raster-space target, applies it
once, and measures how far the result lands from the prediction. There is
deliberately no correction loop: the residual after one correction is the
result.

Model (capture-03 I1). For a fixed orientation and output size, with Scale2
changing from S0 to S1 = s * S0 and Translation3 from T0 to T1, every image
point moves as

    p1 = O1 + s * (p0 - O0),     O = C + (kx * Tx, ky * Ty)

where C is the image centre, O is where the model origin lands, and kx, ky are
the image shift per metre of Translation3 X and Y (sign included), measured
here by calibration. Translation3 Z is left unchanged (no raster effect,
capture-03 M-O5).

Target, entirely in raster space. Combined foreground edges, as continuous
pixel edges [left - 0.5, right + 0.5], are fitted inside the output with the
test margin m on all four sides: s = min((W - 2m) / w0, (H - 2m) / h0). The
limiting axis therefore lands on m and the other axis gets more. The combined
centre is placed on C, giving O1 = C - s * (c0 - O0) and T1 = (O1 - C) / k.
The margin is an experiment parameter, not a ViewJig default.

Per view (standard isometric and standard Front) and output size:
  1. Bootstrap: standard view + Zoom to Fit on each document; take the fit
     that zooms out further (smaller Scale2), then Scale2 and Translation3
     X/Y x 0.7, which under the model shrinks the image about the image
     centre. The set starts too small, off-centre by Zoom to Fit's own offset
     and the documents' different extents, and clear of every edge so the
     calibration steps do not clip. It is only a starting camera; everything
     the calculation uses is measured from the rasters.
  2. Capture A and B with the bootstrap camera; measure the combined bounds.
  3. Calibrate on A: Translation3 X and Y stepped by +/- delta.
  4. Calculate the corrected camera; record every intermediate value.
  5. Apply it once to A and B; measure margins, centre, clipping, and the
     capture-03 edge-overlap registration test.

Documents are opened one at a time (B to fit, A, B, then A again), read-only,
and closed; capture-02 showed the camera persists across that.

Usage (Windows, SOLIDWORKS running, fixtures closed):

    .venv\\Scripts\\python probes\\capture_04.py fixtures\\vj-f01.SLDPRT ^
        fixtures\\vj-f01b.SLDPRT --tag predict

Output goes to captures/capture-04/<tag>/ (git-ignored).
"""

from __future__ import annotations

import argparse
import ctypes
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from capture_01 import THRESHOLDS, VIEW_FRONT, VIEW_ISOMETRIC, Com, Probe, attach, attempt
from capture_02 import assign_camera, fg, read_camera
from capture_03 import EDGE_LUMA, clipped, edge_overlap, with_camera, with_document

VIEWS = {"isometric": VIEW_ISOMETRIC, "front": VIEW_FRONT}
SIZES = [(1600, 1200), (1000, 1000)]
BOOTSTRAP_SCALE = 0.7
CALIBRATION_STEP = 0.008  # metres; ~45 px at 1600x1200 per capture-03
DEFAULT_MARGIN = 60  # px; experiment parameter only


# --- Raster geometry ----------------------------------------------------------------


def edges(cap: dict[str, Any]) -> list[float] | None:
    """Continuous foreground edges [left, top, right, bottom] from inclusive pixel bounds."""
    bounds = fg(cap).get("bounds")
    if not bounds:
        return None
    left, top, right, bottom = bounds
    return [left - 0.5, top - 0.5, right + 0.5, bottom + 0.5]


def combine(boxes: list[list[float]]) -> list[float]:
    return [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]


def margins(box: list[float], size: tuple[int, int]) -> list[float]:
    """Left, top, right, bottom distances from the box to the image edges, in px."""
    w, h = size
    return [box[0] + 0.5, box[1] + 0.5, (w - 0.5) - box[2], (h - 0.5) - box[3]]


def box_centre(box: list[float]) -> list[float]:
    return [(box[0] + box[2]) / 2, (box[1] + box[3]) / 2]


def image_centre(size: tuple[int, int]) -> list[float]:
    return [(size[0] - 1) / 2, (size[1] - 1) / 2]


def calibrate(steps: dict[str, dict[str, Any]], delta: float) -> dict[str, Any]:
    """Image shift per metre of Translation3 X and Y, from +/- delta steps on one document."""
    c = {key: box_centre(edges(cap)) for key, cap in steps.items()}
    kx = (c["x+"][0] - c["x-"][0]) / (2 * delta)
    ky = (c["y+"][1] - c["y-"][1]) / (2 * delta)
    return {
        "centres": c,
        # A clipped step moves the measured centre less than the model: k would be wrong.
        "clipped": [key for key, cap in steps.items() if cap["clipped"]],
        "kx": kx,
        "ky": ky,
        # Bounds centres are quantized to 0.5 px, so each k is only resolved to about
        # 0.5 px over the 2 * delta span.
        "relative_resolution": [
            0.5 / abs(kx * 2 * delta) if kx else None,
            0.5 / abs(ky * 2 * delta) if ky else None,
        ],
        # Off-axis movement; capture-03 M-O3 found exactly 0.
        "x_step_moves_y_per_m": (c["x+"][1] - c["x-"][1]) / (2 * delta),
        "y_step_moves_x_per_m": (c["y+"][0] - c["y-"][0]) / (2 * delta),
    }


def solve(
    bootstrap: dict[str, Any],
    combined: list[float],
    per_document: dict[str, list[float]],
    kx: float,
    ky: float,
    size: tuple[int, int],
    margin: float,
) -> dict[str, Any]:
    """One predicted correction: the camera and the raster it should produce."""
    w, h = size
    cx, cy = image_centre(size)
    tx0, ty0, tz0 = bootstrap["translation3"]
    o0 = [cx + kx * tx0, cy + ky * ty0]
    w0, h0 = combined[2] - combined[0], combined[3] - combined[1]
    fit_w, fit_h = (w - 2 * margin) / w0, (h - 2 * margin) / h0
    s = min(fit_w, fit_h)
    c0 = box_centre(combined)
    o1 = [cx - s * (c0[0] - o0[0]), cy - s * (c0[1] - o0[1])]
    camera = with_camera(bootstrap)
    camera["scale2"] = bootstrap["scale2"] * s
    camera["translation3"] = [(o1[0] - cx) / kx, (o1[1] - cy) / ky, tz0]

    def move(box: list[float]) -> list[float]:
        return [
            o1[0] + s * (box[0] - o0[0]),
            o1[1] + s * (box[1] - o0[1]),
            o1[0] + s * (box[2] - o0[0]),
            o1[1] + s * (box[3] - o0[1]),
        ]

    predicted = move(combined)
    # If the true k differs from the calibrated k by a fraction e, every predicted point
    # lands off by e * [(O1 - C) - s * (O0 - C)]. Recorded so residuals can be compared
    # with what calibration error alone would explain.
    lever = [(o1[0] - cx) - s * (o0[0] - cx), (o1[1] - cy) - s * (o0[1] - cy)]
    return {
        "image_centre": [cx, cy],
        "origin_before": o0,
        "origin_after": o1,
        "combined_size_before": [w0, h0],
        "scale_ratio": s,
        "limiting_axis": "width" if fit_w <= fit_h else "height",
        "k_error_lever_px": lever,
        "px_per_1pct_k_error": [abs(v) * 0.01 for v in lever],
        "camera": camera,
        "predicted_combined_edges": predicted,
        "predicted_margins": margins(predicted, size),
        "predicted_document_edges": {k: move(v) for k, v in per_document.items()},
    }


# --- Experiment -----------------------------------------------------------------------


def shoot(sw: Com, model: Com, probe: Probe, camera: dict, name: str, size) -> dict[str, Any]:
    assigned = assign_camera(sw, model, camera)
    cap = probe.capture(name, *size)
    cap["clipped"] = clipped(cap)
    return {"camera": camera, "assign": assigned, "capture": cap}


def phase_fit(model: Com, probe: Probe) -> dict[str, Any]:
    """Standard view + Zoom to Fit camera for each view, on one document."""
    out: dict[str, Any] = {}
    for view, view_id in VIEWS.items():
        establish = probe.establish(view_id)
        out[view] = {"establish": establish, "camera": read_camera(model)}
    return out


def phase_a_bootstrap(sw: Com, model: Com, probe: Probe, fits_b: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for view, view_id in VIEWS.items():
        entry: dict[str, Any] = {"establish": probe.establish(view_id)}
        fit_a, fit_b = read_camera(model), fits_b[view]["camera"]
        # Start from the fit that zooms out further, so the bootstrap does not clip either
        # member (capture-03 I4). Orientation is the same standard view in both.
        source = "A" if fit_a["scale2"] <= fit_b["scale2"] else "B"
        fitted = fit_a if source == "A" else fit_b
        bootstrap = with_camera(fitted, scale=BOOTSTRAP_SCALE)
        tx, ty, tz = fitted["translation3"]
        bootstrap["translation3"] = [tx * BOOTSTRAP_SCALE, ty * BOOTSTRAP_SCALE, tz]
        entry["zoom_to_fit_camera"] = {"A": fit_a, "B": fit_b}
        entry["bootstrap_source"] = source
        entry["bootstrap_camera"] = bootstrap
        entry["sizes"] = {}
        for size in SIZES:
            tag = f"{view}_{size[0]}x{size[1]}"
            steps = {
                "x+": [CALIBRATION_STEP, 0.0, 0.0],
                "x-": [-CALIBRATION_STEP, 0.0, 0.0],
                "y+": [0.0, CALIBRATION_STEP, 0.0],
                "y-": [0.0, -CALIBRATION_STEP, 0.0],
            }
            entry["sizes"][tag] = {
                "bootstrap": shoot(sw, model, probe, bootstrap, f"{tag}_A_bootstrap", size),
                "calibration": {
                    key: shoot(
                        sw, model, probe, with_camera(bootstrap, delta=d), f"{tag}_A_cal{key}", size
                    )
                    for key, d in steps.items()
                },
            }
        out[view] = entry
    return out


def phase_b(sw: Com, model: Com, probe: Probe, a: dict, margin: float) -> dict[str, Any]:
    """Bootstrap capture of B, the calculation, and B under the corrected camera."""
    out: dict[str, Any] = {}
    for view in VIEWS:
        bootstrap = a[view]["bootstrap_camera"]
        for size in SIZES:
            tag = f"{view}_{size[0]}x{size[1]}"
            a_case = a[view]["sizes"][tag]
            b_boot = shoot(sw, model, probe, bootstrap, f"{tag}_B_bootstrap", size)
            calibration = calibrate(
                {k: v["capture"] for k, v in a_case["calibration"].items()}, CALIBRATION_STEP
            )
            per_doc = {"A": edges(a_case["bootstrap"]["capture"]), "B": edges(b_boot["capture"])}
            combined = combine(list(per_doc.values()))
            bootstrap_clipped = (
                a_case["bootstrap"]["capture"]["clipped"] or b_boot["capture"]["clipped"]
            )
            case: dict[str, Any] = {
                "view": view,
                "size": list(size),
                "margin": margin,
                "bootstrap_source": a[view]["bootstrap_source"],
                "bootstrap_camera": bootstrap,
                "B_bootstrap": b_boot,
                "bootstrap_edges": per_doc,
                "bootstrap_combined_edges": combined,
                "bootstrap_margins": margins(combined, size),
                "bootstrap_clipped": bootstrap_clipped,
                "calibration": calibration,
            }
            if bootstrap_clipped:
                case["skipped"] = "bootstrap capture clipped; combined bounds are not measurable"
            elif calibration["clipped"]:
                case["skipped"] = (
                    f"calibration steps clipped ({calibration['clipped']}); k is not measurable"
                )
            else:
                case["prediction"] = solve(
                    bootstrap, combined, per_doc, calibration["kx"], calibration["ky"], size, margin
                )
                case["B_corrected"] = shoot(
                    sw, model, probe, case["prediction"]["camera"], f"{tag}_B_corrected", size
                )
            out[tag] = case
    return out


def phase_a_corrected(sw: Com, model: Com, probe: Probe, cases: dict) -> dict[str, Any]:
    return {
        tag: shoot(
            sw,
            model,
            probe,
            case["prediction"]["camera"],
            f"{tag}_A_corrected",
            tuple(case["size"]),
        )
        for tag, case in cases.items()
        if "prediction" in case
    }


def evaluate(cases: dict, a_first: dict, a_corrected: dict, out_dir: Path) -> None:
    """Measured result after the single correction, against the prediction."""
    for tag, case in cases.items():
        if "prediction" not in case:
            continue
        a_cap, b_cap = a_corrected[tag]["capture"], case["B_corrected"]["capture"]
        size = tuple(case["size"])
        measured = {"A": edges(a_cap), "B": edges(b_cap)}
        combined = combine(list(measured.values()))
        got, want = margins(combined, size), case["prediction"]["predicted_margins"]
        view = case["view"]
        a_boot = a_first[view]["sizes"][tag]["bootstrap"]["capture"]
        case["result"] = {
            "A_corrected": a_corrected[tag],
            "readback_exact": [
                a_corrected[tag]["assign"].get("readback_exact"),
                case["B_corrected"]["assign"].get("readback_exact"),
            ],
            "clipped": [a_cap["clipped"], b_cap["clipped"]],
            "measured_edges": measured,
            "measured_combined_edges": combined,
            "measured_margins": got,
            "margin_residual": [round(g - p, 3) for g, p in zip(got, want, strict=True)],
            "centre_residual": [
                round(c - i, 3)
                for c, i in zip(box_centre(combined), image_centre(size), strict=True)
            ],
            "document_edge_residual": {
                k: [
                    round(m - p, 3)
                    for m, p in zip(
                        measured[k], case["prediction"]["predicted_document_edges"][k], strict=True
                    )
                ]
                for k in ("A", "B")
            },
            "registration_after": registration(out_dir, a_cap, b_cap),
            "registration_bootstrap": registration(out_dir, a_boot, case["B_bootstrap"]["capture"]),
        }


def registration(out_dir: Path, a_cap: dict[str, Any], b_cap: dict[str, Any]) -> dict[str, Any]:
    """Capture-03's edge-overlap shift test for one image pair, at each luminance limit."""
    a_png, b_png = out_dir / f"{a_cap['name']}.png", out_dir / f"{b_cap['name']}.png"
    return {
        str(limit): attempt(lambda limit=limit: edge_overlap(a_png, b_png, limit))
        for limit in EDGE_LUMA
    }


# --- Summary ---------------------------------------------------------------------------


def fmt(values: list[float] | None, digits: int = 1) -> str:
    return "None" if values is None else "[" + ", ".join(f"{v:.{digits}f}" for v in values) + "]"


def summarize(report: dict[str, Any]) -> str:
    lines = [f"VJ-CAPTURE-04 one predicted correction (margin {report.get('margin')} px)"]
    for tag, case in report.get("cases", {}).items():
        cal = case["calibration"]
        lines.append("")
        lines.append(
            f"[{tag}] bootstrap from fit of {case.get('bootstrap_source')}"
            f" x{report.get('bootstrap_scale')}: Scale2={case['bootstrap_camera']['scale2']:.6f}"
            f" combined margins L,T,R,B={fmt(case['bootstrap_margins'])} clipped={case['bootstrap_clipped']}"
        )
        lines.append(
            f"  calibration kx={cal['kx']:.1f} ky={cal['ky']:.1f} px/m"
            f"  off-axis {cal['x_step_moves_y_per_m']:.1f}, {cal['y_step_moves_x_per_m']:.1f} px/m"
            f"  resolution ~{fmt([100 * v for v in cal['relative_resolution']], 2)} %"
            f"  clipped steps {cal['clipped']}"
        )
        if "prediction" not in case:
            lines.append(f"  SKIPPED: {case.get('skipped')}")
            continue
        p, r = case["prediction"], case.get("result", {})
        lines.append(
            f"  predicted: scale ratio {p['scale_ratio']:.5f} ({p['limiting_axis']}-limited)"
            f" Scale2={p['camera']['scale2']:.6f} T={fmt(p['camera']['translation3'], 6)}"
        )
        lines.append(
            f"  predicted margins L,T,R,B={fmt(p['predicted_margins'])}"
            f"  (each 1% k error would move the result {fmt(p['px_per_1pct_k_error'])} px)"
        )
        if not r:
            continue
        lines.append(f"  measured  margins L,T,R,B={fmt(r['measured_margins'])}")
        lines.append(
            f"  residual  margins L,T,R,B={fmt(r['margin_residual'])}  centre x,y={fmt(r['centre_residual'])}"
        )
        lines.append(
            f"  per-document edge residual A={fmt(r['document_edge_residual']['A'])} B={fmt(r['document_edge_residual']['B'])}"
        )
        lines.append(f"  clipped A,B={r['clipped']} readback exact A,B={r['readback_exact']}")
        for label, key in (
            ("after", "registration_after"),
            ("bootstrap", "registration_bootstrap"),
        ):
            e = (r[key].get("64") or {}).get("value") or {}
            lines.append(
                f"  registration {label:<9} (luma<=64): best shift={e.get('best_shift')}"
                f" best/runner-up={e.get('best_over_runner_up')} zero-shift share={e.get('zero_shift_share')}"
            )
    lines.append("")
    for key, doc in report.get("documents", {}).items():
        lines.append(
            f"{key}: {doc.get('file_name')} dirty start={doc.get('dirty_start')} end={doc.get('dirty_end')}"
            f" closed={doc.get('closed', {}).get('ok')}"
        )
    if "fatal" in report:
        lines.append(f"FATAL: {report['fatal']}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("model_a", type=Path)
    parser.add_argument("model_b", type=Path)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--margin", type=float, default=DEFAULT_MARGIN, help="test margin in px")
    parser.add_argument("--out", type=Path, default=Path("captures/capture-04"))
    parser.add_argument("--keep-bmp", action="store_true")
    args = parser.parse_args()

    a, b = args.model_a.resolve(), args.model_b.resolve()
    for p in (a, b):
        if not p.is_file():
            raise SystemExit(f"Model not found: {p}")
    out_dir = (args.out / args.tag).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    attempt(lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2))

    report: dict[str, Any] = {
        "probe": "VJ-CAPTURE-04",
        "tag": args.tag,
        "started": datetime.now(UTC).isoformat(timespec="seconds"),
        "thresholds": list(THRESHOLDS),
        "margin": args.margin,
        "bootstrap_scale": BOOTSTRAP_SCALE,
        "calibration_step": CALIBRATION_STEP,
        "documents": {},
    }
    sw = attach()
    try:
        fit_b = with_document(sw, b, out_dir, args.keep_bmp, lambda m, pr: phase_fit(m, pr))
        report["documents"]["b_fit"] = fit_b
        fits_b = fit_b.pop("result")
        report["fits_b"] = fits_b
        first = with_document(
            sw, a, out_dir, args.keep_bmp, lambda m, pr: phase_a_bootstrap(sw, m, pr, fits_b)
        )
        report["documents"]["a_first"] = first
        second = with_document(
            sw,
            b,
            out_dir,
            args.keep_bmp,
            lambda m, pr: phase_b(sw, m, pr, first["result"], args.margin),
        )
        report["documents"]["b"] = second
        cases = second.pop("result")
        third = with_document(
            sw, a, out_dir, args.keep_bmp, lambda m, pr: phase_a_corrected(sw, m, pr, cases)
        )
        report["documents"]["a_again"] = third
        a_corrected = third.pop("result")
        evaluate(cases, first["result"], a_corrected, out_dir)
        report["bootstrap"] = first.pop("result")
        report["cases"] = cases
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
