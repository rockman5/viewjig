from __future__ import annotations

import json
from pathlib import Path

import pytest
from fake_renderer import ORIENT, FakeRenderer

from viewjig import shot as shot_module
from viewjig.camera import Camera
from viewjig.measure import measure
from viewjig.shot import (
    BOOTSTRAP_SCALE,
    MAX_SOLVES,
    ShotError,
    ShotRequest,
    bootstrap_camera,
    resolve_margin,
    run_shot,
    write_record,
)
from viewjig.sw import StandardView


def request(**kw) -> ShotRequest:
    base = {
        "path": Path("vj-f01.SLDPRT"),
        "view": StandardView.ISOMETRIC,
        "width": 400,
        "height": 300,
    }
    return ShotRequest(**{**base, **kw})


def solve_applies(renderer: FakeRenderer, record) -> int:
    solved = [Camera.from_dict(a["solution"]["camera"]) for a in record["attempts"]]
    return sum(1 for c in renderer.applied if c in solved)


# --- Success paths -------------------------------------------------------------------------


def test_first_correction_succeeds_and_record_is_complete() -> None:
    renderer = FakeRenderer()
    result = run_shot(renderer, request())
    r = result.record
    assert r["status"] == "ok" and r["error"] is None
    assert len(r["attempts"]) == 1 and r["attempts"][0]["verification"]["ok"]
    assert r["margin"] == {"px": 15, "source": "default"}  # 5 % of 300
    m = measure(result.image).margins.as_tuple()
    assert min(m[0], m[2]) == pytest.approx(15, abs=3) or min(m[1], m[3]) == pytest.approx(
        15, abs=3
    )
    for key in ("zoom_to_fit_camera", "bootstrap", "background", "calibration", "final"):
        assert key in r
    assert r["calibration"]["result"]["x"]["px_per_m"] > 0
    assert r["calibration"]["result"]["y"]["px_per_m"] < 0
    assert set(r["calibration"]["steps"]) == {"x", "y"}
    json.dumps(r)  # serialisable


def test_bootstrap_is_zoom_to_fit_scaled_about_the_centre() -> None:
    renderer = FakeRenderer()
    record = run_shot(renderer, request()).record
    fitted = Camera.from_dict(record["zoom_to_fit_camera"])
    boot = Camera.from_dict(record["bootstrap"]["camera"])
    assert boot == bootstrap_camera(fitted)
    assert boot.scale == pytest.approx(fitted.scale * BOOTSTRAP_SCALE)
    assert boot.translation[:2] == pytest.approx(
        tuple(BOOTSTRAP_SCALE * t for t in fitted.translation[:2])
    )
    assert boot.translation[2] == fitted.translation[2] and boot.orientation == fitted.orientation
    assert renderer.applied[0] == boot


def test_bootstrap_camera_is_restored_after_calibration() -> None:
    renderer = FakeRenderer()
    record = run_shot(renderer, request()).record
    boot = Camera.from_dict(record["bootstrap"]["camera"])
    first_solution = Camera.from_dict(record["attempts"][0]["solution"]["camera"])
    i = renderer.applied.index(first_solution)
    assert renderer.applied[i - 1] == boot


def test_calibration_step_comes_from_a_bounded_pilot() -> None:
    record = run_shot(FakeRenderer(), request()).record
    for axis in ("x", "y"):
        pilot = record["calibration"]["pilot"][axis]
        assert 1 <= len(pilot) <= shot_module.PILOT_MAX_STEPS
        assert abs(pilot[-1]["moved_px"]) >= shot_module.PILOT_MIN_PX
        assert all(abs(p["moved_px"]) < shot_module.PILOT_MIN_PX for p in pilot[:-1])
        step = record["calibration"]["steps"][axis]
        assert step["target_px"] == pytest.approx(min(15, step["room_px"] / 2))
        samples = record["calibration"]["result"][axis]["samples"]
        assert [s["offset_m"] for s in samples] == pytest.approx(
            [0, -step["step_m"], step["step_m"]]
        )


def test_second_correction_when_the_first_misses() -> None:
    renderer = FakeRenderer(gamma=1.08)
    r = run_shot(renderer, request()).record
    assert r["status"] == "ok"
    assert [a["verification"]["ok"] for a in r["attempts"]] == [False, True]
    first = r["attempts"][0]
    assert first["measurement"]["clipped"] is False
    assert max(abs(v) for v in first["verification"]["margin_residuals"]) > 3
    second_start = r["attempts"][1]["solution"]["start_camera"]
    assert second_start == first["solution"]["camera"]


@pytest.mark.parametrize("view", [StandardView.ISOMETRIC, StandardView.FRONT])
def test_view_is_applied_and_recorded(view) -> None:
    renderer = FakeRenderer()
    r = run_shot(renderer, request(view=view)).record
    assert r["input"]["view"] == view.name
    assert Camera.from_dict(r["final"]["camera"]).orientation == ORIENT[view]


