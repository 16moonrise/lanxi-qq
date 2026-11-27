# -*- coding: utf-8 -*-
"""小肥鱼自绘涂鸦 · 核心逻辑（不依赖 AstrBot，可离线跑）

设计定案（2026-11-08）
- 「手」固定：3 岁小孩蜡笔、皱纸、歪线、涂出界 —— 她的签名，不随题材变
- 「内容」她自己填：从记忆里挑一件想画的事
- 「负面」固定：不要文字、不要写实、不要 3D
- 一张约 25 秒；失败要如实说，不许先承诺
- L1：画完记一条索引（id → 文件 + 描述），并把描述写进 raw，交给日记变卡片
- 编号只给插件用，不主动进她的上下文（否则她会说"编号 0007 那张"，很出戏）
"""
import base64
import json
import os
import time
import urllib.error
import urllib.request

CFG = os.environ.get("ASTRBOT_CMD_CONFIG") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "cmd_config.json")

# 一键关停：改成 False 重启即失效（不动其他代码）
DRAW_ENABLED = True

# [换人 2026-11-20] 画风从「三岁小孩蜡笔」换成「剪影平涂」（试出图确认：两色守住、
# 无渐变无描线、脸留白；原本想要的淡紫点缀被模型自己删了，索性只留两色）。
HAND = ("一幅剪影平涂的画：整张只有两种颜色——深蓝黑的背景、米白的角色。"
        "纯色块，没有渐变、没有光影、没有高光、没有描线，角色靠色块对比成形，边缘干净利落，"
        "像丝网印刷或孔版画。脸部是一整块亮色，不画眼睛、鼻子、嘴巴。"
        "不要质感纹理、不要纸张纹理、不要摄影感、不要3D、不要精细插画感。")
NEG = "画面里不要出现任何文字，不要写实，不要渐变，不要光影，不要描线，不要五官。"

MAX_PER_DAY = 8          # 每天最多画几张（2026-11-11 由 5 提到 8：她自己画会占掉给他的额度）
COOLDOWN_SEC = 60        # 两张之间至少隔多久（2026-11-09 22:08 用户定：60 秒，防她自己在回复里连画）
KEEP_IMAGES = 60         # 本地最多留多少张，超了删最旧的


def _sources():
    try:
        cfg = json.load(open(CFG, encoding="utf-8-sig"))
    except Exception:
        return []
    return cfg.get("provider_sources") or []


def _first_key(src):
    k = src.get("key")
    if isinstance(k, list):
        return k[0] if k else ""
    return str(k or "")


def provider():
    """看图用的源：官方 DeepSeek。

    [fish-fix 2026-11-18] 用户已把方舟（ark）的 API 从 AstrBot 里删掉，
    provider_sources 里只剩 deepseek。原来这里按 provider=="openai" 取方舟源，
    删掉之后会 IndexError，把 describe/celestial/bili 看图三条链路一起带崩。
    """
    for s in _sources():
        if s.get("provider") == "deepseek":
            base = str(s.get("api_base") or "https://api.deepseek.com").rstrip("/")
            return _first_key(s), base
    raise RuntimeError("provider_sources 里没有 deepseek 源")


def _ark():
    """方舟套餐源（出图 Seedream 用）。11-18 被删后返回 None。"""
    for s in _sources():
        if s.get("provider") == "openai":
            return _first_key(s), str(s.get("api_base") or "").rstrip("/")
    return None


def build_prompt(subject: str) -> str:
    """「手」+「内容」+「时间/天气锁定」+「负面」。subject 是她自己的话。"""
    # [fish-patch 2026-11-09 21:52] 实测她要「雨夜」，出图变成「白天+大太阳+下雨」。
    # 出图模型会自作主张改时间和天气，这里明确锁死。
    return (f"{HAND}\n画的内容：{(subject or '').strip()}\n"
            "内容里说的时间、天气、光线（白天／夜里／下雨／晴／灯亮着）照原样画，不要自己改成别的。\n"
            f"{NEG}")


