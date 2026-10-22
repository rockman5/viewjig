# VJ-CAPTURE-03 findings

Characterization of (1) how `Scale2` and `Translation3` map to raster scale and
position at a fixed output size, and (2) applying one camera across two
separate documents with deliberately different geometry. Uses
`probes/capture_03.py` and fixtures VJ-F01 and VJ-F01B. Builds on
[capture-01](capture_01_findings.md) and [capture-02](capture_02_findings.md).

Status: **closed**. Open questions are listed at the end.

## Setup

Same machine, SOLIDWORKS release (34.4.0), display state, background, and
foreground threshold (8) as capture-01/02. Cameras are assigned from plain
numbers, `Translation3` last (capture-01 O9). Both runs took place on
2026-10-22 with a 2560×1297 graphics area. Reports are under
`captures/capture-03/<tag>/` and are not committed.

| Tag | Mode | Documents | Captures |
|-----|------|-----------|----------|
| `mapping` | mapping | VJ-F01 | 40 |
| `documents` | documents | VJ-F01, then VJ-F01B, then VJ-F01 again, one at a time | 10 |

Bounding boxes reported by `GetPartBox` (m), matching
[fixtures/README.md](../fixtures/README.md) to floating-point precision:

| Fixture | X | Y | Z |
|---------|---|---|---|
| VJ-F01 | 0 → 0.12 | 0 → 0.05 | −0.07 → 0 |
| VJ-F01B | 0 → 0.12 | 0 → 0.07 | −0.07 → 0 |

## Mapping: directly observed

Starting camera: `captures/capture-02/portable/camera.json` (isometric, `Scale2`
1.5554151637250084).

**M-O1 — Foreground size is proportional to `Scale2`.** With orientation and
`Translation3` held, `Scale2` × {0.5, 0.7, 0.85, 1.0, 1.1} gave
(foreground size ÷ size at ×1.0) ÷ factor within 0.3 % of 1.0, in width and
height. This held at 1600×1200 (height-limited) and 1000×1000
(width-limited). The deviation is largest at the smallest factor
(1.0029 at ×0.5).

**M-O2 — Changing `Scale2` moves the foreground along a line, not about the
image centre.** The foreground centre moved linearly with the factor:
(+519.0, −88.4) px per unit factor at 1600×1200 and (+335.0, −57.3) at
1000×1000. Extrapolated, the fixed point is (343.0, 687.9) at 1600×1200 and
(205.0, 556.8) at 1000×1000.

**M-O3 — `Translation3` X and Y move the raster linearly, each along one
image axis.** At 1600×1200, adding ±0.004 and ±0.008 to one component gave
these fitted rates, with at most 0.4 px residual and exactly 0 px movement
along the other image axis:

| `Scale2` factor | T_x → image x | T_y → image y |
|-----------------|---------------|---------------|
| ×1.0 | +5575.0 px/m | −5562.5 px/m |
| ×0.7 | +5575.0 px/m | −5575.0 px/m |

Positive T_x moves the model right; positive T_y moves it up.

**M-O4 — The translation rate does not change with `Scale2`.** Per-step shifts
were 22–23 px (0.004) and 44–45 px (0.008) at both factors, a difference below
one pixel of quantization.

**M-O5 — `Translation3` Z has no raster effect.** All eight Z steps were
pixel-identical to the zero step at both `Scale2` factors.

**M-O6 — Controls held.** The base camera's captures at the start and end were
pixel-identical, the camera read back exactly after all 33 assignments, no
capture clipped, no capture moved the camera, `GetSaveFlag` was 0 in all 80
snapshots, and the document closed.

## Documents: directly observed

C_A and C_B are isometric + Zoom to Fit on VJ-F01 and VJ-F01B, with
SOLIDWORKS maximized (Win32-confirmed) for every document.

**D-O1 — Native Zoom to Fit picks different cameras for the two parts.**
C_A: `Scale2` 1.5554151637250084, T (−0.0822, −0.0159, −0.0449).
C_B: `Scale2` 1.366862349629338, T (−0.0695, −0.0251, −0.0473). Orientation
is identical. C_A is bit-identical to the capture-02 `portable` camera, and
VJ-F01 under C_A reproduced the capture-02 reference images exactly.

**D-O2 — Control.** VJ-F01 under C_A, captured again after VJ-F01B had been
opened and closed and VJ-F01 reopened, was pixel-identical at both sizes.

**D-O3 — Edge-overlap shift test.** For each pair, the test counts dark
edge-line pixels that coincide when one image is offset by each integer shift
within ±3 px. The results, across both sizes and all three luminance limits
(≤ 32, 64, 96):

| Pair | Peak shift | Peak ÷ next best | Zero-shift share of each image's edge pixels |
|------|------------|------------------|----------------------------------------------|
| Control (VJ-F01 vs itself after reopening) | (0, 0) in all 6 | 1.78–2.84 | 1.0 |
| Shared camera C_A (VJ-F01 vs VJ-F01B) | (0, 0) in all 6 | 1.80–2.65 | 0.70–0.79 |
| Shared camera C_B (VJ-F01 vs VJ-F01B) | (0, 0) in all 6 | 1.87–2.74 | 0.73–0.79 |
| Native: VJ-F01 under C_A vs VJ-F01B under C_B | (2, 1), (0, 2), (0, 1) depending on limit and size | 1.11–1.42 | 0.003–0.056 |

