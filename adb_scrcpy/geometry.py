"""Device geometry and coordinate normalization helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ScreenGeometry:
    width: int
    height: int
    rotation: int = 0
    density: int | None = None

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("width và height phải lớn hơn 0")
        if self.rotation not in (0, 1, 2, 3):
            raise ValueError("rotation phải là 0..3")

    @property
    def rotated_size(self) -> tuple[int, int]:
        return (self.height, self.width) if self.rotation % 2 else (self.width, self.height)

    def normalize_point(self, x: float, y: float, source_size: tuple[int, int]) -> tuple[int, int]:
        source_width, source_height = source_size
        if source_width <= 0 or source_height <= 0:
            raise ValueError("source_size không hợp lệ")
        if self.rotation == 0:
            rotated_x, rotated_y = x, y
            rotated_width, rotated_height = source_width, source_height
        elif self.rotation == 1:
            rotated_x, rotated_y = source_height - y, x
            rotated_width, rotated_height = source_height, source_width
        elif self.rotation == 2:
            rotated_x, rotated_y = source_width - x, source_height - y
            rotated_width, rotated_height = source_width, source_height
        else:
            rotated_x, rotated_y = y, source_width - x
            rotated_width, rotated_height = source_height, source_width
        return (round(rotated_x * self.width / rotated_width), round(rotated_y * self.height / rotated_height))


def parse_wm_size(output: str) -> tuple[int, int]:
    match = re.search(r"(?:Physical|Override) size:\s*(\d+)x(\d+)", output)
    if not match:
        match = re.search(r"(\d+)x(\d+)", output)
    if not match:
        raise ValueError(f"Không đọc được wm size từ: {output!r}")
    return int(match.group(1)), int(match.group(2))


def parse_density(output: str) -> int | None:
    match = re.search(r"(?:Physical|Override) density:\s*(\d+)", output)
    return int(match.group(1)) if match else None


def parse_rotation(output: str) -> int:
    value = output.strip().splitlines()[-1] if output.strip() else "0"
    rotation = int(value)
    if rotation not in (0, 1, 2, 3):
        raise ValueError(f"rotation không hợp lệ: {rotation}")
    return rotation


class GeometryProvider:
    def __init__(self, client: Any):
        self.client = client

    def read(self) -> ScreenGeometry:
        size = parse_wm_size(self.client.shell("wm", "size"))
        density = parse_density(self.client.shell("wm", "density"))
        rotation = parse_rotation(self.client.shell("settings", "get", "system", "user_rotation"))
        return ScreenGeometry(*size, rotation=rotation, density=density)
