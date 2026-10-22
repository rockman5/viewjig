from __future__ import annotations

import dataclasses
import json
import random

import pytest
from PIL import Image, ImageDraw

from viewjig.measure import (
    BackgroundUncertain,
    Bounds,
    Extent,
    MeasurementError,
    Sides,
    SizeMismatch,
    combine,
    edge_overlap,
    foreground_mask,
    infer_background,
    measure,
)

WHITE = (255, 255, 255)
GREY = (194, 194, 194)


def canvas(w: int, h: int, color=WHITE) -> Image.Image:
    return Image.new("RGB", (w, h), color)


def with_rect(image: Image.Image, box: tuple[int, int, int, int], color=GREY) -> Image.Image:
    """Fill the exclusive box (left, top, right, bottom)."""
    image.paste(color, box)
    return image


# --- Geometry and coordinate conventions ---------------------------------------------------


@pytest.mark.parametrize(
    "size, box, margins, center, offset",
    [
        # Odd image (centre 50.5, 40.5), odd-sized 41x41 foreground: exactly centred.
        ((101, 81), (30, 20, 71, 61), (30, 20, 30, 20), (50.5, 40.5), (0.0, 0.0)),
        # Even image (centre 50, 40), even-sized 40x40 foreground: exactly centred.
        ((100, 80), (30, 20, 70, 60), (30, 20, 30, 20), (50.0, 40.0), (0.0, 0.0)),
        # Odd image, even-sized foreground: cannot be centred; nearest is half a pixel off.
        ((101, 81), (30, 20, 70, 60), (30, 20, 31, 21), (50.0, 40.0), (-0.5, -0.5)),
        # Even image, odd-sized foreground: likewise.
        ((100, 80), (30, 20, 71, 61), (30, 20, 29, 19), (50.5, 40.5), (0.5, 0.5)),
    ],
)
def test_centring_conventions_for_odd_and_even_sizes(size, box, margins, center, offset) -> None:
    m = measure(with_rect(canvas(*size), box))
    assert m.bounds == Bounds(*box)
    assert m.margins.as_tuple() == margins
    assert m.center == center
    assert m.image_center == (size[0] / 2, size[1] / 2)
    assert m.center_offset == offset


@pytest.mark.parametrize("size", [(100, 80), (101, 81), (64, 63), (3, 3)])
def test_margins_and_bounds_partition_the_image(size) -> None:
    w, h = size
    box = (1, 1, w - 1, h - 1) if min(size) == 3 else (5, 7, w - 11, h - 2)
    m = measure(with_rect(canvas(w, h), box))
    mg = m.margins
    assert mg.left + m.bounds.width + mg.right == w
    assert mg.top + m.bounds.height + mg.bottom == h


def test_exact_rectangle_geometry() -> None:
    m = measure(with_rect(canvas(160, 120), (40, 30, 100, 75)))
    assert (m.bounds.width, m.bounds.height) == (60, 45)
    assert m.foreground_pixels == 60 * 45
    assert (m.image_width, m.image_height) == (160, 120)


def test_off_centre_foreground_reports_signed_offset() -> None:
    m = measure(with_rect(canvas(200, 100), (10, 20, 60, 50)))
    assert m.margins.as_tuple() == (10, 20, 140, 50)
    assert m.center == (35.0, 35.0)
    assert m.center_offset == (-65.0, -15.0)  # left of and above centre


def test_single_pixel_foreground_has_unit_bounds_and_pixel_centre() -> None:
    image = canvas(9, 7)
    image.putpixel((4, 2), (0, 0, 0))
    m = measure(image)
    assert m.bounds == Bounds(4, 2, 5, 3)
    assert m.center == (4.5, 2.5)
    assert m.center_offset == (0.0, -1.0)


# --- Clipping ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "box, touches",
    [
        ((0, 20, 30, 40), (True, False, False, False)),
        ((20, 0, 40, 30), (False, True, False, False)),
        ((70, 20, 100, 40), (False, False, True, False)),
        ((20, 50, 40, 80), (False, False, False, True)),
        ((0, 0, 30, 30), (True, True, False, False)),
        ((70, 0, 100, 30), (False, True, True, False)),
        ((70, 50, 100, 80), (False, False, True, True)),
        ((0, 50, 30, 80), (True, False, False, True)),
        ((1, 1, 99, 79), (False, False, False, False)),
    ],
)
def test_touching_each_edge_and_corner(box, touches) -> None:
    m = measure(with_rect(canvas(100, 80), box))
    assert m.touches_edge.as_tuple() == touches
    assert m.clipped is any(touches)
    assert m.margins.as_tuple() == tuple(v for v in (box[0], box[1], 100 - box[2], 80 - box[3]))


