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

| # | Feature | Definition |
|---|---------|------------|
| 1 | Base | Box X 0→120, Z 0→70, Y 0→10 (sketch on Top Plane, one corner at the origin, extrude up 10). |
| 2 | Upright | Box X 0→25, Z 0→70, Y 10→50 (sketch on the base top face, extrude up 40). |
| 3 | Hole | Ø20 through all, vertical (along Y), centre at X 85, Z 25. |
| 4 | Chamfer | 20 × 20 mm on the vertical base edge at X 120, Z 70 (front-right corner in plan). |

Expected bounding box: X [0, 120], Y [0, 50], Z [0, 70] mm. The capture probe
records `GetPartBox`, which should report this in metres.

Authoring rules: single solid body, default configuration only, default
appearance (no colours, textures, decals, or scenes), no external references,
no equations or custom properties. Record the SOLIDWORKS version it was saved in
here, since part files do not open in older releases.

Saved with: _(fill in, e.g. SOLIDWORKS 2026 SP4)_
