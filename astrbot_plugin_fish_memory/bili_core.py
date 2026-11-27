"""[fish-bili 2026-11-15] 她会刷 B 站：先给候选、她自己挑、再抽帧看一眼。

用户定案（2026-11-15）：
  · 不限分区到全站，只放「科技 / 科学科普 / 动物 / 知识 / 游戏」；
  · 不接主人的账号 cookie（推荐流会把她的口味变成主人的口味）；
  · 一天最多一条，标题+UP+时长给她看，她回编号才下。

三个函数：
  search(kw)    搜视频，按分区过滤，返回候选（带 bvid/标题/UP/时长/分区）
  watch(bvid)   下载（只留 480p 以下）→ 均匀抽 6 帧 → 拼 3x2 宫格
  describe(img) 让视觉模型用三句话说说这段片子大概是什么

失败一律返回空/None，不抛异常——独处不能因为刷 B 站挂掉。
"""
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

FFMPEG = os.environ.get("FFMPEG_BIN") or "ffmpeg"
FFPROBE = os.environ.get("FFPROBE_BIN") or "ffprobe"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
REFERER = "https://www.bilibili.com/"

# 用户拍板的分区：科技/科学科普/动物/知识/游戏，2026-11-17 加「生活」及其子区。
# 搜索结果的 typename 里包含这些字样才收（子串匹配，不用记 rid）。
ALLOWED = ("科技", "科学", "科普", "动物", "知识", "游戏", "人文", "历史", "自然",
           "生活", "日常", "手工", "绘画", "美食", "家居", "运动", "时尚", "汽车", "萌宠")

_cands_cache = {}


def _log(msg: str):
    try:
        from astrbot.api import logger
        logger.info(msg)
    except Exception:
        pass


def _get(url: str, params: dict | None = None, timeout: int = 8, retry: int = 1) -> dict:
    """带一个假 buvid 的 GET。B 站会偶发 412 风控，这里重试一次。"""
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(retry + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Referer": REFERER,
                "Accept": "application/json, text/plain, */*",
                # 没有 buvid 时 B 站更容易回 412；这个值只用于过风控，不是登录凭据
                "Cookie": "buvid3=%s" % (
                    "".join(f"{int(time.time() * 1000) % 1000:03d}" for _ in range(3))
                    + "infoc"),
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))
        except Exception as e:
            last = e
            if attempt < retry:
                time.sleep(1.5)
    raise last


def search(keyword: str, limit: int = 6) -> list:
    """按关键词搜，只留允许的分区。返回 [{bvid,title,up,dur,play,tname}]。"""
    kw = (keyword or "").strip()[:40]
    if not kw:
        return []
    hit = _cands_cache.get(kw)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    out = []
    _rejected = []
    try:
        j = _get("https://api.bilibili.com/x/web-interface/search/type",
                 {"search_type": "video", "keyword": kw, "page": 1, "page_size": 30})
        for v in ((j.get("data") or {}).get("result") or []):
            name = str(v.get("typename") or v.get("tname") or "")
            if not any(a in name for a in ALLOWED):
                if len(_rejected) < 6:
                    _rejected.append(name or "?")
                continue
            title = re.sub(r"<[^>]+>", "", str(v.get("title") or "")).strip()
            bvid = str(v.get("bvid") or "")
            dur = str(v.get("duration") or "")[:8]
            if not title or not bvid:
                continue
            # 太长的片子不给（下不动，她也看不完）
            if _secs(dur) > 1200:
                continue
            out.append({
                "bvid": bvid,
                "title": title[:60],
                "up": str(v.get("author") or "")[:20],
                "dur": dur,
                "play": v.get("play"),
                "tname": name[:12],
            })
            if len(out) >= limit:
                break
    except Exception as e:
        _log(f"[fish-bili] 搜索失败: {e}")
    if not out and _rejected:
        # 搜空时把被挡掉的分区记一笔，方便回头判断是不是白名单太窄
        _log(f"[fish-bili] 「{kw}」全被分区挡掉，见到的是：{_rejected}")
    _cands_cache[kw] = (time.time(), out)
    return out


def _secs(txt: str) -> int:
    """'12:34' / '1:02:03' → 秒。"""
    try:
        parts = [int(x) for x in str(txt or "").split(":")]
    except Exception:
        return 0
    s = 0
    for p in parts:
        s = s * 60 + p
    return s


def _run(cmd: list, timeout: int = 120) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return p.stdout.decode("utf-8", "ignore")
    except Exception as e:
        _log(f"[fish-bili] 命令失败 {cmd[0]}: {e}")
        return ""


