# ViewJig

**ViewJig is a repeatable camera jig for CAD.**

Capture SOLIDWORKS models with consistent position, scale, framing, and output dimensions—so before/after images stay perfectly registered.

## The problem

Screenshots of SOLIDWORKS models rarely line up. Zoom to Fit depends on the
size of your window, every model fits differently, and two versions of a part
end up at different scales and positions. Flip between them on a slide and the
model jumps.

ViewJig frames the model for you, at an exact output size, and reports exactly
how it did it:

- **`viewjig shot`** frames one part, centred with an even margin, and saves a PNG.
- **`viewjig pair`** captures two parts with **one shared camera** at one size.
  Geometry the two parts have in common lands on the same pixels in both images,
  so you can flip between them or overlay them.

## See it work

Two revisions of the same SOLIDWORKS part. The upright depth changes from
**40 mm to 60 mm**.

ViewJig captures both with one shared camera, so unchanged geometry stays
registered while the design change moves.

![ViewJig registered before-and-after pair](docs/images/viewjig-pair-vj-f01-vj-f01b.gif)

**No post-capture alignment.** These are the raw registered outputs from
`viewjig pair`.

Every capture also writes a JSON record of the camera, the measurements, and
the verification, so a result can be checked and reproduced.

## Quick start

SOLIDWORKS must already be running. ViewJig attaches to it, opens each part
read-only, and closes it again. It never saves or edits your files, and it
refuses to touch a part that is already open. Whatever background you use in
SOLIDWORKS, ViewJig captures on plain white and puts your settings back
afterwards.

