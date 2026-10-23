from __future__ import annotations

import pytest
from fakes import FakeComError, FakeSolidWorks, Preferences, preference_app

from viewjig.environment import (
    WHITE_SCENE,
    CaptureEnvironmentError,
    EnvironmentRestoreError,
    application_background,
    document_background,
)
from viewjig.steps import open_renderer
from viewjig.sw import DISPATCH_PROPERTYPUT, Com, open_document

ORIGINAL_SCENE = {"BackgroundType": 3, "BackgroundTopGradientColor": 0xC0C0C0}

# --- Application layer ------------------------------------------------------------------------


def test_document_scene_mode_is_left_alone() -> None:
    prefs = Preferences(appearance=3)
    with application_background(Com(preference_app(prefs))) as env:
        assert prefs.appearance == 3
    assert prefs.writes == []
    assert env.switched is False and env.restored is True
    assert env.to_dict()["original_mode"] == "Use document scene background (3)"


@pytest.mark.parametrize("mode, name", [(0, "Plain"), (1, "Gradient"), (2, "Image")])
def test_other_modes_switch_once_and_restore_exactly(mode, name) -> None:
    prefs = Preferences(appearance=mode)
    toggle_before = prefs.get_toggle(68)
    with application_background(Com(preference_app(prefs))) as env:
        assert prefs.appearance == 3  # routed to document scenes during the block
    assert prefs.writes == [(305, 3), (305, mode)]  # one switch, one restore, nothing else
    assert prefs.appearance == mode and prefs.get_toggle(68) == toggle_before
    record = env.to_dict()
    assert record["original_mode"] == f"{name} ({mode})"
    assert record["temporarily_switched_to_document_scene"] is True
    assert record["restored"] is True
    assert record["gradient_part_background"] == {"before": toggle_before, "after": toggle_before}


def test_application_restore_after_a_capture_exception() -> None:
    prefs = Preferences(appearance=1)
    with (
        pytest.raises(RuntimeError, match="capture failed"),
        application_background(Com(preference_app(prefs))),
    ):
        raise RuntimeError("capture failed")
    assert prefs.appearance == 1 and prefs.writes == [(305, 3), (305, 1)]


def test_application_restore_failure_is_loud_and_actionable() -> None:
    prefs = Preferences(appearance=1)
    prefs.ignore_after = 1  # the restore write is accepted but has no effect
    with (
        pytest.raises(EnvironmentRestoreError) as info,
        application_background(Com(preference_app(prefs))),
    ):
        pass
    message = str(info.value)
    assert "Background appearance" in message and "System Options" in message
    assert "should be Gradient (1)" in message
    assert "is Use document scene background (3)" in message


def test_application_restore_failure_is_not_hidden_by_the_capture_error() -> None:
    prefs = Preferences(appearance=2)
    prefs.ignore_after = 1
    with (
        pytest.raises(EnvironmentRestoreError, match="should be Image") as info,
        application_background(Com(preference_app(prefs))),
    ):
        raise RuntimeError("capture failed")
    assert isinstance(info.value.__cause__, RuntimeError)
    assert str(info.value.__cause__) == "capture failed"


def test_a_switch_that_does_not_take_fails_before_capturing() -> None:
    prefs = Preferences(appearance=0)
    prefs.refuse_writes = True
    ran = []
    with (
        pytest.raises(CaptureEnvironmentError, match="could not switch"),
        application_background(Com(preference_app(prefs))),
    ):
        ran.append(True)
    assert ran == [] and prefs.appearance == 0


def test_a_gradient_toggle_that_does_not_come_back_is_reported() -> None:
    prefs = Preferences(appearance=1)
    with (
        pytest.raises(EnvironmentRestoreError, match="gradient part background"),
        application_background(Com(preference_app(prefs))),
    ):
        prefs.toggle_override = False  # SOLIDWORKS failed to bring it back
    assert prefs.appearance == 1


# --- Document layer ---------------------------------------------------------------------------


def scene_puts(solidworks: FakeSolidWorks) -> list[tuple[str, object]]:
    return [
        (name, args[0])
        for who, name, flags, args in solidworks.log
        if who == "scene" and flags & DISPATCH_PROPERTYPUT
    ]


