"""Offline stand-ins for the SOLIDWORKS COM surface used by viewjig.sw and friends."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PIL import Image

from viewjig.sw import DISPATCH_PROPERTYPUT


class FakeComError(Exception):
    pass


class FakeDispatch:
    """An IDispatch with named methods and properties that logs every Invoke."""

    def __init__(
        self,
        label: str,
        log: list[tuple[str, str, int, tuple[Any, ...]]],
        methods: dict[str, Callable[..., Any]] | None = None,
        props: dict[str, Any] | None = None,
    ) -> None:
        self.label = label
        self.log = log
        self.methods = methods or {}
        self.props = props or {}
        self._names: list[str] = []

    def GetIDsOfNames(self, name: str) -> int:
        if name not in self.methods and name not in self.props:
            raise FakeComError(f"{self.label} has no member {name}")
        if name not in self._names:
            self._names.append(name)
        return 1000 + self._names.index(name)

    def Invoke(self, dispid: int, lcid: int, flags: int, result: bool, *args: Any) -> Any:
        name = self._names[dispid - 1000]
        self.log.append((self.label, name, flags, args))
        if flags & DISPATCH_PROPERTYPUT:
            self.props[name] = args[0]
            return None
        if name in self.methods:
            return self.methods[name](*args)
        return self.props[name]


class FakeVariant:
    def __init__(self, value: Any) -> None:
        self.value = value


def math_object(log: list, label: str, values: Any) -> FakeDispatch:
    return FakeDispatch(label, log, props={"ArrayData": tuple(float(v) for v in values)})


class FakeSolidWorks:
    """One application, one document, one view, with knobs for failure cases."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.log: list[tuple[str, str, int, tuple[Any, ...]]] = []
        self.already_open = False
        self.open_returns_none = False
        self.read_only = True
        self.active_after_open = True
        self.activation_works = True
        self.dirty_at_open = 0
        self.close_raises = False
        self.closed: list[str] = []
        self.save_bmp: Callable[[str, int, int], Any] = self._write_bmp
        self.bmp_size_override: tuple[int, int] | None = None
        self.saved_paths: list[str] = []
        self.dirty = 0
        self.preferences = Preferences()

        self.view = FakeDispatch(
            "view",
            self.log,
            props={
                "Orientation3": math_object(self.log, "transform", range(16)),
                "Translation3": math_object(self.log, "vector", (0.0, 0.0, 0.0)),
                "Scale2": 1.0,
            },
        )
        self.model = FakeDispatch(
            "model",
            self.log,
            methods={
                "GetTitle": lambda: "vj-f01",
                "GetPathName": lambda: str(self.path),
                "IsOpenedReadOnly": lambda: self.read_only,
                "GetSaveFlag": lambda: self.dirty,
                "ShowNamedView2": lambda name, view_id: None,
                "ViewZoomtofit2": lambda: None,
                "GraphicsRedraw2": lambda: None,
                "SaveBMP": lambda path, w, h: self.save_bmp(path, w, h),
                "GetActiveConfiguration": lambda: self.configuration,
            },
            props={"ActiveView": self.view},
        )
        # Active configuration's scene: an image background, like a stock SOLIDWORKS scene.
        self.scene = FakeDispatch(
            "scene",
            self.log,
            props={
                "BackgroundType": 3,
                "BackgroundTopGradientColor": 0xC0C0C0,
                "BackgroundBottomGradientColor": 0,
                "BackgroundBrightness": 0.9,
                "BackgroundImage": "Studio Gradient.jpg",
            },
        )
        self.configuration = FakeDispatch(
            "configuration", self.log, methods={"GetScene": lambda: self.scene}
        )
        self.math = FakeDispatch(
            "math",
            self.log,
            methods={
                "CreateTransform": lambda data: math_object(self.log, "transform", data.value),
                "CreateVector": lambda data: math_object(self.log, "vector", data.value),
            },
        )
        self.app = FakeDispatch(
            "app",
            self.log,
            methods={
                "GetOpenDocumentByName": self._get_open,
                "OpenDoc6": self._open,
                "ActivateDoc3": self._activate,
                "CloseDoc": self._close,
                "GetMathUtility": lambda: self.math,
                **preference_methods(self.preferences),
            },
            props={"ActiveDoc": None},
        )

    def calls(self, label: str | None = None) -> list[str]:
        return [name for who, name, _, _ in self.log if label is None or who == label]

    def _get_open(self, path: str) -> Any:
        return self.model if self.already_open else None

    def _open(self, path, doc_type, options, config, errors, warnings) -> Any:
        if self.open_returns_none:
            errors.value, warnings.value = 2, 0
            return None
        self.dirty = self.dirty_at_open
        if self.active_after_open:
            self.app.props["ActiveDoc"] = self.model
        return self.model

    def _activate(self, title, use_user_preferences, option, errors) -> Any:
        if self.activation_works:
            self.app.props["ActiveDoc"] = self.model
        return self.model

    def _close(self, title: str) -> None:
        if self.close_raises:
            raise FakeComError("CloseDoc failed")
        self.closed.append(title)

    def _write_bmp(self, path: str, width: int, height: int) -> bool:
        self.saved_paths.append(path)
        size = self.bmp_size_override or (width, height)
        Image.new("RGB", size, (255, 255, 255)).save(path, format="BMP")
        return True


class Preferences:
    """SOLIDWORKS background preferences as characterized: the gradient-part-background
    toggle follows the background appearance mode (on only for Gradient)."""

    def __init__(self, appearance: int = 3) -> None:
        self.appearance = appearance
        self.writes: list[tuple[int, int]] = []
        self.refuse_writes = False  # Set returns False and changes nothing
        self.ignore_after: int | None = None  # writes after this many are accepted but ignored
        self.toggle_override: bool | None = None

    def get_int(self, key: int) -> int:
        assert key == 305, f"unexpected preference read {key}"
        return self.appearance

    def set_int(self, key: int, value: int) -> bool:
        assert key == 305, f"ViewJig wrote preference {key}; only 305 may change"
        self.writes.append((key, value))
        if self.refuse_writes:
            return False
        if self.ignore_after is not None and len(self.writes) > self.ignore_after:
            return True
        self.appearance = value
        return True

    def get_toggle(self, key: int) -> bool:
        assert key == 68, f"unexpected toggle read {key}"
        if self.toggle_override is not None:
            return self.toggle_override
        return self.appearance == 1


def preference_methods(prefs: Preferences) -> dict[str, Callable[..., Any]]:
    return {
        "GetUserPreferenceIntegerValue": prefs.get_int,
        "SetUserPreferenceIntegerValue": prefs.set_int,
        "GetUserPreferenceToggle": prefs.get_toggle,
    }


def preference_app(prefs: Preferences) -> FakeDispatch:
    """An application with only the background preferences, for orchestration tests."""
    return FakeDispatch("app", [], methods=preference_methods(prefs))
