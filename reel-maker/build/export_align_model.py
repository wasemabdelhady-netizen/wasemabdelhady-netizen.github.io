"""Export the Arabic wav2vec2 CTC model WhisperX uses for alignment to ONNX (int8).

    python3 export_align_model.py <out_dir>

Writes <out_dir>/align-ar.onnx and <out_dir>/align-ar.vocab.json. Needs torch,
transformers, onnx, onnxruntime (CI only; the app runs it with onnxruntime alone).
"""
import json
import os
import sys

import numpy as np
import torch
from transformers import Wav2Vec2ForCTC, Wav2Vec2Processor

REPO = "jonatasgrosman/wav2vec2-large-xlsr-53-arabic"  # WhisperX default for "ar"


class Wrap(torch.nn.Module):
    def __init__(self, m):
        super().__init__()
        self.m = m

    def forward(self, x):
        return torch.log_softmax(self.m(x).logits, dim=-1)


def main():
    out = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "models-out")
    os.makedirs(out, exist_ok=True)
    proc = Wav2Vec2Processor.from_pretrained(REPO)
    model = Wav2Vec2ForCTC.from_pretrained(REPO).eval()
    vocab = proc.tokenizer.get_vocab()
    with open(os.path.join(out, "align-ar.vocab.json"), "w", encoding="utf-8") as f:
        json.dump(dict(vocab=vocab, blank=proc.tokenizer.pad_token_id,
                       word_delim=proc.tokenizer.word_delimiter_token, sr=16000, repo=REPO),
                  f, ensure_ascii=False, indent=1)
    fp32 = os.path.join(out, "align-ar.fp32.onnx")
    x = torch.randn(1, 16000 * 4)
    torch.onnx.export(Wrap(model), (x,), fp32, input_names=["audio"], output_names=["logp"],
                      dynamic_axes={"audio": {1: "t"}, "logp": {1: "f"}}, opset_version=17,
                      dynamo=False)
    from onnxruntime.quantization import QuantType, quantize_dynamic
    q = os.path.join(out, "align-ar.onnx")
    quantize_dynamic(fp32, q, weight_type=QuantType.QInt8, op_types_to_quantize=["MatMul"])
    os.remove(fp32)
    for f in os.listdir(out):  # external-data side files from the fp32 export
        if f.startswith("align-ar.fp32"):
            os.remove(os.path.join(out, f))
    # sanity: the int8 model must agree with torch on real-ish input
    import onnxruntime as ort
    s = ort.InferenceSession(q, providers=["CPUExecutionProvider"])
    rng = np.random.default_rng(0)
    t = np.arange(16000 * 3) / 16000.0
    a = (0.3 * np.sin(2 * np.pi * 220 * t) * (1 + np.sin(2 * np.pi * 3 * t)) + 0.02 * rng.standard_normal(len(t)))
    a = ((a - a.mean()) / (a.std() + 1e-7)).astype(np.float32)[None]
    ref = Wrap(model)(torch.from_numpy(a)).detach().numpy()
    got = s.run(None, {"audio": a})[0]
    agree = float((ref.argmax(-1) == got.argmax(-1)).mean())
    print("frames", got.shape, "argmax agreement", agree, "size MB", os.path.getsize(q) >> 20)
    assert got.shape == ref.shape and agree > 0.8


if __name__ == "__main__":
    main()
