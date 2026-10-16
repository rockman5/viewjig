from viewjig.measure import Bounds, FramingMeasurement


def test_centered_bounds_have_zero_center_error() -> None:
    measurement = FramingMeasurement(
        image_width=101,
        image_height=81,
        foreground=Bounds(left=30, top=20, right=70, bottom=60),
    )

    assert measurement.center_error_x == 0.0
    assert measurement.center_error_y == 0.0
    assert measurement.margins == (30, 20, 30, 20)


def test_offset_bounds_report_signed_center_error() -> None:
    measurement = FramingMeasurement(
        image_width=100,
        image_height=100,
        foreground=Bounds(left=20, top=30, right=59, bottom=69),
    )

    assert measurement.center_error_x == -10.0
    assert measurement.center_error_y == 0.0
    assert measurement.margins == (20, 30, 40, 30)
