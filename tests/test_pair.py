from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest
from fake_renderer import ORIENT, FakeRenderer
from PIL import Image

from viewjig import pair as pair_module
from viewjig.camera import Camera
from viewjig.measure import combine, measure
from viewjig.pair import MAX_SOLVES, PairError, PairRequest, pair, run_pair
from viewjig.shot import bootstrap_camera
from viewjig.sw import StandardView

# Projected model rectangles (qx0, qy0, qx1, qy1). In the fake, negative q_y extends down
# the image, so TALL shares SHORT's left, right, and top (q_y = 0) edges and reaches lower.
# In a 400 x 300 frame both are height-limited, so their native fits differ.
SHORT = (0.0, -0.05, 0.12, 0.0)
TALL = (0.0, -0.10, 0.12, 0.0)
NARROW = (0.0, -0.05, 0.08, 0.0)


class FakeOpener:
    """Opens one fake document per path, never two at once; reopening resets the view."""

    def __init__(self, a: FakeRenderer, b: FakeRenderer) -> None:
        self.renderers = {"a.SLDPRT": a, "b.SLDPRT": b}
        self.open_now = 0
        self.log: list[tuple[str, str]] = []

    @contextmanager
    def __call__(self, path: Path):
        assert self.open_now == 0, "two documents open at once"
        name = Path(path).name
        renderer = self.renderers[name]
        renderer.camera = Camera(renderer.camera.orientation, (0.0, 0.0, 0.01), 1.0)
        self.open_now += 1
        self.log.append(("open", name))
        try:
            yield renderer
        finally:
            self.open_now -= 1
            self.log.append(("close", name))


def world(a_model=SHORT, b_model=TALL, **kw) -> tuple[FakeOpener, FakeRenderer, FakeRenderer]:
    a = FakeRenderer(model=a_model, **kw)
    b = FakeRenderer(model=b_model, **kw)
    return FakeOpener(a, b), a, b


def request(**kw) -> PairRequest:
    base = {
        "path_a": Path("a.SLDPRT"),
        "path_b": Path("b.SLDPRT"),
        "view": StandardView.ISOMETRIC,
        "width": 400,
        "height": 300,
    }
    return PairRequest(**{**base, **kw})


def solved_cameras(record) -> list[Camera]:
    return [Camera.from_dict(a["solution"]["camera"]) for a in record["attempts"]]


# --- Shared camera and sequence ----------------------------------------------------------


@pytest.mark.parametrize(
    "a_model, b_model, farther",
    [(SHORT, TALL, "b"), (TALL, SHORT, "a"), ((0.0, -0.05, 0.16, 0.0), SHORT, "a")],
)
def test_the_farther_out_native_fit_starts_the_shared_camera(a_model, b_model, farther) -> None:
    opener, _a, _b = world(a_model, b_model)
    r = run_pair(opener, request()).record
    fits = {m: Camera.from_dict(c) for m, c in r["native_fit"].items()}
    assert fits[farther].scale < fits["b" if farther == "a" else "a"].scale
    assert r["shared_start"]["member"] == farther
    assert Camera.from_dict(r["bootstrap"]["camera"]) == bootstrap_camera(fits[farther])


def test_both_documents_get_exactly_the_same_bootstrap_camera() -> None:
    opener, a, b = world()
    r = run_pair(opener, request()).record
    boot = Camera.from_dict(r["bootstrap"]["camera"])
    assert boot in a.applied and boot in b.applied
    assert all(Camera.from_dict(c) == boot for c in r["bootstrap"]["readback"].values())