def test_explicit_margin_is_used_instead_of_the_default() -> None:
    r = run_shot(FakeRenderer(), request(margin_px=30)).record
    assert r["margin"] == {"px": 30, "source": "explicit"}
    assert r["attempts"][-1]["solution"]["margin_px"] == 30


@pytest.mark.parametrize(
    "size, margin", [((1600, 1200), 60), ((1000, 1000), 50), ((1001, 751), 38), ((400, 300), 15)]
)
def test_default_margin_is_five_percent_of_the_shorter_side(size, margin) -> None:
    assert resolve_margin(*size, None) == (margin, "default")
    assert resolve_margin(*size, 7) == (7, "explicit")
    with pytest.raises(ValueError):
        resolve_margin(*size, -1)


def test_later_captures_classify_against_the_bootstrap_background() -> None:
    r = run_shot(FakeRenderer(background=(30, 40, 50)), request()).record
    assert r["background"] == [30, 40, 50]
    assert r["bootstrap"]["measurement"]["background"]["source"] == "inferred"
    assert r["final"]["measurement"]["background"]["source"] == "supplied"
    assert r["final"]["measurement"]["background"]["color"] == [30, 40, 50]


# --- Failure paths -------------------------------------------------------------------------


def test_failure_after_the_second_correction_stops_at_two_solves() -> None:
    renderer = FakeRenderer(gamma=0.7)  # every result too small, but measurable
    with pytest.raises(ShotError, match=f"after {MAX_SOLVES} solves") as info:
        run_shot(renderer, request())
    r = info.value.record
    assert r["status"] == "failed" and "after 2 solves" in r["error"]
    assert len(r["attempts"]) == MAX_SOLVES
    assert solve_applies(renderer, r) == MAX_SOLVES
    assert "final" not in r


def test_bootstrap_touching_an_edge_fails() -> None:
    with pytest.raises(ShotError, match="bootstrap foreground touches the image edge") as info:
        run_shot(FakeRenderer(fit_fill=1.6), request())
    assert info.value.record["attempts"] == []


def test_empty_bootstrap_fails() -> None:
    with pytest.raises(ShotError, match="no foreground"):
        run_shot(FakeRenderer(fit_shift_m=5.0), request())


def test_too_little_room_to_calibrate_fails() -> None:
    with pytest.raises(ShotError, match="too little room to calibrate"):
        # 0.7 x 1.36 fill leaves about 10 px either side in x: unclipped but too tight.
        run_shot(renderer := FakeRenderer(fit_fill=1.36, fit_shift_m=0.0), request())
    assert len(renderer.applied) == 1  # refused before any pilot capture


@pytest.mark.parametrize("kx, ky", [(-5575.0, -5575.0), (5575.0, 5575.0)])
def test_unexpected_calibration_signs_fail(kx, ky) -> None:
    with pytest.raises(ShotError, match="wrong sign") as info:
        run_shot(FakeRenderer(kx=kx, ky=ky), request())
    assert info.value.record["status"] == "failed"


def test_pilot_that_never_moves_the_image_fails_after_its_step_limit() -> None:
    # Translation3 X does nothing; the model is centred on its origin so it still fits.
    renderer = FakeRenderer(pan_gain=(0.0, 1.0), model=(-0.06, -0.05, 0.06, 0.0), fit_shift_m=0.0)
    with pytest.raises(ShotError, match="pilot never moved"):
        run_shot(renderer, request())
    assert len(renderer.applied) == 1 + shot_module.PILOT_MAX_STEPS


def test_background_drift_after_bootstrap_fails() -> None:
    renderer = FakeRenderer()
    renderer.drift = (3, (120, 160, 200))
    with pytest.raises(ShotError, match="BackgroundUncertain") as info:
        run_shot(renderer, request())
    assert "supplied background (255, 255, 255)" in info.value.record["error"]


@pytest.mark.parametrize("which", [1, 5, "solve"])
def test_camera_application_failures_propagate(which) -> None:
    renderer = FakeRenderer()
    if which == "solve":
        healthy = FakeRenderer()
        run_shot(healthy, request())
        which = len(healthy.applied)  # the final solve apply
    renderer.fail_apply = which
    with pytest.raises(ShotError, match="CameraReadbackMismatch") as info:
        run_shot(renderer, request())
    assert info.value.record["status"] == "failed"


def test_invalid_explicit_margin_is_rejected_before_rendering() -> None:
    renderer = FakeRenderer()
    with pytest.raises(ValueError):
        run_shot(renderer, request(margin_px=-5))
    assert renderer.applied == []


def test_impossible_margin_fails_as_a_shot_error() -> None:
    with pytest.raises(ShotError, match="ImpossibleFraming"):
        run_shot(FakeRenderer(), request(margin_px=200))


def test_failure_records_are_written(tmp_path: Path) -> None:
    with pytest.raises(ShotError) as info:
        run_shot(FakeRenderer(fit_fill=1.6), request())
    path = tmp_path / "out" / "record.json"
    write_record(info.value.record, path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["status"] == "failed" and data["format"] == "viewjig.shot"
