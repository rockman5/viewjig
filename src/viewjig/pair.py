"""Matched pair: two separate documents captured with one shared camera and one size.

ViewJig does not decide what the two documents have in common or what changed
between them. The pair contract is construction: both images come from the same
numeric camera (read back exactly on each document) at the same output size, and
that camera frames the combined foreground of both to the raster target.

Sequence (V1). Documents are opened one at a time; neither stays open while the
other is used.

1. Standard view and native Zoom to Fit on each document; keep both cameras as
   numbers. Their orientations must agree, or the pair fails.
2. The starting shared camera is the fit that zooms out further (smaller Scale2),
   shrunk by ``steps.bootstrap_camera``.
3. Capture both documents with that one bootstrap camera. Each background is
   inferred independently; they must agree within the threshold, and A's then
   becomes the single classification reference for every later capture.
4. Both bootstrap foregrounds must be fully in view, and one must leave room to
   calibrate; otherwise the camera zooms farther out and both are captured again
   (``steps.find_bootstrap``, bounded). From here on, framing uses their combined
   extent.
5. Calibrate the shared camera on the member with more room to move, with the
   SOLIDWORKS signs required, then restore the bootstrap camera.
6. Solve from the combined extent; apply the solved camera to each document,
   capture, measure, combine, verify. Each member must also stay non-empty and
   unclipped. At most ``MAX_SOLVES`` solves; a second only when the combined result
   is measurable and merely outside tolerance.
7. Record ``edge_overlap`` between the final images as a diagnostic. It never
   affects success.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
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
from viewjig.measure import (
    BackgroundUncertain,
    Extent,
    Measurement,
    combine,
    edge_overlap,
    measure,
)
from viewjig.steps import (
    CAPTURE_ERRORS,
    MAX_SOLVES,
    CaptureError,
    Renderer,
    calibrate_around,
    calibration_room,
    constants,
    environment_record,
    failure_record,
    find_bootstrap,
    open_renderer,
    resolve_margin,
    room_problem,
    visibility_problem,
    write_record,
)
from viewjig.sw import StandardView

RECORD_FORMAT = "viewjig.pair"
RECORD_VERSION = 1
MEMBERS = ("a", "b")
# Native fits of the same standard view should report identical orientations; this
# only absorbs floating-point noise.
ORIENTATION_TOLERANCE = 1e-9

Opener = Callable[[Path], AbstractContextManager[Renderer]]


class PairError(CaptureError):
    """A pair that could not be completed; ``record`` says how far it got and why."""


@dataclass(frozen=True)
class PairRequest:
    path_a: Path
    path_b: Path
    view: StandardView
    width: int
    height: int
    margin_px: int | None = None
    threshold: int = 8
    tolerance_px: float = framing.DEFAULT_TOLERANCE_PX


@dataclass(frozen=True)
class PairResult:
    images: dict[str, Image.Image]
    camera: Camera
    measurements: dict[str, Measurement]
    combined: Extent
    record: dict[str, Any]


class _Pair:
    def __init__(self, opener: Opener, request: PairRequest, record: dict[str, Any]) -> None:
        self.opener = opener
        self.request = request
        self.record = record
        self.paths = {"a": request.path_a, "b": request.path_b}
        self.background: tuple[int, int, int] | None = None

    @contextmanager
    def session(self, member: str) -> Iterator[Renderer]:
        doc = self.record["documents"][member]
        doc["sessions"] += 1
        with self.opener(self.paths[member]) as renderer:
            yield renderer

    def capture(
        self, renderer: Renderer, member: str, camera: Camera
    ) -> tuple[Image.Image, Camera]:
        readback = renderer.apply_camera(camera)
        image = renderer.capture(self.request.width, self.request.height)
        self.record["documents"][member]["captures"] += 1
        return image, readback

    def shoot(
        self, renderer: Renderer, member: str, camera: Camera
    ) -> tuple[Image.Image, Measurement, Camera]:
        image, readback = self.capture(renderer, member, camera)
        m = measure(image, self.request.threshold, background=self.background)
        return image, m, readback

    def fail(self, message: str) -> PairError:
        return PairError(message, self.record)


def run_pair(opener: Opener, request: PairRequest) -> PairResult:
    """Run the V1 pair sequence; raise ``PairError`` (with its record) on any failure."""
    margin, margin_source = resolve_margin(request.width, request.height, request.margin_px)
    record: dict[str, Any] = {
        "format": RECORD_FORMAT,
        "version": RECORD_VERSION,
        "status": "running",
        "error": None,
        "input": {
            "a": str(request.path_a),
            "b": str(request.path_b),
            "view": request.view.name,
        },
        "output": {"width": request.width, "height": request.height},
        "margin": {"px": margin, "source": margin_source},
        "threshold": request.threshold,
        "tolerance_px": request.tolerance_px,
        "constants": {**constants(), "orientation_tolerance": ORIENTATION_TOLERANCE},
        "documents": {m: {"sessions": 0, "captures": 0} for m in MEMBERS},
        "calibration": {"pilot": {}, "steps": {}},
        "attempts": [],
    }
    pair = _Pair(opener, request, record)
    try:
        return _run(pair, margin)
    except PairError as exc:
        record["status"], record["error"] = "failed", str(exc)
        raise
    except CAPTURE_ERRORS as exc:
        record["status"], record["error"] = "failed", f"{type(exc).__name__}: {exc}"
        raise PairError(record["error"], record) from exc


def _member_failures(measurements: dict[str, Measurement]) -> list[str]:
    return [
        f"{member}: {problem}"
        for member, m in measurements.items()
        if (problem := visibility_problem(m)) is not None
    ]


@dataclass
class _Bootstrap:
    """What the accepted bootstrap attempt established."""

    readbacks: dict[str, Camera] | None = None
    inferred: dict[str, Measurement] | None = None
    colors: dict[str, tuple[int, int, int]] | None = None
    base: dict[str, Measurement] | None = None
    room: dict[str, int] | None = None
    member: str | None = None


def _try_bootstrap(
    pair: _Pair, camera: Camera, accepted: _Bootstrap
) -> tuple[str | None, dict[str, Any]]:
    """Capture both documents with ``camera``; return ``(problem, details)``."""
    request = pair.request
    images: dict[str, Image.Image] = {}
    readbacks: dict[str, Camera] = {}
    inferred: dict[str, Measurement] = {}
    details: dict[str, Any] = {"inferred": {}}
    for member in MEMBERS:
        with pair.session(member) as renderer:
            images[member], readbacks[member] = pair.capture(renderer, member, camera)
        try:
            inferred[member] = measure(images[member], request.threshold)
        except BackgroundUncertain as exc:
            details["inferred"][member] = {"error": str(exc)}
            return f"{member}: the background could not be identified", details
        details["inferred"][member] = inferred[member].to_dict()
    colors = {m: v.background.color for m, v in inferred.items()}
    disagreement = max(abs(p - q) for p, q in zip(colors["a"], colors["b"], strict=True))
    if disagreement > request.threshold:
        return (
            f"the backgrounds differ (a {colors['a']}, b {colors['b']}, "
            f"difference {disagreement} > threshold {request.threshold})"
        ), details
    try:
        base = {m: measure(images[m], request.threshold, background=colors["a"]) for m in MEMBERS}
    except BackgroundUncertain as exc:
        return "the shared background could not be confirmed", {**details, "error": str(exc)}
    details["measurements"] = {m: v.to_dict() for m, v in base.items()}
    failures = _member_failures(base)
    if failures:
        return "; ".join(failures), details
    room = {m: min(calibration_room(base[m], "x"), calibration_room(base[m], "y")) for m in MEMBERS}
    member = "a" if room["a"] >= room["b"] else "b"
    problem = room_problem(base[member], min(request.width, request.height))
    if problem is not None:
        return problem, details
    accepted.readbacks, accepted.inferred, accepted.colors = readbacks, inferred, colors
    accepted.base, accepted.room, accepted.member = base, room, member
    return None, details


def _run(pair: _Pair, margin: int) -> PairResult:
    request, record = pair.request, pair.record

    # 1. Native fits, one document at a time.
    fits: dict[str, Camera] = {}
    for member in MEMBERS:
        with pair.session(member) as renderer:
            renderer.show_standard_view(request.view)
            renderer.zoom_to_fit()
            fits[member] = renderer.read_camera()
    record["native_fit"] = {m: c.to_dict() for m, c in fits.items()}
    difference = max(
        abs(p - q) for p, q in zip(fits["a"].orientation, fits["b"].orientation, strict=True)
    )
    record["orientation_difference"] = difference
    if difference > ORIENTATION_TOLERANCE:
        raise pair.fail(
            f"the documents' {request.view.name} orientations differ by {difference:g}; "
            "a shared camera needs the same orientation"
        )

    # 2-4. Shared bootstrap: the farther-out fit, shrunk, and zoomed out further while
    # needed, until both parts are fully in view against agreeing backgrounds and one
    # of them leaves room to calibrate.
    start_member = "a" if fits["a"].scale <= fits["b"].scale else "b"
    record["shared_start"] = {"member": start_member, "camera": fits[start_member].to_dict()}
    accepted = _Bootstrap()
    record["bootstrap_attempts"] = []
    boot = find_bootstrap(
        fits[start_member],
        lambda camera: _try_bootstrap(pair, camera, accepted),
        record["bootstrap_attempts"],
        pair.fail,
        "both parts",
    )
    base, colors, room, cal_member = accepted.base, accepted.colors, accepted.room, accepted.member
    pair.background = colors["a"]
    record["background"] = {
        "inferred": {m: list(c) for m, c in colors.items()},
        "shared": list(pair.background),
    }
    combined = combine(base.values())
    record["bootstrap"] = {
        "camera": boot.to_dict(),
        "readback": {m: c.to_dict() for m, c in accepted.readbacks.items()},
        "inferred": {m: v.to_dict() for m, v in accepted.inferred.items()},
        "measurements": {m: v.to_dict() for m, v in base.items()},
        "combined": combined.to_dict(),
    }

    # 5. Calibrate on the member with more room to move, then restore the bootstrap.
    record["calibration"]["member"] = cal_member
    record["calibration"]["room_px"] = room
    with pair.session(cal_member) as renderer:
        calibration = calibrate_around(
            lambda camera: pair.shoot(renderer, cal_member, camera)[1],
            boot,
            base[cal_member],
            min(request.width, request.height),
            record["calibration"],
            pair.fail,
        )
        renderer.apply_camera(boot)

    # 6. Solve from the combined extent; apply the same camera to each document.
    camera, extent = boot, combined
    for attempt in range(1, MAX_SOLVES + 1):
        solution = framing.solve(camera, extent, calibration, margin)
        shots = {}
        for member in MEMBERS:
            with pair.session(member) as renderer:
                shots[member] = pair.shoot(renderer, member, solution.camera)
        measured = {m: s[1] for m, s in shots.items()}
        combined = combine(measured.values())
        verification = framing.verify(solution, combined, request.tolerance_px)
        failures = _member_failures(measured)
        ok = verification.ok and not failures
        record["attempts"].append(
            {
                "attempt": attempt,
                "solution": solution.to_dict(),
                "readback": {m: s[2].to_dict() for m, s in shots.items()},
                "measurements": {m: v.to_dict() for m, v in measured.items()},
                "combined": combined.to_dict(),
                "verification": verification.to_dict(),
                "member_failures": failures,
                "ok": ok,
            }
        )
        if ok:
            return _finish(pair, solution.camera, shots, combined)
        if failures or combined.is_empty or combined.clipped:
            raise pair.fail(
                f"attempt {attempt} is not measurable: {failures or list(verification.failures)}"
            )
        camera, extent = solution.camera, combined
    raise pair.fail(
        f"framing still outside tolerance after {MAX_SOLVES} solves: {verification.failures}"
    )


def _finish(
    pair: _Pair,
    camera: Camera,
    shots: dict[str, tuple[Image.Image, Measurement, Camera]],
    combined: Extent,
) -> PairResult:
    record = pair.record
    images = {m: s[0] for m, s in shots.items()}
    measured = {m: s[1] for m, s in shots.items()}
    record["status"] = "ok"
    record["final"] = {
        "camera": camera.to_dict(),
        "readback_exact": {m: s[2] == camera for m, s in shots.items()},
        "measurements": {m: v.to_dict() for m, v in measured.items()},
        "combined": combined.to_dict(),
    }
    # Diagnostic only: it can neither fail nor change the pair.
    try:
        diagnostic: dict[str, Any] = edge_overlap(images["a"], images["b"]).to_dict()
    except Exception as exc:  # noqa: BLE001 - a diagnostic must never fail the pair
        diagnostic = {"error": f"{type(exc).__name__}: {exc}"}
    record["diagnostics"] = {"edge_overlap": diagnostic}
    return PairResult(images, camera, measured, combined, record)


def pair(
    request: PairRequest,
    png_a: Path,
    png_b: Path,
    record_path: Path,
    *,
    app: sw.Com | None = None,
    opener: Opener | None = None,
) -> PairResult:
    """Run a pair against the running SOLIDWORKS and export it.

    The whole pair runs inside one controlled-background application environment
    (``application_background``); every document session gets its own plain white
    scene, restored before it closes (``open_renderer``).

    Writes the JSON record whether or not the pair succeeds; writes the two PNGs
    only on success, with every setting restored. ``app`` and ``opener`` replace the
    SOLIDWORKS connection and document opener (used by tests).
    """
    record: dict[str, Any] = {"format": RECORD_FORMAT, "version": RECORD_VERSION}
    app_env: ApplicationBackground | None = None
    documents: list[DocumentBackground] = []
    try:
        if app is None:
            app = sw.attach()
        if opener is None:
            connected = app

            def opener(path: Path) -> AbstractContextManager[Renderer]:
                return open_renderer(connected, path, documents)

        with application_background(app) as app_env:
            result = run_pair(opener, request)
            record = result.record
    except (PairError, sw.SolidWorksError) as exc:
        record = failure_record(exc, record)
        record["capture_environment"] = environment_record(app_env, documents)
        write_record(record, record_path)
        if isinstance(exc, PairError):
            raise
        raise PairError(record["error"], record) from exc
    record["capture_environment"] = environment_record(app_env, documents)
    for member, path in (("a", png_a), ("b", png_b)):
        path.parent.mkdir(parents=True, exist_ok=True)
        result.images[member].save(path, format="PNG")
    record["output"]["png"] = {"a": str(png_a), "b": str(png_b)}
    write_record(record, record_path)
    return result
