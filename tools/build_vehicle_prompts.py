"""
Rebuild the CLIP text embeddings: sentinel/detection/vehicle_prompts.npz (VEHICLE_TYPES) and
sentinel/detection/subject_prompts.npz (SUBJECT_TYPES, the person/animal/object second opinion).

Only needed after changing the descriptions in vehicle_type.py or subject_check.py.
Needs the CLIP ViT-B/32 text encoder and tokenizer (not used at runtime):

    pip install tokenizers
    curl -L -o /tmp/text_model.onnx https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/onnx/text_model.onnx
    curl -L -o /tmp/tokenizer.json https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/tokenizer.json
    python tools/build_vehicle_prompts.py /tmp/text_model.onnx /tmp/tokenizer.json
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sentinel.detection import subject_check  # noqa: E402
from sentinel.detection.vehicle_type import PROMPTS_FILE, TEMPLATES, VEHICLE_TYPES  # noqa: E402


def main(text_model: str, tokenizer_json: str) -> None:
    import openvino as ov
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_file(tokenizer_json)
    request = ov.Core().compile_model(text_model, "CPU").create_infer_request()

    def embed(text: str) -> np.ndarray:
        ids = np.array([tokenizer.encode(text).ids], dtype=np.int64)
        request.infer({0: ids})
        vec = request.get_output_tensor(0).data[0].copy()
        return vec / np.linalg.norm(vec)

    vehicle_descriptions = {key: descriptions for key, (_, _, descriptions) in VEHICLE_TYPES.items()}
    for path, types in ((PROMPTS_FILE, vehicle_descriptions), (subject_check.PROMPTS_FILE, subject_check.SUBJECT_TYPES)):
        keys, rows = [], []
        for key, descriptions in types.items():
            for description in descriptions:
                # One row per description, averaged over the templates (prompt ensembling).
                mean = np.mean([embed(t.format(description)) for t in TEMPLATES], axis=0)
                keys.append(key)
                rows.append(mean / np.linalg.norm(mean))
        np.savez(path, keys=np.array(keys), embeddings=np.array(rows, dtype=np.float32), logit_scale=np.float32(100.0))
        print(f"wrote {len(keys)} descriptions of {len(set(keys))} types to {path}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
