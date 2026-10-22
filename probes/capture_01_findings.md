# VJ-CAPTURE-01 findings

Characterization of native SOLIDWORKS view setup and fixed-size raster capture
(`IModelDoc2.SaveBMP`) using `probes/capture_01.py` and fixture VJ-F01.

Status: **closed**. Follow-up questions move to VJ-CAPTURE-02.

## Setup

| Item | Value |
|------|-------|
| SOLIDWORKS | Design Premium 2026 SP4.0 (`RevisionNumber` 34.4.0) |
| OS / display | Windows 11, primary screen 2560×1600 physical px, 144 DPI (150 %) |
| Display state | Shaded with edges (`DisplayMode` 5), no perspective, plain white background |
| Fixture | VJ-F01 (`fixtures/vj-f01.SLDPRT`), opened read-only by the probe |
| View sequence | `ShowNamedView2("", isometric)` → `ViewZoomtofit2` → `GraphicsRedraw2` |
| Capture | `SaveBMP(path, w, h)`; foreground = pixels whose max channel differs from the border colour by more than 8 |

Runs (all on 2026-10-22; reports under `captures/capture-01/<tag>/`, not committed):

| Tag | SOLIDWORKS window | Graphics area | Probe revision |
|-----|-------------------|---------------|----------------|
| `max-run1` | maximized | 2560×1297 | single restore order |
| `max` | maximized size; very likely minimized during the run (see O13) | 2560×1297 | three restore orders |
| `half` | snapped to left half, not maximized | 1276×1284 | + Win32 window state |
| `max-repeat` | maximized, visible (Win32-confirmed) | 2560×1297 | + Win32 window state |

Sizes requested in every run: 0×0, 800×600, 1600×1200, 1000×1000, 1600×900,
600×1000, 1001×751, 3840×2160, each captured twice back to back.

## Directly observed

**O1 — Output dimensions are exact.** All 102 captures across the four runs
returned `True` and had exactly the requested dimensions, including the odd
1001×751 and the 3840×2160 request, which is larger than the screen. Files are
24-bit, bottom-up BMPs with a 40-byte header.

**O2 — `SaveBMP(0, 0)` returns the graphics-area client size.** 2560×1297 when
maximized and 1276×1284 when half-width. In `half` and `max-repeat` this equals
the client size of the view window as reported by Win32.

**O3 — Capture is repeatable and does not move the camera.** Every back-to-back
pair was pixel-identical, and `Scale2`, `Translation3`, and `Orientation3` were
unchanged by every capture.

**O4 — Results are repeatable across runs.** `max-run1`, `max`, and `max-repeat`
produced pixel-identical images for every Phase A size, every Phase B start
view, and the Phase C reference, each run opening the document fresh. The
camera after Zoom to Fit was bit-identical.

**O5 — Standard view and Zoom to Fit.** `ShowNamedView2(isometric)` sets
`Scale2` = 1.0 and `Translation3` = (0, 0, 0). `ViewZoomtofit2` then sets
`Scale2` = 1.5554151637250084 (maximized) or 1.4689632363906868 (half). A second
`ViewZoomtofit2` changes nothing.

**O6 — Starting view does not matter.** Six starting states (unchanged, front,
top zoomed ×3, trimetric zoomed ×0.4, isometric panned, isometric rotated)
followed by isometric + Zoom to Fit gave the identical camera and identical
pixels within each run.

**O7 — Zoom to Fit framing depends on the window.** At the same requested size
the images differ between maximized and half-width:

| Requested | Maximized: model px, fill (w, h), centre offset x | Half-width: model px, fill (w, h), centre offset x |
|-----------|-----------------------------------------------|---------------------------------------------|
| 1600×1200 | 1041×1024, (0.651, 0.853), +62.5 | 983×968, (0.614, 0.807), +105.5 |
| 1000×1000 | 671×660, (0.671, 0.660), +40.5 | 634×624, (0.634, 0.624), +68.0 |
| 1600×900 | 781×769, (0.488, 0.854), +47.5 | 738×726, (0.461, 0.807), +79.0 |
| 600×1000 | 403×396, (0.672, 0.396), +24.5 | 381×374, (0.635, 0.374), +40.5 |

Vertical centre offset was 0 to ±0.5 px in every capture. Horizontal offset
was always positive (model right of centre).

