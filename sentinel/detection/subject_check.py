"""
Second opinion on "person" and "animal" detections, using the CLIP image encoder
already loaded for vehicle types.

The small detector mistakes a sitting black cat for a person, a bin for a person,
a cat for a "bird" or "cow". CLIP looks at the crop and picks between a person, a few
animals, and everyday objects; the camera pipeline then relabels the sighting (so the
alert says "A cat…") or drops it when it's clearly an object. Measured on the
household's footage: a cat by the bins that YOLOX called a person at 0.59 → cat 0.73;
a bin YOLOX called a person → object 0.98.
"""

import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np

from sentinel.detection.vehicle_type import crop_for_clip

PROMPTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "subject_prompts.npz")

# key -> CLIP descriptions (each scored separately; a key's probability is the sum)
SUBJECT_TYPES: Dict[str, List[str]] = {
    "person": ["a person", "a man walking", "a woman", "a child", "a delivery driver", "a person carrying a package"],
    "cat": ["a cat", "a black cat"],
    "dog": ["a dog"],
    "deer": ["a deer"],
    "bird": ["a bird"],
    "object": ["a trash can", "a garbage bin", "a recycling bin", "a mailbox", "a bush", "a potted plant",
               "a shadow on the ground", "a chair", "a box", "a fan", "a coat hanging up", "a bag", "a hose"],
}
ANIMALS = ("cat", "dog", "deer", "bird")
# A person-sized "bird" isn't plausible: CLIP once called a girl walking past the bins a bird.
PERSON_LOOKALIKES = ("cat", "dog", "deer")

RELABEL_MIN = 0.5  # the other kind must be at least this likely ...
RELABEL_RATIO = 2.0  # ... and this many times likelier than what the detector said
OBJECT_MIN = 0.6  # an "object" verdict this sure drops the sighting ...
OBJECT_MAX_DETECTOR_SCORE = 0.8  # ... unless the detector itself was this sure (someone right at the lens)


@dataclass
class Verdict:
    kind: str  # "person", one of ANIMALS, "object", or "" (agree with the detector / unsure)
    probs: Dict[str, float]


class SubjectChecker:
    """Uses ``classifier.embed`` (the CLIP image encoder); ``available`` is False without prompts."""

    def __init__(self, classifier, prompts_path: str = PROMPTS_FILE) -> None:
        self.classifier = classifier
        self.available = False
        try:
            data = np.load(prompts_path)
            self._owners = [str(k) for k in data["keys"]]
            self._text = data["embeddings"].astype(np.float32)
            self._scale = float(data["logit_scale"])
            self.available = getattr(classifier, "available", False) and set(self._owners) <= set(SUBJECT_TYPES)
        except (OSError, KeyError, ValueError):
            pass

    def probabilities(self, frame: np.ndarray, box: Tuple[int, int, int, int]) -> Dict[str, float]:
        embedding = self.classifier.embed(crop_for_clip(frame, box))
        logits = self._scale * (self._text @ embedding)
        p = np.exp(logits - logits.max())
        p /= p.sum()
        out = dict.fromkeys(SUBJECT_TYPES, 0.0)
        for key, value in zip(self._owners, p):
            out[key] += float(value)
        return out

    def check(self, frame: np.ndarray, box: Tuple[int, int, int, int], detector_group: str,
              detector_score: float = 0.0) -> Optional[Verdict]:
        """What the crop really is, when clearly different from what the detector said; else Verdict("")."""
        if not self.available or frame is None:
            return None
        probs = self.probabilities(frame, box)
        if probs["object"] >= OBJECT_MIN and detector_score < OBJECT_MAX_DETECTOR_SCORE:
            return Verdict("object", probs)
        animal = max(ANIMALS, key=lambda k: probs[k])
        animal_p = sum(probs[k] for k in ANIMALS)
        if detector_group == "person":
            lookalike = max(PERSON_LOOKALIKES, key=lambda k: probs[k])
            if probs[lookalike] >= RELABEL_MIN and probs[lookalike] >= RELABEL_RATIO * probs["person"]:
                return Verdict(lookalike, probs)
            return Verdict("", probs)
        if detector_group == "animal":
            if probs["person"] >= RELABEL_MIN and probs["person"] >= RELABEL_RATIO * animal_p:
                return Verdict("person", probs)
            if probs[animal] >= RELABEL_MIN:
                return Verdict(animal, probs)  # e.g. a cat the detector called a "bird" or "cow"
        return Verdict("", probs)