The examples use `viewjig`. If you followed [Installation](#installation)
without adding ViewJig to your `PATH`, type `.venv\Scripts\viewjig.exe` (from
the ViewJig folder) instead.

Capture one part:

```console
> viewjig shot bracket.SLDPRT
viewjig shot: C:\Users\you\Documents\renders\bracket-isometric-1600x1200.png
  isometric, 1600x1200, margin 60 px (5% default), 1 correction, verified within 3.0 px (worst side 1.0 px)
  record: C:\Users\you\Documents\renders\bracket-isometric-1600x1200.json
```

Capture a matched pair:

```console
> viewjig pair bracket-rev1.SLDPRT bracket-rev2.SLDPRT
viewjig pair: C:\Users\you\Documents\renders\bracket-rev1-vs-bracket-rev2-isometric-1600x1200-before.png
              C:\Users\you\Documents\renders\bracket-rev1-vs-bracket-rev2-isometric-1600x1200-after.png
  isometric, 1600x1200, margin 60 px (5% default), 1 correction, verified within 3.0 px (worst side 1.1 px)
  one shared camera, read back exactly on both: True
  record: C:\Users\you\Documents\renders\bracket-rev1-vs-bracket-rev2-isometric-1600x1200.json
```

The images are saved in the folder you run the command from, unless you give
`--output`. ViewJig prints the full paths of the images and of their JSON record.

The pair is framed so that **both** parts fit, with the margin applied to
their combined extent.

### Options

| Option | Default | Meaning |
|--------|---------|---------|
| `--view` | `isometric` | `isometric` or `front` |
| `--width`, `--height` | `1600`, `1200` | Output size in pixels |
| `--margin PX` | 5 % of the shorter side | Margin around the framed model, in pixels |
| `--output` | in the current directory | `shot`: the PNG path (the record goes beside it as `.json`). `pair`: a base path; `-before.png`, `-after.png`, and `.json` are added to it |

```console
> viewjig shot bracket.SLDPRT --view front --width 1920 --height 1080 --margin 80 --output slides/bracket-front.png
> viewjig pair rev1.SLDPRT rev2.SLDPRT --output review/bracket
```

Existing files at the chosen or default output paths are overwritten.

Run `viewjig shot --help` or `viewjig pair --help` for details.

### When something goes wrong

If ViewJig can't produce a verified result, it writes no image, exits with
status 1, and says what happened:

```console
> viewjig shot bracket.SLDPRT
viewjig shot failed: bracket.SLDPRT is already open in SOLIDWORKS
  Next: Close it in SOLIDWORKS, then run the command again.
  Your part files were not modified (ViewJig opens them read-only and never saves).
  SOLIDWORKS settings: unchanged.
  Details: C:\Users\you\Documents\renders\bracket-isometric-1600x1200.json
```

A `Next:` line appears when there is something specific to do: start
SOLIDWORKS, close a part that is already open, or check that SOLIDWORKS can open
the file. The settings line says whether ViewJig had temporarily changed the
SOLIDWORKS background (the application setting or the part's scene) and whether
every change was put back. The JSON record holds the full details. Invalid
arguments exit with status 2 before SOLIDWORKS is touched.

## Installation

Requirements:

- Windows with SOLIDWORKS installed (developed and tested with SOLIDWORKS 2026 SP4)
- Python 3.14 or later, from [python.org](https://www.python.org/downloads/windows/)
  (its installer includes the `py` launcher used below)

Get ViewJig with `git clone https://github.com/rockman5/viewjig`, or download
the repository as a ZIP from GitHub and extract it. Then, in the ViewJig folder:

```console
> py -3.14 -m venv .venv
> .venv\Scripts\python.exe -m pip install .
> .venv\Scripts\viewjig.exe --help
```

This installs ViewJig and its dependencies (Pillow and pywin32) into the
`.venv` folder. You don't need to activate the environment: run
`.venv\Scripts\viewjig.exe` directly, from any folder if you give its full path.

To type just `viewjig` in any terminal, add the full path of the ViewJig
folder's `.venv\Scripts` to your user `PATH` environment variable.

**Updating:** if you used `git clone`, run `git pull` in the ViewJig folder,
then `.venv\Scripts\python.exe -m pip install .` again. If you used a ZIP,
download and extract the new version to a new folder and install from there,
following the steps above.

**Developing ViewJig:** install it editable with the test tools instead:
`.venv\Scripts\python.exe -m pip install -e ".[dev]"`.

## Scope (V1)

- **Files:** SOLIDWORKS parts (`.SLDPRT`).
- **Views:** Isometric and Front, the two standard views validated against
  live SOLIDWORKS.
- **Display:** orthographic views. ViewJig captures on a plain white background
  whatever background SOLIDWORKS normally shows, then restores your settings
  (see the JSON record's `capture_environment`).
- **Output:** PNG at exactly the requested size, framed to within 3 px of the
  target margin on every side.

## How it works

1. Set the standard view and run native Zoom to Fit, only as a starting point.
2. Shrink that camera slightly, capture, and measure where the model actually
   lands in the image.
3. Nudge the camera's pan on each axis and measure how far the image moves.
   This calibrates the camera for this run.
4. Calculate the camera that centres the model with the requested margin,
   apply it, capture, and verify the result. If it misses the 3 px tolerance,
   correct once more from the measured result. It makes two corrections at
   most, and fails rather than return an unverified image.

For a pair, steps 2–4 use the combined extent of both parts, and the final
camera is applied identically to each.

## The JSON record

The PNG is the result. The JSON record beside it is the complete engineering
and reproducibility record of how that image was made. You don't need it for
normal use, and it is long by design.

The fields worth knowing:

- `status` is `ok` or `failed`, and `error` says why a capture failed;
- `output` lists the image files that were written;
- `final.camera` records, as plain numbers, the camera that produced the final
  framing;
- `capture_environment` says which SOLIDWORKS settings ViewJig changed
  temporarily, and confirms they were restored.

In full, each record contains:

- the input path(s), view, output size, and the margin with whether it was the
  default or explicit;
- the Zoom to Fit and bootstrap cameras, as plain numbers, and what was
  measured at each stage;
- the calibration: the pan steps tried, the measured pixels per metre, and how
  well they fit;
- every correction attempt: the calculated camera, the camera SOLIDWORKS read
  back, the measured margins, and the per-side residuals against the
  prediction;
- the final camera and measurement, and `status` (`ok` or `failed`, with the
  error);
- for a pair, both members' measurements, their combined extent, confirmation
  that each read back the same camera, and an edge-overlap check of the two
  images. The edge-overlap check is a diagnostic only and never affects the
  result.

## Boundaries

ViewJig makes CAD models visually comparable. It does not decide what their differences mean.

It does not compare feature trees, match faces, diff geometry, or classify
what was added or removed. A pair is registered because both images come from
the same camera at the same size, not because ViewJig worked out what the
parts share.

## Engineering evidence

ViewJig's behaviour rests on a set of characterization experiments against live
SOLIDWORKS: capture determinism, window independence, the camera-to-image
model, cross-document registration, and one-step framing accuracy. The probes
and their findings are in [probes/README.md](probes/README.md); the design
notes are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## License

MIT
