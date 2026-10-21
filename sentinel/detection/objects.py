"""
Object detection (person, vehicles, animals) with YOLOX on OpenVINO.

Free and local: YOLOX (Apache-2.0) ONNX weights from the official releases,
run by OpenVINO (Apache-2.0) on the CPU. Used only when motion is seen, so it
costs nothing while the scene is still.
"""

import os
import threading
from dataclasses import dataclass
from typing import List, Optional, Tuple

import cv2
import numpy as np

COCO_CLASSES = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog",
    "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket", "bottle",
    "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch", "potted plant",
    "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard", "cell phone",
    "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase", "scissors",
    "teddy bear", "hair drier", "toothbrush",
)

# Classes Sentinel cares about, grouped for alerts and the timeline.
CLASS_GROUPS = {
    "person": "person",
    "bicycle": "vehicle", "car": "vehicle", "motorcycle": "vehicle", "bus": "vehicle", "truck": "vehicle",
    "dog": "animal", "cat": "animal", "horse": "animal", "sheep": "animal", "cow": "animal",
    "bear": "animal", "bird": "animal",
}
WATCHED_CLASSES = frozenset(CLASS_GROUPS)

DEFAULT_MODEL = os.path.join("models", "detector", "yolox_s.onnx")


@dataclass
class Detection:
    label: str
    score: float
    box: Tuple[int, int, int, int]  # x, y, w, h in frame pixels

    @property
    def group(self) -> str:
        return CLASS_GROUPS.get(self.label, "other")

    @property
    def foot(self) -> Tuple[int, int]:
        x, y, w, h = self.box
        return x + w // 2, y + h


class ObjectDetector:
    """Thread-safe YOLOX detector. ``available`` is False if OpenVINO or the model is missing."""

    def __init__(self, model_path: str = DEFAULT_MODEL, threads: int = 2) -> None:
        self.model_path = model_path
        self.available = False
        self.error: Optional[str] = None
        self._lock = threading.Lock()
        self._input_size = (640, 640)
        self._grids: Optional[np.ndarray] = None
        self._strides: Optional[np.ndarray] = None
        try:
            import openvino as ov

            if not os.path.isfile(model_path):
                raise FileNotFoundError(f"model not found: {model_path}")
            core = ov.Core()
            model = core.read_model(model_path)
            shape = model.inputs[0].get_partial_shape()
            self._input_size = (int(shape[2].get_length()), int(shape[3].get_length()))
            self._compiled = core.compile_model(
                model, "CPU", {"PERFORMANCE_HINT": "LATENCY", "INFERENCE_NUM_THREADS": str(threads)}
            )
            self._request = self._compiled.create_infer_request()
            self._build_grids()
            self.available = True
        except Exception as e:  # ImportError, missing file, bad model
            self.error = str(e)

    def _build_grids(self) -> None:
        h, w = self._input_size
        grids, strides = [], []
        for stride in (8, 16, 32):
            gh, gw = h // stride, w // stride
            yv, xv = np.meshgrid(np.arange(gh), np.arange(gw), indexing="ij")
            grid = np.stack((xv, yv), 2).reshape(-1, 2)
            grids.append(grid)
            strides.append(np.full((grid.shape[0], 1), stride))
        self._grids = np.concatenate(grids, 0).astype(np.float32)
        self._strides = np.concatenate(strides, 0).astype(np.float32)

    def _preprocess(self, frame: np.ndarray) -> Tuple[np.ndarray, float]:
        ih, iw = self._input_size
        h, w = frame.shape[:2]
        ratio = min(ih / h, iw / w)
        resized = cv2.resize(frame, (int(w * ratio), int(h * ratio)), interpolation=cv2.INTER_LINEAR)
        padded = np.full((ih, iw, 3), 114, dtype=np.uint8)
        padded[: resized.shape[0], : resized.shape[1]] = resized
        blob = padded.transpose(2, 0, 1)[None].astype(np.float32)
        return blob, ratio

    def detect(
        self,
        frame: np.ndarray,
        min_score: float = 0.45,
        classes: frozenset = WATCHED_CLASSES,
    ) -> List[Detection]:
        if not self.available:
            return []
        blob, ratio = self._preprocess(frame)
        with self._lock:
            self._request.infer({0: blob})
            out = self._request.get_output_tensor(0).data[0].copy()

        # Decode YOLOX grid outputs: (cx, cy, w, h, obj, 80 class probs)
        out[:, :2] = (out[:, :2] + self._grids) * self._strides
        out[:, 2:4] = np.exp(out[:, 2:4]) * self._strides
        class_ids = out[:, 5:].argmax(1)
        scores = out[:, 4] * out[np.arange(len(out)), 5 + class_ids]
        keep = scores >= min_score
        if not keep.any():
            return []
        out, class_ids, scores = out[keep], class_ids[keep], scores[keep]

        boxes = np.empty((len(out), 4), dtype=np.float32)
        boxes[:, 0] = (out[:, 0] - out[:, 2] / 2) / ratio
        boxes[:, 1] = (out[:, 1] - out[:, 3] / 2) / ratio
        boxes[:, 2] = out[:, 2] / ratio
        boxes[:, 3] = out[:, 3] / ratio

        results: List[Detection] = []
        h, w = frame.shape[:2]
        for cls in np.unique(class_ids):
            label = COCO_CLASSES[int(cls)]
            if label not in classes:
                continue
            idx = np.where(class_ids == cls)[0]
            chosen = cv2.dnn.NMSBoxes(boxes[idx].tolist(), scores[idx].tolist(), min_score, 0.45)
            for j in np.array(chosen).reshape(-1):
                bx, by, bw, bh = boxes[idx[j]]
                x0, y0 = max(0, int(bx)), max(0, int(by))
                x1, y1 = min(w, int(bx + bw)), min(h, int(by + bh))
                if x1 - x0 < 4 or y1 - y0 < 4:
                    continue
                results.append(Detection(label, float(scores[idx[j]]), (x0, y0, x1 - x0, y1 - y0)))
        return sorted(results, key=lambda d: -d.score)
