# -*- coding: utf-8 -*-
"""搜索与阅读（2026-11-10 00:50）。

实测结论（都是当天真跑出来的，别凭印象改）：
- cn.bing.com 加 `&format=rss` 返回干净 XML，3.5KB / 0.3 秒，多次请求不封 → 主搜索源
- 百度直接验证码；搜狗第一次极好、第二次就被 JS 挑战页挡掉 → 都不用
- anysearch 的 search 对中文问句很差（鲸鱼 → 法院判决书），对英文尚可 → 只当补充
- anysearch 的 extract 在它自己的服务器上抓，**能读维基百科**（本机 DNS 被墙）
  → 「读」这一侧统一走它

设计：搜可以不准，读必须深。标题差一点没关系，正文才是她真正读的东西。
"""
import hashlib
import json
import os
import re
import time
import tempfile
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36 Edg/157.0.0.0")
ANYSEARCH_CFG = os.environ.get("ANYSEARCH_CONFIG") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "config",
    "astrbot_plugin_anysearch_config.json")
CACHE_DIR = os.environ.get("FISH_SEARCH_CACHE") or os.path.join(
    tempfile.gettempdir(), "fish_search_cache")
CACHE_TTL = 24 * 3600

STATS = {"search": 0, "read": 0, "fail": 0}


def _cache_path(key: str) -> str:
    h = hashlib.md5(key.encode("utf-8")).hexdigest()[:16]
    return os.path.join(CACHE_DIR, h + ".json")


def _cache_get(key: str):
    try:
        p = _cache_path(key)
        if time.time() - os.path.getmtime(p) > CACHE_TTL:
            return None
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _cache_set(key: str, val):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(_cache_path(key), "w", encoding="utf-8") as f:
            json.dump(val, f, ensure_ascii=False)
    except Exception:
        pass


def _get(url: str, timeout=20) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6"})
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")


def _anysearch(tool: str, args: dict, timeout=25) -> str:
    key = ""
    try:
        with open(ANYSEARCH_CFG, encoding="utf-8-sig") as f:
            key = str(json.load(f).get("api_key") or "")
    except Exception:
        pass
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": tool, "arguments": args}}
    hdr = {"Content-Type": "application/json"}
    if key:
        hdr["Authorization"] = "Bearer " + key
    req = urllib.request.Request("https://api.anysearch.com/mcp",
                                 data=json.dumps(payload, ensure_ascii=False).encode(),
                                 headers=hdr)
    j = json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore"))
    items = ((j.get("result") or {}).get("content") or [])
    return "\n".join(str(i.get("text") or "") for i in items
                     if isinstance(i, dict) and i.get("type") == "text")


def _strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    for a, b in (("&amp;", "&"), ("&quot;", '"'), ("&lt;", "<"), ("&gt;", ">"),
                 ("&ensp;", " "), ("&nbsp;", " "), ("&#0183;", "·")):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


def bing_rss(q: str, n=6, timeout=20):
    """Bing 的 RSS 输出：最稳的一条。返回 [{title,url,snippet}]"""
    try:
        raw = _get("https://cn.bing.com/search?q=%s&format=rss" % urllib.parse.quote(q),
                   timeout=timeout)
    except Exception as e:
        STATS["fail"] += 1
        return []
    out = []
    for it in re.findall(r"<item>.*?</item>", raw, re.S)[:n]:
        t = re.search(r"<title>(.*?)</title>", it, re.S)
        l = re.search(r"<link>(.*?)</link>", it, re.S)
        d = re.search(r"<description>(.*?)</description>", it, re.S)
        title = _strip_tags(t.group(1)) if t else ""
        url = (l.group(1).strip() if l else "")
        if title and url:
            out.append({"title": title, "url": url,
                        "snippet": _strip_tags(d.group(1))[:160] if d else ""})
    return out


