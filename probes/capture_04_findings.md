# VJ-CAPTURE-04 findings

A predictive test of the camera-to-raster model from
[capture-03](capture_03_findings.md) (I1). From measured rasters, calculate
one shared camera that frames a two-document set to a raster-space target,
apply it once, and measure how far the result lands from the prediction. The
probe has no correction loop. Uses `probes/capture_04.py` and fixtures
VJ-F01 (A) and VJ-F01B (B).

Status: **closed**. This completes the capture-01 to capture-04
characterization sequence (see [README](README.md)).

## Setup

Same machine, SOLIDWORKS release (34.4.0), display state, background, and
foreground threshold (8) as capture-01 to capture-03. SOLIDWORKS was maximized
(Win32-confirmed) during every document session.

Run `predict`, 2026-10-22. The report is under `captures/capture-04/predict/`
and is not committed. 32 captures, with documents opened one at a time,
read-only, in the order B (fit), A, B, A.

Per case (view ∈ {isometric, Front} × output ∈ {1600×1200, 1000×1000}):

1. **Bootstrap.** Standard view + Zoom to Fit on each document. Take the fit
   with the smaller `Scale2`, then multiply `Scale2` and `Translation3` X/Y by
   0.7. Under the model, that shrinks the image about the image centre.
2. **Measure.** Capture A and B; take the combined foreground edges (continuous
   edges, bounds ± 0.5 px).
3. **Calibrate.** On A, shift `Translation3` X and Y by ±0.008 m:
   k = Δcentre ÷ 0.016 m.
4. **Calculate one correction:**
   - s = min((W − 2m)/w₀, (H − 2m)/h₀);
   - O₀ = C + k·T₀;
   - O₁ = C − s·(c₀ − O₀);
   - S₁ = s·S₀, T₁ = (O₁ − C)/k, T_z unchanged.

   C is the image centre and m = 60 px, which is an experiment parameter, not
   a product default.
5. **Apply once.** Capture A and B, then measure margins, centre, clipping, and
   the capture-03 edge-overlap registration test.

## Directly observed

**O1 — Every case ran.** No case was skipped, no bootstrap or calibration
capture clipped, and calibration off-axis movement was exactly 0 px/m in all
four cases.

**O2 — Bootstrap.** Zoom to Fit `Scale2` was 1.555415 (A) and 1.366862 (B)
in isometric, so the bootstrap used B's fit. In Front it was 1.985814 for
both, so A's fit was used (a tie). Neither document clipped under either
bootstrap.

**O3 — Calibrated k (px/m), quantized in steps of 31.25 px/m.** A 0.5 px centre
quantum over the 0.016 m span gives that step size.

| Case | k_x | k_y | Resolution | 1 % k error moves the result (x, y) |
|------|-----|-----|------------|--------------------------------------|
| isometric 1600×1200 | 5562.5 | −5531.2 | 0.56 % | 0.74, 0.00 px |
| isometric 1000×1000 | 3593.8 | −3562.5 | 0.87 % | 0.60, 0.00 px |
| Front 1600×1200 | 5562.5 | −5500.0 | 0.56 % | 1.38, 1.23 px |
| Front 1000×1000 | 3562.5 | −3562.5 | 0.88 % | 0.82, 0.74 px |

**O4 — One predicted correction, predicted vs measured.**

| Case | Limiting axis | Scale ratio | Predicted margins L, T, R, B | Measured margins | Residual L, T, R, B | Centre residual |
|------|---------------|-------------|------------------------------|------------------|---------------------|-----------------|
| isometric 1600×1200 | height | 1.5042 | 317.9, 60.0, 317.9, 60.0 | 318, 61, 319, 61 | +0.1, +1.0, +1.1, +1.0 | −0.5, 0.0 |
| isometric 1000×1000 | height | 1.9048 | 106.7, 60.0, 106.7, 60.0 | 108, 60, 107, 60 | +1.3, 0.0, +0.3, 0.0 | +0.5, 0.0 |
| Front 1600×1200 | width | 1.5931 | 60.0, 167.5, 60.0, 167.5 | 61, 170, 60, 167 | +1.0, +2.5, 0.0, −0.5 | +0.5, +1.5 |
| Front 1000×1000 | width | 1.4716 | 60.0, 242.5, 60.0, 242.5 | 59, 244, 60, 242 | −1.0, +1.5, 0.0, −0.5 | −0.5, +1.0 |

