"""
"Is this a package?" for something new left on the porch, using the CLIP image encoder already loaded for
vehicle types. The object detector has no "box" class; CLIP picks between parcels and the things a porch
picture usually changes by (leaves, shadows, a pumpkin moved, a cat).

Measured on the household's footage: an Amazon mailer leaned against the house by the front door 0.80;
blown leaves, the lawn, the walkway, a bucket 0.06-0.14.
"""

import os
from typing import Dict, List, Tuple

import numpy as np

from sentinel.detection.vehicle_type import crop_for_clip

PROMPTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "package_prompts.npz")

PACKAGE_TYPES: Dict[str, List[str]] = {
    "package": ["a cardboard box", "a package on a doorstep", "a delivered parcel", "a padded mailer envelope",
                "a plastic shipping bag", "an Amazon package", "a brown box"],
    "other": ["a doormat", "a shadow on the ground", "dead leaves", "a pumpkin", "a flower pot", "a pair of shoes",
              "an empty concrete porch", "a person", "a dog", "a cat", "a lawn", "a garden bush", "a door",
              "a brick wall", "a bucket", "a chair", "a bird", "a sidewalk", "a plant",
              # someone sitting by the door the detector missed: his jacket and jeans scored 0.69-0.84 without these
              "a person sitting on a chair", "a man's legs in jeans", "a grey jacket", "a person's hand"],
}
PACKAGE_MIN = 0.7  # real parcels 0.98-1.00; a man sitting by the door 0.51-0.56


class PackageChecker:
    """Uses ``classifier.embed`` (the CLIP image encoder); ``available`` is False without prompts."""

    def __init__(self, classifier, prompts_path: str = PROMPTS_FILE) -> None:
        self.classifier = classifier
        self.available = False
        try:
            data = np.load(prompts_path)
            self._owners = [str(k) for k in data["keys"]]
            self._text = data["embeddings"].astype(np.float32)
            self._scale = float(data["logit_scale"])
            self.available = getattr(classifier, "available", False) and set(self._owners) <= set(PACKAGE_TYPES)
        except (OSError, KeyError, ValueError):
            pass

    def probability(self, frame: np.ndarray, box: Tuple[int, int, int, int]) -> float:
        """How likely the crop is a package (0-1)."""
        embedding = self.classifier.embed(crop_for_clip(frame, box))
        logits = self._scale * (self._text @ embedding)
        p = np.exp(logits - logits.max())
        p /= p.sum()
        return float(sum(v for owner, v in zip(self._owners, p) if owner == "package"))
