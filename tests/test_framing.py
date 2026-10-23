from __future__ import annotations

import json
import math

import pytest

from viewjig.camera import Camera
from viewjig.framing import (
    AxisCalibration,
    Calibration,
    CalibrationError,
    FramingError,
    ImpossibleFraming,
    Observation,
    calibrate,
    calibrate_axis,
    solve,
    verify,
)
from viewjig.measure import Bounds, Extent

ORIENTATION = tuple(float(i) for i in range(16))


def cam(scale=1.0, tx=0.0, ty=0.0, tz=0.0123) -> Camera:
    return Camera(ORIENTATION, (tx, ty, tz), scale)


def render(camera: Camera, size, kx, ky, model=(0.0, -0.05, 0.12, 0.0), cross=0.0) -> Extent:
    """Raster extent of a rectangular model under the framing model, edges rounded to pixels.

    ``model`` is (qx0, qy0, qx1, qy1) in projected model metres. ``cross`` adds raster x
    movement per metre of Translation3 Y, to simulate a misbehaving axis.
    """
    w, h = size
    s = camera.scale
    tx, ty, _ = camera.translation
    xs = [w / 2 + kx * (s * q + tx) + cross * ty for q in (model[0], model[2])]
    ys = [h / 2 + ky * (s * q + ty) for q in (model[1], model[3])]
    left, right = sorted(round(v) for v in xs)
    top, bottom = sorted(round(v) for v in ys)
    left, top, right, bottom = max(left, 0), max(top, 0), min(right, w), min(bottom, h)
    if left >= right or top >= bottom:
        return Extent(w, h, None)
    return Extent(w, h, Bounds(left, top, right, bottom))


def observe(base: Camera, axis: str, offsets, size, kx, ky, **kw) -> list[Observation]:
    out = []
    for d in offsets:
        tx, ty, tz = base.translation
        moved = Camera(
            base.orientation, (tx + d, ty, tz) if axis == "x" else (tx, ty + d, tz), base.scale
        )
        out.append(Observation(d, render(moved, size, kx, ky, **kw)))
    return out


def fixed(kx: float, ky: float, size=(1600, 1200)) -> Calibration:
    return Calibration(
        AxisCalibration("x", size, kx, (), 0.0, 0.0, 0.0),
        AxisCalibration("y", size, ky, (), 0.0, 0.0, 0.0),
    )


# --- Calibration -------------------------------------------------------------------------


@pytest.mark.parametrize("kx, ky", [(5000.0, -5000.0), (-4000.0, 4500.0), (3500.0, 3500.0)])
def test_calibration_recovers_signed_px_per_metre(kx, ky) -> None:
    base, size = cam(scale=1.2, tx=-0.06, ty=0.02), (1600, 1200)
    c = calibrate(
        observe(base, "x", (-0.008, 0.008), size, kx, ky),
        observe(base, "y", (-0.008, 0.008), size, kx, ky),
    )
    assert c.kx == pytest.approx(kx, rel=0.01)
    assert c.ky == pytest.approx(ky, rel=0.01)
    assert c.image_size == size
    assert c.x.off_axis_px == 0 and c.y.off_axis_px == 0


def test_calibration_records_its_samples() -> None:
    base, size = cam(tx=-0.06), (1600, 1200)
    x = calibrate_axis("x", observe(base, "x", (-0.004, 0.0, 0.004), size, 5000, -5000))
    assert x.px_per_m == pytest.approx(5000)
    assert [s.offset_m for s in x.samples] == [-0.004, 0.0, 0.004]
    assert x.displacement_px == 40.0 and x.fit_residual_px == pytest.approx(0)
    record = json.loads(json.dumps(x.to_dict()))
    assert record["samples"][0]["center"] == list(x.samples[0].center)


def test_calibration_rejects_too_little_movement() -> None:
    obs = observe(cam(tx=-0.06), "x", (-0.0005, 0.0005), (1600, 1200), 5000, -5000)
    with pytest.raises(CalibrationError, match="moved only"):
        calibrate_axis("x", obs)


def test_calibration_rejects_movement_on_the_other_axis() -> None:
    obs = observe(cam(tx=-0.06), "y", (-0.008, 0.008), (1600, 1200), 5000, -5000, cross=500.0)
    with pytest.raises(CalibrationError, match="other axis"):
        calibrate_axis("y", obs)