# ---------------- 豆包网页版反代（2026-11-18） ----------------
# 方舟删掉后，出图改走 DSH 插件 dsh-doubao-image-free 的纯 HTTP 反向接口。
# 我们只调一条 node 命令，不依赖 DSH 进程在不在。
NODE_CANDIDATES = (
    os.environ.get("NODE_BIN", ""),
    r"E:\SnowLuma\node.exe",
)
_BASE = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".."))
DOUBAO_CLI = os.path.join(_BASE, "tools", "doubao_gen.mjs")
DOUBAO_WAIT = 200          # 秒；豆包出一张约 20–60 秒，留足余量


def _node() -> str:
    for p in NODE_CANDIDATES:
        if os.path.isfile(p):
            return p
    return "node"


def _shrink(src: str, out_dir: str) -> str:
    """豆包给的是 2048×2048 PNG（7–8MB），压成 ≤1024 的 JPEG 再发。

    压不动就原样返回——宁可大，也别丢图。
    """
    try:
        from PIL import Image
        im = Image.open(src)
        im.load()
        w, h = im.size
        side = max(w, h)
        if side > 1024:
            k = 1024.0 / side
            im = im.resize((max(1, int(w * k)), max(1, int(h * k))), Image.LANCZOS)
        if im.mode != "RGB":
            im = im.convert("RGB")
        p = os.path.splitext(src)[0] + ".jpg"
        im.save(p, "JPEG", quality=88, optimize=True)
        try:
            os.remove(src)
        except Exception:
            pass
        return p
    except Exception:
        return src


def _draw_doubao(subject: str, out_dir: str):
    """走豆包网页版：调 tools/doubao_gen.mjs，返回 (路径 | None, 错误)。"""
    import subprocess
    if not os.path.isfile(DOUBAO_CLI):
        return None, "豆包生图脚本缺失"
    os.makedirs(out_dir, exist_ok=True)
    tmp = os.path.join(out_dir, time.strftime("%Y%m%d-%H%M%S") + ".png")
    cmd = [_node(), DOUBAO_CLI, "--prompt", build_prompt(subject),
           "--out", tmp, "--size", "1024x1024", "--timeout", "180000"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=DOUBAO_WAIT)
    except subprocess.TimeoutExpired:
        return None, "豆包出图超时"
    except Exception as e:
        return None, f"调不起豆包脚本：{type(e).__name__}"
    raw = (r.stdout or b"").decode("utf-8", "ignore").strip()
    info = {}
    for line in reversed(raw.splitlines()):
        try:
            info = json.loads(line)
            break
        except Exception:
            continue
    if not info.get("ok"):
        err = str(info.get("error") or "").strip() or "豆包出图失败"
        if "user invalid" in err or "登录" in err:
            err = "豆包登录过期了"
        elif "710022004" in err or "人机校验" in err:
            # [fish-fix 2026-11-19] 这个码实测有两种成因：真·人机校验，和**登录态掉了**。
            # 11-19 17:36 那次是后者，但提示词只写了"过验证"，把人引到错方向。
            err = "豆包那边过不去（可能是登录掉了，也可能要过人机校验）：先看设置页豆包还在不在登录"
        return None, err[:120]
    p = str(info.get("path") or tmp)
    if not os.path.isfile(p):
        return None, "豆包没回图片"
    return _shrink(p, out_dir), ""