def test_documents_are_processed_one_at_a_time() -> None:
    opener, _a, _b = world()
    r = run_pair(opener, request()).record
    assert opener.open_now == 0
    kinds = [k for k, _ in opener.log]
    assert kinds == ["open", "close"] * (len(kinds) // 2)
    assert r["documents"]["a"]["sessions"] + r["documents"]["b"]["sessions"] == len(kinds) // 2


@pytest.mark.parametrize("a_model, b_model", [(SHORT, TALL), (TALL, SHORT)])
def test_combined_extent_drives_the_solve(a_model, b_model) -> None:
    opener, _, _ = world(a_model, b_model)
    r = run_pair(opener, request()).record
    combined = r["bootstrap"]["combined"]["bounds"]
    a_b, b_b = (r["bootstrap"]["measurements"][m]["bounds"] for m in ("a", "b"))
    assert combined == [
        min(a_b[0], b_b[0]),
        min(a_b[1], b_b[1]),
        max(a_b[2], b_b[2]),
        max(a_b[3], b_b[3]),
    ]
    assert r["attempts"][0]["solution"]["start_bounds"] == combined
    # The taller member sets the combined bottom edge (negative q_y is down in the fake).
    taller = "a" if a_model == TALL else "b"
    assert combined[3] == r["bootstrap"]["measurements"][taller]["bounds"][3]


def test_first_correction_success_frames_the_combined_extent() -> None:
    opener, _a, _b = world()
    result = run_pair(opener, request())
    r = result.record
    assert r["status"] == "ok" and len(r["attempts"]) == 1
    assert r["margin"] == {"px": 15, "source": "default"}
    assert result.combined == combine(result.measurements.values())
    margins = result.combined.margins.as_tuple()
    assert min(margins) == pytest.approx(15, abs=3)
    assert r["final"]["readback_exact"] == {"a": True, "b": True}
    assert Camera.from_dict(r["final"]["camera"]) == result.camera
    for member in ("a", "b"):
        assert result.measurements[member] == measure(
            result.images[member], background=tuple(r["background"]["shared"])
        )
    json.dumps(r)


def test_the_final_images_share_registration_by_construction() -> None:
    opener, _a, _b = world()
    result = run_pair(opener, request())
    ma, mb = (result.measurements[m].bounds for m in ("a", "b"))
    # Same origin and X extent under one camera: the shared left, right, and top edges
    # land on the same pixels; only B's extra extent differs.
    assert (ma.left, ma.right, ma.top) == (mb.left, mb.right, mb.top)
    assert mb.bottom > ma.bottom


def test_second_correction_success() -> None:
    opener, _, _ = world(gamma=1.08)
    r = run_pair(opener, request()).record
    assert [a["ok"] for a in r["attempts"]] == [False, True]
    assert r["attempts"][1]["solution"]["start_bounds"] == r["attempts"][0]["combined"]["bounds"]


def test_failure_after_the_second_correction() -> None:
    opener, a, b = world(gamma=0.7)
    with pytest.raises(PairError, match=f"after {MAX_SOLVES} solves") as info:
        run_pair(opener, request())
    r = info.value.record
    assert len(r["attempts"]) == MAX_SOLVES and "final" not in r
    solved = solved_cameras(r)
    assert sum(c in solved for c in a.applied) == MAX_SOLVES
    assert sum(c in solved for c in b.applied) == MAX_SOLVES


# --- Backgrounds -----------------------------------------------------------------------------


def test_independent_backgrounds_that_agree_become_one_shared_reference() -> None:
    opener, _a, b = world()
    b.background = (252, 253, 255)  # within threshold 8 of white
    r = run_pair(opener, request()).record
    assert r["background"]["inferred"] == {"a": [255, 255, 255], "b": [252, 253, 255]}
    assert r["background"]["shared"] == [255, 255, 255]
    for m in ("a", "b"):
        assert r["final"]["measurements"][m]["background"]["source"] == "supplied"


def test_background_disagreement_fails() -> None:
    opener, _a, b = world()
    b.background = (200, 220, 255)
    with pytest.raises(PairError, match="bootstrap backgrounds differ"):
        run_pair(opener, request())


# --- Member checks ---------------------------------------------------------------------------


def test_a_member_lost_from_the_final_images_fails_even_if_the_combined_extent_fits() -> None:
    opener, _a, b = world()
    healthy = world()[0]
    run_pair(healthy, request())  # how many captures b makes before its final one
    b_final_capture = healthy.renderers["b.SLDPRT"].captured
    original = b.capture

    def vanish(width, height):
        image = original(width, height)
        if b.captured >= b_final_capture:
            image.paste((255, 255, 255), (0, 0, width, height))  # B renders nothing
        return image

    b.capture = vanish
    with pytest.raises(PairError, match="b: no foreground") as info:
        run_pair(opener, request())
    attempt = info.value.record["attempts"][-1]
    assert attempt["member_failures"] == ["b: no foreground"]
    assert attempt["ok"] is False
    assert attempt["combined"]["bounds"] == attempt["measurements"]["a"]["bounds"]


def test_a_clipped_member_fails() -> None:
    opener, _a, b = world(b_model=(0.0, -0.09, 0.12, 0.0))
    original = b.capture
    healthy = world(b_model=(0.0, -0.09, 0.12, 0.0))[0]
    run_pair(healthy, request())
    last = healthy.renderers["b.SLDPRT"].captured

    def clip(width, height):
        image = original(width, height)
        if b.captured >= last:  # B's final image runs off the top
            image.paste((194, 194, 194), (100, 0, 140, 40))
        return image

    b.capture = clip
    with pytest.raises(PairError, match="b: foreground touches the image edge"):
        run_pair(opener, request())


def test_a_bootstrap_member_off_the_shared_frame_fails() -> None:
    # Same size as A, so the fits tie and A starts the camera; B sits to the side of it.
    opener, _, _ = world(b_model=(0.06, -0.05, 0.18, 0.0))
    with pytest.raises(PairError, match="bootstrap: b: (no foreground|foreground touches)"):
        run_pair(opener, request())


def test_mismatched_orientations_fail() -> None:
    opener, _a, b = world()

    def front_view(view):
        b.camera = Camera(ORIENT[StandardView.FRONT], (0.0, 0.0, 0.01), 1.0)

    b.show_standard_view = front_view
    with pytest.raises(PairError, match="orientations differ"):
        run_pair(opener, request())


def test_camera_readback_failure_on_either_document_fails() -> None:
    for member in ("a", "b"):
        opener, a, b = world()
        (a if member == "a" else b).fail_apply = 1
        with pytest.raises(PairError, match="CameraReadbackMismatch"):
            run_pair(opener, request())


# --- Calibration ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "a_model, b_model, expected",
    [(SHORT, NARROW, "b"), (NARROW, SHORT, "a"), (SHORT, SHORT, "a")],
)
def test_calibration_runs_on_the_member_with_more_room(a_model, b_model, expected) -> None:
    opener, _a, _b = world(a_model, b_model)
    r = run_pair(opener, request()).record
    room = r["calibration"]["room_px"]
    assert r["calibration"]["member"] == expected
    assert room[expected] >= room["b" if expected == "a" else "a"]
    other = r["documents"]["b" if expected == "a" else "a"]["captures"]
    assert other == 1 + len(r["attempts"])  # bootstrap + one per attempt, no calibration
    assert r["calibration"]["result"]["x"]["px_per_m"] > 0
    assert r["calibration"]["result"]["y"]["px_per_m"] < 0


