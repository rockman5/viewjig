# ViewJig

**ViewJig is a repeatable camera jig for CAD.**

Capture SOLIDWORKS models with consistent position, scale, framing, and output dimensions—so before/after images stay perfectly registered.

## Why ViewJig?

Engineers routinely capture SOLIDWORKS models for design reviews, presentations, documentation, and before/after comparisons. Standard screenshots and zoom-to-fit workflows can leave otherwise comparable images at different positions, scales, or framing.

ViewJig is being built to make those captures reproducible.

## V1 scope

ViewJig will focus on:

- presentation-ready SOLIDWORKS image capture
- standard and named views
- fixed output dimensions
- repeatable position, scale, and framing
- matched before/after captures across model states or files
- raster-based framing measurement and correction
- PNG export

The initial command-line workflow is intended to stay small: capture a single model (`shot`) or a registered set (`pair`).

## Non-goals

ViewJig does **not** interpret engineering changes. It does not determine what geometry changed, why it changed, whether two changes are compatible, or how changes should be merged.

V1 intentionally excludes:

- feature-tree or face correspondence
- semantic or geometric diffing
- added/removed geometry classification
- change attribution
- compatibility reasoning
- reconstruction or merge behavior

ViewJig makes CAD models visually comparable. It does not decide what their differences mean.

## Status

Early development. The first engineering milestone is independent characterization of SOLIDWORKS view and raster-capture behavior using neutral public fixtures.

## Platform

The initial implementation targets SOLIDWORKS on Windows using Python and the SOLIDWORKS COM API.
