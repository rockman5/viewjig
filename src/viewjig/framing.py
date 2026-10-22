"""Pure framing math: calibration, one predicted camera correction, verification.

No SOLIDWORKS, images, files, or capture here. Inputs are a ``Camera`` and raster
``Extent`` numbers; outputs are plain immutable values.

Model
-----
For a fixed orientation and output size, a point at projected model position
``q`` lands in the raster at

    raster = C + (kx * (Scale2 * q_x + T_x),  ky * (Scale2 * q_y + T_y))

where ``C = (W / 2, H / 2)`` is the image centre in ``viewjig.measure``'s continuous
coordinates, ``T`` is ``Translation3``, and ``kx``, ``ky`` are the raster
displacement in pixels per metre of ``Translation3`` X and Y. ``kx`` and ``ky`` are
measured per run by ``calibrate`` and carry their own sign. Raster y grows
downward, and SOLIDWORKS moves the image up for positive ``Translation3`` Y, so
the measured ``ky`` comes out negative. ``solve`` uses the measured signs as they
are and applies no fixed negation. ``Translation3`` Z and the orientation do not
move the raster and are preserved.

Solve
-----
Given a starting camera ``(S0, T0)`` whose raster shows a combined foreground of
size ``w0 x h0`` centred at ``c0``, and a pixel margin ``m``:

    s  = min((W - 2m) / w0, (H - 2m) / h0)
    S1 = s * S0
    T1 = s * T0 - s * (c0 - C) / k            (per axis, with that axis's k)

This scales the foreground about ``c0`` by ``s`` and moves ``c0`` onto ``C``, so each
foreground edge ``e`` is predicted at ``C + s * (e - c0)``. The limiting axis lands
exactly on margin ``m``, and the other axis gets the remaining space split
equally. The prediction does not depend on k; whether SOLIDWORKS lands on it
does.

A second correction is the same ``solve`` applied to the camera that was just
used and the extent it produced. The caller decides whether to make one.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from viewjig.camera import Camera
from viewjig.measure import Extent, Sides

Axis = Literal["x", "y"]

DEFAULT_TOLERANCE_PX = 3.0
# Calibration checks. Measured centres move in 0.5 px steps, so a calibration
# needs enough on-axis movement to resolve k (16 px keeps one step under about
# 3 % of it), while off-axis movement and fit residuals of up to a pixel are
# measurement noise.
DEFAULT_MIN_DISPLACEMENT_PX = 16.0
DEFAULT_OFF_AXIS_TOLERANCE_PX = 1.0
DEFAULT_FIT_TOLERANCE_PX = 1.0
DEFAULT_SIZE_TOLERANCE_PX = 1.0


class FramingError(Exception):
    pass


class CalibrationError(FramingError):
    pass


class ImpossibleFraming(FramingError):
    pass


def _check_extent(extent: Extent, size: tuple[int, int] | None, what: str, error=FramingError):
    if size is not None and (extent.image_width, extent.image_height) != size:
        raise error(
            f"{what}: image is {extent.image_width}x{extent.image_height}, expected "
            f"{size[0]}x{size[1]}"
        )
    if extent.is_empty:
        raise error(f"{what}: no foreground")
    if extent.clipped:
        raise error(f"{what}: foreground touches the image edge {extent.touches_edge}")


# --- Calibration -----------------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    """A raster extent captured with ``Translation3`` changed by ``offset_m`` on one axis."""

    offset_m: float
    extent: Extent


@dataclass(frozen=True)
class CalibrationSample:
    offset_m: float
    center: tuple[float, float]
    foreground_size: tuple[int, int]


@dataclass(frozen=True)
class AxisCalibration:
    """Measured raster response to ``Translation3`` changes along one axis."""

    axis: Axis
    image_size: tuple[int, int]
    px_per_m: float  # signed raster displacement along this axis per metre
    samples: tuple[CalibrationSample, ...]
    displacement_px: float  # on-axis centre movement across the samples
    fit_residual_px: float  # largest on-axis deviation from the fitted line
    off_axis_px: float  # largest centre movement along the other axis

    def to_dict(self) -> dict[str, Any]:
        return {
            "axis": self.axis,
            "image_size": list(self.image_size),
            "px_per_m": self.px_per_m,
            "samples": [
                {
                    "offset_m": s.offset_m,
                    "center": list(s.center),
                    "foreground_size": list(s.foreground_size),
                }
                for s in self.samples
            ],
            "displacement_px": self.displacement_px,
            "fit_residual_px": self.fit_residual_px,
            "off_axis_px": self.off_axis_px,
        }


@dataclass(frozen=True)
class Calibration:
    x: AxisCalibration
    y: AxisCalibration

    def __post_init__(self) -> None:
        if self.x.axis != "x" or self.y.axis != "y":
            raise CalibrationError("calibration needs an x and a y axis calibration")
        if self.x.image_size != self.y.image_size:
            raise CalibrationError(
                f"x and y were calibrated at different sizes: "
                f"{self.x.image_size} vs {self.y.image_size}"
            )

    @property
    def image_size(self) -> tuple[int, int]:
        return self.x.image_size

    @property
    def kx(self) -> float:
        return self.x.px_per_m

    @property
    def ky(self) -> float:
        return self.y.px_per_m

    def to_dict(self) -> dict[str, Any]:
        return {"x": self.x.to_dict(), "y": self.y.to_dict()}


def calibrate_axis(
    axis: Axis,
    observations: Sequence[Observation],
    *,
    min_displacement_px: float = DEFAULT_MIN_DISPLACEMENT_PX,
    off_axis_tolerance_px: float = DEFAULT_OFF_AXIS_TOLERANCE_PX,
    fit_tolerance_px: float = DEFAULT_FIT_TOLERANCE_PX,
    size_tolerance_px: float = DEFAULT_SIZE_TOLERANCE_PX,
    expected_sign: int | None = None,
) -> AxisCalibration:
    """Fit the signed px/m response of the foreground centre to ``Translation3`` on ``axis``.

    Rejects observations that cannot give a trustworthy k: fewer than two distinct
    offsets; empty, edge-touching, or mixed-size extents; too little movement;
    movement along the other axis; a non-linear response; a foreground that changes
    size (it should only move); or, if ``expected_sign`` is given, the other sign.
    """
    if axis not in ("x", "y"):
        raise ValueError(f"axis must be 'x' or 'y', got {axis!r}")
    if len(observations) < 2:
        raise CalibrationError(f"{axis}: need at least two observations")
    first = observations[0].extent
    size = (first.image_width, first.image_height)
    for i, obs in enumerate(observations):
        _check_extent(obs.extent, size, f"{axis} calibration observation {i}", CalibrationError)
    offsets = [float(o.offset_m) for o in observations]
    if not all(math.isfinite(o) for o in offsets) or len(set(offsets)) < 2:
        raise CalibrationError(f"{axis}: observations need at least two distinct finite offsets")

    on, off = (0, 1) if axis == "x" else (1, 0)
    centers = [o.extent.center for o in observations]
    along = [c[on] for c in centers]
    across = [c[off] for c in centers]
    sizes = [(o.extent.bounds.width, o.extent.bounds.height) for o in observations]

    mean_o, mean_a = sum(offsets) / len(offsets), sum(along) / len(along)
    sxx = sum((o - mean_o) ** 2 for o in offsets)
    k = sum((o - mean_o) * (a - mean_a) for o, a in zip(offsets, along, strict=True)) / sxx
    fit_residual = max(
        abs(a - (mean_a + k * (o - mean_o))) for o, a in zip(offsets, along, strict=True)
    )
    displacement = max(along) - min(along)
    off_axis = max(across) - min(across)
    size_change = max(max(s[i] for s in sizes) - min(s[i] for s in sizes) for i in (0, 1))

    if displacement < min_displacement_px:
        raise CalibrationError(
            f"{axis}: centre moved only {displacement:.2f} px (need {min_displacement_px})"
        )
    if off_axis > off_axis_tolerance_px:
        raise CalibrationError(
            f"{axis}: centre also moved {off_axis:.2f} px along the other axis "
            f"(tolerance {off_axis_tolerance_px})"
        )
    if fit_residual > fit_tolerance_px:
        raise CalibrationError(
            f"{axis}: response is not linear in the offset (residual {fit_residual:.2f} px, "
            f"tolerance {fit_tolerance_px})"
        )
    if size_change > size_tolerance_px:
        raise CalibrationError(f"{axis}: foreground size changed by {size_change} px while moving")
    if expected_sign is not None and (k > 0) != (expected_sign > 0):
        raise CalibrationError(f"{axis}: px/m {k:.1f} has the wrong sign")

    samples = tuple(
        CalibrationSample(o, c, s) for o, c, s in zip(offsets, centers, sizes, strict=True)
    )
    return AxisCalibration(axis, size, k, samples, displacement, fit_residual, off_axis)


def calibrate(
    x_observations: Sequence[Observation], y_observations: Sequence[Observation], **checks: Any
) -> Calibration:
    """Calibrate both axes at one output size. ``checks`` go to ``calibrate_axis``."""
    return Calibration(
        calibrate_axis("x", x_observations, **checks),
        calibrate_axis("y", y_observations, **checks),
    )


# --- Solve -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Edges:
    """Continuous foreground edges in raster coordinates."""

    left: float
    top: float
    right: float
    bottom: float


@dataclass(frozen=True)
class Solution:
    """One predicted correction: the camera to apply and the raster it should produce."""

    camera: Camera
    start_camera: Camera
    image_size: tuple[int, int]
    margin_px: float
    start_bounds: Edges
    scale_ratio: float
    limiting_axis: Literal["width", "height", "both"]
    predicted: Edges

    @property
    def predicted_margins(self) -> Sides:
        w, h = self.image_size
        p = self.predicted
        return Sides(p.left, p.top, w - p.right, h - p.bottom)

    @property
    def predicted_center(self) -> tuple[float, float]:
        p = self.predicted
        return ((p.left + p.right) / 2, (p.top + p.bottom) / 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "camera": self.camera.to_dict(),
            "start_camera": self.start_camera.to_dict(),
            "image_size": list(self.image_size),
            "margin_px": self.margin_px,
            "start_bounds": list(vars(self.start_bounds).values()),
            "scale_ratio": self.scale_ratio,
            "limiting_axis": self.limiting_axis,
            "predicted_bounds": list(vars(self.predicted).values()),
            "predicted_margins": list(self.predicted_margins.as_tuple()),
            "predicted_center": list(self.predicted_center),
        }


def solve(camera: Camera, extent: Extent, calibration: Calibration, margin_px: float) -> Solution:
    """Calculate the camera that frames ``extent`` with ``margin_px``, centred, in one step.

    ``extent`` is what ``camera`` produced (for a set, the combined extent). The
    margin is an explicit resolved pixel value; there is no default.
    """
    w, h = calibration.image_size
    _check_extent(extent, (w, h), "starting extent", ImpossibleFraming)
    if isinstance(margin_px, bool) or not math.isfinite(margin_px) or margin_px < 0:
        raise ImpossibleFraming(f"margin must be a finite number >= 0, got {margin_px!r}")
    avail_w, avail_h = w - 2 * margin_px, h - 2 * margin_px
    if avail_w <= 0 or avail_h <= 0:
        raise ImpossibleFraming(f"margin {margin_px} px leaves no room in a {w}x{h} image")
    kx, ky = calibration.kx, calibration.ky
    if not (math.isfinite(kx) and math.isfinite(ky)) or kx == 0 or ky == 0:
        raise CalibrationError(f"invalid calibration: kx={kx}, ky={ky}")

    b = extent.bounds
    fit_w, fit_h = avail_w / b.width, avail_h / b.height
    s = min(fit_w, fit_h)
    limiting = "both" if fit_w == fit_h else ("width" if fit_w < fit_h else "height")
    cx, cy = w / 2, h / 2
    c0x, c0y = extent.center
    tx, ty, tz = camera.translation
    new = Camera(
        orientation=camera.orientation,
        translation=(s * tx - s * (c0x - cx) / kx, s * ty - s * (c0y - cy) / ky, tz),
        scale=s * camera.scale,
    )
    if not new.scale > 0:
        raise ImpossibleFraming(f"calculated scale {new.scale} is not positive")
    predicted = Edges(
        cx + s * (b.left - c0x),
        cy + s * (b.top - c0y),
        cx + s * (b.right - c0x),
        cy + s * (b.bottom - c0y),
    )
    return Solution(
        camera=new,
        start_camera=camera,
        image_size=(w, h),
        margin_px=margin_px,
        start_bounds=Edges(b.left, b.top, b.right, b.bottom),
        scale_ratio=s,
        limiting_axis=limiting,
        predicted=predicted,
    )


# --- Verify ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Verification:
    ok: bool
    tolerance_px: float
    failures: tuple[str, ...]
    measured_margins: Sides | None
    predicted_margins: Sides
    margin_residuals: Sides | None  # measured - predicted, per side
    center_residual: tuple[float, float] | None  # measured - predicted centre

    def to_dict(self) -> dict[str, Any]:
        def sides(value: Sides | None) -> list[float] | None:
            return None if value is None else list(value.as_tuple())

        return {
            "ok": self.ok,
            "tolerance_px": self.tolerance_px,
            "failures": list(self.failures),
            "measured_margins": sides(self.measured_margins),
            "predicted_margins": sides(self.predicted_margins),
            "margin_residuals": sides(self.margin_residuals),
            "center_residual": None if self.center_residual is None else list(self.center_residual),
        }


def verify(
    solution: Solution, measured: Extent, tolerance_px: float = DEFAULT_TOLERANCE_PX
) -> Verification:
    """Compare the measured result of applying ``solution`` against its prediction.

    Passes only with a non-empty foreground that touches no image edge and lands
    within ``tolerance_px`` of the predicted margin on every side.
    """
    if not math.isfinite(tolerance_px) or tolerance_px < 0:
        raise ValueError(f"tolerance must be a finite number >= 0, got {tolerance_px!r}")
    if (measured.image_width, measured.image_height) != solution.image_size:
        raise FramingError(
            f"measured image {measured.image_width}x{measured.image_height} does not match "
            f"the solved size {solution.image_size}"
        )
    predicted = solution.predicted_margins
    if measured.is_empty:
        return Verification(False, tolerance_px, ("no foreground",), None, predicted, None, None)

    got = measured.margins
    residuals = Sides(*(g - p for g, p in zip(got.as_tuple(), predicted.as_tuple(), strict=True)))
    pc, mc = solution.predicted_center, measured.center
    failures = []
    if measured.clipped:
        failures.append(f"foreground touches the image edge {measured.touches_edge}")
    names = ("left", "top", "right", "bottom")
    for name, r in zip(names, residuals.as_tuple(), strict=True):
        if abs(r) > tolerance_px:
            failures.append(f"{name} margin off by {r:+.2f} px (tolerance {tolerance_px})")
    return Verification(
        ok=not failures,
        tolerance_px=tolerance_px,
        failures=tuple(failures),
        measured_margins=got,
        predicted_margins=predicted,
        margin_residuals=residuals,
        center_residual=(mc[0] - pc[0], mc[1] - pc[1]),
    )