def test_wrong_calibration_signs_fail() -> None:
    opener, _, _ = world(ky=5575.0)
    with pytest.raises(PairError, match="wrong sign"):
        run_pair(opener, request())


# --- Diagnostics ------------------------------------------------------------------------------


def test_edge_overlap_is_recorded_as_a_diagnostic() -> None:
    opener, _, _ = world()
    d = run_pair(opener, request()).record["diagnostics"]["edge_overlap"]
    assert d["best_shift"] == [0, 0] and d["peak_inside_range"] is True
    assert len(d["counts"]) == (2 * d["max_shift"] + 1) ** 2


def test_edge_overlap_cannot_fail_the_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(a, b, **kw):
        raise RuntimeError("diagnostic exploded")

    monkeypatch.setattr(pair_module, "edge_overlap", broken)
    opener, _, _ = world()
    r = run_pair(opener, request()).record
    assert r["status"] == "ok"
    assert r["diagnostics"]["edge_overlap"] == {"error": "RuntimeError: diagnostic exploded"}


def test_unrelated_documents_with_no_shared_lines_still_pair() -> None:
    opener, _, _ = world(SHORT, (0.02, -0.04, 0.10, -0.01))  # no shared outline
    r = run_pair(opener, request()).record
    assert r["status"] == "ok"
    assert r["diagnostics"]["edge_overlap"]["zero_shift_share"][0] < 0.5


# --- Export ------------------------------------------------------------------------------------


def test_successful_pair_writes_two_pngs_and_one_record(tmp_path: Path) -> None:
    opener, _, _ = world()
    paths = (tmp_path / "a.png", tmp_path / "b.png", tmp_path / "pair.json")
    result = pair(request(), *paths, opener=opener)
    assert all(p.is_file() for p in paths)
    data = json.loads(paths[2].read_text(encoding="utf-8"))
    assert data["status"] == "ok" and data["format"] == "viewjig.pair"
    assert data["output"]["png"] == {"a": str(paths[0]), "b": str(paths[1])}
    assert Camera.from_dict(data["final"]["camera"]) == result.camera
    assert png_size(paths[0]) == png_size(paths[1]) == (400, 300)


def test_failed_pair_writes_the_record_but_no_pngs(tmp_path: Path) -> None:
    opener, _, b = world()
    b.background = (0, 0, 0)
    paths = (tmp_path / "a.png", tmp_path / "b.png", tmp_path / "pair.json")
    with pytest.raises(PairError):
        pair(request(), *paths, opener=opener)
    assert not paths[0].exists() and not paths[1].exists()
    data = json.loads(paths[2].read_text(encoding="utf-8"))
    assert data["status"] == "failed" and "backgrounds differ" in data["error"]


def png_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        return image.size
