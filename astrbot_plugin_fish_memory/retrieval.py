# -*- coding: utf-8 -*-
"""记忆卡检索（2026-11-09 23:20）。

要点（都是实测定下来的）：
- 向量用 Ark `doubao-embedding-vision`（2048 维），单次最多 10 条，必须分批
- 只靠向量不行：「店」和「火腿肠」这类词面不搭；只靠关键词也不行。
  → 向量 top5 + 关键词 top3 合并加权
- 检索永远会返回东西，所以要打分阈值（< 0.40 丢掉），不能硬塞
- 热卡兜底：最近 7 天动过的卡无条件加 1-2 条（新事最常用，不冒漏的风险）
- 纯离线可测：不依赖 AstrBot
"""
import json
import math
import os
import re
import time
import urllib.request

CFG = os.environ.get("ASTRBOT_CMD_CONFIG") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "cmd_config.json")
MODEL = "doubao-embedding-vision"
MIN_COS = 0.42          # 绝对下限（[fish-local 2026-11-18] bge 实测重标：top1 中位 0.555、p10 0.469）
VEC_TOP = 5
KW_TOP = 3
FINAL_TOP = 4
HOT_DAYS = 7
TOP_GAP = 0.10          # 相对阈值：离最高分 0.10 以内都算相关
SHORT_Q = 3             # 规范化后短于这个字数就不做语义检索，只靠常挂轮转

# ---- [fish-local 2026-11-18] 向量后端：local（默认）| remote | off ----
BACKEND = "local"
EMBED_TAG = ""          # 索引指纹（模型名|维度），写进 index.npz；不符自动重建

try:
    import local_embed  # 同目录：进程内 bge-small-zh-v1.5（ONNX），不依赖 torch
except Exception:  # pragma: no cover
    local_embed = None
    BACKEND = "remote"


def configure(backend=None, model_dir=None):
    """插件启动时调用。环境变量 FISH_EMBED_MODEL_DIR 也可覆盖模型目录。"""
    global BACKEND, EMBED_TAG
    if model_dir:
        os.environ["FISH_EMBED_MODEL_DIR"] = str(model_dir)
    if backend:
        BACKEND = str(backend).strip().lower()
    EMBED_TAG = ""          # 换后端必须重算指纹，否则会沿用上一个模型的名字
    _IDX_CACHE.clear()
    return BACKEND


def _sync_tag():
    """索引指纹：换模型 / 换维度后，老 index.npz 会因指纹不符自动重建。"""
    global EMBED_TAG
    if not EMBED_TAG:
        if BACKEND == "remote":
            EMBED_TAG = f"{MODEL}|2048"
        else:
            try:
                EMBED_TAG = f"{local_embed.MODEL_NAME}|{local_embed.dim()}"
            except Exception:
                name = getattr(local_embed, "MODEL_NAME", "none")
                EMBED_TAG = f"{name}|?"
    return EMBED_TAG


def _api():
    """远端后端（方舟）的源。11-18 用户删掉方舟后这里没有条目，抛异常由 embed() 兜住。"""
    cfg = json.load(open(CFG, encoding="utf-8-sig"))
    for s in cfg.get("provider_sources") or []:
        if s.get("provider") == "openai":
            k = s.get("key")
            key = k[0] if isinstance(k, list) and k else str(k or "")
            return key, str(s.get("api_base") or "").rstrip("/")
    raise RuntimeError("远端向量后端不可用（方舟源已删除）")


# [fish-patch 2026-11-09 23:40] 累计 embedding 用量（成本观测用，不影响检索）
USAGE = {"calls": 0, "items": 0, "tokens": 0}


def reset_usage():
    USAGE.update({"calls": 0, "items": 0, "tokens": 0})


