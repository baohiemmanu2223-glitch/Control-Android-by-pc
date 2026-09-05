"""UI and image recognition adapters with geometry normalization."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .geometry import ScreenGeometry


def normalize_image(image: Any, geometry: ScreenGeometry, target_size: tuple[int, int] | None = None) -> Any:
    """Rotate and resize a PIL image or image path to a stable geometry."""
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Cần cài Pillow để chuẩn hóa ảnh") from exc
    if isinstance(image, (str, Path)):
        image = Image.open(image)
    if not isinstance(image, Image.Image):
        raise TypeError("image phải là PIL.Image hoặc đường dẫn ảnh")
    rotations = {1: 90, 2: 180, 3: 270}
    if geometry.rotation:
        image = image.rotate(rotations[geometry.rotation], expand=True)
    size = target_size or (geometry.width, geometry.height)
    return image.resize(size, Image.Resampling.LANCZOS) if image.size != size else image.copy()


@dataclass(frozen=True)
class MatchResult:
    found: bool
    score: float
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0

    @property
    def center(self) -> tuple[int, int]:
        return (self.x + self.width // 2, self.y + self.height // 2)


class TemplateMatcher:
    """OpenCV template matcher; returns coordinates in normalized screen space."""

    def __init__(self, threshold: float = 0.85) -> None:
        if not 0 <= threshold <= 1:
            raise ValueError("threshold phải trong khoảng 0..1")
        self.threshold = threshold

    def match(self, screen: bytes | str | Path, template: bytes | str | Path) -> MatchResult:
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise RuntimeError("Cần cài opencv-python và numpy để nhận diện template") from exc

        def load(value: bytes | str | Path):
            if isinstance(value, bytes):
                image = cv2.imdecode(np.frombuffer(value, dtype=np.uint8), cv2.IMREAD_COLOR)
            else:
                image = cv2.imread(str(value), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError(f"Không đọc được ảnh: {value}")
            return image

        source, pattern = load(screen), load(template)
        if source.shape[0] < pattern.shape[0] or source.shape[1] < pattern.shape[1]:
            return MatchResult(False, 0.0)
        score_map = cv2.matchTemplate(source, pattern, cv2.TM_CCOEFF_NORMED)
        _, score, _, location = cv2.minMaxLoc(score_map)
        h, w = pattern.shape[:2]
        return MatchResult(score >= self.threshold, float(score), int(location[0]), int(location[1]), w, h)


class UiAutomatorAdapter:
    """Optional semantic UI adapter; keeps selectors ahead of pixel coordinates."""

    def __init__(self, serial: str, device: Any | None = None) -> None:
        self.serial = serial
        self._device = device

    @property
    def device(self) -> Any:
        if self._device is None:
            try:
                import uiautomator2 as u2
            except ImportError as exc:
                raise RuntimeError("Cần cài uiautomator2 để dùng UI selector") from exc
            self._device = u2.connect(self.serial)
        return self._device

    def exists(self, **selector: Any) -> bool:
        return bool(self.device(**selector).exists)

    def click(self, **selector: Any) -> Any:
        return self.device(**selector).click()
