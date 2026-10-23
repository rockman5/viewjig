from __future__ import annotations

from pathlib import Path

import pytest
from fakes import FakeComError, FakeDispatch, FakeSolidWorks

from viewjig import sw
from viewjig.sw import (
    DISPATCH_METHOD,
    DISPATCH_PROPERTYGET,
    DISPATCH_PROPERTYPUT,
    Com,
    DocumentActivationFailed,
    DocumentAlreadyOpen,
    DocumentModified,
    DocumentNotClean,
    DocumentNotReadOnly,
    DocumentOpenFailed,
    StandardView,
    open_document,
)

# --- Com -------------------------------------------------------------------------------


def make(log: list) -> FakeDispatch:
    child = FakeDispatch("child", log, props={"Name": "child"})
    return FakeDispatch(
        "parent",
        log,
        methods={"GetTitle": lambda: "title", "Add": lambda a, b: a + b, "Child": lambda: child},
        props={"Count": 3, "Target": None},
    )


def test_call_invokes_as_method_or_propget_with_arguments() -> None:
    log: list = []
    com = Com(make(log))
    assert com.call("Add", 2, 3) == 5
    assert log[-1] == ("parent", "Add", DISPATCH_METHOD | DISPATCH_PROPERTYGET, (2, 3))


def test_get_invokes_propget_and_put_invokes_propput() -> None:
    log: list = []
    com = Com(make(log))
    assert com.get("Count") == 3
    com.put("Count", 4)
    assert com.get("Count") == 4
    assert [entry[2] for entry in log] == [
        DISPATCH_PROPERTYGET,
        DISPATCH_PROPERTYPUT,
        DISPATCH_PROPERTYGET,
    ]


def test_zero_argument_method_runs_only_when_called() -> None:
    log: list = []
    com = Com(make(log))
    with pytest.raises(AttributeError):
        com.GetTitle  # noqa: B018 - attribute access must not reach COM
    assert log == []
    assert com.call("GetTitle") == "title"
    assert len(log) == 1


def test_returned_dispatch_objects_are_wrapped_and_others_pass_through() -> None:
    log: list = []
    com = Com(make(log))
    child = com.call("Child")
    assert isinstance(child, Com)
    assert child.get("Name") == "child"
    assert sw.wrap(None) is None
    assert sw.wrap((1.0, 2.0)) == (1.0, 2.0)


def test_com_arguments_are_unwrapped_to_their_dispatch() -> None:
    log: list = []
    parent, other = make(log), FakeDispatch("other", log)
    Com(parent).put("Target", Com(other))
    assert parent.props["Target"] is other


def test_unknown_member_error_propagates() -> None:
    with pytest.raises(FakeComError):
        Com(make([])).call("NoSuchMember")


# --- attach ---------------------------------------------------------------------------------


class FakeComErrorType(Exception):
    pass


def fake_pywin32(monkeypatch: pytest.MonkeyPatch, active: object | None) -> list:
    """Stand-in pythoncom/pywintypes modules, so attach() never reaches a real SOLIDWORKS."""
    import types

    requested: list = []

    def get_active_object(clsid):
        requested.append(clsid)
        if active is None:
            raise FakeComErrorType("not running")
        return active

    pythoncom = types.SimpleNamespace(
        GetActiveObject=get_active_object, com_error=FakeComErrorType, IID_IDispatch="IDispatch"
    )
    pywintypes = types.SimpleNamespace(IID=lambda progid: f"IID({progid})")
    monkeypatch.setitem(__import__("sys").modules, "pythoncom", pythoncom)
    monkeypatch.setitem(__import__("sys").modules, "pywintypes", pywintypes)
    return requested


def test_attach_wraps_the_running_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    dispatch = FakeDispatch("app", [])
    unknown = type("Unknown", (), {"QueryInterface": lambda self, iid: dispatch})()
    requested = fake_pywin32(monkeypatch, unknown)
    app = sw.attach()
    assert isinstance(app, Com) and app.dispatch is dispatch
    assert requested == ["IID(SldWorks.Application)"]