def is_ascii_q(q: str) -> bool:
    return bool(q) and len(re.findall(r"[A-Za-z]", q)) >= max(3, len(q) // 3)


def search(q: str, n=6, use_anysearch=None):
    """搜索：Bing RSS 为主；英文或结果太少时补 anysearch。带 24h 缓存。"""
    q = (q or "").strip()
    if not q:
        return []
    ck = "search|" + q
    hit = _cache_get(ck)
    if hit is not None:
        return hit
    STATS["search"] += 1
    # [实测 2026-11-10] 英文查询走 Bing 会返回一堆广告（查鲸歌返回 ANSYS 软件），
    # 所以英文优先 anysearch，中文优先 Bing。
    ascii_q = is_ascii_q(q)
    res = [] if ascii_q else bing_rss(q, n=n)
    need_as = (use_anysearch if use_anysearch is not None else
               (ascii_q or len(res) < 3))
    if ascii_q:
        try:
            txt = _anysearch("search", {"query": q, "max_results": n})
            for m in re.finditer(r"### \d+\. (.+?)\n.*?\*\*URL\*\*: (\S+)", txt, re.S):
                res.append({"title": _strip_tags(m.group(1))[:120],
                            "url": m.group(2).strip(), "snippet": ""})
        except Exception:
            STATS["fail"] += 1
        if len(res) < 3:
            res += bing_rss(q, n=n)
    if need_as:
        try:
            txt = _anysearch("search", {"query": q, "max_results": n})
            for m in re.finditer(r"### \d+\. (.+?)\n.*?\*\*URL\*\*: (\S+)", txt, re.S):
                res.append({"title": _strip_tags(m.group(1))[:120],
                            "url": m.group(2).strip(), "snippet": ""})
        except Exception:
            STATS["fail"] += 1
    seen, uniq = set(), []
    for r in res:
        u = r.get("url") or ""
        if not u or u in seen:
            continue
        if _bad_host(u):
            continue
        seen.add(u)
        uniq.append(r)
    uniq = uniq[:n]
    _cache_set(ck, uniq)
    return uniq


# [fish-search 2026-11-11] 字典/词典站黑名单。
# 起因：她独处查「半醒半睡 睡眠 动物 研究」，Bing 把汉字字典页排上来，读完把「"半"字字形从牛从八」
# 当成事实记进 learned.json——第二天她会接着研究字源。这类站在科普题上永远是噪声，读之前就剔掉。
_BAD_HOSTS = (
    "zdic.net", "zidian", "chazidian", "hanyu.baidu.com", "cidian", "gushici",
    "hgcha.com", "dict.cn", "youdao.com", "iciba.com", "ciku", "bushou",
    "baike.baidu.com", "baike.com", "baike.sogou.com", "wenwen.sogou.com",
)


def _bad_host(url: str) -> bool:
    u = (url or "").lower()
    return any(h in u for h in _BAD_HOSTS)


def _wiki_summary(url: str) -> str:
    """维基百科：改走 REST summary 接口，拿到干净摘要（正文页全是导航垃圾）。

    [fish-search 2026-11-11] URL 可能是 /zh-hans/鲸歌、/zh-cn/鲸歌、/en/Whale_song，
    语言前缀卡在域名和 wiki/ 之间。原来的正则只认 /wiki/，遇到 /zh-hans/ 就匹配不上，
    于是静默失败、fallback 到整页正文——前 1200 字全是「跳转到内容」「维基百科，自由的
    百科全书」和 File: 链接。2026-11-11 四臂评测里 A 臂 3/10 就是被这个吃掉的。
    """
    m = re.search(r"https?://([a-z.]+wikipedia\.org)/(?:[a-z]{2,3}(?:-[a-z]{2,8})?/)?wiki/(.+)$",
                  url or "")
    if not m:
        return ""
    host, title = m.group(1), m.group(2)
    title = title.split("#")[0].split("?")[0]
    try:
        raw = _anysearch("extract", {"url": f"https://{host}/api/rest_v1/page/summary/{title}"})
        outer = json.loads(raw)
        inner = json.loads(outer.get("content") or "{}")
        ex = str(inner.get("extract") or "")
        if ex:
            return f"（维基百科·{inner.get('title') or title}）{ex}"
    except Exception:
        pass
    return ""


def read(url: str, limit=1800):
    """读一篇正文。维基走 summary 接口；其它先 anysearch extract，失败再自己抓。"""
    ck = "read|" + url
    hit = _cache_get(ck)
    if hit is not None:
        return hit
    STATS["read"] += 1
    text = _wiki_summary(url) if "wikipedia.org" in url else ""
    if not text:
        try:
            raw = _anysearch("extract", {"url": url})
            try:
                j = json.loads(raw)
                text = str(j.get("content") or j.get("text") or "")
            except Exception:
                if raw and not raw.startswith("extract_failed"):
                    text = raw
        except Exception:
            STATS["fail"] += 1
    if not text:
        try:
            html = _get(url, timeout=20)
            html = re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", html)
            text = _strip_tags(html)
        except Exception:
            STATS["fail"] += 1
            return ""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)          # 图片
    text = re.sub(r"\[\]\([^)]*\)", "", text)                  # 维基的空链接 []()
    text = re.sub(r"\[([^\]]{1,60})\]\([^)]*\)", r"\1", text)  # 链接保留文字
    text = re.sub(r"\[\d+\]", "", text)
    # [fish-search 2026-11-11] 去掉页面骨架。两轮评测下来，真正吃掉正文的是两样：
    #   ① 维基的维护模板：「致使用者：请搜索一下条目的标题」「本条目存在以下问题」
    #      「维基百科所有的内容都应该可供查证」——都是长句，躲得过短行过滤
    #   ② 导航行：「所长信箱 | 信息公开 | 内部办公 | …」——一行几十个竖线
    _junk = re.compile(
        r"(致使用者|若您熟悉|维基百科所有|请协助|请按照校对|本条目存在|此条目|"
        r"关于与|跳转到内容|自由的百科全书|判定指引|讨论页|编辑摘要|"
        r"^\s*(?:编辑|帮助|分类|目录|导航|跳到)\b|"
        r"^\d{4}年\d{1,2}月\d{1,2}日\s*\S*频道最新文章)")
    _out = []
    for _ln in text.splitlines():
        ln = _ln.strip()
        if not ln:
            continue
        _probe = ln.replace("*", "").replace("#", "").strip()
        if _junk.search(_probe):
            continue
        if ln.count("|") >= 3:                       # 导航行
            segs = [x.strip() for x in ln.split("|") if x.strip()]
            if len(segs) >= 3 and sum(len(s) for s in segs) / len(segs) < 12:
                continue
        _out.append(ln)
    # 开头的状态标记（「濒危（IUCN 3.1）」）、碎行、无句子的标题，一律跳掉，
    # 从第一个像句子的地方开始——她的正文预算只有 1200 字，不能喂给页头。
    while _out and len(_out[0]) < 60 and "。" not in _out[0]:
        _out.pop(0)
    text = "\n".join(_out) if _out else text
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    out = text[:limit]
    _cache_set(ck, out)
    return out


