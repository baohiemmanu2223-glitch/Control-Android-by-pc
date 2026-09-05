"""Unified semantic-first, template-fallback recognition pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .recognition import MatchResult, TemplateMatcher, UiAutomatorAdapter, normalize_image


@dataclass(frozen=True)
class RecognitionResult:
    method: str
    found: bool
    match: MatchResult | None = None
    error: str | None = None


class RecognitionPipeline:
    def __init__(self, client: Any, geometry: Any | None = None):
        self.client = client
        self.geometry = geometry

    def template(self, template: str | Path, threshold: float = 0.85) -> RecognitionResult:
        try:
            screen = self.client.screencap()
            if self.geometry:
                from io import BytesIO
                from PIL import Image
                screen = normalize_image(Image.open(BytesIO(screen)), self.geometry)
                output = BytesIO()
                screen.save(output, format="PNG")
                screen = output.getvalue()
            match = TemplateMatcher(threshold).match(screen, template)
            return RecognitionResult("template", match.found, match)
        except Exception as exc:
            return RecognitionResult("template", False, error=str(exc))

    def ui_or_template(self, selector: dict[str, Any] | None = None, template: str | Path | None = None, threshold: float = 0.85) -> RecognitionResult:
        if selector:
            try:
                if UiAutomatorAdapter(self.client.serial).exists(**selector):
                    return RecognitionResult("ui", True)
            except Exception as exc:
                ui_error = str(exc)
            else:
                ui_error = "selector not found"
        else:
            ui_error = None
        if template is not None:
            result = self.template(template, threshold)
            return result if result.found else RecognitionResult(result.method, False, result.match, result.error or ui_error)
        return RecognitionResult("ui", False, error=ui_error or "no recognition strategy")
