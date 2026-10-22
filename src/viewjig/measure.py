"""Raster-only foreground measurement.

This module knows nothing about SOLIDWORKS, cameras, or CAD geometry. It
describes where non-background pixels land in an image, so framing code can
reason about them.

Coordinate contract
-------------------
Pixel ``(x, y)`` covers the continuous square ``[x, x + 1) x [y, y + 1)``, with x
to the right and y down. An image of ``W x H`` pixels spans ``[0, W] x [0, H]``,
and its centre is ``(W / 2, H / 2)``.

``Bounds`` uses PIL's bbox convention: ``left`` and ``top`` are the first
foreground column and row, ``right`` and ``bottom`` are one past the last
(exclusive). Read as continuous coordinates, those same four numbers are the
foreground's edges, so ``width = right - left`` with no +/- 1 corrections.

Margins are distances from the foreground's continuous edges to the image's
continuous edges: ``left``, ``top``, ``W - right``, ``H - bottom``. For every
measurement, ``left margin + bounds width + right margin == W`` (likewise for
height). The foreground centre is the centre of its bounds, and the centre
offset is that centre minus the image centre (positive = right / down).

Foreground classification
-------------------------
A pixel is foreground when its largest per-channel absolute difference from
the background colour is strictly greater than ``threshold`` (V1 default 8).

The background colour is inferred from the one-pixel image border. It is the
most common border colour, accepted only if at least ``min_border_support`` of
border pixels are within ``threshold`` of it. Otherwise ``BackgroundUncertain``
is raised instead of producing a measurement. A foreground covering most of the
border would make the inference unreliable; passing ``expected_background``
turns that into an explicit check.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageChops

DEFAULT_THRESHOLD = 8
DEFAULT_MIN_BORDER_SUPPORT = 0.6

RGB = tuple[int, int, int]


class MeasurementError(Exception):
    pass


class BackgroundUncertain(MeasurementError):
    """The background colour could not be inferred confidently from the image border."""


class SizeMismatch(MeasurementError):
    pass


@dataclass(frozen=True)
class Bounds:
    """Foreground bounds; ``right`` and ``bottom`` are exclusive (continuous edges)."""

    left: int
    top: int
    right: int
    bottom: int

    def __post_init__(self) -> None:
        if not (0 <= self.left < self.right and 0 <= self.top < self.bottom):
            raise ValueError(f"empty or inverted bounds: {self}")

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def union(self, other: Bounds) -> Bounds:
        return Bounds(
            min(self.left, other.left),
            min(self.top, other.top),
            max(self.right, other.right),
            max(self.bottom, other.bottom),
        )


@dataclass(frozen=True)
class Sides:
    """One value per image side, in left, top, right, bottom order."""

    left: Any
    top: Any
    right: Any
    bottom: Any

    def as_tuple(self) -> tuple[Any, Any, Any, Any]:
        return (self.left, self.top, self.right, self.bottom)


@dataclass(frozen=True)
class Extent:
    """Where the foreground sits in an image of the given size; ``bounds`` is None if empty."""

    image_width: int
    image_height: int
    bounds: Bounds | None

    def __post_init__(self) -> None:
        if self.image_width <= 0 or self.image_height <= 0:
            raise ValueError("image dimensions must be positive")
        b = self.bounds
        if b is not None and (b.right > self.image_width or b.bottom > self.image_height):
            raise ValueError(f"bounds {b} exceed a {self.image_width}x{self.image_height} image")

    @property
    def is_empty(self) -> bool:
        return self.bounds is None

    @property
    def image_center(self) -> tuple[float, float]:
        return (self.image_width / 2, self.image_height / 2)

    @property
    def margins(self) -> Sides | None:
        b = self.bounds
        if b is None:
            return None
        return Sides(b.left, b.top, self.image_width - b.right, self.image_height - b.bottom)

    @property
    def center(self) -> tuple[float, float] | None:
        b = self.bounds
        return None if b is None else ((b.left + b.right) / 2, (b.top + b.bottom) / 2)

    @property
    def center_offset(self) -> tuple[float, float] | None:
        c = self.center
        if c is None:
            return None
        cx, cy = self.image_center
        return (c[0] - cx, c[1] - cy)

    @property
    def touches_edge(self) -> Sides | None:
        """Which image edges the foreground reaches; reaching one may mean it is clipped."""
        m = self.margins
        return None if m is None else Sides(*(v == 0 for v in m.as_tuple()))

    @property
    def clipped(self) -> bool:
        t = self.touches_edge
        return t is not None and any(t.as_tuple())

    def to_dict(self) -> dict[str, Any]:
        b, m, t = self.bounds, self.margins, self.touches_edge
        return {
            "image_size": [self.image_width, self.image_height],
            "bounds": None if b is None else [b.left, b.top, b.right, b.bottom],
            "margins": None if m is None else list(m.as_tuple()),
            "center": None if self.center is None else list(self.center),
            "center_offset": None if self.center_offset is None else list(self.center_offset),
            "touches_edge": None if t is None else list(t.as_tuple()),
            "clipped": self.clipped,
        }


@dataclass(frozen=True)
class Background:
    color: RGB
    border_support: float  # share of border pixels within threshold of ``color``
    border_pixels: int


@dataclass(frozen=True)
class Measurement(Extent):
    """An ``Extent`` measured from one image, with how it was classified."""

    threshold: int
    background: Background
    foreground_pixels: int

    def to_dict(self) -> dict[str, Any]:
        return {
            **super().to_dict(),
            "threshold": self.threshold,
            "background": {
                "color": list(self.background.color),
                "border_support": self.background.border_support,
                "border_pixels": self.background.border_pixels,
            },
            "foreground_pixels": self.foreground_pixels,
        }


def _rgb(image: Image.Image) -> Image.Image:
    return image if image.mode == "RGB" else image.convert("RGB")


def _check_threshold(threshold: int) -> None:
    if isinstance(threshold, bool) or not isinstance(threshold, int) or not 0 <= threshold < 255:
        raise ValueError(f"threshold must be an integer in [0, 255), got {threshold!r}")


def _difference(image: Image.Image, color: RGB) -> Image.Image:
    """Largest per-channel absolute difference from ``color``, as an 'L' image."""
    r, g, b = ImageChops.difference(image, Image.new("RGB", image.size, color)).split()
    return ImageChops.lighter(ImageChops.lighter(r, g), b)


def _border(image: Image.Image) -> Image.Image:
    """The one-pixel border ring as a single row; each border pixel appears once."""
    w, h = image.size
    strips = [
        image.crop((0, 0, w, 1)),
        image.crop((0, h - 1, w, h)),
        image.crop((0, 1, 1, h - 1)).transpose(Image.Transpose.ROTATE_90),
        image.crop((w - 1, 1, w, h - 1)).transpose(Image.Transpose.ROTATE_90),
    ]
    ring = Image.new("RGB", (sum(s.width for s in strips), 1))
    x = 0
    for strip in strips:
        ring.paste(strip, (x, 0))
        x += strip.width
    return ring


def infer_background(
    image: Image.Image,
    threshold: int = DEFAULT_THRESHOLD,
    *,
    min_border_support: float = DEFAULT_MIN_BORDER_SUPPORT,
    expected_background: RGB | None = None,
) -> Background:
    _check_threshold(threshold)
    rgb = _rgb(image)
    if rgb.width < 3 or rgb.height < 3:
        raise MeasurementError(f"image too small to have a border: {rgb.width}x{rgb.height}")
    ring = _border(rgb)
    counts: Counter[RGB] = Counter()
    for n, color in ring.getcolors(ring.width) or []:
        counts[color] += n
    color, _ = counts.most_common(1)[0]
    near = _difference(ring, color).point(lambda v: 255 if v <= threshold else 0)
    support = near.histogram()[255] / ring.width
    if support < min_border_support:
        raise BackgroundUncertain(
            f"most common border colour {color} covers only {support:.1%} of the border "
            f"(within threshold {threshold}); need {min_border_support:.0%}"
        )
    if expected_background is not None:
        delta = max(abs(a - b) for a, b in zip(color, expected_background, strict=True))
        if delta > threshold:
            raise BackgroundUncertain(
                f"border suggests background {color}, expected {tuple(expected_background)}"
            )
    return Background(color, support, ring.width)


def foreground_mask(
    image: Image.Image, background: RGB, threshold: int = DEFAULT_THRESHOLD
) -> Image.Image:
    """'L' mask: 255 where the pixel is foreground, 0 elsewhere."""
    _check_threshold(threshold)
    return _difference(_rgb(image), background).point(lambda v: 255 if v > threshold else 0)


def measure(
    image: Image.Image,
    threshold: int = DEFAULT_THRESHOLD,
    *,
    min_border_support: float = DEFAULT_MIN_BORDER_SUPPORT,
    expected_background: RGB | None = None,
) -> Measurement:
    background = infer_background(
        image,
        threshold,
        min_border_support=min_border_support,
        expected_background=expected_background,
    )
    mask = foreground_mask(image, background.color, threshold)
    box = mask.getbbox()
    return Measurement(
        image_width=image.width,
        image_height=image.height,
        bounds=None if box is None else Bounds(*box),
        threshold=threshold,
        background=background,
        foreground_pixels=mask.histogram()[255],
    )


def combine(extents: Iterable[Extent]) -> Extent:
    """The single foreground extent covering every member of a same-size image set."""
    members = list(extents)
    if not members:
        raise ValueError("combine needs at least one extent")
    size = (members[0].image_width, members[0].image_height)
    for e in members:
        if (e.image_width, e.image_height) != size:
            raise SizeMismatch(f"cannot combine {e.image_width}x{e.image_height} with {size}")
    bounds: Bounds | None = None
    for e in members:
        if e.bounds is not None:
            bounds = e.bounds if bounds is None else bounds.union(e.bounds)
    return Extent(size[0], size[1], bounds)


# --- Diagnostic only ------------------------------------------------------------------


DEFAULT_LINE_LUMINANCE = 64
DEFAULT_MAX_SHIFT = 3


@dataclass(frozen=True)
class EdgeOverlap:
    """How many line pixels of two images coincide under small integer translations.

    A diagnostic, not a criterion. It says how well dark line pixels line up when
    image ``b`` is moved by ``(dx, dy)`` (b's pixel ``(x, y)`` placed at
    ``(x + dx, y + dy)``). It makes no claim that any lines correspond to the same
    geometry, and images with no shared lines simply show no clear peak.
    """

    line_luminance: int
    max_shift: int
    line_pixels: tuple[int, int]
    counts: tuple[tuple[tuple[int, int], int], ...]

    def count(self, dx: int, dy: int) -> int:
        return dict(self.counts)[(dx, dy)]

    @property
    def zero_shift(self) -> int:
        return self.count(0, 0)

    @property
    def zero_shift_share(self) -> tuple[float | None, float | None]:
        a, b = self.line_pixels
        z = self.zero_shift
        return (z / a if a else None, z / b if b else None)

    @property
    def best_shift(self) -> tuple[int, int]:
        # Ties resolve to the shift closest to (0, 0), then in scan order.
        return max(self.counts, key=lambda c: (c[1], -(abs(c[0][0]) + abs(c[0][1]))))[0]

    @property
    def peak_ratio(self) -> float | None:
        """Best count over the next best; near 1 means no clear peak."""
        ordered = sorted((n for _, n in self.counts), reverse=True)
        return ordered[0] / ordered[1] if ordered[1] else None

    @property
    def peak_inside_range(self) -> bool:
        dx, dy = self.best_shift
        return max(abs(dx), abs(dy)) < self.max_shift


def line_mask(image: Image.Image, line_luminance: int = DEFAULT_LINE_LUMINANCE) -> Image.Image:
    """255 where the pixel's luminance is at or below ``line_luminance`` (dark lines)."""
    return image.convert("L").point(lambda v: 255 if v <= line_luminance else 0)


def edge_overlap(
    a: Image.Image,
    b: Image.Image,
    *,
    line_luminance: int = DEFAULT_LINE_LUMINANCE,
    max_shift: int = DEFAULT_MAX_SHIFT,
) -> EdgeOverlap:
    if a.size != b.size:
        raise SizeMismatch(f"edge overlap needs same-size images: {a.size} vs {b.size}")
    la, lb = line_mask(a, line_luminance), line_mask(b, line_luminance)
    counts = []
    for dy in range(-max_shift, max_shift + 1):
        for dx in range(-max_shift, max_shift + 1):
            moved = Image.new("L", lb.size, 0)
            moved.paste(lb, (dx, dy))
            counts.append(((dx, dy), ImageChops.multiply(la, moved).histogram()[255]))
    return EdgeOverlap(
        line_luminance=line_luminance,
        max_shift=max_shift,
        line_pixels=(la.histogram()[255], lb.histogram()[255]),
        counts=tuple(counts),
    )
