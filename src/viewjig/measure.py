"""Pixel-only framing measurements.

This module intentionally contains no SOLIDWORKS or CAD semantics. Its job is
to describe where rendered foreground pixels land inside an image.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Bounds:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left + 1

    @property
    def height(self) -> int:
        return self.bottom - self.top + 1


@dataclass(frozen=True)
class FramingMeasurement:
    image_width: int
    image_height: int
    foreground: Bounds

    @property
    def center_error_x(self) -> float:
        image_center = (self.image_width - 1) / 2.0
        foreground_center = (self.foreground.left + self.foreground.right) / 2.0
        return foreground_center - image_center

    @property
    def center_error_y(self) -> float:
        image_center = (self.image_height - 1) / 2.0
        foreground_center = (self.foreground.top + self.foreground.bottom) / 2.0
        return foreground_center - image_center

    @property
    def margins(self) -> tuple[int, int, int, int]:
        return (
            self.foreground.left,
            self.foreground.top,
            self.image_width - 1 - self.foreground.right,
            self.image_height - 1 - self.foreground.bottom,
        )
