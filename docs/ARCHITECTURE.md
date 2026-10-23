# ViewJig architecture

ViewJig owns one problem: reproducible CAD image capture.

Its core pipeline is:

```text
SOLIDWORKS document(s)
        |
        v
      View
 orientation + camera state
        |
        v
     Raster
 fixed-size capture
        |
        v
    Measurement
 foreground bounds + framing error
        |
        v
     Framing
 bounded camera correction
        |
        v
   Verification
 framing contract satisfied?
        |
        v
      Export
 PNG + capture metadata
```

## Design principles

### Raster truth

ViewJig evaluates the image that was actually produced rather than assuming an intended CAD camera operation produced the desired framing.

### Bounded correction

Framing correction must have explicit attempt limits and return a structured failure when the requested framing cannot be achieved. Capture quality is a measurable output, not an assumption.

### Camera portability

A captured camera state should be representable independently of one document so that the same visual coordinate system can be applied across multiple model files.

### Matched sets

A matched capture is a set-level operation. All members share a framing target and output dimensions. V1's primary matched-set workflow is a pair of separate SOLIDWORKS documents.

### Source safety

ViewJig must not save or intentionally mutate source CAD files. Operations that require temporary state should be reversible, and source documents should be closed without saving.

## Modules

```text
src/viewjig/
  sw.py        SOLIDWORKS boundary: explicit COM calls, attach, read-only document lifecycle
  camera.py    plain-number camera (Orientation3, Translation3, Scale2): read, apply, JSON
  raster.py    fixed-size capture through SaveBMP
  measure.py   raster-only foreground measurement, combined extents, edge-overlap diagnostic
  framing.py   pure math: pan calibration, one predicted correction, verification
  steps.py     orchestration steps shared by shot and pair
  shot.py      one document: bootstrap, calibrate, correct, verify, export
  pair.py      two documents with one shared camera
  cli.py       `viewjig shot` and `viewjig pair`
```

Only `sw.py`, `camera.py`, and `raster.py` talk to SOLIDWORKS. `measure.py` and
`framing.py` work on images and numbers alone. `shot.py` and `pair.py` drive
SOLIDWORKS through a small renderer interface, so the whole sequence is tested
offline with a fake renderer.

## Commands

```text
viewjig shot part.SLDPRT [--view isometric] [--width 1600] [--height 1200] [--margin PX] [--output PNG]
viewjig pair before.SLDPRT after.SLDPRT [same options] [--output BASE]
```

## Deliberate exclusions

ViewJig does not inspect the feature tree or assign engineering meaning to geometry. It does not implement CAD diffing, feature/face correspondence, geometry attribution, compatibility analysis, reconstruction, or merge behavior.

The public capture layer should remain usable by callers that have their own higher-level CAD semantics without depending on those semantics itself.
