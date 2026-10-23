from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from viewjig import cli, sw
from viewjig.environment import EnvironmentRestoreError
from viewjig.pair import PairError
from viewjig.shot import ShotError
from viewjig.steps import write_record
from viewjig.sw import StandardView


@pytest.fixture
def parts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    monkeypatch.chdir(tmp_path)
    a, b = tmp_path / "vj-f01.SLDPRT", tmp_path / "vj-f01b.SLDPRT"
    a.write_bytes(b"part")
    b.write_bytes(b"part")
    return a, b


def ok_record(margin=60, source="default", attempts=1, exact=True) -> dict:
    verification = {"tolerance_px": 3.0, "margin_residuals": [1.0, 0.5, -1.25, 0.0]}
    return {
        "status": "ok",
        "margin": {"px": margin, "source": source},
        "attempts": [{"verification": verification}] * attempts,
        "final": {"readback_exact": {"a": exact, "b": exact}},
    }


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list:
    """Replace shoot/pair with recorders; nothing reaches SOLIDWORKS."""
    seen: list = []

    def fake_shoot(request, png, record_path):
        seen.append(("shot", request, png, record_path))
        return SimpleNamespace(
            record=ok_record(
                margin=request.margin_px or 60,
                source="explicit" if request.margin_px else "default",
            )
        )

    def fake_pair(request, png_a, png_b, record_path):
        seen.append(("pair", request, png_a, png_b, record_path))
        return SimpleNamespace(record=ok_record(attempts=2))

    monkeypatch.setattr(cli, "shoot", fake_shoot)
    monkeypatch.setattr(cli, "pair", fake_pair)
    return seen


@pytest.fixture
def solidworks_not_running(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse():
        raise sw.SolidWorksNotRunning("SOLIDWORKS is not running")

    monkeypatch.setattr(sw, "attach", refuse)


# --- Parsing and dispatch --------------------------------------------------------------------


def test_shot_defaults(parts, calls) -> None:
    assert cli.main(["shot", str(parts[0])]) == 0
    kind, request, png, record = calls[0]
    assert kind == "shot"
    assert request.path == parts[0] and request.view is StandardView.ISOMETRIC
    assert (request.width, request.height, request.margin_px) == (1600, 1200, None)
    assert png == Path("vj-f01-isometric-1600x1200.png")
    assert record == Path("vj-f01-isometric-1600x1200.json")


def test_shot_options(parts, calls) -> None:
    args = [
        "shot",
        str(parts[0]),
        "--view",
        "front",
        "--width",
        "1000",
        "--height",
        "800",
        "--margin",
        "25",
        "--output",
        "out/front.png",
    ]
    assert cli.main(args) == 0
    _, request, png, record = calls[0]
    assert request.view is StandardView.FRONT
    assert (request.width, request.height, request.margin_px) == (1000, 800, 25)
    assert (png, record) == (Path("out/front.png"), Path("out/front.json"))


def test_shot_output_without_png_suffix_gets_one(parts, calls) -> None:
    cli.main(["shot", str(parts[0]), "--output", "out/frame"])
    assert calls[0][2:] == (Path("out/frame.png"), Path("out/frame.json"))


def test_pair_defaults_name_the_relationship(parts, calls) -> None:
    assert cli.main(["pair", str(parts[0]), str(parts[1])]) == 0
    kind, request, png_a, png_b, record = calls[0]
    assert kind == "pair"
    assert (request.path_a, request.path_b) == parts
    base = "vj-f01-vs-vj-f01b-isometric-1600x1200"
    assert png_a == Path(f"{base}-before.png")
    assert png_b == Path(f"{base}-after.png")
    assert record == Path(f"{base}.json")


@pytest.mark.parametrize("output", ["out/review", "out/review.png", "out/review.json"])
def test_pair_output_is_a_base_path(parts, calls, output) -> None:
    cli.main(["pair", str(parts[0]), str(parts[1]), "--output", output])
    assert calls[0][2:] == (
        Path("out/review-before.png"),
        Path("out/review-after.png"),
        Path("out/review.json"),
    )


def test_isometric_and_front_are_the_supported_views(parts, calls) -> None:
    for name in ("isometric", "front"):
        cli.main(["shot", str(parts[0]), "--view", name])
    assert [c[1].view for c in calls] == [StandardView.ISOMETRIC, StandardView.FRONT]


@pytest.mark.parametrize("view", ["top", "trimetric", "dimetric", "back"])
def test_other_standard_views_are_not_offered_in_v1(parts, calls, view, capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["shot", str(parts[0]), "--view", view])
    assert exit_info.value.code == 2 and calls == []
    assert "invalid choice" in capsys.readouterr().err


# --- Validation before SOLIDWORKS -------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        ["--width", "0"],
        ["--width", "-5"],
        ["--height", "abc"],
        ["--view", "sideways"],
        ["--margin", "-1"],
        ["--margin", "600"],  # 2 x 600 leaves nothing of 1200
    ],
)
def test_invalid_options_exit_2_without_capturing(parts, calls, extra, capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["shot", str(parts[0]), *extra])
    assert exit_info.value.code == 2 and calls == []
    assert "Traceback" not in capsys.readouterr().err


