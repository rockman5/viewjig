# VJ-CAPTURE-02 findings

Characterization of whether a SOLIDWORKS camera saved as plain numbers
reproduces the same fixed-size raster in other window configurations, using
`probes/capture_02.py` and fixture VJ-F01. Builds on
[capture-01](capture_01_findings.md).

Status: **closed**. Hypotheses carried forward are listed at the end.

## Setup

Same machine, SOLIDWORKS release, display state, background, foreground
threshold, and fixture as capture-01 (SOLIDWORKS 34.4.0, `DisplayMode` 5, no
perspective, plain white background, threshold 8, VJ-F01 opened read-only).

The camera is the triple (`Orientation3` 16 values, `Translation3` 3 values,
`Scale2`), saved to `camera.json` as plain numbers. It is assigned back by
building a `MathTransform` and `MathVector` with
`IMathUtility.CreateTransform` / `CreateVector` from `VT_ARRAY | VT_R8`
arrays, then assigning `Orientation3`, `Scale2`, `Translation3` in that order
(capture-01 O9).

The probe moves the SOLIDWORKS main window with Win32 and restores its
original placement at the end. Every configuration starts from the saved
camera, so a resize is the only change before the first capture in it.

Runs (2026-10-22; reports under `captures/capture-02/<tag>/`, not committed):

| Tag | What it did | Captures |
|-----|-------------|----------|
| `portable` | Isometric + Zoom to Fit while maximized, camera saved; then maximized → left-half → landscape → portrait → maximized, then minimized | 53 |
| `nopanel` | Separate invocation: camera loaded from `portable/camera.json`, maximized only, intended to have the FeatureManager panel hidden | 8 |

Graphics areas reached in `portable` (view client size, Win32):

| Configuration | Graphics area | Aspect |
|---------------|---------------|--------|
| maximized | 2560×1297 | 1.974 |
| left-half | 1258×1275 | 0.987 |
| landscape | 1514×587 | 2.579 |
| portrait | 797×1275 | 0.625 |

Each resize settled (client size stable for three 0.2 s polls) within 0.7 s.

## Directly observed

**O1 — A saved camera reproduces fixed-size rasters in every window
configuration.** With the camera assigned, every requested size (1600×1200,
1000×1000, 1600×900, 1280×1000, 1300×1000) was pixel-identical in all five
configuration visits to the first maximized reference. The camera read back
exactly after every assignment.

**O2 — Restoring from plain numbers is exact.** In the first maximized window,
the image captured after assigning the camera from numbers was pixel-identical
to the image captured directly after native Zoom to Fit.

**O3 — Resizing the window does not change the camera or the fixed-size
raster.** After every resize, `Orientation3`, `Translation3`, and `Scale2` still
equalled the saved camera, and a 1600×1200 capture taken before reapplying
anything was pixel-identical to the reference.

**O4 — The plain-number camera persists across documents being closed and
reopened and across separate probe runs.** The `nopanel` run was a separate
invocation that opened the fixture fresh and loaded the camera from
`portable/camera.json`. All six captures (0×0 and the five fixed sizes) were
pixel-identical to the corresponding `portable` maximized captures.

**O5 — Native Zoom to Fit depends on the window.** Run in each window, it gave:

| Configuration | `Scale2` | 1600×1200 model px | Size vs reference | Centre offset x |
|---------------|----------|--------------------|-------------------|-----------------|
| maximized | 1.5554151637250084 | 1041×1024 | 1 | +62.5 |
| left-half | 1.4664969614775047 | 982×966 | 0.943 | +108.0 |
| landscape | 1.4960589568554743 | 1001×986 | 0.962 | +215.5 |
| portrait | 1.365371094348646 | 913×900 | 0.877 | +207.5 |

Vertical centre offset was 0 in all of them.

**O6 — For a fixed camera, the model's fill depends only on the raster's own
aspect ratio, whether the raster is the live view (0×0) or a SaveBMP output.**

