"""Plain-number SOLIDWORKS camera.

A ``Camera`` is the view state that, together with an output size, fully
determines a fixed-size capture: ``Orientation3`` (16 values), ``Translation3``
(3 values), and ``Scale2``. It does not depend on the SOLIDWORKS window, window
state, or session, and it holds numbers only, never COM objects, so it can be
stored and carried between documents.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from viewjig.sw import Document, double_array

FORMAT = "viewjig.camera"
VERSION = 1


class CameraError(Exception):
    pass


class CameraReadbackMismatch(CameraError):
    """SOLIDWORKS did not report back exactly the camera that was applied."""

    def __init__(self, applied: Camera, readback: Camera) -> None:
        super().__init__(f"camera readback differs from applied camera: {readback} != {applied}")
        self.applied = applied
        self.readback = readback


@dataclass(frozen=True)
class Camera:
    orientation: tuple[float, ...]
    translation: tuple[float, float, float]
    scale: float

    def __post_init__(self) -> None:
        orientation = tuple(float(v) for v in self.orientation)
        translation = tuple(float(v) for v in self.translation)
        scale = float(self.scale)
        if len(orientation) != 16:
            raise CameraError(f"orientation needs 16 values, got {len(orientation)}")
        if len(translation) != 3:
            raise CameraError(f"translation needs 3 values, got {len(translation)}")
        if not all(math.isfinite(v) for v in (*orientation, *translation, scale)):
            raise CameraError("camera values must be finite")
        if scale <= 0:
            raise CameraError(f"scale must be positive, got {scale}")
        object.__setattr__(self, "orientation", orientation)
        object.__setattr__(self, "translation", translation)
        object.__setattr__(self, "scale", scale)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": FORMAT,
            "version": VERSION,
            "orientation3": list(self.orientation),
            "translation3": list(self.translation),
            "scale2": self.scale,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Camera:
        if data.get("format") != FORMAT or data.get("version") != VERSION:
            raise CameraError(
                f"not a {FORMAT} v{VERSION} record: "
                f"format={data.get('format')!r} version={data.get('version')!r}"
            )
        try:
            return cls(data["orientation3"], data["translation3"], data["scale2"])
        except KeyError as exc:
            raise CameraError(f"camera record is missing {exc.args[0]!r}") from exc

    def to_json(self) -> str:
        # json writes floats with repr, which round-trips exactly.
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_json(cls, text: str) -> Camera:
        return cls.from_dict(json.loads(text))


def read_camera(document: Document) -> Camera:
    view = document.view()
    return Camera(
        orientation=view.get("Orientation3").get("ArrayData"),
        translation=view.get("Translation3").get("ArrayData"),
        scale=view.get("Scale2"),
    )


def apply_camera(document: Document, camera: Camera) -> Camera:
    """Apply ``camera`` to the document's view and return the exact readback.

    Order matters: assigning ``Scale2`` rescales the current ``Translation3``, so
    ``Translation3`` is assigned last. Raises
    ``CameraReadbackMismatch`` if SOLIDWORKS does not report the same numbers back.
    """
    math_utility = document.app.call("GetMathUtility")
    transform = math_utility.call("CreateTransform", double_array(camera.orientation))
    vector = math_utility.call("CreateVector", double_array(camera.translation))
    view = document.view()
    view.put("Orientation3", transform)
    view.put("Scale2", camera.scale)
    view.put("Translation3", vector)
    document.redraw()
    readback = read_camera(document)
    if readback != camera:
        raise CameraReadbackMismatch(camera, readback)
    return readback
