"""Element grounding: coordinate transforms, matching and model-based grounding."""

from .geometry import (  # noqa: F401
    TransformError,
    capture_to_screen,
    clamp_to_monitors,
    normalized_to_screen,
    screen_to_capture,
    validate_rect_on_screens,
)
from .matcher import GroundingResult, best_match, rank_candidates  # noqa: F401

__all__ = [
    "TransformError",
    "capture_to_screen",
    "clamp_to_monitors",
    "normalized_to_screen",
    "screen_to_capture",
    "validate_rect_on_screens",
    "GroundingResult",
    "best_match",
    "rank_candidates",
]