# --- Empty foreground ----------------------------------------------------------------------


def test_empty_foreground_has_no_geometry() -> None:
    m = measure(canvas(50, 40))
    assert m.is_empty and m.bounds is None
    assert m.margins is None and m.center is None and m.center_offset is None
    assert m.touches_edge is None and m.clipped is False
    assert m.foreground_pixels == 0
    assert m.to_dict()["bounds"] is None


# --- Threshold ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, threshold, foreground",
    [
        (247, 8, False),  # difference 8: not strictly greater
        (246, 8, True),  # difference 9
        (254, 0, True),
        (255, 0, False),
        (200, 55, False),
        (199, 55, True),
    ],
)
def test_threshold_is_strictly_greater_than(value, threshold, foreground) -> None:
    image = canvas(20, 20)
    image.putpixel((10, 10), (value, value, value))
    assert measure(image, threshold).is_empty is not foreground


def test_classification_uses_the_largest_channel_difference() -> None:
    image = canvas(20, 20)
    image.putpixel((5, 5), (255, 255, 246))  # one channel differs by 9
    image.putpixel((15, 15), (248, 248, 248))  # all channels differ by 7
    m = measure(image)
    assert m.bounds == Bounds(5, 5, 6, 6)
    assert m.foreground_pixels == 1


@pytest.mark.parametrize("threshold", [-1, 255, 8.0, True])
def test_invalid_thresholds_are_rejected(threshold) -> None:
    with pytest.raises(ValueError):
        measure(canvas(10, 10), threshold)


def test_soft_antialiased_edges_follow_the_threshold() -> None:
    image = with_rect(canvas(60, 40), (20, 10, 40, 30), (120, 120, 130))
    # A one-pixel halo just inside the threshold and another just outside it.
    ImageDraw.Draw(image).rectangle((18, 8, 41, 31), outline=(250, 250, 250))  # diff 5
    ImageDraw.Draw(image).rectangle((19, 9, 40, 30), outline=(240, 240, 240))  # diff 15
    # Shaded interior: every value differs from white by more than 8.
    for x in range(20, 40):
        for y in range(10, 30):
            image.putpixel((x, y), (100 + x, 100 + y, 150))
    m = measure(image)
    assert m.bounds == Bounds(19, 9, 41, 31)
    assert measure(image, threshold=4).bounds == Bounds(18, 8, 42, 32)
    assert measure(image, threshold=20).bounds == Bounds(20, 10, 40, 30)


# --- Background inference ------------------------------------------------------------------


def test_background_is_the_dominant_border_colour() -> None:
    image = with_rect(canvas(80, 60, (40, 50, 60)), (10, 10, 70, 50), WHITE)
    m = measure(image)
    assert m.background.color == (40, 50, 60)
    assert m.background.border_support == 1.0
    assert m.background.border_pixels == 2 * 80 + 2 * 60 - 4
    assert m.bounds == Bounds(10, 10, 70, 50)


def test_noisy_background_within_threshold_is_accepted() -> None:
    rng = random.Random(7)
    image = canvas(60, 40)
    for x in range(60):
        for y in range(40):
            v = 255 - rng.randint(0, 4)
            image.putpixel((x, y), (v, v, v))
    with_rect(image, (20, 10, 40, 30), GREY)
    m = measure(image)
    assert m.background.border_support == 1.0
    assert m.bounds == Bounds(20, 10, 40, 30)


def test_foreground_touching_one_full_edge_does_not_disturb_inference() -> None:
    m = measure(with_rect(canvas(160, 120), (0, 0, 40, 120)))
    assert m.background.color == WHITE
    assert m.background.border_support == pytest.approx(
        1 - (120 + 2 * 39) / (2 * 160 + 2 * 120 - 4)
    )
    assert m.bounds == Bounds(0, 0, 40, 120)
    assert m.touches_edge.as_tuple() == (True, True, False, True)


def test_border_without_a_dominant_colour_fails_explicitly() -> None:
    # Left half grey, right half white: neither colour has 60 % of the border.
    image = with_rect(canvas(100, 80), (0, 0, 50, 80))
    with pytest.raises(BackgroundUncertain, match="covers only"):
        measure(image)


def test_gradient_background_fails_explicitly() -> None:
    image = canvas(100, 80)
    for x in range(100):
        for y in range(80):
            image.putpixel((x, y), (2 * x, 2 * y, 128))
    with pytest.raises(BackgroundUncertain, match="covers only"):
        measure(image)


def test_expected_background_is_checked() -> None:
    image = with_rect(canvas(50, 40), (10, 10, 20, 20))
    assert measure(image, expected_background=(250, 250, 250)).background.color == WHITE
    with pytest.raises(BackgroundUncertain, match="expected"):
        measure(image, expected_background=(0, 0, 0))