def test_missing_and_unsupported_files_are_rejected(parts, calls, tmp_path, capsys) -> None:
    drawing = tmp_path / "sheet.SLDDRW"
    drawing.write_bytes(b"")
    assembly = tmp_path / "set.SLDASM"
    assembly.write_bytes(b"")
    for args in (
        ["shot", "missing.SLDPRT"],
        ["shot", str(drawing)],
        ["shot", str(assembly)],
        ["pair", str(parts[0]), "missing.SLDPRT"],
        ["pair", str(parts[0]), str(parts[0])],
    ):
        with pytest.raises(SystemExit) as exit_info:
            cli.main(args)
        assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert "not found" in err and ".SLDPRT" in err and "two different files" in err
    assert calls == []


def test_no_command_prints_help_and_exits_2(capsys) -> None:
    assert cli.main([]) == 2
    assert "shot" in capsys.readouterr().err


# --- Output ------------------------------------------------------------------------------------


def test_successful_shot_output_is_concise_and_absolute(parts, calls, capsys) -> None:
    cli.main(["shot", str(parts[0])])
    lines = capsys.readouterr().out.splitlines()
    png = (Path.cwd() / "vj-f01-isometric-1600x1200.png").resolve()
    assert lines[0] == f"viewjig shot: {png}"
    assert "isometric, 1600x1200, margin 60 px (5% default), 1 correction" in lines[1]
    assert "verified within 3.0 px (worst side 1.2 px)" in lines[1]
    assert lines[2] == f"  record: {png.with_suffix('.json')}"
    assert len(lines) == 3 and not any("{" in line for line in lines)


def test_successful_pair_output_is_concise_and_absolute(parts, calls, capsys) -> None:
    cli.main(["pair", str(parts[0]), str(parts[1]), "--margin", "40"])
    lines = capsys.readouterr().out.splitlines()
    base = (Path.cwd() / "vj-f01-vs-vj-f01b-isometric-1600x1200").resolve()
    assert lines[0] == f"viewjig pair: {base}-before.png"
    assert lines[1].strip() == f"{base}-after.png"
    assert "2 corrections" in lines[2]
    assert lines[3] == "  one shared camera, read back exactly on both: True"
    assert lines[4] == f"  record: {base}.json"
    assert len(lines) == 5 and not any("{" in line for line in lines)


def test_explicit_output_paths_are_shown_absolute(parts, calls, capsys) -> None:
    cli.main(["shot", str(parts[0]), "--output", "slides/front.png"])
    first = capsys.readouterr().out.splitlines()[0]
    assert first == f"viewjig shot: {(Path.cwd() / 'slides' / 'front.png').resolve()}"


# --- Operational failures ------------------------------------------------------------------------


def failure_lines(err: str) -> list[str]:
    assert "Traceback" not in err
    return err.splitlines()


def test_shot_failure_explains_itself_and_keeps_the_record(
    parts, solidworks_not_running, capsys
) -> None:
    assert cli.main(["shot", str(parts[0])]) == 1
    captured = capsys.readouterr()
    record_path = (Path.cwd() / "vj-f01-isometric-1600x1200.json").resolve()
    assert failure_lines(captured.err) == [
        "viewjig shot failed: SOLIDWORKS is not running",
        "  Next: Start SOLIDWORKS, then run the command again.",
        "  Your part files were not modified (ViewJig opens them read-only and never saves).",
        "  SOLIDWORKS settings: unchanged.",
        f"  Details: {record_path}",
    ]
    assert captured.out == ""
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["status"] == "failed"
    assert record["error"] == "SolidWorksNotRunning: SOLIDWORKS is not running"  # full detail kept
    assert not Path("vj-f01-isometric-1600x1200.png").exists()


def test_pair_failure_explains_itself_and_keeps_the_record(
    parts, solidworks_not_running, capsys
) -> None:
    assert cli.main(["pair", str(parts[0]), str(parts[1])]) == 1
    lines = failure_lines(capsys.readouterr().err)
    base = "vj-f01-vs-vj-f01b-isometric-1600x1200"
    assert lines[0] == "viewjig pair failed: SOLIDWORKS is not running"
    assert lines[-1] == f"  Details: {(Path.cwd() / f'{base}.json').resolve()}"
    assert json.loads(Path(f"{base}.json").read_text(encoding="utf-8"))["status"] == "failed"
    assert not Path(f"{base}-before.png").exists() and not Path(f"{base}-after.png").exists()


def raising(error: Exception, record: dict | None = None):
    """A shoot replacement that fails the way shoot() does: ShotError chained from ``error``."""

    def failing_shoot(request, png, record_path):
        full = {"status": "failed", "error": f"{type(error).__name__}: {error}", **(record or {})}
        write_record(full, record_path)
        raise ShotError(full["error"], full) from error

    return failing_shoot


