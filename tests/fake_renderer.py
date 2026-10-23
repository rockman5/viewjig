"""Deterministic image-producing stand-ins for a SOLIDWORKS document renderer."""

from __future__ import annotations

from PIL import Image, ImageDraw

from viewjig.camera import Camera, CameraReadbackMismatch
from viewjig.sw import StandardView

WHITE, GREY, DARK = (255, 255, 255), (194, 194, 194), (20, 20, 20)
ORIENT = {
    StandardView.ISOMETRIC: tuple(float(i) for i in range(16)),
    StandardView.FRONT: tuple(float(i % 4 == i // 4) for i in range(16)),
}


class FakeRenderer:
    """Renders a rectangular model under the framing model as real images.

    raster = C + (kx * (S**gamma * q_x + Tx), ky * (S**gamma * q_y + Ty))
    gamma != 1 makes size respond non-linearly to Scale2, which the framing model
    does not predict, forcing additional corrections. gamma < 1 keeps every miss
    too small (measurable); gamma > 1 overshoots.
    pan_gain scales how far Translation3 X/Y move the image.
    """

    def __init__(
        self,
        *,
        kx: float = 5575.0,
        ky: float = -5575.0,
        model=(0.0, -0.05, 0.12, 0.0),
        gamma: float = 1.0,
        fit_fill: float = 0.85,
        fit_shift_m: float = 0.004,
        background=WHITE,
        pan_gain=(1.0, 1.0),
    ) -> None:
        self.kx, self.ky, self.model, self.gamma = kx, ky, model, gamma
        self.pan_gain = pan_gain  # how strongly Translation3 X/Y move the image (1 = model)
        self.fit_fill, self.fit_shift_m = fit_fill, fit_shift_m
        self.background = background
        self.drift: tuple[int, tuple[int, int, int]] | None = None
        self.fail_apply: int | None = None
        self.camera = Camera(ORIENT[StandardView.ISOMETRIC], (0, 0, 0.01), 1.0)
        self.applied: list[Camera] = []
        self.captured = 0
        self.size = (400, 300)

    def show_standard_view(self, view: StandardView) -> None:
        self.camera = Camera(ORIENT[view], (0.0, 0.0, 0.01), 1.0)

    def zoom_to_fit(self) -> None:
        # Fit the model in a window the size of the output, slightly off-centre in x.
        w, h = self.size
        qx0, qy0, qx1, qy1 = self.model
        s = self.fit_fill * min(w / abs(self.kx * (qx1 - qx0)), h / abs(self.ky * (qy1 - qy0)))
        s = s ** (1 / self.gamma)
        sg = s**self.gamma
        tx = -sg * (qx0 + qx1) / 2 + self.fit_shift_m
        ty = -sg * (qy0 + qy1) / 2
        self.camera = Camera(self.camera.orientation, (tx, ty, 0.0123), s)

    def read_camera(self) -> Camera:
        return self.camera

    def apply_camera(self, camera: Camera) -> Camera:
        self.applied.append(camera)
        if self.fail_apply is not None and len(self.applied) == self.fail_apply:
            raise CameraReadbackMismatch(
                camera, Camera(camera.orientation, camera.translation, 9.0)
            )
        self.camera = camera
        return camera

    def capture(self, width: int, height: int) -> Image.Image:
        self.captured += 1
        bg = self.background
        if self.drift is not None and self.captured >= self.drift[0]:
            bg = self.drift[1]
        image = Image.new("RGB", (width, height), bg)
        sg = self.camera.scale**self.gamma
        tx, ty, _ = self.camera.translation
        qx0, qy0, qx1, qy1 = self.model
        px, py = self.pan_gain
        xs = sorted(round(width / 2 + self.kx * (sg * q + px * tx)) for q in (qx0, qx1))
        ys = sorted(round(height / 2 + self.ky * (sg * q + py * ty)) for q in (qy0, qy1))
        if xs[1] > xs[0] and ys[1] > ys[0]:
            ImageDraw.Draw(image).rectangle(
                (xs[0], ys[0], xs[1] - 1, ys[1] - 1), fill=GREY, outline=DARK
            )
        return image