def test_a_clipped_model_that_dominates_the_border_is_caught_by_expected_background() -> None:
    # Grey covers most of the border, so no border statistic can tell it from background;
    # without expected_background it is inferred as background. The caller knows better.
    image = with_rect(canvas(100, 80), (0, 0, 100, 80), GREY)
    image.paste(WHITE, (0, 0, 100, 10))
    assert infer_background(image).color == GREY
    with pytest.raises(BackgroundUncertain, match="expected"):
        measure(image, expected_background=WHITE)


def test_min_border_support_is_configurable() -> None:
    # Grey on 158 of 356 border pixels (44 %); white on 198 (55.6 %).
    image = with_rect(canvas(100, 80), (0, 0, 40, 80))
    assert measure(image, min_border_support=0.55).background.color == WHITE
    with pytest.raises(BackgroundUncertain):
        measure(image, min_border_support=0.7)


def test_images_too_small_for_a_border_are_rejected() -> None:
    with pytest.raises(MeasurementError, match="too small"):
        measure(canvas(2, 5))


@pytest.mark.parametrize("mode", ["RGBA", "L", "P"])
def test_other_image_modes_are_measured_as_rgb(mode) -> None:
    image = with_rect(canvas(40, 30), (10, 5, 30, 25), (0, 0, 0)).convert(mode)
    assert measure(image).bounds == Bounds(10, 5, 30, 25)


def test_foreground_mask_matches_measurement() -> None:
    image = with_rect(canvas(40, 30), (10, 5, 30, 25))
    mask = foreground_mask(image, WHITE)
    assert mask.mode == "L" and mask.getbbox() == (10, 5, 30, 25)
    assert mask.histogram()[255] == measure(image).foreground_pixels


# --- Combined extents ----------------------------------------------------------------------


def test_combined_extent_covers_every_member() -> None:
    a = measure(with_rect(canvas(200, 100), (20, 40, 80, 90)))
    b = measure(with_rect(canvas(200, 100), (60, 10, 150, 70)))
    both = combine([a, b])
    assert isinstance(both, Extent)
    assert both.bounds == Bounds(20, 10, 150, 90)
    assert both.margins.as_tuple() == (20, 10, 50, 10)
    assert both.center == (85.0, 50.0)
    assert both.center_offset == (-15.0, 0.0)


def test_combined_extent_equals_bbox_of_unioned_masks() -> None:
    a = with_rect(canvas(120, 90), (5, 30, 40, 60))
    b = with_rect(canvas(120, 90), (70, 20, 110, 85))
    union = Image.new("L", a.size)
    union.paste(255, mask=foreground_mask(a, WHITE))
    union.paste(255, mask=foreground_mask(b, WHITE))
    assert combine([measure(a), measure(b)]).bounds == Bounds(*union.getbbox())


def test_combined_extent_ignores_empty_members_and_reports_clipping() -> None:
    a = measure(with_rect(canvas(100, 80), (0, 10, 30, 40)))
    empty = measure(canvas(100, 80))
    both = combine([empty, a])
    assert both.bounds == a.bounds and both.clipped
    assert combine([empty, empty]).is_empty


def test_combining_different_sizes_is_rejected() -> None:
    with pytest.raises(SizeMismatch):
        combine([measure(canvas(100, 80)), measure(canvas(100, 81))])
    with pytest.raises(ValueError):
        combine([])


# --- Data model ----------------------------------------------------------------------------


def test_measurements_are_immutable_and_serialisable() -> None:
    m = measure(with_rect(canvas(101, 81), (30, 20, 71, 61)))
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.bounds = None  # type: ignore[misc]
    data = json.loads(json.dumps(m.to_dict()))
    assert data["bounds"] == [30, 20, 71, 61]
    assert data["margins"] == [30, 20, 30, 20]
    assert data["center"] == [50.5, 40.5] and data["center_offset"] == [0.0, 0.0]
    assert data["touches_edge"] == [False, False, False, False]
    assert data["threshold"] == 8 and data["background"]["color"] == [255, 255, 255]


@pytest.mark.parametrize("box", [(5, 5, 5, 10), (5, 5, 10, 5), (-1, 0, 3, 3), (6, 0, 5, 3)])
def test_invalid_bounds_are_rejected(box) -> None:
    with pytest.raises(ValueError):
        Bounds(*box)


def test_extent_rejects_bounds_outside_the_image() -> None:
    with pytest.raises(ValueError):
        Extent(10, 10, Bounds(0, 0, 11, 5))
    assert Extent(10, 10, Bounds(0, 0, 10, 10)).margins == Sides(0, 0, 0, 0)


# --- Edge-overlap diagnostic ---------------------------------------------------------------