In the shared-camera pairs, overlap one pixel either side of (0, 0) was
symmetric to within a few pixels (for example 1459 vs 1456 for ±1 in x). The
offline stand-in had predicted that the native pair's best shift would sit on
the edge of the ±3 px search range. On real images it instead produced weak
maxima inside the range that move with the limit, so "peak inside range"
alone did not separate the cases.

**D-O4 — Whole-model bounds agreeing is not registration.** In the native pair
at 1600×1200 the two images had identical top and bottom foreground bounds
(rows 88 and 1111) while their edges barely overlapped (D-O3).

**D-O5 — Where the shared-camera images differ.** Under C_A at 1600×1200,
147,073 pixels differ. Below row 700, only 730 differ by more than 8: 716 lie
on the hole's circular edges, 12 on the upright's own edge, and 2 elsewhere.
Straight edges common to both parts (base outline, chamfer) are
pixel-identical. Under C_B at 1600×1200, 607 pixels differ by more than 8
below row 700.

**D-O6 — Clipping.** VJ-F01B under C_A clips at 1600×1200 (foreground reaches
row 0) but not at 1000×1000 (top row 79). C_B fits both parts at both sizes.

**D-O7 — Safety.** Each document opened read-only, stayed at `GetSaveFlag` 0
in all 26 snapshots, and closed. All 10 captures had the requested size and
none moved the camera.

## Strongly supported interpretations

**I1 — A simple camera-to-raster model fits the mapping data.** For a fixed
orientation and output size:

> pixel = image centre + k(output) · (Scale2 · projected model coordinates + (T_x, −T_y))

In this model, `Scale2` scales the projected model about the projection of the
model origin. The origin lands at image centre + k · (T_x, −T_y), and T_z has
no effect.

Supporting evidence:
- M-O2's fixed point (343.0, 687.9) is where VJ-F01's origin corner (front-left
  bottom) appears in the isometric image.
- Image centre + 5575 px/m × the starting `Translation3` gives (341.3, 687.8),
  within about 2 px of that fixed point.
- k does not depend on `Scale2` (M-O4).

**I2 — One camera registers separate documents.** With the same camera and
output size, model-space geometry present in both VJ-F01 and VJ-F01B projects
to the same pixels, at both sizes and with both cameras. The evidence:
- the edge overlap peaks at (0, 0) with a sharpness comparable to the
  same-document control;
- the overlap falls off symmetrically on either side (D-O3);
- straight shared edges are pixel-identical (D-O5).

The shift test resolves whole pixels only.

**I3 — Byte-identical shared geometry is not guaranteed across documents.**
Curved shared edges (the hole) differ by about one pixel along the curve while
the straight edges match exactly (D-O5). Exact hash equality is therefore a
valid acceptance test within one document (capture-01/02) but not across
documents. Cross-document registration checks need to tolerate this kind of
edge-level difference.

**I4 — A matched set needs a camera that fits every member.** A camera
fitted to one member can clip another (C_A clips VJ-F01B at 1600×1200, D-O6).
This is a framing requirement, not a registration failure: the images that
fit are still registered. For this pair, the camera fitted to the taller
part happened to fit both. That is not evidence that "fit the largest
member" works in general, because members can extend in different directions.

**I5 — Judge the edge-overlap test by zero-shift share and peak sharpness
against the control, not by where the best shift lies.** Shared-camera pairs
kept 70–79 % of edge pixels at zero shift with control-like sharpness. The
native pair kept 0.3–5.6 % with weak, inconsistent maxima (D-O3).

## Open questions

**Q1 — What causes the curved-edge differences?** One candidate is
tessellation of curved geometry depending on the document, such as on its
size, so a larger part gets a coarser hole outline. Not tested.

**Q2 — How general is the I1 model?** Only one orientation (isometric), one
fixture family, orthographic projection, and a model origin inside the frame
have been tested. Other standard views, perspective, and origins far outside
the frame are untested.

**Q3 — How does k depend on output size?** The translation rate was fitted
only at 1600×1200. The 1000×1000 rate (≈ 3584–3609 px/m) comes from
extrapolating the fixed point. Its ratio to 1600×1200 (0.643–0.648) matches
the camera-defined ≈ 1.29 rectangle (capture-02 I3; predicted 0.645), but
only two sizes have been measured.

**Q4 — Is sub-pixel registration exact?** The shift test uses integer shifts.
Symmetric ±1 px overlap suggests no sub-pixel bias, but it was not measured.

**Q5 — Why is size ÷ factor slightly above 1 at small factors (M-O1)?** One
candidate is that edge lines keep a constant pixel width while the model
shrinks. Not tested.

Carried forward unchanged: capture-01 H3 / capture-02 H2 (assigning `Scale2`
rescales `Translation3`) and capture-02 H1 (the FeatureManager panel shifts
native Zoom to Fit; low priority).

## Consequences for ViewJig

- `pair` can rest on one shared camera plus one output size. Registration
  across separate documents is supported for geometry common to both.
- That camera has to be chosen for the whole set. Fitting one member can clip
  another.
- If I1 holds, a framing target (such as margins and centring) can be reached
  by calculating `Scale2` and `Translation3` from one measured capture, rather
  than by searching for them.
- Cross-document verification should use a tolerant raster comparison, such as
  the edge-overlap test, not hash equality.
