"""
Rebuild sentinel/detection/vehicle_prompts.npz from VEHICLE_TYPES.

Only needed after changing the descriptions in sentinel/detection/vehicle_type.py.
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

    keys, rows = [], []
    for key, (_, _, descriptions) in VEHICLE_TYPES.items():
        for description in descriptions:
            # One row per description, averaged over the templates (prompt ensembling).
            mean = np.mean([embed(t.format(description)) for t in TEMPLATES], axis=0)
            keys.append(key)
            rows.append(mean / np.linalg.norm(mean))
    np.savez(PROMPTS_FILE, keys=np.array(keys), embeddings=np.array(rows, dtype=np.float32),
             logit_scale=np.float32(100.0))
    print(f"wrote {len(keys)} descriptions of {len(set(keys))} vehicle types to {PROMPTS_FILE}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