@pytest.mark.parametrize(
    "error, next_step",
    [
        (sw.SolidWorksNotRunning("SOLIDWORKS is not running"), "Start SOLIDWORKS"),
        (sw.DocumentAlreadyOpen("vj-f01.SLDPRT is already open in SOLIDWORKS"), "Close it"),
        (sw.DocumentNotReadOnly("vj-f01.SLDPRT is open for editing in SOLIDWORKS"), "Close it"),
        (
            sw.DocumentOpenFailed("OpenDoc6 failed for vj-f01.SLDPRT (errors=2, warnings=0)"),
            "Check that SOLIDWORKS can open it on its own.",
        ),
    ],
)
def test_known_failures_get_a_next_step(parts, monkeypatch, capsys, error, next_step) -> None:
    monkeypatch.setattr(cli, "shoot", raising(error))
    assert cli.main(["shot", str(parts[0])]) == 1
    lines = failure_lines(capsys.readouterr().err)
    assert lines[0] == f"viewjig shot failed: {error}"  # no internal class name
    assert lines[1].startswith("  Next: ") and next_step in lines[1]


def test_other_failures_get_no_invented_advice(parts, monkeypatch, capsys) -> None:
    def failing_shoot(request, png, record_path):
        record = {"status": "failed", "error": "framing still outside tolerance after 2 solves"}
        write_record(record, record_path)
        raise ShotError(record["error"], record)

    def failing_pair(request, png_a, png_b, record_path):
        record = {
            "status": "failed",
            "error": "attempt 1 is not measurable: ['b: nothing is visible']",
        }
        write_record(record, record_path)
        raise PairError(record["error"], record)

    monkeypatch.setattr(cli, "shoot", failing_shoot)
    monkeypatch.setattr(cli, "pair", failing_pair)
    assert cli.main(["shot", str(parts[0])]) == 1
    assert cli.main(["pair", str(parts[0]), str(parts[1])]) == 1
    err = capsys.readouterr().err
    assert "after 2 solves" in err and "b: nothing is visible" in err
    assert "Next:" not in err and "Traceback" not in err
    assert err.count("Your part files were not modified") == 2


UNSWITCHED = {"temporarily_switched_to_document_scene": False, "restored": True}
SWITCHED = {"temporarily_switched_to_document_scene": True, "restored": True}
NOT_RESTORED = {"temporarily_switched_to_document_scene": True, "restored": False}
SCENE_RESTORED = {"vj-f01.SLDPRT": {"sessions": 2, "all_restored": True}}
SCENE_NOT_RESTORED = {"vj-f01.SLDPRT": {"sessions": 2, "all_restored": False}}
UNFULLY = "could NOT be fully restored; see the message above."


@pytest.mark.parametrize(
    "application, documents, expected",
    [
        (None, {}, "unchanged."),
        (UNSWITCHED, {}, "unchanged."),
        (SWITCHED, {}, "restored."),
        (UNSWITCHED, SCENE_RESTORED, "restored."),  # document scene only
        (SWITCHED, SCENE_RESTORED, "restored."),  # application and document scene
        (NOT_RESTORED, SCENE_RESTORED, UNFULLY),
        (UNSWITCHED, SCENE_NOT_RESTORED, UNFULLY),  # document restoration failed
        (SWITCHED, SCENE_NOT_RESTORED, UNFULLY),
        (None, SCENE_NOT_RESTORED, UNFULLY),
        ({**UNSWITCHED, "restored": False}, {}, UNFULLY),  # a check failed without a switch
    ],
)
def test_failure_reports_the_settings_state(
    parts, monkeypatch, capsys, application, documents, expected
):
    record = {
        "capture_environment": {
            "background": "plain white",
            "application": application,
            "documents": documents,
        }
    }
    monkeypatch.setattr(cli, "shoot", raising(RuntimeError("capture failed"), record))
    assert cli.main(["shot", str(parts[0])]) == 1
    lines = failure_lines(capsys.readouterr().err)
    assert f"  SOLIDWORKS settings: {expected}" in lines


def test_restore_failure_message_leads(parts, monkeypatch, capsys) -> None:
    error = EnvironmentRestoreError(
        "ViewJig could not restore the SOLIDWORKS background setting (System Options > Colors > "
        "Background appearance): it should be Gradient (1) but is Use document scene background "
        "(3). Please set it back in System Options."
    )
    record = {
        "capture_environment": {
            "application": {"temporarily_switched_to_document_scene": True, "restored": False}
        }
    }
    monkeypatch.setattr(cli, "shoot", raising(error, record))
    cli.main(["shot", str(parts[0])])
    lines = failure_lines(capsys.readouterr().err)
    assert lines[0].startswith("viewjig shot failed: ViewJig could not restore")
    assert "Please set it back in System Options." in lines[0]
    assert lines[2] == f"  SOLIDWORKS settings: {UNFULLY}"


# --- Packaging ---------------------------------------------------------------------------------


def test_the_viewjig_command_is_installed() -> None:
    from importlib.metadata import entry_points

    (script,) = entry_points(group="console_scripts", name="viewjig")
    assert script.value == "viewjig.cli:main"


def test_version_flag(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.startswith("viewjig ")
