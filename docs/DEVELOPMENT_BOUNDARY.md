# Development boundary

ViewJig is an independently implemented open-source CAD capture utility.

## Scope rule

> ViewJig makes CAD models visually comparable. It does not decide what their differences mean.

Implementation in this repository should be derived from ViewJig's public requirements, public SOLIDWORKS API behavior, and experiments performed with neutral ViewJig fixtures.

## Allowed territory

- document/session lifecycle needed for capture
- standard and named view selection
- camera orientation, scale, and translation
- fixed-size raster capture
- pixel-space foreground detection
- framing and margin measurement
- bounded camera correction
- reusable camera/capture recipes
- matched capture across separate documents
- image and metadata export

## Out of scope

Do not add the following without an explicit scope review:

- feature-tree inspection for comparison purposes
- feature or face correspondence
- Boolean/geometric diffing
- added/removed geometry classification
- volume/body-count comparison used to infer model differences
- engineering-change attribution
- compatibility or dependency reasoning
- reconstruction, composition, or merge behavior
- color conventions that imply engineering meaning such as added/removed geometry

## Clean implementation rules

1. Do not copy source files, fixtures, images, constants, test cases, commit history, or implementation-specific comments from private adjacent projects.
2. Reproduce relevant SOLIDWORKS behavior with neutral fixtures and probes created in this repository.
3. Derive framing thresholds and calibration parameters from ViewJig experiments and keep them configurable where practical.
4. Use generic capture vocabulary: document, model, image, view, camera, frame, member, set, before, after, A, and B.
5. Tests and examples must use newly authored neutral geometry.
6. The library must have zero dependencies on private projects.
7. Higher-level applications may depend on ViewJig; ViewJig must not depend on their semantic models.

## Architecture seam

The reusable boundary is camera/capture infrastructure. A caller may prepare a document or visual scene before asking ViewJig to capture it, but ViewJig must not infer the engineering meaning of that scene.

If a proposed feature begins answering **what changed**, **why it changed**, **whether changes are compatible**, or **how to combine them**, it belongs outside ViewJig and requires a new boundary review before implementation.