def test_calibration_rejects_a_non_linear_response() -> None:
    size = (1600, 1200)
    obs = observe(cam(tx=-0.06), "x", (-0.008, 0.0, 0.008), size, 5000, -5000)
    b = obs[1].extent.bounds
    obs[1] = Observation(0.0, Extent(*size, Bounds(b.left + 6, b.top, b.right + 6, b.bottom)))
    with pytest.raises(CalibrationError, match="not linear"):
        calibrate_axis("x", obs)


def test_calibration_rejects_a_foreground_that_changes_size() -> None:
    size = (1600, 1200)
    obs = observe(cam(tx=-0.06), "x", (-0.008, 0.008), size, 5000, -5000)
    b = obs[1].extent.bounds
    obs[1] = Observation(0.008, Extent(*size, Bounds(b.left, b.top, b.right + 4, b.bottom)))
    with pytest.raises(CalibrationError, match="size changed"):
        calibrate_axis("x", obs, fit_tolerance_px=5)


def test_calibration_checks_the_expected_sign() -> None:
    obs = observe(cam(tx=-0.06), "y", (-0.008, 0.008), (1600, 1200), 5000, -5000)
    assert calibrate_axis("y", obs, expected_sign=-1).px_per_m < 0
    with pytest.raises(CalibrationError, match="wrong sign"):
        calibrate_axis("y", obs, expected_sign=1)


def test_calibration_rejects_clipped_empty_and_mixed_inputs() -> None:
    size = (1600, 1200)
    good = observe(cam(tx=-0.06), "x", (-0.008, 0.008), size, 5000, -5000)
    clipped = Observation(0.01, Extent(*size, Bounds(0, 100, 300, 400)))
    with pytest.raises(CalibrationError, match="touches the image edge"):
        calibrate_axis("x", [*good, clipped])
    with pytest.raises(CalibrationError, match="no foreground"):
        calibrate_axis("x", [*good, Observation(0.01, Extent(*size, None))])
    with pytest.raises(CalibrationError, match="expected 1600x1200"):
        calibrate_axis("x", [*good, Observation(0.01, Extent(1000, 1000, Bounds(1, 1, 9, 9)))])
    with pytest.raises(CalibrationError, match="at least two observations"):
        calibrate_axis("x", good[:1])
    with pytest.raises(CalibrationError, match="distinct"):
        calibrate_axis("x", [good[0], Observation(good[0].offset_m, good[1].extent)])


def test_axes_must_match_in_name_and_size() -> None:
    x = AxisCalibration("x", (1600, 1200), 1.0, (), 0, 0, 0)
    with pytest.raises(CalibrationError, match="different sizes"):
        Calibration(x, AxisCalibration("y", (1000, 1000), 1.0, (), 0, 0, 0))
    with pytest.raises(CalibrationError, match="an x and a y"):
        Calibration(x, x)


# --- Solve -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "size, model, limiting",
    [
        ((1600, 1200), (0.0, -0.05, 0.12, 0.0), "width"),  # wide model in a 4:3 frame
        ((1600, 1200), (0.0, -0.11, 0.07, 0.0), "height"),  # tall model
        ((1000, 1000), (0.0, -0.07, 0.12, 0.0), "width"),
        ((1001, 751), (0.0, -0.09, 0.05, 0.0), "height"),
    ],
)
def test_solve_fits_the_limiting_axis_and_centres(size, model, limiting) -> None:
    w, h = size
    kx, ky, margin = 5200.0, -5200.0, 60
    start = cam(scale=0.6, tx=0.01, ty=0.03)  # small and off-centre, fully in frame
    extent = render(start, size, kx, ky, model)
    sol = solve(start, extent, fixed(kx, ky, size), margin)
    m = sol.predicted_margins
    assert sol.limiting_axis == limiting
    if limiting == "width":
        assert (m.left, m.right) == pytest.approx((margin, margin))
        assert m.top == pytest.approx(m.bottom) and m.top >= margin
    else:
        assert (m.top, m.bottom) == pytest.approx((margin, margin))
        assert m.left == pytest.approx(m.right) and m.left >= margin
    assert sol.predicted_center == pytest.approx((w / 2, h / 2))
    # With an exact k, the result lands within pixel rounding: each measured start edge
    # is off by up to 0.5 px, scaled by s, plus 0.5 px rounding in the result.
    result = verify(sol, render(sol.camera, size, kx, ky, model))
    assert result.ok, result.failures
    bound = 0.5 * (sol.scale_ratio + 1)
    assert max(abs(r) for r in result.margin_residuals.as_tuple()) <= bound