**O8 — Each output shows an undistorted, centred region.** Converting each
capture back to graphics-area pixels (using the 0×0 capture's model size), the
region an output covers has the output's own aspect ratio to within 0.3 %.
Which dimension stays fixed switches between output aspect 1.0 and 1.333:

| Graphics area | Outputs with aspect ≥ 1.333 span | Outputs with aspect ≤ 1.0 span |
|---------------|----------------------------------|--------------------------------|
| 2560×1297 | height ≈ 1297 px (the full height) | width ≈ 1675 px |
| 1276×1284 | height ≈ 987 px | width ≈ 1274 px (the full width) |

**O9 — Camera restore by direct assignment.** After saving the camera,
disturbing the view, and assigning the saved values back:

- `Orientation3` → `Scale2` → `Translation3` and `Scale2` → `Orientation3` →
  `Translation3` reproduced the reference image pixel-identically (maximized
  and half-width).
- `Orientation3` → `Translation3` → `Scale2` did not; the result was itself
  deterministic across runs.
- Assigning `Translation3` reads back exactly the assigned value.
- Assigning `Orientation3` changes neither `Scale2` nor `Translation3`.
- Assigning `Scale2` multiplied the current `Translation3` by the newly
  assigned value (×1.5554 maximized, ×1.4690 half), not by the new/old ratio.
  Both observations started from `Scale2` = 0.4.
- Saved `MathTransform` / `MathVector` objects did not change when the view
  changed (they are snapshots), and `DISPATCH_PROPERTYPUT` was accepted for
  both object-valued properties.

**O10 — The source document was never modified.** `GetSaveFlag` was 0 in all
363 state snapshots, and the read-only document was closed at the end of every
completed run.

**O11 — Foreground detection is insensitive to threshold 0–32.** With a plain
white background, the border is a single colour in every capture. Foreground
bounds at thresholds 0, 8, and 32 differ by at most 1 px. At 96 the shaded
faces (≈194 grey) drop out, but bounds stay within 1 px because edges are
drawn dark.

**O12 — SOLIDWORKS objects provide no type information.**
`IDispatch::GetTypeInfo` fails on the application, document, view,
`MathVector`, and `MathTransform` objects. Under pywin32 dynamic dispatch this
made `ModelDoc2.GetPathName` execute on attribute access (it returned a `str`
before it could be called).

**O13 — SOLIDWORKS `Frame*` values are unreliable.** `FrameLeft/Top/Width/Height`
stayed at 2560×1528 (and once −1958, 333) while Win32 reported a half-width
window at (−9, 0) or a maximized window at (0, 0). `FrameState` was 0 for the
snapped window and 1 when maximized. In `max` it was 2, and the view's visible
box was reported near (−31714, −31829).

## Strongly supported interpretations

**I1 — With the same display settings, capture output is a deterministic
function of camera state, graphics-area size, and requested size.** Start-view
history, back-to-back repetition, and reopening the document did not affect
pixels (O3, O4, O6).

**I2 — Native Zoom to Fit is not portable across window sizes.** Both the
resulting camera (O5) and the framing at any fixed output size (O7) change
with the graphics area. "Isometric + Zoom to Fit + fixed size" alone does not
make captures registered.

**I3 — Zoom to Fit does not visually centre the model.** It centres vertically
to within half a pixel but leaves a horizontal offset of 6–11 % of the model
width that also depends on the window (O7).

**I4 — A camera can be restored exactly in the same window if `Translation3`
is assigned last** (O9). Whether the restored camera reproduces framing in a
*different* window is untested and is VJ-CAPTURE-02's primary question.

**I5 — SOLIDWORKS was minimized during the `max` run.** `FrameState` 2 next to
0 = normal and 1 = maximized, plus a visible box at Windows' parking position
for minimized windows, point that way (O13). If so, capture works while the
window is minimized (O4). This was not observed through Win32 and is retested
on purpose in VJ-CAPTURE-02.

**I6 — Use Win32 rather than SOLIDWORKS `Frame*` values for window state**
(O13).

## Hypotheses that still need a probe

**H1 — SaveBMP frames a fixed-aspect reference rectangle.** All 14 non-0×0
sizes in both window shapes fit one rule to within about 0.3 %. SaveBMP takes
the largest centred rectangle of aspect ≈ 1.2915 that fits in the graphics
area (1675×1297 maximized, 1276×988 half-width). It then scales that rectangle
to fit wholly inside the output, extending the region along the output's spare
dimension. Open points:

- Only two graphics-area shapes have been seen, and both happen to reach the
  rectangle from a different side.
- The switch is bracketed only between output aspects 1.0 and 1.333.
- The value 1.2915 is fitted, not explained. US Letter landscape (11 / 8.5 =
  1.294) is close but sits at the edge of the measurement precision; treat any
  explanation as unestablished.

Test: more graphics-area shapes (especially portrait) and output aspects just
either side of 1.29.

**H2 — The FeatureManager panel shifts Zoom to Fit's centre.** The view's
visible box starts at x ≈ 287 because of the panel, and SaveBMP renders the
area behind the panel too. If Zoom to Fit centres the model in the visible
region only, the image centre shifts right by about 143 px in graphics-area
pixels. Separately, VJ-F01's chamfer trims the right-most corner of the
isometric outline, which would pull the visible model left by a fixed fraction
of its width.

Fitting `offset = c + k · model width` to the two 0×0 captures gives
c ≈ 136 px and k ≈ −0.060. The panel predicts c ≈ 143–144 px; the 8 px gap is
unexplained, and two points cannot confirm a two-parameter model. Test: repeat
with the panel hidden.

**H3 — Assigning `Scale2` multiplies `Translation3` by the new value.**
Observed only from a starting `Scale2` of 0.4 (O9). The practical rule (assign
`Translation3` last) holds either way.

**H4 — Capture does not need a visible window.** Rests on I5. Test: minimize
on purpose.

## Consequences for ViewJig

- Output sizes can be requested directly; no resampling is needed to get exact
  dimensions.
- Native Zoom to Fit framing changes with window size, so registration needs
  either a portable camera (if VJ-CAPTURE-02 shows one exists) or
  measurement-and-correction against the actual raster, or both.
- Pixel-hash equality is a usable acceptance test in experiments, because
  identical inputs gave bit-identical outputs.
- Threshold 8 against a plain background is adequate for VJ-F01. Other
  backgrounds, display modes, and fixtures are untested.
- Any camera-restore code must assign `Translation3` after `Scale2`.