| Raster | Aspect | Fill (w, h) |
|--------|--------|-------------|
| portrait view 797×1275 | 0.625 | (0.6713, 0.4133) |
| left-half view 1258×1275 | 0.987 | (0.6709, 0.6518) |
| 1000×1000 | 1.000 | (0.6710, 0.6600) |
| 1280×1000 | 1.280 | (0.6703, 0.8440) |
| 1300×1000 | 1.300 | (0.6669, 0.8540) |
| 1600×1200 | 1.333 | (0.6506, 0.8533) |
| 1600×900 | 1.778 | (0.4881, 0.8544) |
| maximized view 2560×1297 | 1.974 | (0.4391, 0.8535) |
| landscape view 1514×587 | 2.579 | (0.3369, 0.8535) |

Below the switch, width fill is constant at about 0.671. Above it, height fill
is constant at about 0.8535. The switch lies between output aspects 1.28 and
1.30.

**O7 — Capture works while SOLIDWORKS is minimized.** With Win32 reporting the
window chain minimized (view visible box at (−31714, −31829)), all six captures
were pixel-identical to the same captures taken visible just before. 0×0 kept
the maximized graphics-area size, 2560×1297, and the camera was unchanged.

**O8 — The source document was never modified, and the window was restored.**
`GetSaveFlag` was 0 in all 159 state snapshots across both runs. The fixture
was closed at the end of each run. Win32 confirmed the original show state and
normal rectangle were restored after `portable`.

**O9 — The FeatureManager test was inconclusive.** In `nopanel` the visible
box (left edge 286) and the native Zoom to Fit camera were bit-identical to
`portable`. The hidden-panel condition was not confirmed in the document
window the probe opened, so this run neither supports nor refutes capture-01
H2.

## Strongly supported interpretations

**I1 — Fixed-size SaveBMP framing is a function of the camera and the
requested size only.** The window size, window state (including minimized),
document session, and probe invocation did not change it (O1, O3, O4, O7).

**I2 — The plain-number camera is a portable framing recipe.** It can be
recorded once and reapplied later or elsewhere to reproduce a framing exactly,
for the same document (O2, O4). Whether it registers *different* documents is
capture-03's question.

**I3 — The ≈ 1.29 framing rectangle belongs to the camera, not the window.**
This corrects the attribution in capture-01 H1. Every raster — the live view
and every SaveBMP size — scales one camera-defined region uniformly so it fits
inside the raster, centred (O6). Within one window, "inscribed in the graphics
area" described the same region, which is why capture-01 could not tell the
two apart. The aspect ratio is fitted (≈ 1.29, switch between 1.28 and 1.30),
not explained.

**I4 — Window dependence enters only through native Zoom to Fit** (O3, O5).
Zoom to Fit is therefore usable at most as a starting point for a framing;
recording and reapplying the camera removes the window from the result.

## Hypotheses still open

**H1 — The FeatureManager panel shifts native Zoom to Fit's centre.** Carried
over from capture-01 H2. O5 is consistent with it (the offset grows as the
window narrows), but the one direct test was inconclusive (O9). **Low
priority:** it can only affect native Zoom to Fit, which ViewJig will use at
most as a bootstrap. Not worth a further probe unless Zoom to Fit becomes part
of a final framing path.

**H2 — Assigning `Scale2` multiplies `Translation3` by the new value.** Still
observed only from a starting `Scale2` of 0.4 (capture-01 H3).

**H3 — How `Scale2` and `Translation3` map to raster scale and position at a
fixed output size.** Not measured: every capture here used one camera. This is
capture-03's first question.

## Consequences for ViewJig

- A capture's framing can be specified as (camera, output size) and
  reproduced exactly, independent of the SOLIDWORKS window.
- Native Zoom to Fit is window-dependent and should only seed a framing,
  never define it.
- Matched capture across documents is plausible as "apply one camera, capture
  each document at one size", pending capture-03's two-document check.