def line_drawing(size=(120, 90)) -> Image.Image:
    image = canvas(*size)
    draw = ImageDraw.Draw(image)
    draw.line((20, 20, 100, 20), fill=(0, 0, 0))
    draw.line((20, 20, 20, 70), fill=(0, 0, 0))
    draw.line((30, 70, 95, 40), fill=(0, 0, 0))
    draw.ellipse((60, 45, 80, 60), outline=(0, 0, 0))
    return image


def translated(image: Image.Image, dx: int, dy: int) -> Image.Image:
    out = canvas(*image.size)
    out.paste(image, (dx, dy))
    return out


def test_identical_images_peak_at_zero_with_full_share() -> None:
    a = line_drawing()
    o = edge_overlap(a, a.copy())
    assert o.best_shift == (0, 0) and o.peak_inside_range
    assert o.zero_shift_share == (1.0, 1.0)
    assert o.line_pixels[0] == o.line_pixels[1] > 0
    assert o.peak_ratio > 1.0


def test_known_translation_is_recovered() -> None:
    a = line_drawing()
    b = translated(a, 2, -1)  # b is a moved right 2 and up 1
    o = edge_overlap(a, b)
    assert o.best_shift == (-2, 1)  # moving b back by (-2, +1) realigns it
    assert o.count(-2, 1) == o.line_pixels[0]
    assert o.zero_shift < o.count(-2, 1)


def test_translation_beyond_the_search_range_has_no_interior_peak() -> None:
    a = line_drawing()
    o = edge_overlap(a, translated(a, 6, 0), max_shift=3)
    assert not o.peak_inside_range


def test_images_without_shared_lines_show_no_alignment() -> None:
    a = line_drawing()
    b = canvas(*a.size)
    ImageDraw.Draw(b).line((5, 85, 115, 85), fill=(0, 0, 0))
    o = edge_overlap(a, b)
    assert o.zero_shift == 0 and o.zero_shift_share == (0.0, 0.0)


def test_line_luminance_selects_which_pixels_count_as_lines() -> None:
    a = canvas(40, 30)
    ImageDraw.Draw(a).line((5, 15, 35, 15), fill=(100, 100, 100))
    assert edge_overlap(a, a, line_luminance=64).line_pixels == (0, 0)
    assert edge_overlap(a, a, line_luminance=128).line_pixels == (31, 31)


def test_edge_overlap_requires_same_size_images() -> None:
    with pytest.raises(SizeMismatch):
        edge_overlap(canvas(40, 30), canvas(40, 31))


@pytest.mark.parametrize("max_shift", [0, -1, 1.5, True])
def test_edge_overlap_rejects_invalid_max_shift(max_shift) -> None:
    with pytest.raises(ValueError, match="max_shift"):
        edge_overlap(canvas(10, 10), canvas(10, 10), max_shift=max_shift)


@pytest.mark.parametrize("limit", [-1, 255, 64.0, False])
def test_edge_overlap_rejects_invalid_line_luminance(limit) -> None:
    with pytest.raises(ValueError, match="line_luminance"):
        edge_overlap(canvas(10, 10), canvas(10, 10), line_luminance=limit)


# --- Supplied background -----------------------------------------------------------------


def test_supplied_background_is_the_classification_reference() -> None:
    # A near-white border colour that would be inferred instead of the supplied white.
    image = with_rect(canvas(60, 40, (250, 250, 250)), (20, 10, 40, 30))
    image.putpixel((5, 5), (245, 245, 245))  # 10 from supplied white, 5 from the border colour
    inferred = measure(image)
    supplied = measure(image, background=WHITE)
    assert inferred.background.color == (250, 250, 250) and inferred.bounds == Bounds(
        20, 10, 40, 30
    )
    assert supplied.background.color == WHITE and supplied.background.source == "supplied"
    assert supplied.bounds == Bounds(5, 5, 40, 30)  # (245, ...) is foreground against white


def test_supplied_background_drift_is_rejected() -> None:
    image = with_rect(canvas(60, 40, (40, 50, 60)), (20, 10, 40, 30))
    with pytest.raises(BackgroundUncertain, match="supplied background"):
        measure(image, background=WHITE)


def test_supplied_background_records_border_support_and_provenance() -> None:
    m = measure(with_rect(canvas(60, 40), (0, 10, 30, 30)), background=WHITE)
    assert m.background.source == "supplied"
    assert 0.6 < m.background.border_support < 1.0
    assert m.to_dict()["background"]["source"] == "supplied"
    assert measure(canvas(60, 40)).background.source == "inferred"


def test_background_and_expected_background_are_exclusive() -> None:
    with pytest.raises(ValueError, match="either"):
        measure(canvas(20, 20), background=WHITE, expected_background=WHITE)
