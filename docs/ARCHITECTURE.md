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

## Planned modules

```text
src/viewjig/
  session.py      SOLIDWORKS connection and COM helpers
  documents.py    document lifecycle and source-safety guards
  view.py         orientation and portable camera state
  raster.py       fixed-size raster capture
  measure.py      pixel-only foreground/framing measurement
  framing.py      camera correction policy
  matched.py      matched capture across two or more documents
  export.py       output images and metadata
  cli.py          `viewjig shot` and `viewjig pair`
```

The framing policy should be testable without SOLIDWORKS by depending on small camera/renderer interfaces. Synthetic renderers and generated images can exercise the control policy offline.

## V1 commands

```text
viewjig shot model.SLDPRT --view iso --size 1600x1200
viewjig pair before.SLDPRT after.SLDPRT --view iso --size 1600x1200
```

The exact CLI is provisional until the underlying capture path is characterized.

## Deliberate exclusions

ViewJig does not inspect the feature tree or assign engineering meaning to geometry. It does not implement CAD diffing, feature/face correspondence, geometry attribution, compatibility analysis, reconstruction, or merge behavior.

The public capture layer should remain usable by callers that have their own higher-level CAD semantics without depending on those semantics itself.
