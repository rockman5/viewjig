"""Controlled white capture background, restored exactly afterwards.

ViewJig measures against a plain background, so it captures on white whatever
background the user has. It does this in two layers, and undoes both.

Document layer (every capture session): the active configuration's scene is
set to a plain white background (``BackgroundType`` Plain,
``BackgroundTopGradientColor`` white, which is the colour a plain scene uses).
Only those two properties are written. They are restored and read back before
the document closes. The document is read-only and closed without saving, so
even an interrupted restore cannot reach the file.

Application layer (once around a shot or pair): the scene only shows when
SOLIDWORKS uses document scene backgrounds (System Options > Colors >
Background appearance). If the user has another mode (plain, gradient, or
image), that one setting is switched to document scenes for the capture and
then restored and read back. Colours, gradient settings, and image paths are
never written. The gradient-part-background toggle follows the mode, so it is
read before and after as a check.

A restore that cannot be confirmed raises ``EnvironmentRestoreError``, naming
the setting and the value it should have, and chained to any capture error that
was already in flight.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from viewjig.sw import Com, Document, SolidWorksError

# swUserPreferenceIntegerValue_e / swUserPreferenceToggle_e values.
BACKGROUND_APPEARANCE = 305
GRADIENT_PART_BACKGROUND = 68
# swColorsBackgroundAppearance_e
APPEARANCE_NAMES = {
    0: "Plain",
    1: "Gradient",
    2: "Image",
    3: "Use document scene background",
}
DOCUMENT_SCENE = 3
# ISwScene: swSceneBackgroundType_e Plain, and its colour (COLORREF white).
WHITE_SCENE = {"BackgroundType": 1, "BackgroundTopGradientColor": 0xFFFFFF}
CONTROLLED_BACKGROUND = "plain white"


class CaptureEnvironmentError(SolidWorksError):
    """The controlled capture background could not be set up."""


class EnvironmentRestoreError(SolidWorksError):
    """A setting ViewJig changed could not be confirmed restored."""


def _appearance_name(value: Any) -> str:
    return f"{APPEARANCE_NAMES.get(value, 'unknown')} ({value})"


@dataclass
class ApplicationBackground:
    original_mode: int
    switched: bool
    gradient_part_background_before: bool
    restored: bool | None = None
    gradient_part_background_after: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_mode": _appearance_name(self.original_mode),
            "temporarily_switched_to_document_scene": self.switched,
            "restored": self.restored,
            "gradient_part_background": {
                "before": self.gradient_part_background_before,
                "after": self.gradient_part_background_after,
            },
        }


@contextmanager
def application_background(app: Com) -> Iterator[ApplicationBackground]:
    """Route SOLIDWORKS to document scene backgrounds for the block, if it isn't already."""

    def mode() -> int:
        return int(app.call("GetUserPreferenceIntegerValue", BACKGROUND_APPEARANCE))

    def toggle() -> bool:
        return bool(app.call("GetUserPreferenceToggle", GRADIENT_PART_BACKGROUND))

    original = mode()
    env = ApplicationBackground(original, original != DOCUMENT_SCENE, toggle())
    if env.switched:
        app.call("SetUserPreferenceIntegerValue", BACKGROUND_APPEARANCE, DOCUMENT_SCENE)
        if mode() != DOCUMENT_SCENE:
            _restore_application(app, env, mode, toggle)  # undo anything that did change
            raise CaptureEnvironmentError(
                "ViewJig could not switch SOLIDWORKS to document scene backgrounds "
                "(System Options > Colors > Background appearance) for the capture"
            )
    try:
        yield env
    except BaseException as exc:
        _restore_application(app, env, mode, toggle, cause=exc)
        raise
    _restore_application(app, env, mode, toggle)


def _restore_application(app, env, mode, toggle, cause: BaseException | None = None) -> None:
    if env.switched:
        app.call("SetUserPreferenceIntegerValue", BACKGROUND_APPEARANCE, env.original_mode)
    current = mode()
    env.gradient_part_background_after = toggle()
    env.restored = (
        current == env.original_mode
        and env.gradient_part_background_after == env.gradient_part_background_before
    )
    if current != env.original_mode:
        raise EnvironmentRestoreError(
            "ViewJig could not restore the SOLIDWORKS background setting "
            "(System Options > Colors > Background appearance): it should be "
            f"{_appearance_name(env.original_mode)} but is {_appearance_name(current)}. "
            "Please set it back in System Options."
        ) from cause
    if env.gradient_part_background_after != env.gradient_part_background_before:
        raise EnvironmentRestoreError(
            "ViewJig restored the SOLIDWORKS background appearance, but the 'gradient "
            "part background' option is now "
            f"{env.gradient_part_background_after} instead of "
            f"{env.gradient_part_background_before}. Please check System Options > Colors."
        ) from cause


@dataclass
class DocumentBackground:
    document: str
    original_scene: dict[str, Any] = field(default_factory=dict)
    restored: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "document": self.document,
            "original_scene": self.original_scene,
            "restored": self.restored,
        }


@contextmanager
def document_background(document: Document) -> Iterator[DocumentBackground]:
    """Give the document's active configuration a plain white scene for the block."""
    env = DocumentBackground(document.path.name)
    scene = document.model.call("GetActiveConfiguration").call("GetScene")
    if scene is None:
        raise CaptureEnvironmentError(
            f"{document.path.name}: SOLIDWORKS returned no scene for the active configuration"
        )
    env.original_scene = {name: scene.get(name) for name in WHITE_SCENE}
    try:
        for name, value in WHITE_SCENE.items():
            scene.put(name, value)
        current = {name: scene.get(name) for name in WHITE_SCENE}
        if current != WHITE_SCENE:
            raise CaptureEnvironmentError(
                f"{document.path.name}: could not set a plain white scene background "
                f"(got {current})"
            )
        document.redraw()
        yield env
    except BaseException as exc:
        _restore_scene(scene, env, cause=exc)
        raise
    _restore_scene(scene, env)


def _restore_scene(scene: Com, env: DocumentBackground, cause: BaseException | None = None) -> None:
    write_errors = []
    for name, value in env.original_scene.items():
        try:
            scene.put(name, value)
        except Exception as exc:  # noqa: BLE001 - every property gets its restore attempt
            write_errors.append(f"{name}: {exc}")
    current = {}
    for name in env.original_scene:
        try:
            current[name] = scene.get(name)
        except Exception as exc:  # noqa: BLE001
            current[name] = f"unreadable ({exc})"
    env.restored = current == env.original_scene and not write_errors
    if not env.restored:
        detail = f" (write errors: {'; '.join(write_errors)})" if write_errors else ""
        raise EnvironmentRestoreError(
            f"ViewJig could not restore the scene background of {env.document}: expected "
            f"{env.original_scene}, found {current}{detail}. The document was opened "
            "read-only and is closed without saving, so the file itself is unchanged."
        ) from cause