def test_scene_is_plain_white_during_the_block(solidworks: FakeSolidWorks) -> None:
    others_before = {k: v for k, v in solidworks.scene.props.items() if k not in WHITE_SCENE}
    with (
        open_document(Com(solidworks.app), solidworks.path) as document,
        document_background(document) as env,
    ):
        assert solidworks.scene.props["BackgroundType"] == 1
        assert solidworks.scene.props["BackgroundTopGradientColor"] == 0xFFFFFF
        assert {k: v for k, v in solidworks.scene.props.items() if k not in WHITE_SCENE} == (
            others_before
        )
    assert env.original_scene == ORIGINAL_SCENE


def test_only_the_two_proven_scene_properties_are_written(solidworks: FakeSolidWorks) -> None:
    with (
        open_document(Com(solidworks.app), solidworks.path) as document,
        document_background(document),
    ):
        pass
    assert scene_puts(solidworks) == [
        ("BackgroundType", 1),
        ("BackgroundTopGradientColor", 0xFFFFFF),
        ("BackgroundType", 3),
        ("BackgroundTopGradientColor", 0xC0C0C0),
    ]


def test_scene_is_restored_exactly(solidworks: FakeSolidWorks) -> None:
    before = dict(solidworks.scene.props)
    with (
        open_document(Com(solidworks.app), solidworks.path) as document,
        document_background(document) as env,
    ):
        pass
    assert solidworks.scene.props == before and env.restored is True
    assert env.to_dict() == {
        "document": "vj-f01.SLDPRT",
        "original_scene": ORIGINAL_SCENE,
        "restored": True,
    }


def test_scene_restore_after_a_capture_exception(solidworks: FakeSolidWorks) -> None:
    before = dict(solidworks.scene.props)
    with (
        pytest.raises(RuntimeError, match="capture failed"),
        open_document(Com(solidworks.app), solidworks.path) as document,
        document_background(document),
    ):
        raise RuntimeError("capture failed")
    assert solidworks.scene.props == before
    assert solidworks.closed == ["vj-f01"]


def failing_restore(solidworks: FakeSolidWorks) -> None:
    """Scene writes after the two forcing writes raise, as if SOLIDWORKS refused them."""
    original = solidworks.scene.Invoke
    writes = []

    def invoke(dispid, lcid, flags, result, *args):
        if flags & DISPATCH_PROPERTYPUT:
            writes.append(args)
            if len(writes) > 2:
                raise FakeComError("scene is locked")
        return original(dispid, lcid, flags, result, *args)

    solidworks.scene.Invoke = invoke


def test_scene_restore_failure_is_loud_and_actionable(solidworks: FakeSolidWorks) -> None:
    failing_restore(solidworks)
    with (
        pytest.raises(EnvironmentRestoreError) as info,
        open_document(Com(solidworks.app), solidworks.path) as document,
        document_background(document),
    ):
        pass
    message = str(info.value)
    assert "could not restore the scene background of vj-f01.SLDPRT" in message
    assert "scene is locked" in message and "closed without saving" in message
    assert solidworks.closed == ["vj-f01"]  # the document still closes


def test_scene_restore_failure_is_not_hidden_by_the_capture_error(solidworks) -> None:
    failing_restore(solidworks)
    with (
        pytest.raises(EnvironmentRestoreError) as info,
        open_document(Com(solidworks.app), solidworks.path) as document,
        document_background(document),
    ):
        raise RuntimeError("capture failed")
    assert str(info.value.__cause__) == "capture failed"


def test_a_missing_scene_fails_cleanly(solidworks: FakeSolidWorks) -> None:
    solidworks.configuration.methods["GetScene"] = lambda: None
    with (
        pytest.raises(CaptureEnvironmentError, match="no scene"),
        open_document(Com(solidworks.app), solidworks.path) as document,
        document_background(document),
    ):
        pytest.fail("block must not run")
    assert solidworks.closed == ["vj-f01"]


def test_open_renderer_restores_the_scene_before_closing(solidworks: FakeSolidWorks) -> None:
    sessions = []
    with open_renderer(Com(solidworks.app), solidworks.path, sessions) as renderer:
        assert renderer.document.title == "vj-f01"
        assert solidworks.scene.props["BackgroundType"] == 1
    assert [s.restored for s in sessions] == [True]
    names = [name for who, name, _, _ in solidworks.log if who in ("scene", "app")]
    last_scene_write = max(
        i
        for i, (who, name, flags, _) in enumerate(solidworks.log)
        if who == "scene" and flags & DISPATCH_PROPERTYPUT
    )
    close = next(i for i, entry in enumerate(solidworks.log) if entry[1] == "CloseDoc")
    assert last_scene_write < close and "CloseDoc" in names
    assert solidworks.preferences.writes == []  # the document layer never touches preferences