@pytest.mark.parametrize("kx, ky", [(5200.0, -5200.0), (-5200.0, 5200.0), (-3000.0, -4000.0)])
def test_solve_uses_the_measured_signs(kx, ky) -> None:
    size = (1600, 1200)
    start = cam(scale=0.7, tx=0.02, ty=0.01)
    sol = solve(start, render(start, size, kx, ky), fixed(kx, ky, size), 60)
    assert verify(sol, render(sol.camera, size, kx, ky)).ok


def test_solve_preserves_orientation_and_translation_z() -> None:
    size = (1600, 1200)
    start = cam(scale=0.7, tx=0.02, ty=0.01, tz=-0.0473)
    sol = solve(start, render(start, size, 5200, -5200), fixed(5200, -5200, size), 60)
    assert sol.camera.orientation == start.orientation
    assert sol.camera.translation[2] == -0.0473
    assert sol.start_camera == start


def test_solve_matches_the_closed_form() -> None:
    size, kx, ky, m = (1600, 1200), 5000.0, -4000.0, 100
    start = cam(scale=2.0, tx=0.01, ty=-0.03)
    extent = Extent(*size, Bounds(300, 200, 900, 500))  # 600 x 300, centre (600, 350)
    sol = solve(start, extent, fixed(kx, ky, size), m)
    s = min((1600 - 200) / 600, (1200 - 200) / 300)
    assert sol.scale_ratio == pytest.approx(s)
    assert sol.camera.scale == pytest.approx(2.0 * s)
    assert sol.camera.translation[0] == pytest.approx(s * 0.01 - s * (600 - 800) / kx)
    assert sol.camera.translation[1] == pytest.approx(s * -0.03 - s * (350 - 600) / ky)


@pytest.mark.parametrize(
    "size, box, center",
    [
        ((101, 81), (10, 10, 41, 31), (50.5, 40.5)),
        ((100, 80), (10, 10, 41, 31), (50.0, 40.0)),
        ((1001, 751), (100, 50, 600, 401), (500.5, 375.5)),
    ],
)
def test_predicted_centre_is_the_continuous_image_centre(size, box, center) -> None:
    sol = solve(cam(), Extent(*size, Bounds(*box)), fixed(5000, -5000, size), 5)
    assert sol.predicted_center == pytest.approx(center)
    m = sol.predicted_margins
    assert m.left + (sol.predicted.right - sol.predicted.left) + m.right == pytest.approx(size[0])


@pytest.mark.parametrize("margin", [600, 800, -1, math.nan, math.inf, True])
def test_impossible_or_invalid_margins_are_rejected(margin) -> None:
    extent = Extent(1600, 1200, Bounds(300, 200, 900, 500))
    with pytest.raises(ImpossibleFraming):
        solve(cam(), extent, fixed(5000, -5000), margin)


def test_solve_rejects_empty_clipped_or_mismatched_extents() -> None:
    c = fixed(5000, -5000)
    with pytest.raises(ImpossibleFraming, match="no foreground"):
        solve(cam(), Extent(1600, 1200, None), c, 60)
    with pytest.raises(ImpossibleFraming, match="touches the image edge"):
        solve(cam(), Extent(1600, 1200, Bounds(0, 200, 900, 500)), c, 60)
    with pytest.raises(ImpossibleFraming, match="expected 1600x1200"):
        solve(cam(), Extent(1000, 1000, Bounds(10, 10, 90, 90)), c, 60)


@pytest.mark.parametrize("kx, ky", [(0.0, -5000.0), (5000.0, 0.0), (math.nan, 5000.0)])
def test_solve_rejects_invalid_calibration(kx, ky) -> None:
    with pytest.raises(CalibrationError):
        solve(cam(), Extent(1600, 1200, Bounds(300, 200, 900, 500)), fixed(kx, ky), 60)


def test_solution_serialises() -> None:
    sol = solve(cam(), Extent(1600, 1200, Bounds(300, 200, 900, 500)), fixed(5000, -5000), 60)
    data = json.loads(json.dumps(sol.to_dict()))
    assert data["margin_px"] == 60 and data["limiting_axis"] == "width"
    assert Camera.from_dict(data["camera"]) == sol.camera


