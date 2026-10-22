# Fixtures

Neutral CAD parts authored specifically for ViewJig experiments. They carry no
product meaning; they exist to make position, scale, orientation, and framing
easy to see and measure.

## VJ-F01 — stepped bracket (`vj-f01.SLDPRT`)

An L-shaped block with one off-centre hole and one chamfered corner. Every
overall dimension is different (120 × 50 × 70 mm), so aspect-ratio effects are
visible, and no standard view is mirror-symmetric, so orientation is obvious.
The origin sits at a corner, not the centre, so "centred on the origin" and
"centred on the model" give visibly different results.

Units: MMGS. Coordinates are model coordinates (+Y up, Front view looks from +Z).
The part lies behind the Front Plane (Z ≤ 0), with the origin at its
front-left-bottom corner. This is the geometry of the committed part file.

| # | Feature | Definition |
|---|---------|------------|
| 1 | Base | Box X 0→120, Z −70→0, Y 0→10 (sketch on Top Plane, one corner at the origin, extrude up 10). |
| 2 | Upright | Box X 0→25, Z −70→0, Y 10→50 (sketch on the base top face, extrude up 40). |
| 3 | Hole | Ø20 through all, vertical (along Y), centre at X 85, Z −25. |
| 4 | Chamfer | 20 × 20 mm on the vertical base edge at X 120, Z −70 (back-right corner in plan). |

Bounding box: X [0, 120], Y [0, 50], Z [−70, 0] mm. The capture probe records
`GetPartBox` in metres; the first live run reported exactly this.

Authoring rules: single solid body, default configuration only, default
appearance (no colours, textures, decals, or scenes), no external references,
no equations or custom properties. Record the SOLIDWORKS version it was saved in
here, since part files do not open in older releases.

Saved with: SOLIDWORKS Design Premium 2026 SP4.0

## VJ-F01B — stepped bracket, tall upright (`vj-f01b.SLDPRT`)

VJ-F01 with exactly one deliberate difference: the upright is 60 mm tall
instead of 40 mm. Everything else — base, hole, chamfer, origin, orientation,
units, and authoring rules — is identical, so the two parts occupy the same
model coordinates except for the extra upright height.

It exists to test matched capture across two separate documents: one camera
applied to both should put the geometry they share on the same pixels, while
native Zoom to Fit, seeing a taller part, picks a different camera.

| # | Feature | Definition |
|---|---------|------------|
| 1 | Base | As VJ-F01: box X 0→120, Z −70→0, Y 0→10. |
| 2 | Upright | Box X 0→25, Z −70→0, **Y 10→70** (extrude up **60**). |
| 3 | Hole | As VJ-F01: Ø20 through all, vertical, centre at X 85, Z −25. |
| 4 | Chamfer | As VJ-F01: 20 × 20 mm on the vertical base edge at X 120, Z −70. |

Bounding box: X [0, 120], Y [0, **70**], Z [−70, 0] mm.

Authoring: open `vj-f01.SLDPRT`, immediately use **File → Save As** to
`vj-f01b.SLDPRT` (so VJ-F01 is never saved), change only the upright
extrusion depth from 40 to 60 mm, rebuild, and save. VJ-F01 must remain
byte-identical in Git afterwards.

Saved with: _(fill in)_