def watch(bvid: str, want_seconds: int = 120, cells: int = 6, out_dir: str = "") -> dict:
    """下载并抽帧拼宫格。返回 {sheet, seconds, err}。"""
    bvid = str(bvid or "").strip()
    if not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid):
        return {"sheet": "", "seconds": 0, "err": "bvid 不对"}
    wd = tempfile.mkdtemp(prefix="bili_")
    try:
        import yt_dlp
        opts = {
            "outtmpl": os.path.join(wd, "v.%(ext)s"),
            # B 站的视频流和音轨是分开的（30032 只有画面、30232 只有声音），必须 merge
            "format": "bestvideo[height<=480]+bestaudio/best[height<=480]/mp4/best",
            "ffmpeg_location": os.path.dirname(FFMPEG),
            "quiet": True, "no_warnings": True, "noplaylist": True,
            "merge_output_format": "mp4",
            # 只下前 want_seconds 秒，免得一条一小时的长片把硬盘和时间吃光
            "download_ranges": lambda info, ydl: [{"start_time": 0,
                                                   "end_time": int(want_seconds)}],
            "force_keyframes_at_cuts": True,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"https://www.bilibili.com/video/{bvid}", download=True)
                path = ydl.prepare_filename(info)
        except Exception:
            # 老版本 yt-dlp 不认 download_ranges：退回整条下载
            opts.pop("download_ranges", None)
            opts.pop("force_keyframes_at_cuts", None)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"https://www.bilibili.com/video/{bvid}", download=True)
                path = ydl.prepare_filename(info)
        if not os.path.exists(path):
            cand = [f for f in os.listdir(wd) if f.startswith("v.")]
            path = os.path.join(wd, cand[0]) if cand else ""
        if not path or not os.path.exists(path):
            return {"sheet": "", "seconds": 0, "err": "没下下来"}
        dur = 0.0
        try:
            out = _run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", path], timeout=30)
            dur = float(out.strip() or 0)
        except Exception:
            dur = 0.0
        if dur <= 0:
            return {"sheet": "", "seconds": 0, "err": "读不出时长"}
        # 只抽前 want_seconds 秒的帧：太长的片子她也没工夫看
        span = min(dur, float(want_seconds))
        n = max(1, min(int(cells), int(span // 4) or 1))
        times = [span * (i + 0.5) / n for i in range(n)]
        frames = []
        for i, t in enumerate(times):
            fp = os.path.join(wd, f"f{i}.jpg")
            _run([FFMPEG, "-v", "error", "-ss", f"{t:.2f}", "-i", path,
                  "-frames:v", "1", "-vf", "scale=320:-2", fp], timeout=40)
            if os.path.exists(fp):
                frames.append(fp)
        if not frames:
            return {"sheet": "", "seconds": int(dur), "err": "抽帧失败"}
        sheet = _tile(frames, wd, out_dir)
        return {"sheet": sheet, "seconds": int(dur), "err": ""}
    except Exception as e:
        return {"sheet": "", "seconds": 0, "err": f"{type(e).__name__}: {e}"[:80]}
    finally:
        # 只留宫格图；视频和碎帧都删掉
        try:
            keep = locals().get("sheet") or ""
            for f in os.listdir(wd):
                p = os.path.join(wd, f)
                if keep and os.path.abspath(p) == os.path.abspath(keep):
                    continue
                if os.path.isfile(p):
                    os.remove(p)
        except Exception:
            pass
        try:
            shutil.rmtree(wd, ignore_errors=True)
        except Exception:
            pass


def _tile(frames: list, wd: str, out_dir: str = "") -> str:
    """3 列拼图；没有 PIL 就退回第一帧。"""
    try:
        from PIL import Image
    except Exception:
        return frames[0]
    ims = []
    for f in frames:
        try:
            ims.append(Image.open(f).convert("RGB"))
        except Exception:
            continue
    if not ims:
        return frames[0]
    cw = max(i.width for i in ims)
    ch = max(i.height for i in ims)
    cols = 3
    rows = (len(ims) + cols - 1) // cols
    canvas = Image.new("RGB", (cw * cols, ch * rows), (30, 30, 30))
    for k, im in enumerate(ims):
        canvas.paste(im, ((k % cols) * cw, (k // cols) * ch))
    d = out_dir or wd
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"bili-{time.strftime('%Y%m%d-%H%M%S')}.jpg")
    canvas.save(p, quality=80)
    return p


def describe(path: str, model: str = "") -> str:
    """看一眼宫格，三句话说说这段片子大概在讲什么。失败返回空串。"""
    try:
        import draw_core as _dc
        key, base = _dc.provider()
        model = model or getattr(_dc, "CAP_MODEL", "deepseek-flash")
    except Exception:
        return ""
    if not key or not path or not os.path.exists(path):
        return ""
    try:
        b64 = base64.b64encode(open(path, "rb").read()).decode()
    except Exception:
        return ""
    prompt = ("这是一个人看视频时随手截下的几帧拼成的图。用三句话说说：画面里有什么、"
              "这件事大概在讲什么。不要评价，不要列点，120 字以内。")
    body = {
        "model": model,
        "temperature": 0.3,
        # [fish-bili 2026-11-16] 800 会被思考吃光（11-16 11:46 实测「空正文」）。
        # 交接里那条老坑：看图给 200 全被吃、给 1000 才出。六帧宫格描述更长，抬到 1600。
        "max_tokens": 1600,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]}],
    }

    def _post(payload: dict):
        req = urllib.request.Request(
            base + "/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))

    last = None
    # 第一次正常调；空正文就关掉思考再要一次（关思考时正文正常，是交接 7.5 的降级手段）
    plans = [dict(body), dict(body, max_tokens=2500, thinking={"type": "disabled"})]
    for payload in plans:
        try:
            j = _post(payload)
            msg = j["choices"][0]["message"]
            txt = str(msg.get("content") or "").strip()
            if txt:
                return txt[:200]
            last = "空正文（思考吃光了？）"
        except Exception as e:
            last = e
        time.sleep(1)
    _log(f"[fish-bili] 看图失败: {last}")
    return ""
