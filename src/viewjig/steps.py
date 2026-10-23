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
from viewjig.framing import Calibration, Observation
from viewjig.measure import Measurement, MeasurementError
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
def open_renderer(app: sw.Com, path: Path) -> Iterator[DocumentRenderer]:
    """A renderer over ``path`` opened read-only for the block (see ``sw.open_document``)."""
    with sw.open_document(app, path) as document:
        yield DocumentRenderer(document)


def constants() -> dict[str, Any]:
    return {
        "bootstrap_scale": BOOTSTRAP_SCALE,
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


def bootstrap_camera(fitted: Camera) -> Camera:
    """Scale2 and Translation3 X/Y x BOOTSTRAP_SCALE: shrinks the image about its centre.

    That leaves room to calibrate and keeps the first correction's scale ratio
    modest, so measurement rounding (amplified by that ratio) stays well inside the
    verification tolerance.
    """
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
    # The pilot stops after moving at least PILOT_MIN_PX, having moved less than that
    # on the step before, so it can move up to about 2 * PILOT_MIN_PX; check before it runs.
    if 2 * target < framing.DEFAULT_MIN_DISPLACEMENT_PX or room <= 2 * PILOT_MIN_PX + 1:
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


def write_record(record: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
