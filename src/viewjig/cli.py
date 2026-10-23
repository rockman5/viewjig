"""Command-line entry point: ``viewjig shot`` and ``viewjig pair``.

Arguments are validated before SOLIDWORKS is touched. Expected operational
failures (a ``ShotError`` or ``PairError``) print what failed, a next step where
one is known, that the part files were not modified, whether any temporary
SOLIDWORKS setting was restored, and the path of the JSON record; they exit with
status 1. Usage errors exit with status 2.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from viewjig import __version__
from viewjig.pair import PairError, PairRequest, pair
from viewjig.shot import ShotError, ShotRequest, resolve_margin, shoot
from viewjig.steps import CaptureError
from viewjig.sw import (
    DocumentAlreadyOpen,
    DocumentNotReadOnly,
    DocumentOpenFailed,
    SolidWorksNotRunning,
    StandardView,
)

DEFAULT_WIDTH = 1600
DEFAULT_HEIGHT = 1200
SUPPORTED_SUFFIXES = (".sldprt",)
# Views validated against live SOLIDWORKS for V1. StandardView itself has all nine.
VIEWS = {"isometric": StandardView.ISOMETRIC, "front": StandardView.FRONT}

EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected a whole number of pixels, got {text!r}"
        ) from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be greater than 0, got {value}")
    return value


def _non_negative_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected a whole number of pixels, got {text!r}"
        ) from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {value}")
    return value


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--view",
        choices=sorted(VIEWS),
        default="isometric",
        help="view to capture: isometric or front (default: isometric)",
    )
    parser.add_argument(
        "--width", type=_positive_int, default=DEFAULT_WIDTH, help="output width in pixels"
    )
    parser.add_argument(
        "--height", type=_positive_int, default=DEFAULT_HEIGHT, help="output height in pixels"
    )
    parser.add_argument(
        "--margin",
        type=_non_negative_int,
        default=None,
        metavar="PX",
        help="margin around the framed model, in pixels (default: 5%% of the shorter output side)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="viewjig",
        description=(
            "Repeatable camera jig for SOLIDWORKS. Captures parts at a fixed output size "
            "with consistent position, scale, and framing. SOLIDWORKS must already be "
            "running; documents are opened read-only and never saved."
        ),
    )
    parser.add_argument("--version", action="version", version=f"viewjig {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="{shot,pair}")

    shot = commands.add_parser(
        "shot",
        help="frame and capture one part",
        description="Frame one part in a standard view and export a PNG plus a JSON record.",
    )
    shot.add_argument("part", type=Path, help="the .SLDPRT file to capture")
    _add_common(shot)
    shot.add_argument(
        "--output",
        type=Path,
        metavar="PNG",
        help="PNG path; the record is written beside it with a .json suffix "
        "(default: <part>-<view>-<W>x<H>.png in the current directory). "
        "Existing files at these paths are overwritten.",
    )

    pair_cmd = commands.add_parser(
        "pair",
        help="capture two parts with one shared camera",
        description=(
            "Capture two parts with one shared camera at one output size, so the images "
            "are registered. Exports <base>-before.png, <base>-after.png, and <base>.json."
        ),
    )
    pair_cmd.add_argument("before", type=Path, help="the first .SLDPRT file")
    pair_cmd.add_argument("after", type=Path, help="the second .SLDPRT file")
    _add_common(pair_cmd)
    pair_cmd.add_argument(
        "--output",
        type=Path,
        metavar="BASE",
        help="output base path without extension "
        "(default: <before>-vs-<after>-<view>-<W>x<H> in the current directory). "
        "Existing files at the resulting paths are overwritten.",
    )
    return parser


def _check_part(parser: argparse.ArgumentParser, path: Path, what: str) -> None:
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        parser.error(f"{what} must be a SOLIDWORKS part (.SLDPRT): {path}")
    if not path.is_file():
        parser.error(f"{what} not found: {path}")


def _check_margin(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    margin, _ = resolve_margin(args.width, args.height, args.margin)
    if 2 * margin >= min(args.width, args.height):
        parser.error(f"a {margin} px margin leaves no room in a {args.width}x{args.height} image")


def shot_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    if args.output is not None:
        png = (
            args.output if args.output.suffix.lower() == ".png" else args.output.with_suffix(".png")
        )
    else:
        png = Path(f"{args.part.stem}-{args.view}-{args.width}x{args.height}.png")
    return png, png.with_suffix(".json")


def pair_paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    if args.output is not None:
        base = args.output
        if base.suffix.lower() in (".png", ".json"):
            base = base.with_suffix("")
    else:
        base = Path(
            f"{args.before.stem}-vs-{args.after.stem}-{args.view}-{args.width}x{args.height}"
        )
    return (
        base.with_name(base.name + "-before.png"),
        base.with_name(base.name + "-after.png"),
        base.with_name(base.name + ".json"),
    )


def _attempt_summary(record: dict[str, Any]) -> str:
    attempts = record.get("attempts", [])
    last = attempts[-1]["verification"] if attempts else {}
    residuals = last.get("margin_residuals") or []
    worst = max((abs(r) for r in residuals), default=0.0)
    corrections = f"{len(attempts)} correction" + ("" if len(attempts) == 1 else "s")
    return (
        f"{corrections}, verified within {last.get('tolerance_px')} px (worst side {worst:.1f} px)"
    )


def _margin_text(record: dict[str, Any]) -> str:
    margin = record["margin"]
    return f"{margin['px']} px" + (" (5% default)" if margin["source"] == "default" else "")


def _causes(exc: BaseException) -> list[BaseException]:
    chain, current = [], exc.__cause__ or exc.__context__
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _headline(exc: CaptureError) -> str:
    """What failed, in the underlying error's own words (no internal class names)."""
    for cause in _causes(exc):
        if not isinstance(cause, CaptureError):
            return str(cause)
    return str(exc)


