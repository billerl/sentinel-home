"""
Car fingerprints for telling the family's own cars apart (DINOv2-small, Apache-2.0, ONNX export).

CLIP (vehicle_type.py) is trained to match pictures with words, so to it every sedan seen from
above is "a car" and the family's cars all score ~0.85-0.93 alike. DINOv2 is trained to tell
images themselves apart. Measured 2026-11-03 on 518 photos the family named (each against photos
from other days): the right car scored best 95% of the time by day (CLIP 86%) and 97% in infrared
(CLIP 92%); the grey Fusion / blue Edge mix-up, the usual one, fell from 39 to 11 by day.
"""
import os
import threading
from typing import Optional

import cv2
import numpy as np

from sentinel.detection.objects import PROJECT_ROOT, compile_for_device

MODEL = os.path.join(PROJECT_ROOT, "models", "classifier", "dinov2_small.onnx")
MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)  # ImageNet
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


class CarFingerprint:
    """Thread-safe. ``available`` is False when OpenVINO or the model is missing."""

    name = "dinov2"

    def __init__(self, model_path: str = MODEL, threads: int = 2, device: str = "CPU") -> None:
        self.available = False
        self.device = None
        self.error: Optional[str] = None
        self._lock = threading.Lock()
        try:
            import openvino as ov

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
        """Unit-length fingerprint (384 numbers) of a square vehicle crop."""
        rgb = cv2.cvtColor(cv2.resize(crop_bgr, (224, 224), interpolation=cv2.INTER_CUBIC), cv2.COLOR_BGR2RGB)
        blob = ((rgb.astype(np.float32) / 255.0 - MEAN) / STD).transpose(2, 0, 1)[None]
        with self._lock:
            self._request.infer({0: blob})
            vec = self._request.get_output_tensor(0).data[0, 0].copy()  # the class token
        return vec / (np.linalg.norm(vec) + 1e-8)