def test_attach_never_starts_solidworks(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_pywin32(monkeypatch, None)
    with pytest.raises(sw.SolidWorksNotRunning):
        sw.attach()


# --- open_document -------------------------------------------------------------------------


def test_opens_read_only_silently_and_closes_on_success(solidworks: FakeSolidWorks) -> None:
    with open_document(Com(solidworks.app), solidworks.path) as document:
        assert document.title == "vj-f01"
        assert document.path == solidworks.path
        assert isinstance(document.view(), Com)
    opens = [args for who, name, _, args in solidworks.log if name == "OpenDoc6"]
    assert len(opens) == 1
    path, doc_type, options, config = opens[0][:4]
    assert (path, doc_type, options, config) == (str(solidworks.path), 1, 0x1 | 0x2, "")
    assert solidworks.closed == ["vj-f01"]
    assert document.dirty_at_close == 0


def test_refuses_a_document_that_is_already_open(solidworks: FakeSolidWorks) -> None:
    solidworks.already_open = True
    with pytest.raises(DocumentAlreadyOpen), open_document(Com(solidworks.app), solidworks.path):
        pytest.fail("body must not run")
    assert "OpenDoc6" not in solidworks.calls()
    assert solidworks.closed == []


def test_never_closes_an_editable_document(solidworks: FakeSolidWorks) -> None:
    solidworks.read_only = False
    with pytest.raises(DocumentNotReadOnly), open_document(Com(solidworks.app), solidworks.path):
        pytest.fail("body must not run")
    assert solidworks.closed == []


def test_open_failure_reports_solidworks_codes(solidworks: FakeSolidWorks) -> None:
    solidworks.open_returns_none = True
    with (
        pytest.raises(DocumentOpenFailed, match="errors=2"),
        open_document(Com(solidworks.app), solidworks.path),
    ):
        pytest.fail("body must not run")
    assert solidworks.closed == []


def test_rejects_missing_files_and_unsupported_types(tmp_path: Path, solidworks) -> None:
    app = Com(solidworks.app)
    with (
        pytest.raises(DocumentOpenFailed, match="not found"),
        open_document(app, tmp_path / "missing.SLDPRT"),
    ):
        pass
    drawing = tmp_path / "sheet.SLDDRW"
    drawing.write_bytes(b"")
    with pytest.raises(DocumentOpenFailed, match="unsupported"), open_document(app, drawing):
        pass
    assert solidworks.calls("app") == []


def test_activates_the_document_when_it_is_not_active(solidworks: FakeSolidWorks) -> None:
    solidworks.active_after_open = False
    with open_document(Com(solidworks.app), solidworks.path):
        pass
    assert "ActivateDoc3" in solidworks.calls("app")
    assert solidworks.closed == ["vj-f01"]


def test_activation_failure_still_closes(solidworks: FakeSolidWorks) -> None:
    solidworks.active_after_open = False
    solidworks.activation_works = False
    with (
        pytest.raises(DocumentActivationFailed),
        open_document(Com(solidworks.app), solidworks.path),
    ):
        pytest.fail("body must not run")
    assert solidworks.closed == ["vj-f01"]


def test_closes_when_the_body_raises(solidworks: FakeSolidWorks) -> None:
    with (
        pytest.raises(RuntimeError, match="boom"),
        open_document(Com(solidworks.app), solidworks.path),
    ):
        raise RuntimeError("boom")
    assert solidworks.closed == ["vj-f01"]


def test_close_failure_does_not_hide_the_original_error(solidworks: FakeSolidWorks) -> None:
    solidworks.close_raises = True
    with (
        pytest.raises(RuntimeError, match="boom") as info,
        open_document(Com(solidworks.app), solidworks.path),
    ):
        raise RuntimeError("boom")
    assert any("closing vj-f01.SLDPRT also failed" in note for note in info.value.__notes__)


def test_close_failure_after_success_is_raised(solidworks: FakeSolidWorks) -> None:
    solidworks.close_raises = True
    with (
        pytest.raises(FakeComError, match="CloseDoc failed"),
        open_document(Com(solidworks.app), solidworks.path),
    ):
        pass


def test_clean_document_that_becomes_dirty_raises_modified_after_closing(
    solidworks: FakeSolidWorks,
) -> None:
    with (
        pytest.raises(DocumentModified, match="became dirty"),
        open_document(Com(solidworks.app), solidworks.path) as document,
    ):
        solidworks.dirty = 1
    assert document.dirty_at_close == 1
    assert solidworks.closed == ["vj-f01"]


def test_document_dirty_immediately_after_opening_is_refused_and_closed(
    solidworks: FakeSolidWorks,
) -> None:
    solidworks.dirty_at_open = 2
    with (
        pytest.raises(DocumentNotClean, match=r"unsaved changes \(2\) immediately after opening"),
        open_document(Com(solidworks.app), solidworks.path),
    ):
        pytest.fail("body must not run")
    assert solidworks.closed == ["vj-f01"]


def test_initial_dirty_state_is_checked_after_activation(solidworks: FakeSolidWorks) -> None:
    solidworks.active_after_open = False
    solidworks.dirty_at_open = 1
    with (
        pytest.raises(DocumentNotClean),
        open_document(Com(solidworks.app), solidworks.path),
    ):
        pytest.fail("body must not run")
    calls = solidworks.calls()
    assert calls.index("ActivateDoc3") < calls.index("GetSaveFlag")
    assert solidworks.closed == ["vj-f01"]


def test_body_error_takes_precedence_over_becoming_dirty(solidworks: FakeSolidWorks) -> None:
    with (
        pytest.raises(RuntimeError, match="boom"),
        open_document(Com(solidworks.app), solidworks.path),
    ):
        solidworks.dirty = 1
        raise RuntimeError("boom")
    assert solidworks.closed == ["vj-f01"]


def test_document_view_operations_are_explicit_calls(solidworks: FakeSolidWorks) -> None:
    with open_document(Com(solidworks.app), solidworks.path) as document:
        document.show_standard_view(StandardView.FRONT)
        document.zoom_to_fit()
        document.redraw()
    entries = [(name, args) for who, name, _, args in solidworks.log if who == "model"]
    assert ("ShowNamedView2", ("", 1)) in entries
    assert ("ViewZoomtofit2", ()) in entries
    assert ("GraphicsRedraw2", ()) in entries