def _rerank(q: str, rs: list) -> list:
    """[fish-search 2026-11-11] 用 embedding 重排候选。

    四臂评测（_work\\20261111\\search_eval.md，10 条真实 query、模型判「能不能回答」）：
      基线 0/10 ｜ A（维基偏好）3/10 ｜ B（重排）1/10 ｜ A+B **6/10**
    B 单独有害——短 query 的余弦会把汉语词典页、文言字典排到第一。所以这里先按
    「标题/摘要里含不含 query 实词」分组，只对含实词的那组做重排，其余挂在后面。
    """
    try:
        terms = [w for w in re.split(r"[\s，。、？?！!的]+", q or "") if len(w) >= 2]
        hit, miss = [], []
        for r in rs or []:
            blob = str(r.get("title", "")) + " " + str(r.get("snippet", ""))
            (hit if any(t in blob for t in terms) else miss).append(r)
        if not hit:
            return rs
        if len(hit) > 1:
            try:
                from . import retrieval as _R
            except Exception:
                import retrieval as _R
            texts = [f"{r['title']}。{r.get('snippet', '')}" for r in hit]
            vs = _R.embed([q] + texts)
            qv, mvs = vs[0], vs[1:]
            scored = sorted(zip(mvs, hit),
                            key=lambda x: -sum(a * b for a, b in zip(qv, x[0])))
            hit = [r for _, r in scored]
        return hit + miss
    except Exception:
        return rs


def look_up(q: str, read_top=True, titles=5, body_limit=1500) -> str:
    """给她用的一站式：搜 + 读正文。返回可以直接塞进 prompt 的文本。

    [fish-search 2026-11-11] 先查维基（site: 限定，不足 2 条再走通用），再向量重排，
    再读前 3 条里第一条能读的。这是四臂评测里唯一及格的组合（6/10，基线 0/10）。
    """
    rs = [r for r in search("site:zh.wikipedia.org " + q, n=titles)
          if "wikipedia.org" in (r.get("url") or "")]
    if len(rs) < 2:
        rs = search(q, n=titles)
    # [fish-search 2026-11-11] 黑名单把结果清空了：退一步只用前两个词再搜一次。
    # 词袋越长越容易撞上字典/百科页；「半醒半睡 睡眠 动物 研究」就是四个词一起把噪声顶上来的。
    if not rs:
        _ws = [w for w in re.split(r"\s+", (q or "").strip()) if w]
        if len(_ws) > 2:
            rs = search(" ".join(_ws[:2]), n=titles)
    if not rs:
        return "（这次没搜到东西）"
    rs = _rerank(q, rs)
    lines = []
    for i, r in enumerate(rs, 1):
        lines.append(f"{i}. {r['title']}｜{r['snippet'][:70]}")
    head = "搜到这些：\n" + "\n".join(lines)
    if not read_top:
        return head
    skip = ("baike.baidu.com", "zhihu.com", "zhidao.baidu.com", "douban.com")
    t0 = time.time()
    for i, r in enumerate(rs[:3], 1):
        if time.time() - t0 > 40:          # 整轮最多耗 40 秒，别把独处卡死（实测有跑 188 秒的）
            break
        if any(k in (r.get("url") or "") for k in skip):
            continue                      # 这几个抓不到正文（实测），跳过
        body = read(r["url"], limit=body_limit)
        if body and len(body) > 200:
            return head + f"\n\n〔第 {i} 条正文：{r['title'][:40]}〕\n{body}"
    return head
