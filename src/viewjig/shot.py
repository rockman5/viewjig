"""Single-document shot: frame one SOLIDWORKS document to a raster target and export it.

Sequence (V1):

1. Standard view, then native Zoom to Fit, used only to bootstrap a camera.
2. Bootstrap camera: the Zoom to Fit camera with Scale2 and Translation3 X/Y
   multiplied by ``BOOTSTRAP_SCALE``. Under the framing model this shrinks the
   image about its centre, which leaves room for calibration and keeps the first
   correction's scale ratio modest, so measurement rounding (amplified by that
   ratio) stays well inside the verification tolerance.
3. Capture and measure the bootstrap. Its foreground must be non-empty and clear
   of every edge. Its inferred background becomes the classification reference
   for every later capture in the shot.
4. Calibrate X and Y around the bootstrap camera (see ``_calibration_step``),
   requiring the SOLIDWORKS signs kx > 0 and ky < 0, then restore the bootstrap.
5. Solve, apply, capture, measure, verify. If the only failure is margins out of
   tolerance on a measurable, unclipped result, solve once more from that result.
   ``MAX_SOLVES`` is a hard limit; anything else fails with a ``ShotError``.

The orchestration talks to SOLIDWORKS only through a ``Renderer``. It never
calls COM itself, and offline tests drive it with a fake.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from PIL import Image

from viewjig import framing, sw
from viewjig.camera import Camera, CameraError, apply_camera, read_camera
from viewjig.framing import Observation
from viewjig.measure import Measurement, MeasurementError, measure
from viewjig.raster import RasterCaptureError, capture
from viewjig.sw import StandardView

# V1 orchestration constants.
BOOTSTRAP_SCALE = 0.7
DEFAULT_MARGIN_FRACTION = 0.05  # of the shorter output side, when no margin is given
MAX_SOLVES = 2
# Calibration step sizing: aim for this share of the shorter side of centre movement
# per step, but use at most CALIBRATION_ROOM_SHARE of the bootstrap margin on that axis.
CALIBRATION_FRACTION = 0.05
CALIBRATION_ROOM_SHARE = 0.5
# Pilot: Translation3 steps doubling from PILOT_START_M, for at most PILOT_MAX_STEPS
# captures, until the centre moves PILOT_MIN_PX. Centres move in 0.5 px steps, so
# 8 px bounds the pilot's estimate error at about 6 %.
PILOT_START_M = 1e-5
PILOT_MAX_STEPS = 20
PILOT_MIN_PX = 8.0
EXPECTED_SIGNS = {"x": 1, "y": -1}  # SOLIDWORKS: +X moves the image right, +Y moves it up

RECORD_FORMAT = "viewjig.shot"
RECORD_VERSION = 1


class ShotError(Exception):
    """A shot that could not be completed; ``record`` says how far it got and why."""

    def __init__(self, message: str, record: dict[str, Any]) -> None:
        super().__init__(message)
        self.record = record


class Renderer(Protocol):
    def show_standard_view(self, view: StandardView) -> None: ...

    def zoom_to_fit(self) -> None: ...

    def read_camera(self) -> Camera: ...

    def apply_camera(self, camera: Camera) -> Camera:
        """Apply and return the exact readback, or raise ``CameraError``."""
        ...

    def capture(self, width: int, height: int) -> Image.Image: ...


class DocumentRenderer:
    """``Renderer`` over an open document, via the production SOLIDWORKS boundary."""

    def __init__(self, document: sw.Document) -> None:
        self.document = document

    def show_standard_view(self, view: StandardView) -> None:
        self.document.show_standard_view(view)

    def zoom_to_fit(self) -> None:
        self.document.zoom_to_fit()
        self.document.redraw()

    def read_camera(self) -> Camera:
        return read_camera(self.document)

    def apply_camera(self, camera: Camera) -> Camera:
        return apply_camera(self.document, camera)

    def capture(self, width: int, height: int) -> Image.Image:
        return capture(self.document, width, height)


@dataclass(frozen=True)
class ShotRequest:
    path: Path
    view: StandardView
    width: int
    height: int
    margin_px: int | None = None  # None: DEFAULT_MARGIN_FRACTION of the shorter side
    threshold: int = 8
    tolerance_px: float = framing.DEFAULT_TOLERANCE_PX


@dataclass(frozen=True)
class ShotResult:
    image: Image.Image
    camera: Camera
    measurement: Measurement
    record: dict[str, Any]


def resolve_margin(width: int, height: int, margin_px: int | None) -> tuple[int, str]:
    if margin_px is None:
        return round(DEFAULT_MARGIN_FRACTION * min(width, height)), "default"
    if isinstance(margin_px, bool) or not isinstance(margin_px, int) or margin_px < 0:
        raise ValueError(f"margin must be a non-negative integer of pixels, got {margin_px!r}")
    return margin_px, "explicit"


def bootstrap_camera(fitted: Camera) -> Camera:
    tx, ty, tz = fitted.translation
    return Camera(
        fitted.orientation,
        (tx * BOOTSTRAP_SCALE, ty * BOOTSTRAP_SCALE, tz),
        fitted.scale * BOOTSTRAP_SCALE,
    )


def offset_camera(camera: Camera, axis: str, offset_m: float) -> Camera:
    tx, ty, tz = camera.translation
    t = (tx + offset_m, ty, tz) if axis == "x" else (tx, ty + offset_m, tz)
    return Camera(camera.orientation, t, camera.scale)


class _Shot:
    def __init__(self, renderer: Renderer, request: ShotRequest, record: dict[str, Any]) -> None:
        self.renderer = renderer
        self.request = request
        self.record = record
        self.background: tuple[int, int, int] | None = None
        self.captures = 0

    def shoot(self, camera: Camera) -> tuple[Image.Image, Measurement, Camera]:
        readback = self.renderer.apply_camera(camera)
        image = self.renderer.capture(self.request.width, self.request.height)
        self.captures += 1
        self.record["captures"] = self.captures
        m = measure(image, self.request.threshold, background=self.background)
        return image, m, readback

    def fail(self, message: str) -> ShotError:
        return ShotError(message, self.record)


def _calibration_step(shot: _Shot, axis: str, boot: Camera, base: Measurement) -> float:
    """Translation3 step (m) for calibrating ``axis``, sized from a bounded pilot.

    k is unknown before calibration, so a pilot doubles a tiny step until the centre
    moves at least PILOT_MIN_PX, giving a rough |k|. The step then aims for
    CALIBRATION_FRACTION of the shorter side of movement, capped at
    CALIBRATION_ROOM_SHARE of the bootstrap margin on that axis so the +/- step
    captures stay clear of the edges even if the pilot's estimate is somewhat off.
    """
    i = 0 if axis == "x" else 1
    centre = base.center[i]
    margins = base.margins.as_tuple()
    room = min(margins[i], margins[i + 2])
    wanted = CALIBRATION_FRACTION * min(shot.request.width, shot.request.height)
    target = min(wanted, CALIBRATION_ROOM_SHARE * room)
    # The pilot stops after moving at least PILOT_MIN_PX, having moved less than that
    # on the step before, so it can move up to about 2 * PILOT_MIN_PX; check before it runs.
    if 2 * target < framing.DEFAULT_MIN_DISPLACEMENT_PX or room <= 2 * PILOT_MIN_PX + 1:
        raise shot.fail(
            f"{axis}: bootstrap margin of {room} px leaves too little room to calibrate"
        )
    pilot: list[dict[str, Any]] = []
    shot.record["calibration"]["pilot"][axis] = pilot
    offset = PILOT_START_M
    for _ in range(PILOT_MAX_STEPS):
        _, m, _ = shot.shoot(offset_camera(boot, axis, offset))
        if m.is_empty or m.clipped:
            raise shot.fail(f"{axis} calibration pilot lost the foreground at {offset} m")
        moved = m.center[i] - centre
        pilot.append({"offset_m": offset, "moved_px": moved})
        if abs(moved) >= PILOT_MIN_PX:
            break
        offset *= 2
    else:
        raise shot.fail(
            f"{axis} calibration pilot never moved the image {PILOT_MIN_PX} px "
            f"(last offset {offset / 2} m)"
        )
    k_estimate = abs(moved) / offset
    step = target / k_estimate
    shot.record["calibration"]["steps"][axis] = {
        "k_estimate_abs": k_estimate,
        "target_px": target,
        "room_px": room,
        "step_m": step,
    }
    return step


def run_shot(renderer: Renderer, request: ShotRequest) -> ShotResult:
    """Run the V1 shot sequence; raise ``ShotError`` (with its record) on any failure."""
    margin, margin_source = resolve_margin(request.width, request.height, request.margin_px)
    record: dict[str, Any] = {
        "format": RECORD_FORMAT,
        "version": RECORD_VERSION,
        "status": "running",
        "error": None,
        "input": {"path": str(request.path), "view": request.view.name},
        "output": {"width": request.width, "height": request.height},
        "margin": {"px": margin, "source": margin_source},
        "threshold": request.threshold,
        "tolerance_px": request.tolerance_px,
        "constants": {
            "bootstrap_scale": BOOTSTRAP_SCALE,
            "default_margin_fraction": DEFAULT_MARGIN_FRACTION,
            "max_solves": MAX_SOLVES,
            "calibration_fraction": CALIBRATION_FRACTION,
            "calibration_room_share": CALIBRATION_ROOM_SHARE,
            "pilot_start_m": PILOT_START_M,
            "pilot_max_steps": PILOT_MAX_STEPS,
            "pilot_min_px": PILOT_MIN_PX,
            "expected_signs": EXPECTED_SIGNS,
        },
        "captures": 0,
        "calibration": {"pilot": {}, "steps": {}},
        "attempts": [],
    }
    shot = _Shot(renderer, request, record)
    try:
        return _run(shot, margin)
    except ShotError as exc:
        record["status"], record["error"] = "failed", str(exc)
        raise
    except (
        framing.FramingError,
        MeasurementError,
        CameraError,
        RasterCaptureError,
        sw.SolidWorksError,
    ) as exc:
        record["status"], record["error"] = "failed", f"{type(exc).__name__}: {exc}"
        raise ShotError(record["error"], record) from exc


def _run(shot: _Shot, margin: int) -> ShotResult:
    request, record, renderer = shot.request, shot.record, shot.renderer

    renderer.show_standard_view(request.view)
    renderer.zoom_to_fit()
    fitted = renderer.read_camera()
    boot = bootstrap_camera(fitted)
    record["zoom_to_fit_camera"] = fitted.to_dict()
    _, base, _ = shot.shoot(boot)
    record["bootstrap"] = {"camera": boot.to_dict(), "measurement": base.to_dict()}
    if base.is_empty:
        raise shot.fail("bootstrap capture has no foreground")
    if base.clipped:
        raise shot.fail(f"bootstrap foreground touches the image edge {base.touches_edge}")
    shot.background = base.background.color
    record["background"] = list(shot.background)

    observations: dict[str, list[Observation]] = {}
    for axis in ("x", "y"):
        step = _calibration_step(shot, axis, boot, base)
        obs = [Observation(0.0, base)]
        for offset in (-step, step):
            _, m, _ = shot.shoot(offset_camera(boot, axis, offset))
            obs.append(Observation(offset, m))
        observations[axis] = obs
    calibration = framing.Calibration(
        *(
            framing.calibrate_axis(axis, observations[axis], expected_sign=EXPECTED_SIGNS[axis])
            for axis in ("x", "y")
        )
    )
    record["calibration"]["result"] = calibration.to_dict()
    renderer.apply_camera(boot)  # restore the exact bootstrap camera

    camera, extent = boot, base
    for attempt in range(1, MAX_SOLVES + 1):
        solution = framing.solve(camera, extent, calibration, margin)
        image, measured, readback = shot.shoot(solution.camera)
        verification = framing.verify(solution, measured, request.tolerance_px)
        record["attempts"].append(
            {
                "attempt": attempt,
                "solution": solution.to_dict(),
                "readback": readback.to_dict(),
                "measurement": measured.to_dict(),
                "verification": verification.to_dict(),
            }
        )
        if verification.ok:
            record["status"] = "ok"
            record["final"] = {"camera": readback.to_dict(), "measurement": measured.to_dict()}
            return ShotResult(image, readback, measured, record)
        if measured.is_empty or measured.clipped:
            raise shot.fail(f"attempt {attempt} is not measurable: {verification.failures}")
        camera, extent = solution.camera, measured
    raise shot.fail(
        f"framing still outside tolerance after {MAX_SOLVES} solves: {verification.failures}"
    )


def write_record(record: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def shoot(request: ShotRequest, png_path: Path, record_path: Path) -> ShotResult:
    """Open ``request.path`` read-only in the running SOLIDWORKS, run the shot, export it.

    Writes the JSON record whether or not the shot succeeds; writes the PNG only on
    success, including the document closing cleanly.
    """
    record: dict[str, Any] = {"status": "failed", "input": {"path": str(request.path)}}
    try:
        app = sw.attach()
        with sw.open_document(app, request.path) as document:
            result = run_shot(DocumentRenderer(document), request)
            record = result.record
            record["document"] = {"title": document.title}
        record["document"]["dirty_at_close"] = document.dirty_at_close
    except ShotError as exc:
        write_record(exc.record, record_path)
        raise
    except sw.SolidWorksError as exc:
        record["status"], record["error"] = "failed", f"{type(exc).__name__}: {exc}"
        write_record(record, record_path)
        raise ShotError(record["error"], record) from exc
    png_path.parent.mkdir(parents=True, exist_ok=True)
    result.image.save(png_path, format="PNG")
    record["output"]["png"] = str(png_path)
    write_record(record, record_path)
    return result
