from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from viewjig import cli, sw
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


def test_successful_shot_output_is_concise(parts, calls, capsys) -> None:
    cli.main(["shot", str(parts[0])])
    out = capsys.readouterr().out
    assert "vj-f01-isometric-1600x1200.png" in out and "vj-f01-isometric-1600x1200.json" in out
    assert "isometric, 1600x1200, margin 60 px (5% default), 1 correction" in out
    assert "verified within 3.0 px (worst side 1.2 px)" in out
    assert "{" not in out and len(out.splitlines()) == 3


def test_successful_pair_output_is_concise(parts, calls, capsys) -> None:
    cli.main(["pair", str(parts[0]), str(parts[1]), "--margin", "40"])
    out = capsys.readouterr().out
    assert "-before.png" in out and "-after.png" in out and ".json" in out
    assert "2 corrections" in out
    assert "one shared camera, read back exactly on both: True" in out
    assert "{" not in out and len(out.splitlines()) == 5


# --- Operational failures ------------------------------------------------------------------------


def test_shot_failure_exits_1_with_record_and_no_traceback(
    parts, solidworks_not_running, capsys
) -> None:
    assert cli.main(["shot", str(parts[0])]) == 1
    captured = capsys.readouterr()
    assert "viewjig shot failed: SolidWorksNotRunning" in captured.err
    assert "record: vj-f01-isometric-1600x1200.json" in captured.err
    assert "Traceback" not in captured.err and captured.out == ""
    record = json.loads(Path("vj-f01-isometric-1600x1200.json").read_text(encoding="utf-8"))
    assert record["status"] == "failed" and "not running" in record["error"]
    assert not Path("vj-f01-isometric-1600x1200.png").exists()


def test_pair_failure_exits_1_with_record_and_no_traceback(
    parts, solidworks_not_running, capsys
) -> None:
    assert cli.main(["pair", str(parts[0]), str(parts[1])]) == 1
    captured = capsys.readouterr()
    base = "vj-f01-vs-vj-f01b-isometric-1600x1200"
    assert "viewjig pair failed" in captured.err and f"record: {base}.json" in captured.err
    assert "Traceback" not in captured.err
    assert json.loads(Path(f"{base}.json").read_text(encoding="utf-8"))["status"] == "failed"
    assert not Path(f"{base}-before.png").exists() and not Path(f"{base}-after.png").exists()


def test_framing_failures_are_reported_from_the_record(parts, monkeypatch, capsys) -> None:
    def failing_shoot(request, png, record_path):
        record = {"status": "failed", "error": "framing still outside tolerance after 2 solves"}
        write_record(record, record_path)
        raise ShotError(record["error"], record)

    def failing_pair(request, png_a, png_b, record_path):
        record = {"status": "failed", "error": "attempt 1 is not measurable: ['b: no foreground']"}
        write_record(record, record_path)
        raise PairError(record["error"], record)

    monkeypatch.setattr(cli, "shoot", failing_shoot)
    monkeypatch.setattr(cli, "pair", failing_pair)
    assert cli.main(["shot", str(parts[0])]) == 1
    assert cli.main(["pair", str(parts[0]), str(parts[1])]) == 1
    err = capsys.readouterr().err
    assert "after 2 solves" in err and "b: no foreground" in err and "Traceback" not in err


def test_version_flag(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.startswith("viewjig ")
