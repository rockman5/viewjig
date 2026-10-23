"""Fixed-size raster capture through ``IModelDoc2.SaveBMP``.

For a given camera and requested size, SaveBMP output is deterministic and is
expected to have exactly the requested dimensions; this module checks that. It
only captures: it writes a temporary BMP, loads it into memory as an RGB image,
checks its size, and deletes the file. Framing and measurement live elsewhere.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from PIL import Image

from viewjig.sw import Document


class RasterCaptureError(Exception):
    pass


def capture(
    document: Document, width: int, height: int, *, workdir: Path | str | None = None
) -> Image.Image:
    """Capture the document's current view at exactly ``width`` x ``height`` pixels.

    Both dimensions must be positive integers: SaveBMP's (0, 0) means "current window
    size", which would make the output depend on the window. Returns a fully loaded RGB
    image; no file is left behind.
    """
    for name, value in (("width", width), ("height", height)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer, got {value!r}")

    fd, name = tempfile.mkstemp(prefix="viewjig-", suffix=".bmp", dir=workdir)
    os.close(fd)
    path = Path(name)
    try:
        path.unlink()  # so a file existing afterwards means SaveBMP wrote it
        ok = document.model.call("SaveBMP", str(path), width, height)
        if not ok or not path.is_file():
            raise RasterCaptureError(f"SaveBMP did not write {width}x{height} (returned {ok!r})")
        try:
            with Image.open(path) as bmp:
                image = bmp.convert("RGB")
                image.load()
        except OSError as exc:  # includes PIL.UnidentifiedImageError
            raise RasterCaptureError(f"SaveBMP wrote an unreadable file: {exc}") from exc
        if image.size != (width, height):
            raise RasterCaptureError(
                f"SaveBMP returned {image.size[0]}x{image.size[1]}, requested {width}x{height}"
            )
        return image
    finally:
        path.unlink(missing_ok=True)
