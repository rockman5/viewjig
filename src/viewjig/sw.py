"""Thin SOLIDWORKS COM boundary.

Everything that talks to SOLIDWORKS goes through this module. It is deliberately
small: explicit COM invocation, attaching to a running SOLIDWORKS, and a safe
read-only document lifecycle. Framing and measurement live elsewhere.

Why explicit invocation: SOLIDWORKS objects expose no IDispatch type information,
so pywin32's dynamic dispatch guesses whether a name is a property or a method
and invokes unknown names on attribute access. A zero-argument method such as
``GetPathName`` then runs, and returns its value, before it can be called.
``Com`` never invokes on attribute access;
callers state whether a member is a method (``call``) or a property (``get`` /
``put``), as the SOLIDWORKS API documents it.

pywin32 is imported lazily, only where a real COM object or VARIANT is needed, so
the rest of the package and its tests run without SOLIDWORKS.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any, Protocol

# IDispatch::Invoke flags (oaidl.h).
DISPATCH_METHOD = 0x1
DISPATCH_PROPERTYGET = 0x2
DISPATCH_PROPERTYPUT = 0x4

# Public SOLIDWORKS API enum values.
DOCUMENT_TYPES = {".sldprt": 1, ".sldasm": 2}  # swDocumentTypes_e
OPEN_SILENT = 0x1  # swOpenDocOptions_e
OPEN_READ_ONLY = 0x2
DONT_REBUILD_ON_ACTIVATION = 1  # swRebuildOnActivation_e


class StandardView(IntEnum):
    """swStandardViews_e values for ``IModelDoc2.ShowNamedView2``."""

    FRONT = 1
    BACK = 2
    LEFT = 3
    RIGHT = 4
    TOP = 5
    BOTTOM = 6
    ISOMETRIC = 7
    TRIMETRIC = 8
    DIMETRIC = 9


class SolidWorksError(Exception):
    """Base class for errors raised at the SOLIDWORKS boundary."""


class SolidWorksNotRunning(SolidWorksError):
    pass


class DocumentAlreadyOpen(SolidWorksError):
    pass


class DocumentOpenFailed(SolidWorksError):
    pass


class DocumentNotReadOnly(SolidWorksError):
    """SOLIDWORKS returned an editable document; ViewJig neither uses nor closes it."""


class DocumentActivationFailed(SolidWorksError):
    pass


class DocumentModified(SolidWorksError):
    """A document that was clean when ViewJig opened it became dirty while ViewJig held it."""


class DocumentNotClean(SolidWorksError):
    """A document ViewJig had just opened read-only already reported unsaved changes."""


class Dispatch(Protocol):
    """The two IDispatch methods ``Com`` relies on (pywin32's PyIDispatch, or a fake)."""

    def GetIDsOfNames(self, name: str) -> int: ...

    def Invoke(self, dispid: int, lcid: int, flags: int, result: bool, *args: Any) -> Any: ...


def _is_dispatch(value: Any) -> bool:
    return hasattr(value, "GetIDsOfNames") and hasattr(value, "Invoke")


class Com:
    """Explicit IDispatch access. Nothing is invoked by attribute lookup."""

    def __init__(self, dispatch: Dispatch) -> None:
        self.dispatch = dispatch

    def call(self, name: str, *args: Any) -> Any:
        """Invoke a documented method (method|propget, as late-bound callers do)."""
        return self._invoke(name, DISPATCH_METHOD | DISPATCH_PROPERTYGET, True, args)

    def get(self, name: str) -> Any:
        """Read a documented property."""
        return self._invoke(name, DISPATCH_PROPERTYGET, True, ())

    def put(self, name: str, value: Any) -> None:
        """Assign a documented property (PROPERTYPUT; accepted for object values too)."""
        self._invoke(name, DISPATCH_PROPERTYPUT, False, (value,))

    def _invoke(self, name: str, flags: int, result: bool, args: tuple[Any, ...]) -> Any:
        dispid = self.dispatch.GetIDsOfNames(name)
        raw = tuple(a.dispatch if isinstance(a, Com) else a for a in args)
        return wrap(self.dispatch.Invoke(dispid, 0, flags, result, *raw))


def wrap(value: Any) -> Any:
    """Wrap returned dispatch objects as ``Com``; pass other values through."""
    return Com(value) if _is_dispatch(value) else value


def byref_long() -> Any:
    """A by-reference VT_I4 VARIANT for SOLIDWORKS out-parameters."""
    import pythoncom
    import win32com.client

    return win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)