# --- Verify ------------------------------------------------------------------------------


def exact_case():
    # 600 x 300 at centre (600, 350) in 1600 x 1200, margin 100: s = 7/3 (width-limited),
    # predicted 1400 x 700 centred: edges (100, 250, 1500, 950).
    return solve(cam(), Extent(1600, 1200, Bounds(300, 200, 900, 500)), fixed(5000, -5000), 100)


def test_exact_result_verifies_with_zero_residuals() -> None:
    sol = exact_case()
    assert sol.predicted_margins.as_tuple() == pytest.approx((100, 250, 100, 250))
    v = verify(sol, Extent(1600, 1200, Bounds(100, 250, 1500, 950)))
    assert v.ok and v.failures == ()
    assert v.margin_residuals.as_tuple() == pytest.approx((0, 0, 0, 0))
    assert v.center_residual == pytest.approx((0, 0))


def test_residuals_within_three_pixels_pass() -> None:
    v = verify(exact_case(), Extent(1600, 1200, Bounds(103, 247, 1497, 953)))
    assert v.ok
    assert v.margin_residuals.as_tuple() == pytest.approx((3, -3, 3, -3))
    assert v.center_residual == pytest.approx((0, 0))


def test_a_side_beyond_three_pixels_fails_with_its_residual() -> None:
    v = verify(exact_case(), Extent(1600, 1200, Bounds(104, 250, 1500, 950)))
    assert not v.ok
    assert v.failures == ("left margin off by +4.00 px (tolerance 3.0)",)
    assert v.margin_residuals.as_tuple() == pytest.approx((4, 0, 0, 0))
    assert v.center_residual == pytest.approx((2, 0))


def test_clipped_or_empty_results_fail() -> None:
    clipped = verify(exact_case(), Extent(1600, 1200, Bounds(0, 250, 1500, 950)))
    assert not clipped.ok and any("touches the image edge" in f for f in clipped.failures)
    empty = verify(exact_case(), Extent(1600, 1200, None))
    assert not empty.ok and empty.failures == ("no foreground",)
    assert empty.margin_residuals is None and empty.center_residual is None


def test_verification_checks_size_and_tolerance() -> None:
    with pytest.raises(FramingError, match="does not match"):
        verify(exact_case(), Extent(1000, 1000, Bounds(10, 10, 90, 90)))
    with pytest.raises(ValueError):
        verify(exact_case(), Extent(1600, 1200, Bounds(100, 250, 1500, 950)), tolerance_px=-1)
    loose = verify(exact_case(), Extent(1600, 1200, Bounds(104, 250, 1500, 950)), tolerance_px=4.0)
    assert loose.ok


def test_verification_serialises() -> None:
    v = verify(exact_case(), Extent(1600, 1200, Bounds(104, 250, 1500, 950)))
    data = json.loads(json.dumps(v.to_dict()))
    assert data["ok"] is False and data["margin_residuals"] == [4, 0, 0, 0]


# --- One additional correction -------------------------------------------------------------


def test_one_more_solve_from_the_measured_result_converges() -> None:
    """A 4 % k error misses the first time; solving again from what it produced fixes it.

    The second correction inherits the same k error, but only on the remaining miss, so
    its residual is a small fraction of the first.
    """
    size, true_kx, true_ky, margin = (1600, 1200), 5575.0, -5575.0, 80
    model = (0.0, -0.05, 0.12, 0.0)
    believed = fixed(true_kx * 1.04, true_ky * 0.96, size)  # what calibration reported
    start = cam(scale=0.5, tx=0.05, ty=-0.04)  # small and far off-centre

    first = solve(start, render(start, size, true_kx, true_ky, model), believed, margin)
    first_result = render(first.camera, size, true_kx, true_ky, model)
    first_check = verify(first, first_result)
    first_miss = max(abs(r) for r in first_check.margin_residuals.as_tuple())
    assert not first_check.ok and first_miss > 3

    second = solve(first.camera, first_result, believed, margin)
    second_check = verify(second, render(second.camera, size, true_kx, true_ky, model))
    second_miss = max(abs(r) for r in second_check.margin_residuals.as_tuple())
    assert second_check.ok, second_check.failures
    assert second_miss <= first_miss / 10