def _embed_remote(texts, timeout=60):
    """远端嵌入（Ark，上限 10 条/次，2048 维）。已不是默认路径，保留作退路。"""
    key, base = _api()
    out = []
    for i in range(0, len(texts), 10):
        chunk = [t if t else " " for t in texts[i:i + 10]]
        body = {"model": MODEL, "input": chunk}
        req = urllib.request.Request(
            base + "/embeddings",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
        j = json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8"))
        USAGE["calls"] += 1
        USAGE["items"] += len(j.get("data") or [])
        try:
            USAGE["tokens"] += int((j.get("usage") or {}).get("total_tokens") or 0)
        except Exception:
            pass
        out.extend(d["embedding"] for d in j["data"])
    norm = []
    for v in out:
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        norm.append([x / n for x in v])
    return norm


def embed(texts, timeout=60):
    """统一入口：本地优先，本地不可用退远端，都不行返回 []（上层自动退化纯字面）。

    [fish-local 2026-11-18] 本地失败**不**偷偷走远端——方舟已经在退场，
    悄悄联网既慢又不可预期；宁可这轮退化到关键词。
    """
    if not texts:
        return []
    if BACKEND == "off":
        return []
    if BACKEND != "remote" and local_embed is not None:
        try:
            return local_embed.encode(texts)
        except Exception:
            if BACKEND == "local":
                return []
    return _embed_remote(texts, timeout=timeout)


def _norm_text(t):
    return re.sub(r"[\s，。、；：！？,.!?~…—\-\"“”‘’()（）]", "", t or "")


def kw_score(query, text):
    """极简关键词分：问句二元组被卡片覆盖的比例。"""
    q = _norm_text(query)
    t = _norm_text(text)
    if len(q) < 2 or not t:
        return 0.0
    grams = {q[i:i + 2] for i in range(len(q) - 1)}
    if not grams:
        return 0.0
    return sum(1 for g in grams if g in t) / len(grams)


class CardIndex:
    """卡片向量索引：一个 .npz（ids + 矩阵），卡池变了就重建。"""

    def __init__(self, card_dir):
        self.dir = card_dir
        self.path = os.path.join(card_dir, "index.npz")
        self.cards_path = os.path.join(card_dir, "memories.json")
        self.ids = []
        self.mat = None
        self._load_or_build()

    def _cards(self):
        try:
            d = json.load(open(self.cards_path, encoding="utf-8"))
            return {m["id"]: m for m in d.get("memories", []) if m.get("id")}
        except Exception:
            return {}

    def _load_or_build(self):
        cards = self._cards()
        tag = _sync_tag()
        try:
            import numpy as np
            z = np.load(self.path, allow_pickle=False)
            ids = list(z["ids"])
            mat = z["mat"]
            # [fish-local 2026-11-18] 只比对 id 数量是不够的：换模型后维度会变，
            # 老索引照样"有效"，直到 mat@q 形状不符被 except 吞掉、静默退化纯字面。
            meta = str(z["meta"][0]) if "meta" in z.files else ""
            if meta != tag:
                raise ValueError(f"索引指纹不符（{meta or '无'} != {tag}），重建")
            if len(ids) == len(cards) and all(str(i) in cards for i in ids):
                self.ids, self.mat = [str(i) for i in ids], mat
                return
        except Exception:
            pass
        self.rebuild()

    def rebuild(self):
        cards = self._cards()
        ids = list(cards)
        if not ids:
            self.ids, self.mat = [], None
            return
        texts = [f"{cards[i].get('key', '')}：{cards[i].get('text', '')}" for i in ids]
        vecs = embed(texts)
        if not vecs:
            self.ids, self.mat = [], None
            return
        import numpy as np
        os.makedirs(self.dir, exist_ok=True)
        np.savez_compressed(self.path, ids=np.array(ids),
                            mat=np.array(vecs, dtype="float32"),
                            meta=np.array([_sync_tag()]))
        self.ids, self.mat = ids, np.array(vecs, dtype="float32")

    def search(self, query, cards, top=VEC_TOP):
        if not self.ids or self.mat is None:
            return []
        try:
            qv = embed([query])[0]
        except Exception:
            return []
        import numpy as np
        q = np.array(qv, dtype="float32")
        if q.shape[0] != self.mat.shape[1]:
            return []          # 维度不符（换模型没重建索引）：宁可退化，别算了
        sims = self.mat @ q
        order = sims.argsort()[::-1][:top]
        return [(self.ids[i], float(sims[i])) for i in order]


def _age_tag(m):
    """给卡片加个「多久以前」的尾巴。"""
    try:
        ts = int(m.get("created") or 0)
        if not ts:
            return ""
        days = int((time.time() - ts) / 86400)
        if days <= 0:
            return "今天："
        if days == 1:
            return "昨天："
        if days < 7:
            return f"{days}天前："
        if days < 30:
            return f"{days // 7}周前："
        return f"{days // 30}个月前："
    except Exception:
        return ""


_IDX_CACHE = {}


def _get_index(cards_dir):
    """[fish-patch 2026-11-11] 索引按「卡文件 mtime」缓存，别每轮重建。

    原来 retrieve() 每次都 `CardIndex(cards_dir)`，卡数一变就整库重算 embedding
    （32 张 = 4 批，50 张 = 6 批，每批一次 HTTP）。卡池从 0 长到 50 之后这笔开销会显出来。
    """
    try:
        mt = os.path.getmtime(os.path.join(cards_dir, "memories.json"))
    except Exception:
        mt = 0
    key = (os.path.abspath(cards_dir), mt)
    ent = _IDX_CACHE.get(key)
    if ent is not None:
        return ent
    idx = CardIndex(cards_dir)
    _IDX_CACHE.clear()
    _IDX_CACHE[key] = idx
    return idx


def retrieve(query, cards_dir, final_top=FINAL_TOP, min_cos=MIN_COS, hot_days=HOT_DAYS):
    """返回 (选中的卡列表, 调试信息)。cards_dir 是 memory/card。"""
    try:
        d = json.load(open(os.path.join(cards_dir, "memories.json"), encoding="utf-8"))
        cards = d.get("memories", [])
    except Exception:
        return [], {"err": "no cards"}
    by_id = {m.get("id"): m for m in cards if m.get("id")}
    if not by_id:
        return [], {"err": "empty"}

    vec_hits = []
    # [fish-local 2026-11-18] 「嗯」「好」「在吗」这种短句做语义检索只会捞噪声：
    # bge 对任何两句短中文都能给出 0.45+ 的分数。短句交给常挂轮转。
    if len(_norm_text(query)) >= SHORT_Q:
        try:
            idx = _get_index(cards_dir)
            vec_hits = idx.search(query, by_id)
        except Exception:
            vec_hits = []

    kw_hits = sorted(
        ((m.get("id"), kw_score(query, f"{m.get('key', '')}：{m.get('text', '')}"))
         for m in cards),
        key=lambda x: -x[1])[:KW_TOP]

    # 阈值只对「余弦」生效；混合分只用来排序（混了关键词会把 cos 0.42 压到 0.32 而误杀）
    cos_by_id, kw_by_id = {}, {}
    for cid, sv in vec_hits:
        cos_by_id[cid] = float(sv)
    for cid, sk in kw_hits:
        kw_by_id[cid] = float(sk)

    ranked = []
    for cid in set(cos_by_id) | set(kw_by_id):
        cos = cos_by_id.get(cid, 0.0)
        kw = kw_by_id.get(cid, kw_score(
            query, f"{by_id[cid].get('key', '')}：{by_id[cid].get('text', '')}"))
        if cos < min_cos and kw < 0.55:
            continue
        if by_id[cid].get("closed"):
            cos *= 0.85          # 已结清的旧账：还能被想起来，但别抢新事的位子
        ranked.append((cid, 0.75 * cos + 0.25 * kw, cos, kw))
    ranked.sort(key=lambda x: -x[1])
    # 相对阈值：短文本的余弦分布很窄，用全局硬阈值会误杀（「目标是多少」对「账:看海」只有 0.396）。
    # 改成「离最高分不超过 TOP_GAP」，同时绝对分不能低于 MIN_COS。
    if ranked:
        top_cos = max(c for _, _, c, _ in ranked)
        if top_cos >= MIN_COS:
            ranked = [r for r in ranked if r[2] >= max(MIN_COS - 0.03, top_cos - TOP_GAP)]
    picked = ranked[:final_top]
    chosen = [by_id[cid] for cid, _, _, _ in picked]

    # 热卡兜底：只在她「什么都没想起来」的时候补 1 条（最近 2 天动过的），
    # 不再无条件塞 2 条 —— 否则每轮都是同样的两张卡，纯噪音。
    now = time.time()
    hot = []
    if len(chosen) == 0:
        have = {m.get("id") for m in chosen}
        for m in sorted(cards, key=lambda x: -(int(x.get("last_seen") or 0))):
            if m.get("id") in have:
                continue
            if now - int(m.get("last_seen") or 0) <= 2 * 86400:
                hot.append(m)
                break
    out = chosen + hot
    dbg = {"vec": len(vec_hits), "kw": len(kw_hits), "picked": len(chosen),
           "hot": len(hot),
           "scores": [(cid, round(sc, 2), round(c, 2), round(k, 2))
                       for cid, sc, c, k in picked]}
    return out, dbg


def _closed_tag(m):
    c = m.get("closed")
    if c == "done":
        return "（已兑现）"
    if c == "void":
        return "（已作废）"
    return ""


def hot_pick(cards, n=3, limit=140):
    """A 层：常挂候选（积分 >= 80、未结清、只认常驻类），按「最久没挂过」轮转。

    这是三层里的第一层 —— 保证当下连贯；不是每轮全挂，所以不会出现某张卡赖着不走。
    调用方负责把 last_mounted 写回并保存。
    """
    import time as _t
    KINDS = ("约定", "承诺", "账", "叮嘱")
    cand = []
    for m in cards or []:
        if m.get("closed"):
            continue
        if int(m.get("importance") or 0) < 80:
            continue
        head = str(m.get("key") or "").split(":")[0].split("｜")[0]
        if head not in KINDS and not m.get("pinned"):
            continue
        cand.append(m)
    cand.sort(key=lambda m: float(m.get("last_mounted") or 0))
    out, total = [], 0
    for m in cand:
        line = f"- {_closed_tag(m)}{(m.get('text') or '')[:60]}"
        if total + len(line) > limit:
            continue
        out.append(m)
        total += len(line)
        m["last_mounted"] = _t.time()
        if len(out) >= n:
            break
    return out


def render_block(cards, limit=420):
    """把检索到的卡拼成注入块（短，别把预算吃掉）。"""
    lines, total = [], 0
    for m in cards:
        line = f"- {_closed_tag(m)}{_age_tag(m)}{(m.get('text') or '')[:70]}"
        if total + len(line) > limit:
            break
        lines.append(line)
        total += len(line)
    if not lines:
        return ""
    return "\n".join(lines)
