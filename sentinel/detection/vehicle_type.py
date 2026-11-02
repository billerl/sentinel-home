"""
Vehicle type and delivery-company recognition with CLIP (zero-shot).

YOLOX only knows "car/truck/bus". When a vehicle event fires, its crop is run
through the CLIP ViT-B/32 image encoder (MIT licence, OpenAI weights exported to
ONNX) and compared with text embeddings of short descriptions ("a brown UPS
delivery truck", "a pickup truck", ...). The text side is precomputed once by
tools/build_vehicle_prompts.py, so at runtime only the image encoder is needed.

Each description is scored separately and a type's probability is the sum over
its descriptions, so common vehicles described several ways ("a grey sedan",
"a blue car") soak up look-alikes. Prompts and thresholds were tuned on 112
delivery-vehicle photos (clean and shrunk to camera size) against 154 crops of
this household's own cars from the driveway cameras: no false deliveries, about
83% of real ones named.
"""

import os
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

from sentinel.detection.objects import PROJECT_ROOT, compile_for_device

CLASSIFIER_DIR = os.path.join(PROJECT_ROOT, "models", "classifier")
VISION_MODEL = os.path.join(CLASSIFIER_DIR, "clip_vit_b32_vision.onnx")
PROMPTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vehicle_prompts.npz")

# key -> (phrase used in alerts, is a delivery, CLIP descriptions)
VEHICLE_TYPES: Dict[str, Tuple[Optional[str], bool, List[str]]] = {
    "ups": ("UPS truck", True, ["a brown UPS delivery truck", "a brown UPS package car"]),
    "fedex": ("FedEx truck", True, ["a FedEx delivery van", "a white FedEx truck with a purple and orange logo"]),
    "amazon": ("Amazon van", True, ["an Amazon delivery van", "a blue Amazon Prime van",
                                    "a white Amazon van with the smile logo"]),
    "usps": ("mail truck", True, ["a USPS mail truck", "a white postal service mail truck",
                                  "a small white mail delivery truck with a blue eagle logo"]),
    "dhl": ("DHL van", True, ["a yellow DHL delivery van"]),
    # Colour variants matter: a bright blue car otherwise drifts towards "Amazon van".
    "car": ("car", False, ["a car", "a sedan", "a hatchback car", "a grey sedan", "a blue car", "a black car",
                           "a silver car"]),
    "suv": ("SUV", False, ["an SUV", "a crossover SUV", "a silver SUV", "a blue SUV"]),
    "pickup": ("pickup truck", False, ["a pickup truck", "a blue pickup truck", "a pickup truck with a bed cover"]),
    "minivan": ("minivan", False, ["a minivan"]),
    "van": ("van", False, ["a plain white work van", "a passenger van", "a cargo van"]),
    "school_bus": ("school bus", False, ["a yellow school bus"]),
    "garbage": ("garbage truck", False, ["a garbage truck", "a recycling truck"]),
    "motorcycle": ("motorcycle", False, ["a motorcycle"]),
    "scenery": (None, False, ["an empty street", "a driveway", "the front of a house", "trees and grass"]),
}
TEMPLATES = ("a security camera photo of {}.", "a photo of {}.", "a blurry low resolution photo of {}.")

MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)

# A delivery call must be both likely and clearly ahead of the runner-up; a wrong
# "Amazon van" is worse than a plain "van". Greyscale (IR) frames lose the brand
# colours CLIP relies on, so they need more certainty.
DELIVERY_MIN_PROB = 0.55
DELIVERY_MIN_PROB_NIGHT = 0.75
DELIVERY_MIN_RATIO = 2.0  # times the runner-up
DELIVERY_MIN_AREA = 2500  # px²; far-away specks are guesses
TYPE_MIN_PROB = 0.4


@dataclass
class VehicleType:
    key: str
    phrase: Optional[str]
    delivery: bool
    prob: float
    probs: Dict[str, float]


def crop_for_clip(frame: np.ndarray, box: Tuple[int, int, int, int], pad: float = 0.12) -> np.ndarray:
    """Square crop around the box (with a margin) so wide vehicles aren't cut off."""
    h, w = frame.shape[:2]
    x, y, bw, bh = box
    side = int(max(bw, bh) * (1 + 2 * pad))
    cx, cy = x + bw // 2, y + bh // 2
    x0, y0 = cx - side // 2, cy - side // 2
    out = np.full((side, side, 3), 114, dtype=np.uint8)
    sx0, sy0 = max(0, x0), max(0, y0)
    sx1, sy1 = min(w, x0 + side), min(h, y0 + side)
    if sx1 > sx0 and sy1 > sy0:
        out[sy0 - y0: sy1 - y0, sx0 - x0: sx1 - x0] = frame[sy0:sy1, sx0:sx1]
    return out


