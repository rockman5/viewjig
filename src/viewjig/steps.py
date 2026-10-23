"""Orchestration steps shared by ``shot`` and ``pair``.

The V1 constants, the renderer interface, the bootstrap camera, margin
resolution, and the bounded calibration that sizes its own Translation3 step
from the raster. Nothing here decides what a shot or a pair is; those modules
sequence these steps.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

from PIL import Image

from viewjig import framing, sw
from viewjig.camera import Camera, CameraError, apply_camera, read_camera
from viewjig.environment import (
    CONTROLLED_BACKGROUND,
    ApplicationBackground,
    DocumentBackground,
    document_background,
)
from viewjig.framing import Calibration, Observation
from viewjig.measure import Measurement, MeasurementError
from viewjig.raster import RasterCaptureError, capture
from viewjig.sw import StandardView

# V1 orchestration constants.
BOOTSTRAP_SCALE = 0.7
# Bootstrap recovery: if a bootstrap capture is not measurable (the model runs off the
# frame, covers it entirely, or leaves no room to calibrate), retry with the image
# halved again about its centre. Each retry doubles the extent that fits; the first
# attempt is the plain BOOTSTRAP_SCALE bootstrap. Five attempts reach 0.7 / 16 of Zoom
# to Fit, enough for a part Zoom to Fit leaves about 23 times too large.
BOOTSTRAP_RETRY_SHRINK = 0.5
BOOTSTRAP_MAX_ATTEMPTS = 5
BOOTSTRAP_FACTORS = tuple(
    BOOTSTRAP_SCALE * BOOTSTRAP_RETRY_SHRINK**i for i in range(BOOTSTRAP_MAX_ATTEMPTS)
)
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

# Errors from the lower layers that end a shot or pair with a structured failure.
CAPTURE_ERRORS = (
    framing.FramingError,
    MeasurementError,
    CameraError,
    RasterCaptureError,
    sw.SolidWorksError,
)


class CaptureError(Exception):
    """A shot or pair that could not be completed; ``record`` says how far it got and why."""

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


@contextmanager
def open_renderer(
    app: sw.Com, path: Path, environments: list[DocumentBackground] | None = None
) -> Iterator[DocumentRenderer]:
    """A renderer over ``path``, opened read-only and on a plain white background.

    The document's scene background is made plain white for the block and restored
    before the document closes (see ``environment.document_background``). Each
    session's environment is appended to ``environments`` when given.
    """
    with sw.open_document(app, path) as document, document_background(document) as env:
        if environments is not None:
            environments.append(env)
        yield DocumentRenderer(document)


def environment_record(
    application: ApplicationBackground | None, documents: list[DocumentBackground]
) -> dict[str, Any]:
    """The capture-environment section of a record: what was changed and whether it was undone."""
    sessions: dict[str, dict[str, Any]] = {}
    for env in documents:
        entry = sessions.setdefault(
            env.document,
            {"original_scene": env.original_scene, "sessions": 0, "all_restored": True},
        )
        entry["sessions"] += 1
        entry["all_restored"] = entry["all_restored"] and env.restored is True
    return {
        "background": CONTROLLED_BACKGROUND,
        "application": None if application is None else application.to_dict(),
        "documents": sessions,
    }


def constants() -> dict[str, Any]:
    return {
        "bootstrap_scale": BOOTSTRAP_SCALE,
        "bootstrap_retry_shrink": BOOTSTRAP_RETRY_SHRINK,
        "bootstrap_max_attempts": BOOTSTRAP_MAX_ATTEMPTS,
        "default_margin_fraction": DEFAULT_MARGIN_FRACTION,
        "max_solves": MAX_SOLVES,
        "calibration_fraction": CALIBRATION_FRACTION,
        "calibration_room_share": CALIBRATION_ROOM_SHARE,
        "pilot_start_m": PILOT_START_M,
        "pilot_max_steps": PILOT_MAX_STEPS,
        "pilot_min_px": PILOT_MIN_PX,
        "expected_signs": EXPECTED_SIGNS,
    }


def resolve_margin(width: int, height: int, margin_px: int | None) -> tuple[int, str]:
    if margin_px is None:
        return round(DEFAULT_MARGIN_FRACTION * min(width, height)), "default"
    if isinstance(margin_px, bool) or not isinstance(margin_px, int) or margin_px < 0:
        raise ValueError(f"margin must be a non-negative integer of pixels, got {margin_px!r}")
    return margin_px, "explicit"


def bootstrap_camera(fitted: Camera, factor: float = BOOTSTRAP_SCALE) -> Camera:
    """Scale2 and Translation3 X/Y x ``factor``: shrinks the image about its centre.

    With the default factor that leaves room to calibrate and keeps the first
    correction's scale ratio modest, so measurement rounding (amplified by that
    ratio) stays well inside the verification tolerance.
    """
    tx, ty, tz = fitted.translation
    return Camera(fitted.orientation, (tx * factor, ty * factor, tz), fitted.scale * factor)


def _edges_text(m: Measurement) -> str:
    sides = [n for n, t in zip(("left", "top", "right", "bottom"), m.touches_edge.as_tuple()) if t]
    if len(sides) == 1:
        return f"the {sides[0]} edge"
    return "the " + ", ".join(sides[:-1]) + f" and {sides[-1]} edges"


def visibility_problem(m: Measurement) -> str | None:
    """Why ``m`` cannot be framed from, in plain words, or None if the model is fully in view."""
    if m.is_empty:
        return "nothing is visible"
    if m.clipped:
        return f"the model runs off {_edges_text(m)}"
    return None


def calibration_room_ok(room: int, shorter_side: int) -> bool:
    """Whether a bootstrap margin of ``room`` px leaves enough movement to calibrate."""
    # The pilot stops after moving at least PILOT_MIN_PX, having moved less than that
    # on the step before, so it can move up to about 2 * PILOT_MIN_PX.
    target = min(CALIBRATION_FRACTION * shorter_side, CALIBRATION_ROOM_SHARE * room)
    return 2 * target >= framing.DEFAULT_MIN_DISPLACEMENT_PX and room > 2 * PILOT_MIN_PX + 1


def room_problem(m: Measurement, shorter_side: int) -> str | None:
    for axis in ("x", "y"):
        room = calibration_room(m, axis)
        if not calibration_room_ok(room, shorter_side):
            return f"the model is too close to the image edge to calibrate ({room} px of room)"
    return None


def find_bootstrap(
    fitted: Camera,
    try_camera: Callable[[Camera], tuple[str | None, dict[str, Any]]],
    attempts: list[dict[str, Any]],
    fail: Callable[[str], Exception],
    subject: str,
) -> Camera:
    """Return the first bootstrap camera, zooming farther out each time, that is usable.

    ``try_camera`` captures with the camera and returns ``(problem, details)``:
    ``problem`` is None when the capture is usable, else a plain-words reason. Every
    attempt is appended to ``attempts``. After BOOTSTRAP_MAX_ATTEMPTS unusable
    attempts, raises ``fail`` with the last reason.
    """
    for number, factor in enumerate(BOOTSTRAP_FACTORS, start=1):
        camera = bootstrap_camera(fitted, factor)
        problem, details = try_camera(camera)
        attempts.append(
            {
                "attempt": number,
                "factor": factor,
                "camera": camera.to_dict(),
                "problem": problem,
                **details,
            }
        )
        if problem is None:
            return camera
    raise fail(
        f"found no usable starting view of {subject} after {BOOTSTRAP_MAX_ATTEMPTS} attempts "
        f"(zoomed out to {factor:.3g} x Zoom to Fit); last attempt: {problem}"
    )


def offset_camera(camera: Camera, axis: str, offset_m: float) -> Camera:
    tx, ty, tz = camera.translation
    t = (tx + offset_m, ty, tz) if axis == "x" else (tx, ty + offset_m, tz)
    return Camera(camera.orientation, t, camera.scale)


def calibration_room(base: Measurement, axis: str) -> int:
    """The smaller bootstrap margin along ``axis``: how far the image can move either way."""
    i = 0 if axis == "x" else 1
    margins = base.margins.as_tuple()
    return min(margins[i], margins[i + 2])


def calibrate_around(
    shoot: Callable[[Camera], Measurement],
    boot: Camera,
    base: Measurement,
    shorter_side: int,
    record: dict[str, Any],
    fail: Callable[[str], Exception],
) -> Calibration:
    """Calibrate X and Y around ``boot`` (whose capture measured ``base``).

    For each axis, a bounded pilot sizes the step (see ``_step``), then the image
    is captured at -step and +step, and ``framing.calibrate_axis`` fits k from those
    and ``base`` with the SOLIDWORKS signs required. ``record`` receives the pilot,
    the chosen steps, and the result.
    """
    record.setdefault("pilot", {})
    record.setdefault("steps", {})
    observations: dict[str, list[Observation]] = {}
    for axis in ("x", "y"):
        step = _step(shoot, axis, boot, base, shorter_side, record, fail)
        obs = [Observation(0.0, base)]
        for offset in (-step, step):
            obs.append(Observation(offset, shoot(offset_camera(boot, axis, offset))))
        observations[axis] = obs
    calibration = Calibration(
        *(
            framing.calibrate_axis(axis, observations[axis], expected_sign=EXPECTED_SIGNS[axis])
            for axis in ("x", "y")
        )
    )
    record["result"] = calibration.to_dict()
    return calibration


def _step(
    shoot: Callable[[Camera], Measurement],
    axis: str,
    boot: Camera,
    base: Measurement,
    shorter_side: int,
    record: dict[str, Any],
    fail: Callable[[str], Exception],
) -> float:
    """Translation3 step (m) for calibrating ``axis``, sized from a bounded pilot.

    k is unknown before calibration, so a pilot doubles a tiny step until the centre
    moves at least PILOT_MIN_PX, giving a rough |k|. The step then aims for
    CALIBRATION_FRACTION of the shorter side of movement, capped at
    CALIBRATION_ROOM_SHARE of the bootstrap margin on that axis so the +/- step
    captures stay clear of the edges even if the pilot's estimate is somewhat off.
    """
    i = 0 if axis == "x" else 1
    centre = base.center[i]
    room = calibration_room(base, axis)
    target = min(CALIBRATION_FRACTION * shorter_side, CALIBRATION_ROOM_SHARE * room)
    if not calibration_room_ok(room, shorter_side):  # checked before the pilot runs
        raise fail(f"{axis}: bootstrap margin of {room} px leaves too little room to calibrate")
    pilot: list[dict[str, Any]] = []
    record["pilot"][axis] = pilot
    offset = PILOT_START_M
    for _ in range(PILOT_MAX_STEPS):
        m = shoot(offset_camera(boot, axis, offset))
        if m.is_empty or m.clipped:
            raise fail(f"{axis} calibration pilot lost the foreground at {offset} m")
        moved = m.center[i] - centre
        pilot.append({"offset_m": offset, "moved_px": moved})
        if abs(moved) >= PILOT_MIN_PX:
            break
        offset *= 2
    else:
        raise fail(
            f"{axis} calibration pilot never moved the image {PILOT_MIN_PX} px "
            f"(last offset {offset / 2} m)"
        )
    k_estimate = abs(moved) / offset
    step = target / k_estimate
    record["steps"][axis] = {
        "k_estimate_abs": k_estimate,
        "target_px": target,
        "room_px": room,
        "step_m": step,
    }
    return step


def failure_record(exc: BaseException, fallback: dict[str, Any]) -> dict[str, Any]:
    """The record for a failed shot or pair, marked failed with ``exc`` as its error.

    If ``exc`` is not itself a ``CaptureError``, for example a restore error raised
    while a capture error was propagating, the capture's own record is found in the
    exception chain and the capture error is kept as ``capture_error``.
    """
    record, cause = fallback, exc
    while cause is not None:
        if isinstance(cause, CaptureError):
            record = cause.record
            if cause is not exc:
                record["capture_error"] = str(cause)
            break
        cause = cause.__cause__ or cause.__context__
    record["status"] = "failed"
    record["error"] = str(exc) if isinstance(exc, CaptureError) else f"{type(exc).__name__}: {exc}"
    return record


def write_record(record: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