The largest residual on any side was 2.5 px. The largest centre residual was
1.5 px.

**O5 — Nothing clipped after correction, and the camera read back exactly** for
both documents in all four cases.

**O6 — Registration held.** After correction, the edge-overlap peak was at
(0, 0) for every case at every luminance limit (12 of 12). The
peak-to-next-best ratio was close to the same pair's bootstrap value:

- isometric: 2.27–2.32 after vs 2.33 at bootstrap (luminance ≤ 64);
- Front: 1.43–1.44 after vs 1.45–1.47 at bootstrap.

Front ratios are lower than isometric ones both before and after correction.

**O7 — Safety.** `GetSaveFlag` was 0 in all 76 snapshots. All four document
sessions were read-only and closed. All 32 captures had the requested size,
and none moved the camera.

## Strongly supported interpretations

**I1 — The capture-03 camera model predicts framing in one step.** The
correction changed scale by 1.47–1.90× and moved the set's edges by up to about
360 px (Front 1600×1200 left margin 422 → 61 px). It still landed within 2.5 px of the predicted margins on every side,
and within 1.5 px of centre, in both views and both output regimes. There is
no sign of a systematic model failure.

**I2 — Centre residuals are explained by the precision of k.** If capture-03's
better-determined k is substituted (5575 px/m at 1600×1200; × 0.6452 at
1000×1000, per the camera rectangle), the k error predicts the measured centre
residuals to within 0.81 px in every case:

| Case | Centre residual explained by k | Observed | Unexplained |
|------|--------------------------------|----------|-------------|
| isometric 1600×1200 | −0.17, 0.00 | −0.50, 0.00 | −0.33, 0.00 |
| isometric 1000×1000 | −0.06, 0.00 | +0.50, 0.00 | +0.56, 0.00 |
| Front 1600×1200 | −0.31, +1.68 | +0.50, +1.50 | +0.81, −0.18 |
| Front 1000×1000 | −0.80, +0.72 | −0.50, +1.00 | +0.30, +0.28 |

What remains is within the quantization of the measured edges: ±0.5 px in the
result, plus ±0.5 px × s carried from the bootstrap measurement. The largest
single residual, Front 1600×1200 y (+1.5 px), comes from a k_y estimate
(−5500) two quantization steps below the reference.

**I3 — The model holds for the standard Front view as well as isometric.**
That covers both the width-limited and height-limited raster regimes and both
limiting axes of the target.

**I4 — Applying the correction keeps registration intact.** One shared camera
still registers the two documents after the correction (O6).

**I5 — A single ±δ calibration limits accuracy, not the model.** A longer
calibration span or more steps would shrink the k quantization, which was
31.25 px/m here (about 0.56–0.88 %).

## Open questions

**Q1 — Is there a small size bias?** Of the 8 case axes, the result was
1–2 px smaller than predicted in 6, equal in 1 (isometric 1000×1000 height),
and 1 px larger in 1 (Front 1000×1000 width). Constant-width edge lines
being scaled by s in the prediction would produce this (capture-03 Q5). It is
consistent, but not tested.

**Q2 — Would a second correction converge further?** A second step starts from
a much smaller correction, so its error lever (and k sensitivity) shrinks. This
was not measured, by design: the probe applies one correction only.

**Q3 — How general are these results?** They cover two orthographic standard
views, one fixture family, and one display state. Perspective, other standard
views, shaded-only or other display modes, and non-white backgrounds are
untested.

Carried forward unchanged: capture-03 Q1 (curved-edge differences across
documents) and capture-02 H1 (FeatureManager; low priority).

## Consequences for ViewJig

- `shot` and `pair` can calculate framing directly: bootstrap, measure,
  calibrate k, then one predicted correction, followed by raster verification.
  No search is needed.
- With a single ±0.008 m calibration, the expected landing accuracy is about
  ±2.5 px per side. Production can tighten it by improving k (a longer or
  multi-step calibration) or by allowing one bounded verification-driven
  second correction. Which of the two to use is a product tolerance decision,
  not a model question.
- Verification against the target must tolerate pixel-level residuals. Across
  documents, registration checks use the edge-overlap test, not hash equality
  (capture-03 I3).
