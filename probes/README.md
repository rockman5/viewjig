# Probes

Experimental characterization of native SOLIDWORKS view and raster-capture
behaviour, run against neutral fixtures (`fixtures/`). Probes are not
production code. Each one records what SOLIDWORKS actually does, and its
findings file separates direct observations, supported interpretations, and
open questions.

Probe output goes to `captures/` (git-ignored). Findings cite those runs by
tag.

## Characterization sequence: complete

| Probe | Question | Findings |
|-------|----------|----------|
| [capture-01](capture_01.py) | What does standard view + Zoom to Fit + fixed-size `SaveBMP` do? | [capture_01_findings.md](capture_01_findings.md) |
| [capture-02](capture_02.py) | Does a camera restored in another window give the same raster? | [capture_02_findings.md](capture_02_findings.md) |
| [capture-03](capture_03.py) | How do `Scale2` / `Translation3` map to the raster, and does one camera register two documents? | [capture_03_findings.md](capture_03_findings.md) |
| [capture-04](capture_04.py) | How close does one predicted camera correction land? | [capture_04_findings.md](capture_04_findings.md) |

Results the production design rests on:

- **Exact output size.** `SaveBMP` returns exactly the requested size and is
  deterministic for a given camera (capture-01).
- **Portable camera.** The camera (`Orientation3`, `Scale2`, `Translation3` as
  plain numbers) plus the output size fully determines fixed-size framing,
  independent of the SOLIDWORKS window, window state, and session
  (capture-02). Assign `Translation3` last (capture-01).
- **Window-dependent Zoom to Fit.** Native Zoom to Fit depends on the window,
  so it is only a bootstrap (capture-01/02).
- **Simple camera model.** `Scale2` scales the image about the projected model
  origin, `Translation3` X/Y move it linearly at a rate set by the output
  size, and Z has no effect (capture-03).
- **Cross-document registration.** One camera registers separate documents
  for the geometry they share. Curved edges can differ by about a pixel, so
  cross-document checks need tolerance (capture-03).
- **One-step framing.** One correction calculated from measured rasters lands
  within about 2.5 px of a raster-space target, in isometric and Front views
  and at both output regimes (capture-04).

All runs used one machine, SOLIDWORKS 2026 SP4.0, orthographic views, Shaded
with Edges, and a plain white background. Open questions are listed in each
findings file.