def draw(subject: str, out_dir: str, size: str = "1024x1024"):
    """出图，返回 (本地路径 | None, 错误说明)。

    2026-11-18 起两条通道：
      1) 方舟 Seedream —— 用户已删掉方舟 API，默认走不到，代码保留；
      2) 豆包网页版反代（tools/doubao_gen.mjs）—— 现在的主通道。
    """
    src = _ark()
    if not src:
        return _draw_doubao(subject, out_dir)
    key, base = src
    body = {"model": "doubao-seedream-5-0-pro", "prompt": build_prompt(subject),
            "size": size, "response_format": "url", "watermark": False}
    req = urllib.request.Request(
        base + "/images/generations",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=240)
        data = json.loads(r.read().decode("utf-8", "ignore"))
        raw = urllib.request.urlopen(data["data"][0]["url"], timeout=180).read()
    except urllib.error.HTTPError as e:
        return None, f"接口报错 {e.code}: {e.read().decode('utf-8', 'ignore')[:120]}"
    except Exception as e:
        return None, f"出图失败：{type(e).__name__}"
    os.makedirs(out_dir, exist_ok=True)
    # 返回的是 JPEG（ff d8 ff），但偶尔是 png，按字节头判
    ext = ".jpg" if raw[:3] == b"\xff\xd8\xff" else ".png"
    p = os.path.join(out_dir, time.strftime("%Y%m%d-%H%M%S") + ext)
    with open(p, "wb") as f:
        f.write(raw)
    return p, ""


# ---------------- 看图（2026-11-09 21:45） ----------------
# 她看不见自己画的东西：实测画完问她"画的是谁"，她答"没人啊"，而图上明明是个人。
# 出图后让带视觉的对话模型看一眼，把一句话描述并进她 raw 的那一行，她才认自己的画。
CAP_MODEL = "deepseek-flash"   # [fish-fix 2026-11-18] 方舟删掉后改用官方 DeepSeek 识图
CAP_PROMPT = "用一句话客观描述这张画上有什么，不要评价，40 字以内。"


def describe(path: str, model: str = CAP_MODEL) -> str:
    """看一眼画，返回一句话客观描述；失败返回空串（不抛异常）。"""
    key, base = provider()
    try:
        b64 = base64.b64encode(open(path, "rb").read()).decode()
    except Exception:
        return ""
    mime = "image/png" if str(path).lower().endswith(".png") else "image/jpeg"
    body = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": 2000,   # [fish-fix 2026-11-18] 官方 flash 识图思考重（实测 completion 1551），1600 以下容易空正文
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": CAP_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ]}],
    }
    try:
        req = urllib.request.Request(
            base + "/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        r = json.loads(urllib.request.urlopen(req, timeout=120)
                       .read().decode("utf-8", "ignore"))
        return (r["choices"][0]["message"].get("content") or "").strip()[:120]
    except Exception:
        return ""


# ---------------- 太阳/月亮核对（2026-11-11） ----------------
# 用户定案：「只有一个问题，不把太阳和月亮画错就行」。
# 11-10 21:03 实见：她写「窗外是水和月亮」，出图是深蓝夜空 + 带光芒的黄太阳 + 两朵白云。
# 13 张对照实验里只错了这 1 张 —— 不是锁定失效，是蜡笔画的「太阳图式」（圆+光芒+云）
# 偶发抢占。所以不判审美，只核事实：那个圆的是太阳还是月亮。

CELESTIAL_PROMPT = ("看这张画。画里天上那个圆的东西是太阳还是月亮？"
                    "只回答四个词之一：太阳、月亮、都有、都没有。不要解释。")

_WANT_MOON = ("月亮", "满月", "月牙", "夜空", "夜里", "星星", "晚上")
_WANT_SUN = ("太阳", "白天", "日出", "晴天", "阳光", "正午")


def celestial(path: str, model: str = CAP_MODEL) -> str:
    """返回 '太阳'/'月亮'/'都有'/'都没有'/''（判不出来给空串）。"""
    key, base = provider()
    try:
        b64 = base64.b64encode(open(path, "rb").read()).decode()
    except Exception:
        return ""
    mime = "image/png" if str(path).lower().endswith(".png") else "image/jpeg"
    body = {
        "model": model,
        "temperature": 0.0,
        "max_tokens": 1600,   # 官方 flash 判日月同样吃思考，给足预算
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": CELESTIAL_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ]}],
    }
    try:
        req = urllib.request.Request(
            base + "/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        r = json.loads(urllib.request.urlopen(req, timeout=120)
                       .read().decode("utf-8", "ignore"))
        ans = (r["choices"][0]["message"].get("content") or "").strip()
        # 注意顺序：「都没有」里含「都有」，必须先判它
        for k in ("都没有", "都有", "太阳", "月亮"):
            if k in ans:
                return k
        return ""
    except Exception:
        return ""


