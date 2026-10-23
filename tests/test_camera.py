from __future__ import annotations

import json

import pytest
from fakes import FakeSolidWorks, math_object

from viewjig.camera import Camera, CameraError, CameraReadbackMismatch, apply_camera, read_camera
from viewjig.sw import DISPATCH_PROPERTYPUT, Com, open_document

ORIENTATION = tuple(0.1 * i for i in range(16))
CAMERA = Camera(
    ORIENTATION, (-0.08217945535870855, -0.015878329553721256, -0.0449), 1.5554151637250084
)


def test_json_round_trip_is_exact() -> None:
    restored = Camera.from_json(CAMERA.to_json())
    assert restored == CAMERA
    data = json.loads(CAMERA.to_json())
    assert data["format"] == "viewjig.camera" and data["version"] == 1
    assert data["scale2"] == 1.5554151637250084


def test_values_are_stored_as_plain_floats() -> None:
    camera = Camera(list(range(16)), [1, 2, 3], 2)
    assert camera.orientation == tuple(float(i) for i in range(16))
    assert all(type(v) is float for v in (*camera.orientation, *camera.translation, camera.scale))


@pytest.mark.parametrize(
    "orientation, translation, scale",
    [
        (range(15), (0, 0, 0), 1.0),
        (range(16), (0, 0), 1.0),
        (range(16), (0, 0, 0), 0.0),
        (range(16), (0, 0, float("nan")), 1.0),
    ],
)
def test_invalid_cameras_are_rejected(orientation, translation, scale) -> None:
    with pytest.raises(CameraError):
        Camera(orientation, translation, scale)


def test_foreign_or_incomplete_records_are_rejected() -> None:
    data = CAMERA.to_dict()
    with pytest.raises(CameraError, match="not a viewjig.camera"):
        Camera.from_dict({**data, "version": 2})
    del data["scale2"]
    with pytest.raises(CameraError, match="missing 'scale2'"):
        Camera.from_dict(data)


def test_read_camera_returns_plain_numbers(solidworks: FakeSolidWorks) -> None:
    solidworks.view.props["Scale2"] = 2.5
    solidworks.view.props["Translation3"] = math_object(solidworks.log, "vector", (1, 2, 3))
    with open_document(Com(solidworks.app), solidworks.path) as document:
        camera = read_camera(document)
    assert camera == Camera(range(16), (1.0, 2.0, 3.0), 2.5)


def test_apply_assigns_translation_last_and_returns_exact_readback(solidworks) -> None:
    with open_document(Com(solidworks.app), solidworks.path) as document:
        readback = apply_camera(document, CAMERA)
    assert readback == CAMERA
    puts = [name for who, name, flags, _ in solidworks.log if flags & DISPATCH_PROPERTYPUT]
    assert puts == ["Orientation3", "Scale2", "Translation3"]
    assert "GraphicsRedraw2" in solidworks.calls("model")


def test_apply_builds_math_objects_from_the_camera_numbers(solidworks) -> None:
    with open_document(Com(solidworks.app), solidworks.path) as document:
        apply_camera(document, CAMERA)
    created = {name: args[0].value for who, name, _, args in solidworks.log if who == "math"}
    assert created == {
        "CreateTransform": list(CAMERA.orientation),
        "CreateVector": list(CAMERA.translation),
    }


def test_apply_raises_when_solidworks_reports_a_different_camera(solidworks) -> None:
    # The view reports every float property slightly off from what was assigned.
    original_invoke = solidworks.view.Invoke

    def drifting_invoke(dispid, lcid, flags, result, *args):
        value = original_invoke(dispid, lcid, flags, result, *args)
        return value * 1.001 if isinstance(value, float) else value

    solidworks.view.Invoke = drifting_invoke
    with (
        open_document(Com(solidworks.app), solidworks.path) as document,
        pytest.raises(CameraReadbackMismatch) as info,
    ):
        apply_camera(document, CAMERA)
    assert info.value.applied == CAMERA
    assert info.value.readback.scale == pytest.approx(CAMERA.scale * 1.001)
