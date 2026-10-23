"""Single-document shot: frame one SOLIDWORKS document to a raster target and export it.

Sequence (V1):

1. Standard view, then native Zoom to Fit, used only to bootstrap a camera.
2. Bootstrap camera (``steps.bootstrap_camera``): the Zoom to Fit camera shrunk
   about the image centre.
3. Capture and measure the bootstrap. If the model is not fully in view with room
   to calibrate, zoom farther out and retry (``steps.find_bootstrap``, bounded).
   The accepted bootstrap's inferred background becomes the classification
   reference for every later capture in the shot.
4. Calibrate X and Y around the bootstrap camera (``steps.calibrate_around``),
   requiring the SOLIDWORKS signs kx > 0 and ky < 0, then restore the bootstrap.
5. Solve, apply, capture, measure, verify. If the only failure is margins out of
   tolerance on a measurable, unclipped result, solve once more from that result.
   ``MAX_SOLVES`` is a hard limit; anything else fails with a ``ShotError``.

The orchestration talks to SOLIDWORKS only through a ``Renderer``. It never
calls COM itself, and offline tests drive it with a fake.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from viewjig import framing, sw
from viewjig.camera import Camera
from viewjig.environment import (
    ApplicationBackground,
    DocumentBackground,
    application_background,
)
from viewjig.measure import BackgroundUncertain, Measurement, measure
from viewjig.steps import (
    BOOTSTRAP_SCALE,
    CALIBRATION_FRACTION,
    CALIBRATION_ROOM_SHARE,
    CAPTURE_ERRORS,
    DEFAULT_MARGIN_FRACTION,
    EXPECTED_SIGNS,
    MAX_SOLVES,
    PILOT_MAX_STEPS,
    PILOT_MIN_PX,
    PILOT_START_M,
    CaptureError,
    DocumentRenderer,
    Renderer,
    bootstrap_camera,
    calibrate_around,
    constants,
    environment_record,
    failure_record,
    find_bootstrap,
    offset_camera,
    open_renderer,
    resolve_margin,
    room_problem,
    visibility_problem,
    write_record,
)
from viewjig.sw import StandardView

__all__ = [
    "BOOTSTRAP_SCALE",
    "CALIBRATION_FRACTION",
    "CALIBRATION_ROOM_SHARE",
    "DEFAULT_MARGIN_FRACTION",
    "EXPECTED_SIGNS",
    "MAX_SOLVES",
    "PILOT_MAX_STEPS",
    "PILOT_MIN_PX",
    "PILOT_START_M",
    "DocumentRenderer",
    "Renderer",
    "ShotError",
    "ShotRequest",
    "ShotResult",
    "bootstrap_camera",
    "offset_camera",
    "resolve_margin",
    "run_shot",
    "shoot",
    "write_record",
]

Opener = Callable[[Path], AbstractContextManager[Renderer]]

RECORD_FORMAT = "viewjig.shot"
RECORD_VERSION = 1


class ShotError(CaptureError):
    """A shot that could not be completed; ``record`` says how far it got and why."""


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
        "constants": constants(),
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
    except CAPTURE_ERRORS as exc:
        record["status"], record["error"] = "failed", f"{type(exc).__name__}: {exc}"
        raise ShotError(record["error"], record) from exc


def _run(shot: _Shot, margin: int) -> ShotResult:
    request, record, renderer = shot.request, shot.record, shot.renderer

    renderer.show_standard_view(request.view)
    renderer.zoom_to_fit()
    fitted = renderer.read_camera()
    record["zoom_to_fit_camera"] = fitted.to_dict()
    shorter = min(request.width, request.height)
    accepted: dict[str, Measurement] = {}

    def try_bootstrap(camera: Camera) -> tuple[str | None, dict[str, Any]]:
        try:
            _, m, _ = shot.shoot(camera)
        except BackgroundUncertain as exc:
            return "the background could not be identified", {"error": str(exc)}
        problem = visibility_problem(m) or room_problem(m, shorter)
        if problem is None:
            accepted["base"] = m
        return problem, {"measurement": m.to_dict()}

    record["bootstrap_attempts"] = []
    boot = find_bootstrap(
        fitted, try_bootstrap, record["bootstrap_attempts"], shot.fail, "the part"
    )
    base = accepted["base"]
    record["bootstrap"] = {"camera": boot.to_dict(), "measurement": base.to_dict()}
    shot.background = base.background.color
    record["background"] = list(shot.background)

    calibration = calibrate_around(
        lambda camera: shot.shoot(camera)[1],
        boot,
        base,
        min(request.width, request.height),
        record["calibration"],
        shot.fail,
    )
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


def shoot(
    request: ShotRequest,
    png_path: Path,
    record_path: Path,
    *,
    app: sw.Com | None = None,
    opener: Opener | None = None,
) -> ShotResult:
    """Open ``request.path`` read-only in the running SOLIDWORKS, run the shot, export it.

    The capture runs on a controlled plain white background: SOLIDWORKS is routed
    to document scene backgrounds for the run if needed (``application_background``)
    and the document's scene is made white for the session (``open_renderer``).
    Both are restored and read back.

    Writes the JSON record whether or not the shot succeeds; writes the PNG only on
    success, including the document closing cleanly and every setting restored.
    ``app`` and ``opener`` replace the SOLIDWORKS connection and document opener
    (used by tests).
    """
    record: dict[str, Any] = {"status": "failed", "input": {"path": str(request.path)}}
    app_env: ApplicationBackground | None = None
    documents: list[DocumentBackground] = []
    try:
        if app is None:
            app = sw.attach()
        if opener is None:
            connected = app

            def opener(path: Path) -> AbstractContextManager[Renderer]:
                return open_renderer(connected, path, documents)

        with application_background(app) as app_env, opener(request.path) as renderer:
            result = run_shot(renderer, request)
            record = result.record
            document = getattr(renderer, "document", None)
        if document is not None:
            record["document"] = {
                "title": document.title,
                "dirty_at_close": document.dirty_at_close,
            }
    except (ShotError, sw.SolidWorksError) as exc:
        record = failure_record(exc, record)
        record["capture_environment"] = environment_record(app_env, documents)
        write_record(record, record_path)
        if isinstance(exc, ShotError):
            raise
        raise ShotError(record["error"], record) from exc
    record["capture_environment"] = environment_record(app_env, documents)
    png_path.parent.mkdir(parents=True, exist_ok=True)
    result.image.save(png_path, format="PNG")
    record["output"]["png"] = str(png_path)
    write_record(record, record_path)
    return result