def is_greyscale(frame: np.ndarray) -> bool:
    """True for infrared (night) pictures. Per-pixel spread between the colour channels
    is ~3 in IR frames from these cameras (JPEG noise only), 5-13 at dawn and 20+ in
    daylight; average saturation was too noisy to tell them apart."""
    small = cv2.resize(frame, (64, 36), interpolation=cv2.INTER_AREA).astype(np.int16)
    return float((small.max(2) - small.min(2)).mean()) < 8.0


def is_daylight(frame: np.ndarray) -> bool:
    """Colour and bright: real daylight. At night the cameras also go to colour when headlights or the
    porch light come on, but those frames are dark (median brightness 30-49 vs 65+ by day) and their
    colours are the lamps', not the paint's (2026-10-29: a dark Fusion under orange light "was" the Jeep)."""
    if is_greyscale(frame):
        return False
    small = cv2.resize(frame, (64, 36), interpolation=cv2.INTER_AREA)
    return float(np.median(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))) >= 60.0


class VehicleClassifier:
    """Thread-safe. ``available`` is False when OpenVINO, the model or the prompts are missing."""

    def __init__(self, model_path: str = VISION_MODEL, prompts_path: str = PROMPTS_FILE,
                 threads: int = 2, device: str = "CPU") -> None:
        self.available = False
        self.device = None
        self.error: Optional[str] = None
        self._lock = threading.Lock()
        try:
            import openvino as ov

            data = np.load(prompts_path)
            self._owners = [str(k) for k in data["keys"]]  # type key of each description
            self._text = data["embeddings"].astype(np.float32)  # (descriptions, 512), unit length
            self._scale = float(data["logit_scale"])
            unknown = set(self._owners) - set(VEHICLE_TYPES)
            if unknown:
                raise ValueError(f"prompt file has unknown types {sorted(unknown)}; rebuild it")
            if not os.path.isfile(model_path):
                raise FileNotFoundError(f"model not found: {model_path}")
            core = ov.Core()
            model = core.read_model(model_path)
            model.reshape([1, 3, 224, 224])
            self._compiled, self.device = compile_for_device(core, model, device, threads)
            self._request = self._compiled.create_infer_request()
            self.available = True
        except Exception as e:  # ImportError, missing file, bad model
            self.error = str(e)

    def embed(self, crop_bgr: np.ndarray) -> np.ndarray:
        rgb = cv2.cvtColor(cv2.resize(crop_bgr, (224, 224), interpolation=cv2.INTER_CUBIC), cv2.COLOR_BGR2RGB)
        blob = ((rgb.astype(np.float32) / 255.0 - MEAN) / STD).transpose(2, 0, 1)[None]
        with self._lock:
            self._request.infer({0: blob})
            vec = self._request.get_output_tensor(0).data[0].copy()
        return vec / (np.linalg.norm(vec) + 1e-8)

    def probabilities(self, frame: np.ndarray, box: Tuple[int, int, int, int],
                      embedding: Optional[np.ndarray] = None) -> Dict[str, float]:
        if embedding is None:
            embedding = self.embed(crop_for_clip(frame, box))
        logits = self._scale * (self._text @ embedding)
        exp = np.exp(logits - logits.max())
        probs = exp / exp.sum()
        out = dict.fromkeys(VEHICLE_TYPES, 0.0)
        for key, p in zip(self._owners, probs):
            out[key] += float(p)
        return out

    def classify(self, frame: np.ndarray, box: Tuple[int, int, int, int],
                 embedding: Optional[np.ndarray] = None) -> Optional[VehicleType]:
        """Best vehicle type for the box, or None when unavailable or unsure."""
        if not self.available:
            return None
        probs = self.probabilities(frame, box, embedding)
        ranked = sorted(probs.items(), key=lambda kv: -kv[1])
        key, prob = ranked[0]
        phrase, delivery, _ = VEHICLE_TYPES[key]
        if delivery:
            need = DELIVERY_MIN_PROB_NIGHT if is_greyscale(frame) else DELIVERY_MIN_PROB
            if prob < need or prob < DELIVERY_MIN_RATIO * ranked[1][1] or box[2] * box[3] < DELIVERY_MIN_AREA:
                return None
        elif phrase is None or prob < TYPE_MIN_PROB:
            return None
        return VehicleType(key, phrase, delivery, prob, probs)