# Next steps only for failures with a clear user action.
NEXT_STEPS = (
    (SolidWorksNotRunning, "Start SOLIDWORKS, then run the command again."),
    (DocumentAlreadyOpen, "Close it in SOLIDWORKS, then run the command again."),
    (DocumentNotReadOnly, "Close it in SOLIDWORKS, then run the command again."),
    (DocumentOpenFailed, "Check that SOLIDWORKS can open it on its own."),
)


def _next_step(exc: CaptureError) -> str | None:
    for cause in _causes(exc):
        for kind, advice in NEXT_STEPS:
            if isinstance(cause, kind):
                return advice
    return None


def _settings_text(record: dict[str, Any]) -> str:
    """Whether the temporary capture environment (application and document scenes) was undone."""
    environment = record.get("capture_environment") or {}
    application = environment.get("application")
    documents = (environment.get("documents") or {}).values()
    changed, restored = False, True
    if application is not None:
        changed = bool(application.get("temporarily_switched_to_document_scene"))
        if changed or application.get("restored") is False:
            restored = application.get("restored") is True
    for document in documents:  # a document is listed only once its scene was made white
        changed = True
        restored = restored and document.get("all_restored") is True
    if not restored:
        return "could NOT be fully restored; see the message above."
    return "restored." if changed else "unchanged."


def _report_failure(command: str, exc: CaptureError, record_path: Path) -> int:
    lines = [f"viewjig {command} failed: {_headline(exc)}"]
    if (advice := _next_step(exc)) is not None:
        lines.append(f"  Next: {advice}")
    lines.append(
        "  Your part files were not modified (ViewJig opens them read-only and never saves)."
    )
    lines.append(f"  SOLIDWORKS settings: {_settings_text(exc.record)}")
    lines.append(f"  Details: {record_path.resolve()}")
    print("\n".join(lines), file=sys.stderr)
    return EXIT_FAILED


def _record_line(record_path: Path) -> str:
    return f"  record: {record_path.resolve()}"


def run_shot_command(args: argparse.Namespace) -> int:
    png, record_path = shot_paths(args)
    request = ShotRequest(
        path=args.part,
        view=VIEWS[args.view],
        width=args.width,
        height=args.height,
        margin_px=args.margin,
    )
    try:
        result = shoot(request, png, record_path)
    except ShotError as exc:
        return _report_failure("shot", exc, record_path)
    r = result.record
    print(f"viewjig shot: {png.resolve()}")
    print(
        f"  {args.view}, {args.width}x{args.height}, margin {_margin_text(r)}, {_attempt_summary(r)}"
    )
    print(_record_line(record_path))
    return EXIT_OK


def run_pair_command(args: argparse.Namespace) -> int:
    before_png, after_png, record_path = pair_paths(args)
    request = PairRequest(
        path_a=args.before,
        path_b=args.after,
        view=VIEWS[args.view],
        width=args.width,
        height=args.height,
        margin_px=args.margin,
    )
    try:
        result = pair(request, before_png, after_png, record_path)
    except PairError as exc:
        return _report_failure("pair", exc, record_path)
    r = result.record
    exact = r["final"]["readback_exact"]
    print(f"viewjig pair: {before_png.resolve()}")
    print(f"              {after_png.resolve()}")
    print(
        f"  {args.view}, {args.width}x{args.height}, margin {_margin_text(r)}, {_attempt_summary(r)}"
    )
    print(f"  one shared camera, read back exactly on both: {exact['a'] and exact['b']}")
    print(_record_line(record_path))
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    if args.command == "shot":
        _check_part(parser, args.part, "part")
        _check_margin(parser, args)
        return run_shot_command(args)
    _check_part(parser, args.before, "before")
    _check_part(parser, args.after, "after")
    if args.before.resolve() == args.after.resolve():
        parser.error("before and after must be two different files")
    _check_margin(parser, args)
    return run_pair_command(args)


if __name__ == "__main__":
    raise SystemExit(main())
