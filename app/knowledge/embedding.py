"""Pinned CPU-only multilingual embeddings. No provider/model fallback."""

import hashlib
import json
import math
import os
from pathlib import Path

import httpx

from app.knowledge.models import digest

MODEL_REPO = "Xenova/paraphrase-multilingual-MiniLM-L12-v2"
MODEL_REVISION = "2c4055b12046f11709e9df2c122e59ffbdc2f900"
FILES = {
    "onnx/model_quantized.onnx": "66fc00f5f29afcaff34092e1bdd20008ca3918265a82fb9695a551e510cc4ebc",
    "tokenizer.json": "b60b6b43406a48bf3638526314f3d232d97058bc93472ff2de930d43686fa441",
}
DIMENSIONS = 384
MAX_TOKENS = 128
CONTRACT = {
    "model": MODEL_REPO,
    "revision": MODEL_REVISION,
    "files": FILES,
    "dimensions": DIMENSIONS,
    "max_tokens": MAX_TOKENS,
    "pooling": "attention-mean-l2",
    "prefix": "none",
    "chunking": "token-bounded-halves-v1",
    "pipeline": "sid-reference-v1",
}
FINGERPRINT = digest(CONTRACT)


def file_hash(path):
    sha = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def download_model(directory: Path):
    """Explicit setup only; runtime never downloads or executes remote model code."""
    directory.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        for name, expected in FILES.items():
            target = directory / name
            if target.exists() and file_hash(target) == expected:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(target.suffix + ".partial")
            with client.stream(
                "GET", f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/{name}"
            ) as response:
                response.raise_for_status()
                with temporary.open("wb") as output:
                    for block in response.iter_bytes():
                        output.write(block)
            if file_hash(temporary) != expected:
                temporary.unlink()
                raise ValueError("Embedding artifact checksum mismatch")
            os.replace(temporary, target)
    (directory / "contract.json").write_text(json.dumps(CONTRACT, indent=2), encoding="utf-8")


def validate_vector(vector, dimensions=DIMENSIONS):
    if len(vector) != dimensions or any(not math.isfinite(float(v)) for v in vector):
        raise ValueError("Embedding dimensions or values do not match the collection")
    if sum(float(v) ** 2 for v in vector) < 1e-12:
        raise ValueError("Zero embedding is invalid")


class LocalEmbedder:
    fingerprint = FINGERPRINT
    dimensions = DIMENSIONS

    def __init__(self, directory: str, threads=2):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.np = np
        root = Path(directory)
        for name, expected in FILES.items():
            if not (root / name).is_file() or file_hash(root / name) != expected:
                raise ValueError("Run the model download command; artifacts are missing/invalid")
        self.tokenizer = Tokenizer.from_file(str(root / "tokenizer.json"))
        self.tokenizer.no_truncation()
        self.tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(root / "onnx/model_quantized.onnx"),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )

    def chunks(self, text: str) -> list[str]:
        text = text.strip()
        if not text:
            raise ValueError("Empty embedding input")
        if len(self.tokenizer.encode(text).ids) <= MAX_TOKENS:
            return [text]
        middle = len(text) // 2
        boundary = text.rfind(" ", middle // 2, middle + 1)
        if boundary > 0:
            middle = boundary
        return self.chunks(text[:middle]) + self.chunks(text[middle:])

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        encodings = self.tokenizer.encode_batch(texts)
        if any(len(e.ids) > MAX_TOKENS for e in encodings):
            raise ValueError("Input exceeds model token limit; chunk before indexing/querying")
        np = self.np
        inputs = {
            "input_ids": np.array([e.ids for e in encodings], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encodings], dtype=np.int64),
            "token_type_ids": np.array([e.type_ids for e in encodings], dtype=np.int64),
        }
        inputs = {i.name: inputs[i.name] for i in self.session.get_inputs()}
        try:
            output = self.session.run(None, inputs)[0]
        except Exception as exc:
            # Normalize failures at the native runtime boundary for bounded worker
            # retries and a controlled API error; never change embedding models.
            raise ValueError("Local embedding inference failed") from exc
        mask = inputs["attention_mask"][:, :, None]
        vectors = (output * mask).sum(axis=1) / np.maximum(mask.sum(axis=1), 1)
        vectors /= np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
        result = vectors.tolist()
        for vector in result:
            validate_vector(vector)
        return result