def double_array(values: Any) -> Any:
    """A VT_ARRAY | VT_R8 VARIANT, as IMathUtility expects for array data."""
    import pythoncom
    import win32com.client

    return win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [float(v) for v in values])


def attach() -> Com:
    """Attach to the running SOLIDWORKS. Never starts a new instance."""
    import pythoncom
    import pywintypes

    try:
        unknown = pythoncom.GetActiveObject(pywintypes.IID("SldWorks.Application"))
    except pythoncom.com_error as exc:
        raise SolidWorksNotRunning("SOLIDWORKS is not running") from exc
    return Com(unknown.QueryInterface(pythoncom.IID_IDispatch))


def _same_file(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


@dataclass
class Document:
    """A document ViewJig opened read-only. Valid only inside ``open_document``."""

    app: Com
    model: Com
    path: Path
    title: str
    dirty_at_close: int | None = None

    def view(self) -> Com:
        return self.model.get("ActiveView")

    def save_flag(self) -> int:
        return int(self.model.call("GetSaveFlag"))

    def show_standard_view(self, view: StandardView) -> None:
        self.model.call("ShowNamedView2", "", int(view))

    def zoom_to_fit(self) -> None:
        self.model.call("ViewZoomtofit2")

    def redraw(self) -> None:
        self.model.call("GraphicsRedraw2")


@contextmanager
def open_document(app: Com, path: Path | str) -> Iterator[Document]:
    """Open ``path`` read-only for the duration of the block, then always close it.

    Refuses a document that is already open in SOLIDWORKS, so ViewJig never acts on
    (or closes) a document someone else is working on.

    Dirty state: the document must report no unsaved changes once opened and
    activated, or ``DocumentNotClean`` is raised (after closing it). If it becomes
    dirty while the block runs, ``DocumentModified`` is raised on an otherwise clean
    exit, after closing. The source file is safe either way: it was opened read-only
    and is never saved.
    """
    path = Path(path).resolve()
    doc_type = DOCUMENT_TYPES.get(path.suffix.lower())
    if doc_type is None:
        raise DocumentOpenFailed(f"unsupported document type: {path.name}")
    if not path.is_file():
        raise DocumentOpenFailed(f"file not found: {path}")
    if app.call("GetOpenDocumentByName", str(path)) is not None:
        raise DocumentAlreadyOpen(f"{path.name} is already open in SOLIDWORKS")

    errors, warnings = byref_long(), byref_long()
    model = app.call(
        "OpenDoc6", str(path), doc_type, OPEN_SILENT | OPEN_READ_ONLY, "", errors, warnings
    )
    if model is None:
        raise DocumentOpenFailed(
            f"OpenDoc6 failed for {path.name} (errors={errors.value}, warnings={warnings.value})"
        )
    if not model.call("IsOpenedReadOnly"):
        # An editable document came back: it was already open; leave it alone.
        raise DocumentNotReadOnly(f"{path.name} is open for editing in SOLIDWORKS")

    title = str(model.call("GetTitle"))
    try:
        _activate(app, title, path)
        initial = int(model.call("GetSaveFlag"))
        if initial:
            raise DocumentNotClean(
                f"{path.name} reported unsaved changes ({initial}) immediately after opening"
            )
        document = Document(app, model, path, title)
        yield document
        document.dirty_at_close = document.save_flag()
    except BaseException as exc:
        # Close even on failure, without letting a close error hide the original one.
        try:
            app.call("CloseDoc", title)
        except Exception as close_exc:  # noqa: BLE001
            exc.add_note(f"closing {path.name} also failed: {close_exc!r}")
        raise
    app.call("CloseDoc", title)
    if document.dirty_at_close:
        raise DocumentModified(
            f"{path.name} became dirty ({document.dirty_at_close}) while ViewJig held it"
        )


def _activate(app: Com, title: str, path: Path) -> None:
    def active_path() -> str | None:
        active = app.get("ActiveDoc")
        return None if active is None else str(active.call("GetPathName"))

    current = active_path()
    if current is not None and _same_file(current, str(path)):
        return
    app.call("ActivateDoc3", title, False, DONT_REBUILD_ON_ACTIVATION, byref_long())
    current = active_path()
    if current is None or not _same_file(current, str(path)):
        raise DocumentActivationFailed(f"could not activate {path.name}")
