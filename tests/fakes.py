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
            },
            props={"ActiveView": self.view},
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