def draw_checked(subject: str, out_dir: str, size: str = "1024x1024", max_retry: int = 1):
    """出图并核对太阳/月亮，对不上就重画一次。返回 (路径|None, 错误, 备注)。"""
    path, err = draw(subject, out_dir, size)
    if err or not path:
        return path, err, ""
    s = subject or ""
    want_moon = any(w in s for w in _WANT_MOON)
    want_sun = any(w in s for w in _WANT_SUN)
    note = ""
    if want_moon == want_sun:      # 没提、或两样都提：不判
        return path, "", note
    for i in range(max(0, int(max_retry))):
        got = celestial(path)
        bad = (want_moon and got in ("太阳", "都有")) or \
              (want_sun and got in ("月亮", "都有"))
        if not bad:
            break
        note = f"第{i + 1}张判成{got}，重画"
        try:
            os.remove(path)
        except Exception:
            pass
        path, err = draw(subject, out_dir, size)
        if err or not path:
            return None, err, note
    return path, "", note


# ---------------- 冷却与每日上限 ----------------

def _load(path: str) -> dict:
    if os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception:
            pass
    return {}


def check_quota(state_path: str):
    """返回 (能不能画, 说明)。"""
    today = time.strftime("%Y-%m-%d")
    st = _load(state_path)
    if st.get("day") != today:
        st = {"day": today, "count": 0, "last": 0}
    last = float(st.get("last") or 0)
    if time.time() - last < COOLDOWN_SEC:
        return False, f"刚画过，{int(COOLDOWN_SEC - (time.time() - last))} 秒后再来"
    if int(st.get("count") or 0) >= MAX_PER_DAY:
        return False, "今天画够了"
    return True, ""


def mark_quota(state_path: str):
    today = time.strftime("%Y-%m-%d")
    st = _load(state_path)
    if st.get("day") != today:
        st = {"day": today, "count": 0, "last": 0}
    st["count"] = int(st.get("count") or 0) + 1
    st["last"] = time.time()
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)


# ---------------- L1：相册索引 ----------------

def record_draw(index_path: str, file_path: str, desc: str) -> str:
    """记一条：id -> 文件 + 描述 + 时间。返回编号（如 "0007"）。"""
    data = _load(index_path)
    items = data.get("items") or []
    seq = int(data.get("seq") or 0) + 1
    did = "%04d" % seq
    items.append({"id": did, "file": os.path.basename(file_path),
                  "desc": (desc or "").strip()[:200],
                  "ts": int(time.time())})
    data.update({"seq": seq, "items": items})
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    return did


def prune(out_dir: str, index_path: str, keep: int = KEEP_IMAGES):
    """只留最近 keep 张，同步从索引里摘掉。"""
    try:
        data = _load(index_path)
        items = data.get("items") or []
        if len(items) <= keep:
            return
        drop, keep_items = items[:-keep], items[-keep:]
        for it in drop:
            p = os.path.join(out_dir, it.get("file") or "")
            try:
                if it.get("file") and os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        data["items"] = keep_items
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def find(index_path: str, did: str):
    """按编号查一张（留给以后的 see_image 用）。"""
    for it in (_load(index_path).get("items") or []):
        if it.get("id") == did:
            return it
    return None


# —— 给模型看的工具说明（main.py 里放 docstring）——
TOOL_DOC = """画一张画发给主人。剪影平涂：整张只有两种颜色，脸留白不画五官。
想画什么都可以，从你记得的东西里挑：今天真的发生过的事、你记得的人（主人、姐姐）、
你答应过或记挂的事、你现在的心情（别写成心情说明，画成画面）、
你没有的东西（人类的饭、真的走出来见他）。
画自己的时候：银白长发、浅色龙角、白色长裙。
只画你确定的事；记不清的部分留白，别编。画完再说话。
subject：画面内容，用你自己的话，一到三句，写清楚画什么、在哪、在做什么。"""
