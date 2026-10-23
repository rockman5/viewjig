"""How shot and pair compose the controlled-background environment (no SOLIDWORKS)."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest
from fake_renderer import FakeRenderer
from fakes import Preferences, preference_app
from test_pair import SHORT, TALL, FakeOpener
from test_pair import request as pair_request

from viewjig import pair as pair_module
from viewjig.environment import DocumentBackground
from viewjig.pair import PairError, pair
from viewjig.shot import ShotError, ShotRequest, shoot
from viewjig.sw import Com, StandardView


def shot_request() -> ShotRequest:
    return ShotRequest(Path("vj-f01.SLDPRT"), StandardView.ISOMETRIC, 400, 300)


def paths(tmp_path: Path, *names: str) -> list[Path]:
    return [tmp_path / n for n in names]


# --- shot --------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [0, 1, 2, 3])
def test_shot_runs_inside_the_application_environment(tmp_path, mode) -> None:
    prefs = Preferences(appearance=mode)
    seen = []

    @contextmanager
    def opener(path):
        seen.append(("open", prefs.appearance))
        yield FakeRenderer()
        seen.append(("close", prefs.appearance))

    png, rec = paths(tmp_path, "shot.png", "shot.json")
    shoot(shot_request(), png, rec, app=Com(preference_app(prefs)), opener=opener)
    # Every capture happened on document scene backgrounds; the mode is back afterwards.
    assert seen == [("open", 3), ("close", 3)]
    assert prefs.appearance == mode
    assert prefs.writes == ([] if mode == 3 else [(305, 3), (305, mode)])
    record = json.loads(rec.read_text(encoding="utf-8"))
    env = record["capture_environment"]
    assert env["background"] == "plain white"
    assert env["application"]["temporarily_switched_to_document_scene"] is (mode != 3)
    assert env["application"]["restored"] is True
    assert png.is_file() and record["status"] == "ok"


def test_shot_fails_without_png_when_a_restore_cannot_be_confirmed(tmp_path) -> None:
    prefs = Preferences(appearance=1)
    prefs.ignore_after = 1  # the restore write has no effect

    @contextmanager
    def opener(path):
        yield FakeRenderer()

    png, rec = paths(tmp_path, "shot.png", "shot.json")
    with pytest.raises(ShotError, match="could not restore the SOLIDWORKS background setting"):
        shoot(shot_request(), png, rec, app=Com(preference_app(prefs)), opener=opener)
    assert not png.exists()
    record = json.loads(rec.read_text(encoding="utf-8"))
    assert record["status"] == "failed" and "should be Gradient (1)" in record["error"]
    assert record["capture_environment"]["application"]["restored"] is False
    assert record["attempts"]  # the shot's own record is kept


def test_shot_capture_failure_still_restores_and_keeps_its_message(tmp_path) -> None:
    prefs = Preferences(appearance=2)

    @contextmanager
    def opener(path):
        yield FakeRenderer(fit_shift_m=5.0)  # nothing visible: the shot fails

    png, rec = paths(tmp_path, "shot.png", "shot.json")
    with pytest.raises(ShotError, match="found no usable starting view"):
        shoot(shot_request(), png, rec, app=Com(preference_app(prefs)), opener=opener)
    assert prefs.appearance == 2 and prefs.writes == [(305, 3), (305, 2)]
    record = json.loads(rec.read_text(encoding="utf-8"))
    assert record["capture_environment"]["application"]["restored"] is True


# --- pair --------------------------------------------------------------------------------------


def test_pair_switches_the_application_once_around_every_session(tmp_path) -> None:
    prefs = Preferences(appearance=1)
    opener = FakeOpener(FakeRenderer(model=SHORT), FakeRenderer(model=TALL))
    seen = []
    original_call = opener.__call__

    @contextmanager
    def watching(path):
        seen.append(prefs.appearance)
        with original_call(path) as renderer:
            yield renderer

    a, b, rec = paths(tmp_path, "a.png", "b.png", "pair.json")
    pair(pair_request(), a, b, rec, app=Com(preference_app(prefs)), opener=watching)
    assert len(seen) >= 7 and set(seen) == {3}  # every session on document scenes
    assert prefs.writes == [(305, 3), (305, 1)]  # switched once, restored once
    record = json.loads(rec.read_text(encoding="utf-8"))
    assert record["capture_environment"]["application"]["restored"] is True


def test_pair_applies_the_document_environment_per_session(tmp_path, monkeypatch) -> None:
    prefs = Preferences(appearance=3)
    app = Com(preference_app(prefs))
    renderers = {"a.SLDPRT": FakeRenderer(model=SHORT), "b.SLDPRT": FakeRenderer(model=TALL)}
    calls = []

    @contextmanager
    def fake_open_renderer(connected, path, environments):
        assert connected is app
        env = DocumentBackground(Path(path).name, {"BackgroundType": 3}, restored=True)
        environments.append(env)
        calls.append(Path(path).name)
        yield renderers[Path(path).name]

    monkeypatch.setattr(pair_module, "open_renderer", fake_open_renderer)
    a, b, rec = paths(tmp_path, "a.png", "b.png", "pair.json")
    pair(pair_request(), a, b, rec, app=app)
    record = json.loads(rec.read_text(encoding="utf-8"))
    documents = record["capture_environment"]["documents"]
    assert set(documents) == {"a.SLDPRT", "b.SLDPRT"}
    assert sum(d["sessions"] for d in documents.values()) == len(calls) >= 7
    assert all(d["all_restored"] for d in documents.values())
    assert prefs.writes == []  # already on document scenes: no global write


def test_pair_restore_failure_fails_the_pair_without_pngs(tmp_path) -> None:
    prefs = Preferences(appearance=0)
    prefs.ignore_after = 1
    opener = FakeOpener(FakeRenderer(model=SHORT), FakeRenderer(model=TALL))
    a, b, rec = paths(tmp_path, "a.png", "b.png", "pair.json")
    with pytest.raises(PairError, match="should be Plain"):
        pair(pair_request(), a, b, rec, app=Com(preference_app(prefs)), opener=opener)
    assert not a.exists() and not b.exists()
    assert json.loads(rec.read_text(encoding="utf-8"))["status"] == "failed"
