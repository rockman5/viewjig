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
4. Both bootstrap foregrounds must be non-empty and clear of every edge. From here
   on, framing uses their combined extent.
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
from viewjig.measure import Extent, Measurement, combine, edge_overlap, measure
from viewjig.steps import (
    CAPTURE_ERRORS,
    MAX_SOLVES,
    CaptureError,
    Renderer,
    bootstrap_camera,
    calibrate_around,
    calibration_room,
    constants,
    open_renderer,
    resolve_margin,
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

    def shoot(
        self, renderer: Renderer, member: str, camera: Camera
    ) -> tuple[Image.Image, Measurement, Camera]:
        readback = renderer.apply_camera(camera)
        image = renderer.capture(self.request.width, self.request.height)
        self.record["documents"][member]["captures"] += 1
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
    failures = []
    for member, m in measurements.items():
        if m.is_empty:
            failures.append(f"{member}: no foreground")
        elif m.clipped:
            failures.append(f"{member}: foreground touches the image edge {m.touches_edge}")
    return failures


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

    # 2. Shared starting camera: the farther-out fit.
    start_member = "a" if fits["a"].scale <= fits["b"].scale else "b"
    boot = bootstrap_camera(fits[start_member])
    record["shared_start"] = {"member": start_member, "camera": fits[start_member].to_dict()}

    # 3-4. Bootstrap both with the same camera; agree one background.
    images: dict[str, Image.Image] = {}
    inferred: dict[str, Measurement] = {}
    readbacks: dict[str, Camera] = {}
    for member in MEMBERS:
        with pair.session(member) as renderer:
            images[member], inferred[member], readbacks[member] = pair.shoot(renderer, member, boot)
    record["bootstrap"] = {
        "camera": boot.to_dict(),
        "readback": {m: c.to_dict() for m, c in readbacks.items()},
        "inferred": {m: v.to_dict() for m, v in inferred.items()},
    }
    colors = {m: v.background.color for m, v in inferred.items()}
    record["background"] = {"inferred": {m: list(c) for m, c in colors.items()}}
    disagreement = max(abs(p - q) for p, q in zip(colors["a"], colors["b"], strict=True))
    if disagreement > request.threshold:
        raise pair.fail(
            f"bootstrap backgrounds differ: a {colors['a']}, b {colors['b']} "
            f"(difference {disagreement} > threshold {request.threshold})"
        )
    pair.background = colors["a"]
    record["background"]["shared"] = list(pair.background)
    base = {m: measure(images[m], request.threshold, background=pair.background) for m in MEMBERS}
    record["bootstrap"]["measurements"] = {m: v.to_dict() for m, v in base.items()}
    failures = _member_failures(base)
    if failures:
        raise pair.fail(f"bootstrap: {'; '.join(failures)}")
    combined = combine(base.values())
    record["bootstrap"]["combined"] = combined.to_dict()

    # 5. Calibrate on the member with more room to move, then restore the bootstrap.
    room = {m: min(calibration_room(base[m], "x"), calibration_room(base[m], "y")) for m in MEMBERS}
    cal_member = "a" if room["a"] >= room["b"] else "b"
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
    opener: Opener | None = None,
) -> PairResult:
    """Run a pair against the running SOLIDWORKS (or ``opener``) and export it.

    Writes the JSON record whether or not the pair succeeds; writes the two PNGs
    only on success.
    """
    try:
        if opener is None:
            app = sw.attach()

            def opener(path: Path) -> AbstractContextManager[Renderer]:
                return open_renderer(app, path)

        result = run_pair(opener, request)
    except PairError as exc:
        write_record(exc.record, record_path)
        raise
    except sw.SolidWorksError as exc:  # attaching failed: nothing has run yet
        record = {"format": RECORD_FORMAT, "version": RECORD_VERSION, "status": "failed"}
        record["error"] = f"{type(exc).__name__}: {exc}"
        write_record(record, record_path)
        raise PairError(record["error"], record) from exc
    record = result.record
    for member, path in (("a", png_a), ("b", png_b)):
        path.parent.mkdir(parents=True, exist_ok=True)
        result.images[member].save(path, format="PNG")
    record["output"]["png"] = {"a": str(png_a), "b": str(png_b)}
    write_record(record, record_path)
    return result
