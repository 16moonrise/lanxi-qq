# -*- coding: utf-8 -*-
"""[fish-local 2026-11-18] 本地中文句向量，替代 doubao-embedding-vision。

定案（用户批准方案 3）：语义检索脱离方舟，改为进程内跑 bge-small-zh-v1.5。
对外只有一个 `encode(list[str]) -> list[list[float]]`，和原 retrieval.embed() 的返回一致，
所以 retrieval.py 之外没有任何调用点要改。

实现走 ONNX（不是 torch）：
- 只用 `tokenizers`（24ms 导入）+ `onnxruntime`（233ms 导入），**不 import torch**
  （torch 导入 + 权重加载实测 5.7s、常驻约 500MB；ONNX 会话加载 99ms、常驻约 200MB）。
- torch / transformers 只在 ONNX 缺失时作为退路，且是懒加载。
- 512 维、CLS pooling、L2 归一化（BGE 口径）；短句对短句的对称相似度，不加 instruction 前缀。
- torch 与 onnx 的一致性实测：cos = 1.000000，最大绝对误差 2.4e-7。
"""
import os
import threading
import time

MODEL_NAME = "bge-small-zh-v1.5"

_lock = threading.Lock()
_tok = None
_sess = None
_torch_model = None  # 仅 ONNX 缺失时才用
_dim = 0
_loaded_ms = 0.0
_backend = ""
_stats = {"calls": 0, "items": 0, "ms": 0.0}


def model_dir() -> str:
    env = str(os.environ.get("FISH_EMBED_MODEL_DIR", "") or "").strip()
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    # plugins/astrbot_plugin_fish_memory -> plugins -> data -> AstrBot -> qqbot
    root = os.path.abspath(os.path.join(here, "..", "..", "..", ".."))
    return os.path.join(root, "models", MODEL_NAME)


def available() -> bool:
    d = model_dir()
    return bool(d) and os.path.isfile(os.path.join(d, "config.json"))


def _onnx_path() -> str:
    return os.path.join(model_dir(), "model.onnx")


def _load():
    """懒加载。优先 ONNX，失败才退回 torch。失败抛异常，由上层决定退路。"""
    global _tok, _sess, _torch_model, _dim, _loaded_ms, _backend
    if _sess is not None or _torch_model is not None:
        return
    with _lock:
        if _sess is not None or _torch_model is not None:
            return
        d = model_dir()
        if not os.path.isfile(os.path.join(d, "config.json")):
            raise FileNotFoundError(f"本地向量模型不存在：{d}")
        t0 = time.time()

        from tokenizers import Tokenizer

        tok = Tokenizer.from_file(os.path.join(d, "tokenizer.json"))
        tok.enable_truncation(max_length=64)
        pad_id = tok.token_to_id("[PAD]")
        tok.enable_padding(pad_id=0 if pad_id is None else pad_id, pad_token="[PAD]")
        _tok = tok

        if os.path.isfile(_onnx_path()):
            import onnxruntime as ort

            so = ort.SessionOptions()
            try:
                so.intra_op_num_threads = 2
                so.inter_op_num_threads = 1
                so.log_severity_level = 3
            except Exception:
                pass
            _sess = ort.InferenceSession(
                _onnx_path(), sess_options=so, providers=["CPUExecutionProvider"])
            _backend = "onnx"
            try:
                _dim = int(_sess.get_outputs()[0].shape[-1])
            except Exception:
                _dim = 0
        else:
            # 退路：没有 model.onnx 就用 torch（慢、占内存，但能用）
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            import torch  # noqa: F401
            from transformers import AutoModel

            try:
                torch.set_num_threads(2)
            except Exception:
                pass
            m = AutoModel.from_pretrained(d, local_files_only=True)
            m.eval()
            _torch_model = m
            _backend = "torch"
            try:
                _dim = int(m.config.hidden_size)
            except Exception:
                _dim = 0
        _loaded_ms = (time.time() - t0) * 1000


def encode(texts, max_length: int = 64):
    """list[str] -> list[list[float]]，向量已 L2 归一化。"""
    texts = [str(t) if str(t or "").strip() else " " for t in (texts or [])]
    if not texts:
        return []
    _load()
    import numpy as np

    t0 = time.time()
    encs = _tok.encode_batch(texts)
    ids = np.array([e.ids for e in encs], dtype="int64")
    att = np.array([e.attention_mask for e in encs], dtype="int64")
    typ = np.array([e.type_ids for e in encs], dtype="int64")

    if _sess is not None:
        vec = _sess.run(["cls"], {
            "input_ids": ids, "attention_mask": att, "token_type_ids": typ,
        })[0]
    else:
        import torch

        with torch.inference_mode():
            out = _torch_model(
                input_ids=torch.from_numpy(ids),
                attention_mask=torch.from_numpy(att),
                token_type_ids=torch.from_numpy(typ),
            )
            vec = out.last_hidden_state[:, 0, :].detach().cpu().numpy()

    vec = np.asarray(vec, dtype="float32")
    vec /= (np.linalg.norm(vec, axis=1, keepdims=True) + 1e-12)
    _stats["calls"] += 1
    _stats["items"] += len(texts)
    _stats["ms"] += (time.time() - t0) * 1000
    return vec.tolist()


def dim() -> int:
    _load()
    return _dim


def stats() -> dict:
    d = dict(_stats)
    d.update({
        "model": MODEL_NAME,
        "dir": model_dir(),
        "backend": _backend,
        "dim": _dim,
        "loaded_ms": round(_loaded_ms, 1),
        "resident": _sess is not None or _torch_model is not None,
    })
    return d
