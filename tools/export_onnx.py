# -*- coding: utf-8 -*-
"""把 bge-small-zh-v1.5 导出成 ONNX（一次性动作）。

目的：进程内推理不再依赖 torch（torch 冷加载 5.7s、常驻 500MB；
onnxruntime 加载约 1s、常驻约 180MB），句向量数值保持一致。

产物：models\\bge-small-zh-v1.5\\model.onnx
导出后本脚本会做一次 torch vs onnx 的一致性比对（余弦差 < 1e-4 视为通过）。
"""
import os
import time

import numpy as np
import torch
from transformers import AutoModel, AutoTokenizer

import sys

# 用法：python export_onnx.py [模型目录]
# 也可以用环境变量 FISH_EMBED_MODEL_DIR 指定（与插件用的是同一个变量）
DIR = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("FISH_EMBED_MODEL_DIR", "")).strip()
if not DIR or not os.path.isdir(DIR):
    raise SystemExit("请给出 bge-small-zh-v1.5 的目录（含 tokenizer.json/config.json 与权重）")
OUT = os.path.join(DIR, "model.onnx")


class Wrap(torch.nn.Module):
    def __init__(self, m):
        super().__init__()
        self.m = m

    def forward(self, input_ids, attention_mask, token_type_ids):
        out = self.m(
            input_ids=input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            return_dict=False,
        )
        return out[0][:, 0, :]  # CLS


def main():
    tok = AutoTokenizer.from_pretrained(DIR, local_files_only=True)
    model = AutoModel.from_pretrained(DIR, local_files_only=True).eval()
    w = Wrap(model).eval()

    sample = ["今天下雨了，窗上全是雾", "面试到底去不去"]
    enc = tok(sample, padding=True, truncation=True, max_length=64, return_tensors="pt")
    print("tokenizer keys:", list(enc.keys()), "shape:", tuple(enc["input_ids"].shape))

    t0 = time.time()
    with torch.inference_mode():
        torch.onnx.export(
            w,
            (enc["input_ids"], enc["attention_mask"], enc["token_type_ids"]),
            OUT,
            input_names=["input_ids", "attention_mask", "token_type_ids"],
            output_names=["cls"],
            dynamic_axes={
                "input_ids": {0: "batch", 1: "seq"},
                "attention_mask": {0: "batch", 1: "seq"},
                "token_type_ids": {0: "batch", 1: "seq"},
                "cls": {0: "batch"},
            },
            opset_version=17,
            do_constant_folding=True,
        )
    print("export ms:", round((time.time() - t0) * 1000), "size MB:", round(os.path.getsize(OUT) / 1048576, 1))

    # ---- 一致性比对 ----
    import onnxruntime as ort

    with torch.inference_mode():
        t = w(enc["input_ids"], enc["attention_mask"], enc["token_type_ids"]).numpy()
    t = t / (np.linalg.norm(t, axis=1, keepdims=True) + 1e-12)

    sess = ort.InferenceSession(OUT, providers=["CPUExecutionProvider"])
    feeds = {
        "input_ids": enc["input_ids"].numpy(),
        "attention_mask": enc["attention_mask"].numpy(),
        "token_type_ids": enc["token_type_ids"].numpy(),
    }
    o = sess.run(["cls"], feeds)[0]
    o = o / (np.linalg.norm(o, axis=1, keepdims=True) + 1e-12)

    cos = (t * o).sum(axis=1)
    print("cos(torch, onnx):", [round(float(x), 6) for x in cos])
    print("max abs diff:", float(np.abs(t - o).max()))


if __name__ == "__main__":
    main()
