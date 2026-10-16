"""Command-line entry point for ViewJig."""

from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="viewjig",
        description="Repeatable, matched CAD image capture.",
    )
    subparsers = parser.add_subparsers(dest="command")

    shot = subparsers.add_parser("shot", help="Capture one SOLIDWORKS document.")
    shot.add_argument("model")
    shot.add_argument("--view", default="iso")
    shot.add_argument("--size", default="1600x1200")

    pair = subparsers.add_parser("pair", help="Capture two documents with matched framing.")
    pair.add_argument("before")
    pair.add_argument("after")
    pair.add_argument("--view", default="iso")
    pair.add_argument("--size", default="1600x1200")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 0

    parser.error("capture implementation is not available yet")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
