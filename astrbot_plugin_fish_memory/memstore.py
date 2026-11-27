"""结构化记忆存储：借 surfcore 记忆卡的思路，做轻量版。

核心规则：
- 每条记忆有 importance（1-100）、clarity（清晰/清楚/模糊/只剩印象）
- 每天最多降一档清晰度；被再次提到会回升
- 主人相关且重要度 >= min_owner_importance 的记忆永不遗忘
- 卡片容量有 80/90/100% 告警，满了先压缩最不重要、最久没提的
- 注入时按 置顶 > 重要度 > 最近 排序，装进预算内
"""

import difflib
import json
import os
import re
import time
from datetime import date, datetime

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("fish_memory")


def _event_day(m: dict) -> str | None:
    """这张卡说的那件事发生在哪天（YYYY-MM-DD）。

    优先用 source 里的日期（diary:2026-10-28 就是写那天日记的日子）；
    没有就用 created。migrate 迁移来的卡日期是迁移时刻，不可信 → 返回 None。
    """
    src = str(m.get("source") or "")
    if src.startswith("migrate"):
        return None
    mo = re.search(r"(\d{4}-\d{2}-\d{2})", src)
    if mo:
        return mo.group(1)
    try:
        return time.strftime("%Y-%m-%d", time.localtime(int(m.get("created"))))
    except Exception:
        return None


def _age_tag(m: dict) -> str:
    """「（3 天前）」。日期不可信就不加，宁缺勿错。"""
    day = _event_day(m)
    if not day:
        return ""
    try:
        d = datetime.strptime(day, "%Y-%m-%d").date()
    except Exception:
        return ""
    delta = (date.today() - d).days
    if delta <= 0:
        return "（今天）"
    if delta == 1:
        return "（昨天）"
    if delta < 30:
        return f"（{delta} 天前）"
    return "（很久以前）"


CLARITIES = ["vivid", "clear", "fuzzy", "trace"]
CLARITY_CN = {
    "vivid": "清晰",
    "clear": "清楚",
    "fuzzy": "模糊",
    "trace": "只剩印象",
}

EMOTION_STRONG = ("喜欢", "想念", "难过", "委屈", "哭", "抱抱", "对不起", "谢谢", "陪", "永远", "恨", "生气")
EMOTION_WEAK = ("累", "烦", "开心", "高兴", "怕", "担心", "舍不得")
PROMISE_WORDS = ("答应", "约定", "说好", "以后", "下次", "记得", "不要忘", "发誓", "保证")
CONFLICT_WORDS = ("吵架", "生气", "道歉", "对不起", "和好", "误会", "冷战")
FIRST_WORDS = ("第一次", "初次", "头一次", "最早", "刚认识", "纪念日")
COMMAND_JUNK = ("清理历史图片", "今日日记", "重写自述", "翻旧账", "记忆管家", "/reset", "/看屏幕")

# ---------------- 打分常量（2.0 定稿，2026-10-30） ----------------
# 打分只表达「这条该记多久」。标签头不参与，只有【常驻】/【普通】这一个二元信号。
SCORE_BASE = 40        # 一条真实日记条目的底分
SCORE_CORE = 25        # 【常驻】加成
SCORE_RULE = 22        # 规则/约定措辞
SCORE_SETTLE = 8       # 当场落定
SCORE_MILE = 12        # 里程碑（第一次/初次…）
SCORE_CONFLICT = 10    # 冲突
SCORE_DETAIL = 5       # 正文 >= 25 字
SCORE_EMO_CAP = 18
SCORE_MIN, SCORE_MAX = 40, 100
# 3.0 精细化分层：类别权重 + 常驻加成 + 正文微调（2026-11-03）
SCORE_CAT = {"约定": 30, "承诺": 26, "信箱": 12, "账": 24, "事": 2, "物": 2, "视频": 2, "玩笑": 2, "饭": 2, "错": 2}
SCORE_CAT_DEFAULT = 2
SCORE_CORE_BONUS = 15   # 常驻但非约定/承诺
SCORE_MICRO_CAP = 8
BIRTH_STEP = 3         # 每被提到一次，出生分 +3（10-31 由 +1 调回 +3）

# [fish-decay 2026-11-11] 分档衰减。
# 起因：四日备份逐日对比时发现「约定:生日」（出生 76、提过 2 次）被 -7/天删掉了——
# 那是一年才该提一次的东西，用"几天没提"去判它，方向就错了。
# 长期档不按天衰减，只等被结清（close_card）退场；再给个数量上限防膨胀。
LONG_KINDS = ("约定", "承诺", "账")
LONG_MAX = 24          # 长期档最多 24 张，超了最久没提的降回普通档
LONG_DECAY = 2         # 长期档：慢，但不是永生（用户 2026-11-11 定：长期 -2/天）
KIND_DECAY = {"错": 4, "信箱": 4}   # 中档：-4/天（同日定）


def _kind_of(m: dict) -> str:
    """取卡的类别（key 冒号/竖线前面那段）。"""
    return str(m.get("key") or "").split(":")[0].split("｜")[0].strip()


# [fish-patch 2026-11-11] 「对象」退化拦截。
# 日记第 8 条早就要求对象必须是能检索的名词，但安静的日子她还是会写【普通|事:十点】、
# 【普通|事:下午】——这样的卡互相覆盖，检索也搜不到。代码这层做最后一道兜底：
# 对象是时间词/泛称时，改用正文前 8 个字，至少能让这张卡唯一、能被内容找到。
_GENERIC_OBJ = {
    "他", "她", "主人", "我", "我们", "你", "今天", "明天", "昨天", "前天", "后天",
    "早上", "上午", "中午", "下午", "傍晚", "晚上", "今晚", "昨晚", "夜里", "深夜",
    "凌晨", "半夜", "白天", "现在", "刚才", "刚刚", "最近", "这几天", "事情", "事",
    "日常", "心情", "时间", "半天", "一下午", "一上午", "一晚上", "一会", "一会儿",
}
_TIME_OBJ = re.compile(
    r"^(?:[一二三四五六七八九十百零两\d]{1,3}点(?:多|半|钟|左右)?"
    r"|[上下前后]午|凌晨|傍晚|夜里|深夜"
    r"|\d{1,2}[:：]\d{2})$")


def _fix_obj(obj: str, body: str) -> str:
    o = str(obj or "").strip()
    if not o:
        return o
    if o in _GENERIC_OBJ or _TIME_OBJ.match(o):
        short = re.sub(r"[，。、；：！？\s「」『』\"'（）()]", "", body or "")[:8]
        return short or "琐事"
    return o


def _fix_key(key: str, body: str) -> str:
    if not key or (":" not in key and "：" not in key):
        return key
    head, obj = re.split(r"[:：]", key, 1)
    return f"{head}:{_fix_obj(obj, body)}"

# 全部用短语，不用裸词 —— 「我说好」不算承诺，「说好了」才算
RULE_PHRASES = (
    "约定", "说好了", "说好的", "说好要", "答应了", "答应过", "保证过",
    "不许反悔", "不许你再", "不许再", "别再", "不要再", "发誓",
    "定下了", "定下来", "记住这个", "别忘了", "不要忘了",
)
SETTLE_PHRASES = (
    "说定了", "定下了", "我应了", "应下了", "我说好", "我答应",
    "那就这样", "等哪天", "到时候", "我记着", "就这么定", "定下", "听他的",
)
EMO_PHRASES = (
    "哭", "哭了", "怕", "难过", "开心", "惊喜", "生气", "心疼",
    "舍不得", "喜欢你", "想你", "舒服",
)


TAG_HEADS = (
    "约定", "承诺", "答应", "说好",
    "人", "名字", "称呼", "关系",
    "板子", "事", "物", "物件", "东西", "饭",
    # [fix 2026-11-06] 补上这三个合法类别，否则「玩笑:抢名字」会掉进兜底分支，
    # 在尾巴里匹配到「名字」而把 key 归一成「名字」（11-05 日记实见）。
    "视频", "玩笑", "信箱", "账", "错",
)

# [fish-patch 2026-11-03] 吃饭类归一：晚饭:面 / 午饭:面 / 饭:面 都算同一个头，
# 避免「晚饭:面」和「饭:面」各建一张卡。
KEY_ALIASES = {"晚饭": "饭", "午饭": "饭", "早饭": "饭", "饭": "饭"}

# 单值类别：同一时间只会有一个值，标签尾巴可以丢（名字:X == 名字）
SINGLE_VALUE_HEADS = ("名字", "称呼", "关系")


def normalize_key(key) -> str | None:
    """把「称呼主人」「称呼:主人」「早睡约定」这类写法归一成同一个 key。"""
    k = (key or "").strip()
    if not k:
        return None
    parts = re.split(r"[:：|]", k, maxsplit=1)
    head = parts[0].strip()
    head = KEY_ALIASES.get(head, head)
    tail = parts[1].strip() if len(parts) > 1 else ""
    for h in TAG_HEADS:
        if head.startswith(h):
            if not tail:
                tail = head[len(h):].strip()
            # 单值类别（名字/称呼/关系）不带尾巴，避免「名字」和「名字:X」分裂
            if h in SINGLE_VALUE_HEADS:
                return h
            return f"{h}:{tail}" if tail else h
    # 语序反了：早睡约定 -> 约定:早睡
    # [fix 2026-11-06] 只在「没有分隔符」的写法上做倒序匹配，否则
    # 「玩笑:抢名字」会在尾巴里命中「名字」，被错误归一成「名字」。
    if ":" not in k and "：" not in k and "|" not in k:
        for h in TAG_HEADS:
            if h in k:
                rest = (k.replace(h, "", 1)).strip(" :：|")
                if h in SINGLE_VALUE_HEADS:
                    return h
                return f"{h}:{rest}" if rest else h
    return f"{head}:{tail}" if tail else head[:8]


def _now() -> int:
    return int(time.time())


def _day(ts: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


class MemoryStore:
    def __init__(self, base_dir: str, cfg: dict):
        self.base = base_dir
        os.makedirs(self.base, exist_ok=True)
        self.path = os.path.join(self.base, "memories.json")
        self.md_path = os.path.join(self.base, "memories.md")
        self.backup_dir = os.path.join(self.base, "backup")
        os.makedirs(self.backup_dir, exist_ok=True)
        self.cfg = cfg or {}
        self.data = self._load()

    # ---------------- 配置 ----------------

    def _c(self, key, default=None):
        try:
            v = self.cfg.get(key, None)
            return default if v is None else v
        except Exception:
            return default

    @property
    def max_chars(self) -> int:
        return int(self._c("memory_max_chars", 4000) or 4000)

    @property
    def min_owner_importance(self) -> int:
        return int(self._c("min_owner_importance", 65) or 65)

    @property
    def recent_days(self) -> int:
        return int(self._c("recent_days", 7) or 7)

    @property
    def fuzzy_after_days(self) -> int:
        return int(self._c("fuzzy_after_days", 7) or 7)

    @property
    def forget_after_days(self) -> int:
        return int(self._c("forget_after_days", 60) or 60)

    @property
    def renew_days_limit(self) -> int:
        """同一件事被重复提到超过这么多天，就不再刷新保鲜时间。"""
        return int(self._c("renew_days_limit", 3) or 3)

    @property
    def decay_per_day(self) -> int:
        """每天固定扣多少分。"""
        return int(self._c("decay_per_day", 5) or 5)

    @property
    def mention_gain(self) -> int:
        """被日记重新提到时回补多少分。"""
        return int(self._c("mention_gain", 25) or 25)

    @property
    def forget_floor(self) -> int:
        """低于这个分数就遗忘。"""
        return int(self._c("forget_floor", 15) or 15)

    @property
    def immune_birth(self) -> int:
        """出生分达到这个值的记忆永不衰减。"""
        return int(self._c("immune_birth", 90) or 90)

    # ---------------- 存取 ----------------

    def _load(self) -> dict:
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict) and isinstance(d.get("memories"), list):
                    return d
            except Exception:
                pass
        return {"version": 1, "first_owner_at": 0, "forgotten": 0, "memories": []}

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)
        self._write_md()

    def _write_md(self):
        lines = ["# 记忆卡", ""]
        for m in self._sorted():
            lines.append(
                f"- [{m['clarity_cn']}] {m['text']}  "
                f"（重要度 {m['importance']}，上次提到 {_day(m['last_seen'])}）"
            )
        with open(self.md_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def backup(self):
        if not os.path.exists(self.path):
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = f.read()
            with open(os.path.join(self.backup_dir, f"memories-{stamp}.json"), "w", encoding="utf-8") as f:
                f.write(raw)
            files = sorted(os.listdir(self.backup_dir))
            for old in files[:-7]:
                os.remove(os.path.join(self.backup_dir, old))
        except OSError:
            pass

    # ---------------- 评分 ----------------

    # [fish-patch 2026-11-03] 精细化分层：类别主导 + 正文微调。
    # 旧口径（规则措辞/当场落定/里程碑/冲突）作废——新日记提示词禁止表态句、正文限 25 字，
    # 那些词表在真实日记里 0 命中（2026-11-02 审计：11 条里 8 条只有底分）。
    _RE_NUM = re.compile(r"\d{2,}|[一二三四五六七八九十百千万两]{1,2}\s*[点块个天年级号]")
    _RE_QUOTE = re.compile(r"[「“\"]")
    _RE_EMO = re.compile(r"开心|难过|怕|心疼|舍不得|想你|喜欢你|生气|委屈|舒服|累")
    _RE_SCENE = re.compile(r"屏幕|视频|看|梦|窗|海|雨|灯")

    def score(self, text: str, is_core: bool = False, key: str | None = None, **_kw) -> int:
        """初始分 = 这条该记多久（3.0，2026-11-03 精细化分层）。

        类别主导（约定 > 承诺 > 信箱 > 常驻杂项 > 日常），正文只做微调。
        寿命 = (分 - forget_floor) / decay_per_day。
        """
        t = text or ""
        head = ""
        if key:
            head = (normalize_key(key) or "").split(":")[0]
        s = SCORE_BASE + SCORE_CAT.get(head, SCORE_CAT_DEFAULT)
        if is_core and head not in ("约定", "承诺"):
            s += SCORE_CORE_BONUS
        micro = 0
        if self._RE_NUM.search(t):
            micro += 3
        if self._RE_QUOTE.search(t):
            micro += 3
        if "主人" in t and "我" in t:
            micro += 3
        if self._RE_EMO.search(t):
            micro += 3
        if self._RE_SCENE.search(t):
            micro += 2
        s += min(SCORE_MICRO_CAP, micro)
        return max(SCORE_MIN, min(SCORE_MAX, s))

    @staticmethod
    def _kind(text: str) -> str:
        t = text or ""
        if any(w in t for w in FIRST_WORDS):
            return "first"
        if any(w in t for w in PROMISE_WORDS):
            return "promise"
        if any(w in t for w in CONFLICT_WORDS):
            return "conflict"
        if any(w in t for w in EMOTION_STRONG + EMOTION_WEAK):
            return "emotion"
        return "event"

    # ---------------- 写入 ----------------

    @staticmethod
    def _norm(text: str) -> str:
        return re.sub(r"[\s，。、；：！？,.!?~…—\-\"“”‘’()（）]", "", text or "")

    def _similar(self, a: str, b: str) -> float:
        """中文改写容忍度：取 difflib 与字符二元组 Jaccard 的较大值"""
        a, b = self._norm(a), self._norm(b)
        if len(a) < 8 or len(b) < 8:
            return 0.0
        base = difflib.SequenceMatcher(None, a, b).ratio()
        ba = {a[i:i + 2] for i in range(len(a) - 1)}
        bb = {b[i:i + 2] for i in range(len(b) - 1)}
        if not ba or not bb:
            return base
        jac = len(ba & bb) / len(ba | bb)
        # 覆盖率：短句被长句包含时也算相近（"面试不顺利" vs "主人去面试，说不顺利"）
        cover = len(ba & bb) / min(len(ba), len(bb))
        return max(base, jac, cover * 0.78)

    # [fish-patch 2026-11-09 23:10] 覆盖前留旧版本：正文被替换时把老的塞进 versions（最多 5 条）。
    # 只影响「能不能翻旧账」，不碰 importance/birth/recall/last_seen 任何一个分数。
    # pinned / 叮嘱 不存：它们永远用最新正文，存了会被「晚安」「去洗澡」这种塞满。
    _VERSION_KEEP = 5

    def _push_version(self, m: dict, old: str, new: str | None = None):
        old = (old or "").strip()
        if not old:
            return
        if new is not None and old == (new or "").strip():
            return
        if m.get("pinned") or m.get("key") == "叮嘱":
            return
        vs = m.setdefault("versions", [])
        if any((v.get("text") or "").strip() == old for v in vs):
            return
        vs.append({"text": old[:200], "date": _day(_now()), "at": _now()})
        if len(vs) > self._VERSION_KEEP:
            del vs[:-self._VERSION_KEEP]

    def _merge(self, m: dict, text: str, importance: int | None, key: str | None) -> dict:
        today = _day(_now())
        imp_up = importance is not None and int(importance) > int(m["importance"])
        if m.get("last_day") != today:
            m["days_seen"] = int(m.get("days_seen", 1)) + 1
            m["last_day"] = today
        m["recall"] = int(m.get("recall", 0)) + 1
        if key and not m.get("key"):
            m["key"] = key
        birth = int(m.get("birth", m.get("importance", 1)))
        if importance is not None and int(importance) > birth:
            birth = max(1, min(100, int(importance)))
            m["birth"] = birth
        # 复述固化（2.0 / 10-31 调整）：每被提到一次 birth +3（封顶 100）。
        # 效果：天天被提到的卡 2-3 周后爬到 100，重要性稳定在 93，等于"反复提起自动变长期记忆"；
        # 偶尔提到的照样按衰减走。注意 immune_birth=101，所以 100 分仍会衰减，只靠不断被提维持。
        if not m.get("pinned"):
            birth = min(SCORE_MAX, birth + BIRTH_STEP)
            m["birth"] = birth
        # 被提到：回补，封顶出生分
        gain = 0 if m.get("pinned") else self.mention_gain
        if gain:
            m["importance"] = min(birth, int(m.get("importance", 1)) + gain)
        self._sync_clarity(m)
        # 置顶记忆（叮嘱/手动记住）：正文永远用最新的
        if m.get("pinned") or m.get("key") == "叮嘱":
            m["text"] = text
            if importance is not None:
                m["importance"] = max(int(m.get("importance", 1)), min(100, int(importance)))
            self._sync_clarity(m)
        elif imp_up:
            self._push_version(m, m.get("text") or "", text)
            m["text"] = text
        # 续期上限：同一件事反复出现太多次且没变得更重要，就不再保鲜
        if imp_up or m.get("pinned") or int(m.get("days_seen", 1)) <= self.renew_days_limit:
            m["last_seen"] = _now()
            self._refresh_clarity(m)
        return m

    def _next_dup_key(self, base: str) -> str:
        """[fish-patch 2026-11-02] 为同名视频 key 生成不冲突的副本 key：视频:机甲 -> 视频:机甲·2。

        只服务于「视频」类：同一个大类（如视频:机甲）里两段完全不同的视频，
        旧逻辑会按 key 直接合并、把上一段文本覆盖掉；改为另起 key 后两张各活各的。
        """
        used = {m.get("key") for m in self.data.get("memories", [])}
        n = 2
        while f"{base}·{n}" in used:
            n += 1
        return f"{base}·{n}"

    def add(
        self,
        text: str,
        importance: int | None = None,
        pinned: bool = False,
        source: str = "manual",
        key: str | None = None,
    ):
        text = (text or "").strip()
        if not text:
            return None
        if any(w in text for w in COMMAND_JUNK):
            return None
        key = normalize_key(key)
        # 1) 有标签：按标签合并
        if key:
            for m in self.data["memories"]:
                if m.get("key") == key:
                    # [fish-patch 2026-11-02] 对「视频」「事」两类：同 key 但内容明显不同时不再覆盖，
                    # 改用一个不冲突的副本 key（视频:机甲 -> 视频:机甲·2），避免两段不同内容互相覆盖。
                    # 拆分阈值：事 = 0.50（更保守，宁可多一张也不误覆盖）；视频 = 0.40（沿用全局合并线）。
                    _head = key.split(":")[0]
                    _thr = 0.50 if _head == "事" else 0.40
                    if _head in ("视频", "事") and self._similar(text, m["text"]) < _thr:
                        key = self._next_dup_key(key)
                        break
                    # [fish-patch 2026-11-11] 这张卡已经结清，而新内容不像同一件事——
                    # 那就是「又欠了一笔」/「又约了一次」：重开，别把它并进已结的那张里，
                    # 也别另起 key（会和 dedupe 打架）。
                    if m.get("closed") and self._similar(text, m["text"]) < 0.45:
                        self._push_version(m, m.get("text") or "", text)
                        m["text"] = text
                        m.pop("closed", None)
                        m.pop("closed_at", None)
                        m["importance"] = max(int(m.get("importance") or 0),
                                              int(importance or 0))
                        logger.info("[fish-mem] 结清后又出现新的一笔，重开：%s", key)
                        return self._merge(m, text, importance, key)
                    return self._merge(m, text, importance, key)
            # 1.5) 同一标签大类 + 文字相近：容忍标签尾巴不一致（板子:倒计时 vs 板子）
            head = key.split(":")[0]
            for m in self.data["memories"]:
                mk = m.get("key") or ""
                if mk.split(":")[0] == head and self._similar(text, m["text"]) >= 0.40:
                    return self._merge(m, text, importance, key)
        # 2) 无标签或标签没命中：相似度兜底
        best = None
        best_r = 0.0
        for m in self.data["memories"]:
            r = self._similar(text, m["text"])
            if r > best_r:
                best, best_r = m, r
        if best is not None and best_r >= 0.40:
            return self._merge(best, text, importance, key)
        imp = max(1, min(100, int(importance if importance is not None else self.score(text, key=key))))
        # [fish-patch 2026-11-11] 结清要跨「重写」。
        # 原来的坑：账结了（closed=done）之后分数自然淡出、卡被删；当晚日记又写了一次同一件事，
        # 于是长出一张「没结清」的新卡——「这事了了」被下一次日记抹掉（交接待办 10）。
        # 现在：同一件事的重述（文字相似 ≥0.45、90 天内）直接继承结清状态；
        # 若是新承诺（文字不像），照常建新卡，不会把新账误标成已结。
        _closed, _closed_at = None, None
        for _s in (self.data.get("settled") or []):
            if str(_s.get("key") or "") != (key or ""):
                continue
            try:
                if _now() - int(_s.get("ts") or 0) > 90 * 86400:
                    continue
            except Exception:
                continue
            if self._similar(text, str(_s.get("text") or "")) >= 0.45:
                _closed = _s.get("outcome") or "done"
                _closed_at = int(_s.get("ts") or 0)
                break
        if _closed:
            imp = 45 if _closed == "void" else 35
        # 2.0：不再自动置顶「第一次/里程碑」（否则常驻类会全部变永久）。永久只由 pinned 决定。
        m = {
            "id": f"mem_{_now()}_{len(self.data['memories'])}",
            "kind": self._kind(text),
            "key": key,
            "text": text,
            "importance": imp,
            "birth": imp,
            "clarity": "vivid",
            "clarity_cn": CLARITY_CN["vivid"],
            "created": _now(),
            "last_seen": _now(),
            "recall": 0,
            "pinned": bool(pinned),
            "source": source,
        }
        if _closed:
            m["closed"] = _closed
            m["closed_at"] = _closed_at
            logger.info("[fish-mem] 重写照旧保持结清：%s -> %s", key, _closed)
        if self.data.get("first_owner_at") in (0, None):
            self.data["first_owner_at"] = _now()
        self.data["memories"].append(m)
        return m

    # [fish-patch 2026-11-09 23:45] 结清：账还了 / 约定作废。
    # 不删卡（B 方案：能翻旧账），只打标记 + 摘掉「常挂」资格。
    # 兑现 -> importance 35（自然淡出）；作废 -> 45（留在检索层，带着「已作废」的标签）。
    def close_card(self, key: str, outcome: str = "done") -> bool:
        nk = normalize_key(key)
        if not nk:
            return False
        for m in self.data["memories"]:
            if normalize_key(m.get("key")) != nk:
                continue
            if m.get("closed"):
                return True          # 幂等：实时写过、日记又写一遍，不重复处理
            m["closed"] = "void" if str(outcome).strip() in ("作废", "void", "fail") else "done"
            m["closed_at"] = _now()
            m["importance"] = 45 if m["closed"] == "void" else 35
            m["last_seen"] = _now()
            # [fish-patch 2026-11-11] 留一份「结清账本」：这张卡以后被删了、日记又重写同一件事，
            # add() 能靠它把 closed 继承回来。留 60 条，够覆盖半年的账。
            self._record_settled(nk, m["closed"], m.get("text") or "")
            logger.info("[fish-mem] 结清 %s -> %s", m.get("key"), m["closed"])
            return True
        return False

    def _record_settled(self, key: str, outcome: str, text: str):
        try:
            items = [x for x in (self.data.get("settled") or []) if x.get("key") != key]
            items.append({"key": key, "outcome": outcome, "ts": _now(),
                          "text": str(text or "")[:100]})
            self.data["settled"] = items[-60:]
        except Exception:
            pass

    def forget(self, keyword: str) -> int:
        before = len(self.data["memories"])
        self.data["memories"] = [
            m for m in self.data["memories"] if keyword not in m["text"] or m.get("pinned")
        ]
        return before - len(self.data["memories"])

    # ---------------- 复述通道（2.0 新增） ----------------

    # [B' 2026-11-04] 复述命中时是否顺手更新卡片文案：新正文必须仍覆盖旧正文 ≥60% 的内容，
    # 且长度 ≥80%，才允许替换。只做「同一事实换措辞」，事实变更仍靠【常驻|同key】重写。
    _TEXT_UPDATE_COVER = 0.60
    _TEXT_UPDATE_LEN = 0.80

    def _coverage(self, old: str, new: str) -> float:
        """旧正文的字符二元组有多少被新正文覆盖（0~1）。"""
        a, b = self._norm(old), self._norm(new)
        if len(a) < 4 or not b:
            return 1.0
        A = {a[i:i + 2] for i in range(len(a) - 1)}
        B = {b[i:i + 2] for i in range(len(b) - 1)}
        return len(A & B) / max(1, len(A))

    def _try_update_text(self, m: dict, old: str, new: str) -> bool:
        """[B' 2026-11-04] 只有「同一事实换个说法」才更新卡片正文，否则只加分。"""
        try:
            new = (new or "").strip()
            if not new or new == (old or "").strip():
                return False
            cov = self._coverage(old, new)
            ratio = len(new) / max(1, len(old))
            if cov >= self._TEXT_UPDATE_COVER and ratio >= self._TEXT_UPDATE_LEN:
                self._push_version(m, old, new)
                m["text"] = new
                logger.info(
                    "[fish-mem] 复述替换 %s｜覆盖 %.2f 长度 %.2f｜%s -> %s",
                    m.get("key"), cov, ratio, (old or "")[:24], new[:24],
                )
                return True
            logger.info(
                "[fish-mem] 复述不换文 %s｜覆盖 %.2f 长度 %.2f（只加分）",
                m.get("key"), cov, ratio,
            )
            return False
        except Exception as e:
            logger.warning(f"[fish-mem] 文案更新判断失败: {e}")
            return False

    def mention(self, key: str, body: str = "") -> bool:
        """通道 A：日记里写了【复述|标签】，按标签刷新已有记忆。不新建卡。

        [B' 2026-11-04] body 是复述行后面的正文，用来判断要不要顺手更新卡片文案。
        """
        nk = normalize_key(key)
        if not nk:
            return False
        for m in self.data["memories"]:
            if normalize_key(m.get("key")) == nk:
                old = m.get("text") or ""
                self._merge(m, old, None, nk)
                self._try_update_text(m, old, body)
                return True
        return False

    def mention_by_keyword(self, text: str) -> int:
        """通道 B：当天对话里出现了某张卡的标签尾巴（>=2 字）就算提到。

        少于 2 字的尾巴（比如「视频:海」的「海」）不参与，否则每句话都会命中。
        """
        t = self._norm(text)
        if not t:
            return 0
        hit = 0
        for m in list(self.data["memories"]):
            if m.get("pinned"):
                continue
            tail = (m.get("key") or "").split(":")[-1].strip()
            nt = self._norm(tail)
            if len(tail) < 2 or len(nt) < 2:
                continue
            if nt in t:
                self._merge(m, m.get("text") or "", None, m.get("key"))
                hit += 1
        return hit

    # ---------------- 衰减 ----------------

    def _retention(self, m: dict, now: int) -> float:
        age_days = (now - m["last_seen"]) / 86400
        r = float(m["importance"])
        r += min(12, int(m.get("recall", 0)) * 2)
        if age_days <= self.recent_days:
            r += 18
        if age_days <= 2:
            r += 8
        if age_days > self.fuzzy_after_days:
            penalty = 0.12 if m["importance"] >= self.min_owner_importance else 0.35
            r -= (age_days - self.fuzzy_after_days) * penalty
        if m.get("pinned"):
            return 100.0
        if m["importance"] >= self.min_owner_importance:
            r = max(r, 55.0)
        return r

    def _target_clarity(self, m: dict, now: int) -> str:
        if m.get("pinned"):
            return "vivid"
        age_days = (now - m["last_seen"]) / 86400
        if age_days <= self.recent_days:
            return "clear"
        r = self._retention(m, now)
        if r >= 90:
            return "vivid"
        if r >= 70:
            return "clear"
        if r >= 50:
            return "fuzzy"
        if age_days > self.forget_after_days and m["importance"] < 50:
            return "forget"
        return "trace"

    @staticmethod
    def clarity_from_score(score: int) -> str:
        if score >= 80:
            return "vivid"
        if score >= 60:
            return "clear"
        if score >= 40:
            return "fuzzy"
        if score >= 20:
            return "trace"
        return "trace"

    def _sync_clarity(self, m: dict):
        c = self.clarity_from_score(int(m.get("importance", 1)))
        m["clarity"] = c
        m["clarity_cn"] = CLARITY_CN[c]

    def _refresh_clarity(self, m: dict):
        """被再次提到：回升一档。"""
        try:
            i = CLARITIES.index(m.get("clarity", "clear"))
        except ValueError:
            i = 1
        i = max(0, i - 1)
        m["clarity"] = CLARITIES[i]
        m["clarity_cn"] = CLARITY_CN[CLARITIES[i]]

    def dedupe(self) -> int:
        """把同一个 key 的多条记忆合并成一条（保留分数最高的正文）。"""
        groups: dict[str, list] = {}
        for m in self.data["memories"]:
            k = normalize_key(m.get("key")) or f"raw:{self._norm(m.get('text', ''))[:16]}"
            groups.setdefault(k, []).append(m)
        out, removed = [], 0
        for k, ms in groups.items():
            if len(ms) == 1:
                out.append(ms[0])
                continue
            ms.sort(key=lambda x: -int(x.get("importance", 1)))
            keep = ms[0]
            if not str(k).startswith("raw:"):
                keep["key"] = k
            for other in ms[1:]:
                keep["recall"] = int(keep.get("recall", 0)) + int(other.get("recall", 0)) + 1
                keep["days_seen"] = min(
                    30, max(int(keep.get("days_seen", 1) or 1), int(other.get("days_seen", 1) or 1)) + 1
                )
                keep["pinned"] = bool(keep.get("pinned")) or bool(other.get("pinned"))
                keep["last_seen"] = max(int(keep.get("last_seen", 0)), int(other.get("last_seen", 0)))
                if int(other.get("importance", 0)) >= int(keep.get("importance", 0)) and len(
                    other.get("text", "")
                ) > len(keep.get("text", "")):
                    keep["text"] = other["text"]
                    keep["importance"] = int(other["importance"])
                removed += 1
            out.append(keep)
        self.data["memories"] = out
        return removed

    def maintain(self) -> dict:
        """每日整理：扣分、遗忘、容量压缩。

        规则（2026-11-11 起分档）：
        - 出生分 >= immune_birth：永不衰减
        - pinned（叮嘱/手动记住）：永不衰减
        - 约定 / 承诺 / 账：**不按天衰减**，活到被结清为止（CLOSE 之后转普通档）
          依据：四日备份对比里「约定:生日」（出生 76 分）因为 6 天没被提就被删了——
          而它本来就该一年才提一次。按天数淘汰这类卡，方向就是错的。
        - 错 / 信箱：每天 -3，底线 20
        - 其余（事/物/视频/玩笑/饭/天气/日常）：每天 -decay_per_day，低于 forget_floor 遗忘
        - 约定类还有上限 LONG_MAX：超了就把最久没提的降回普通档，防止无限膨胀
        """
        now = _now()
        self.backup()
        merged_dup = self.dedupe()
        decayed = 0
        forgotten = 0
        kept = []
        floor = self.forget_floor
        immune = self.immune_birth
        drop = self.decay_per_day

        # 长期档：未结清、非置顶、未免疫的 约定/承诺/账
        _long = [m for m in self.data["memories"]
                 if _kind_of(m) in LONG_KINDS and not m.get("pinned")
                 and not m.get("closed")
                 and int(m.get("birth", m.get("importance", 1)) or 1) < immune]
        _long.sort(key=lambda m: -int(m.get("last_seen") or 0))
        _overflow = {id(m) for m in _long[LONG_MAX:]}

        for m in self.data["memories"]:
            key = m.get("key")
            birth = int(m.get("birth", m.get("importance", 1)) or 1)
            if m.get("pinned") or birth >= immune:
                self._sync_clarity(m)
                kept.append(m)
                continue
            kind = _kind_of(m)
            this_drop = drop
            if kind in LONG_KINDS and not m.get("closed") and id(m) not in _overflow:
                this_drop = LONG_DECAY   # 长期档：-2/天，慢但不永生
            elif kind in KIND_DECAY:
                this_drop = KIND_DECAY[kind]   # 中档：-4/天
            score = int(m.get("importance", 1)) - this_drop
            if this_drop:
                decayed += 1
            if score < floor:
                self.data["forgotten"] = int(self.data.get("forgotten", 0)) + 1
                forgotten += 1
                continue
            m["importance"] = score
            self._sync_clarity(m)
            kept.append(m)
        self.data["memories"] = kept
        compacted = self._compact()
        self.save()
        return {
            "decayed": decayed,
            "forgotten": forgotten,
            "compacted": compacted,
            "deduped": merged_dup,
            "count": len(self.data["memories"]),
            "chars": self.card_chars(),
        }


    # ---------------- 容量 ----------------

    def _sorted(self):
        def key(m):
            order = {"vivid": 0, "clear": 1, "fuzzy": 2, "trace": 3}.get(m.get("clarity", "clear"), 1)
            return (0 if m.get("pinned") else 1, order, -int(m["importance"]), -int(m["last_seen"]))

        return sorted(self.data["memories"], key=key)

    def render(self, limit: int | None = None, header: bool = True) -> str:
        limit = int(limit or self._c("inject_chars", 1200) or 1200)
        parts = []
        total = 0
        for m in self._sorted():
            prefix = ""
            if m.get("key") == "叮嘱" or (m.get("pinned") and m.get("importance", 0) >= 80):
                prefix = "【叮嘱】"
            elif m.get("pinned"):
                prefix = "【记住】"
            line = f"- {prefix}{_age_tag(m)}{m['text']}"
            if total + len(line) > limit:
                break
            parts.append(line)
            total += len(line)
        if not parts:
            return ""
        body = "\n".join(parts)
        if not header:
            return body
        return ("【我的记忆】\n" + body
                + f"\n（共 {len(parts)} 条；括号里是多久以前的事，最久远的已开始模糊）")

    def card_chars(self) -> int:
        return len(self.render(limit=10**9, header=False))

    def capacity(self) -> dict:
        used = self.card_chars()
        cap = max(1, self.max_chars)
        ratio = used / cap
        level = "ok"
        if ratio >= 1.0:
            level = "full"
        elif ratio >= 0.9:
            level = "critical"
        elif ratio >= 0.8:
            level = "warn"
        return {"used": used, "max": cap, "ratio": round(ratio, 3), "level": level}

    def _compact(self) -> int:
        """超容量时：先降档最不重要、最久没提的非置顶记忆，直到回落到 90%。"""
        cap = max(1, self.max_chars)
        if self.card_chars() <= cap:
            return 0
        target = int(cap * 0.9)
        changed = 0
        while self.card_chars() > target:
            cands = [
                m for m in self.data["memories"]
                if not m.get("pinned") and m["importance"] < self.min_owner_importance
            ]
            if not cands:
                break
            cands.sort(key=lambda m: (m["importance"], m["last_seen"]))
            m = cands[0]
            cur = m.get("clarity", "clear")
            if cur == "trace":
                self.data["memories"].remove(m)
                self.data["forgotten"] = int(self.data.get("forgotten", 0)) + 1
            else:
                i = CLARITIES.index(cur) if cur in CLARITIES else 1
                i = min(len(CLARITIES) - 1, i + 1)
                m["clarity"] = CLARITIES[i]
                m["clarity_cn"] = CLARITY_CN[m["clarity"]]
            changed += 1
            if changed > 500:
                break
        if changed:
            self._write_md()
        return changed

    # ---------------- 从日记摄取 ----------------

    def ingest_diary(self, text: str, source: str = "") -> tuple[int, int]:
        """从日记摄取：解析【常驻|标签】/【普通|标签】/【复述|标签】，返回 (新增条数, 合并条数)。"""
        added = merged = 0
        for raw in (text or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            # 容错：模型常给行加前缀（1） / 2. / 3) / - / 新事： / 旧事：），循环剥到不再变化
            for _ in range(4):
                new = re.sub(r"^\s*(?:\d{1,2}\s*[）).、．:：]\s*|[-*·]\s+)", "", line)
                new = re.sub(r"^\s*(?:新事|新发生的事|新发生|旧事)\s*[：:]\s*", "", new)
                new = re.sub(r"^\s*(?:新事|旧事)\s+", "", new)
                if new == line:
                    break
                line = new
            if not line:
                continue
            # 心情/念头行：2026-10-31 起已停用（inner_voice 改成读对话窗口判断）。
            # 模型若还写出这两行，直接跳过，不建卡也不落盘。
            if re.match(r"^【(心情|念头|心境)\s*[|｜:：]?", line):
                continue

            # 结清行（2026-11-09）：【结清|兑现|账:肠】/【结清|作废|约定:看海】
            _mcl = re.match(r"^【结清\s*[|｜]\s*(兑现|作废)?\s*[|｜]?\s*([^】]{1,24})】", line)
            if _mcl:
                _out = _mcl.group(1) or "兑现"
                _k = (_mcl.group(2) or "").strip()
                if _k and self.close_card(_k, _out):
                    logger.info("[fish-mem] 日记结清：%s %s", _out, _k)
                continue
            # 偏好行：写 prefs.json，不建卡（3.0）
            mpref = re.match(r"^【喜好\s*[|｜:：]\s*([^】]{1,12})】(.*)$", line)
            if mpref:
                pref = getattr(self, "prefs", None)
                if pref is not None:
                    pref.upsert(mpref.group(1).strip(), mpref.group(2).strip())
                continue
            # 复述行：刷新已有记忆（B' 决定是否顺手换文案），不新建卡
            mref = re.match(r"^【复述(?:\|([^】]{1,16}))?】(.*)$", line)
            if mref:
                rkey = (mref.group(1) or "").strip()
                if rkey and self.mention(rkey, mref.group(2)):
                    merged += 1
                continue
            # 容错：模型漏了括号或用了别的分隔符（复述|约定:早睡 / 复述：约定:早睡）
            loose = re.match(r"^[【\[]?\s*复述\s*[|｜:：]\s*([^】\]\s]{1,16})[】\]]?(.*)$", line)
            if loose:
                rkey = loose.group(1).strip().rstrip("】]")
                if rkey and self.mention(rkey, loose.group(2)):
                    merged += 1
                continue
            m = re.match(r"^【(常驻|普通|约定|承诺)(?:\|([^】]{1,16}))?】(.*)$", line)
            if m:
                kind, key, body = m.group(1), m.group(2), m.group(3).strip()
                # 模型有时写成【承诺|事:十六进制】——用类别当"常驻"标记，没写常驻/普通。
                # 约定/承诺 本来就该按常驻打分（人设要求），所以直接认。
                is_core = kind in ("常驻", "约定", "承诺")
                if key and ":" not in key and "：" not in key:
                    key = None      # 只写了类别、没写「类别:对象」→ 交给相似度判断
                if key:
                    key = _fix_key(key, body)      # 「对象」是时间词/泛称 → 换正文前 8 字
                if not body or any(w in body for w in COMMAND_JUNK):
                    continue
                imp = self.score(body, is_core=is_core, key=key)
                before = len(self.data["memories"])
                self.add(body, importance=imp, source=f"diary:{source}", key=key)
                if len(self.data["memories"]) > before:
                    added += 1
                else:
                    merged += 1
                continue
            # 容错：只写了【类别:对象】没写【常驻/普通】，按类别推断（约定/承诺算常驻）
            # 注意 {1,8}：单字类别（事:睡觉、物:图）也要能匹配
            mtag = re.match(r"^【([^】|\s]{1,8}[:：][^】]{0,12})】(.*)$", line)
            if mtag:
                tag = mtag.group(1).replace("：", ":").strip()
                body = mtag.group(2).strip()
                tag = _fix_key(tag, body)          # 同上：对象退化时换正文前 8 字
                head = tag.split(":")[0]
                if body and not any(w in body for w in COMMAND_JUNK):
                    imp = self.score(body, is_core=head in ("约定", "承诺"), key=tag)
                    before = len(self.data["memories"])
                    self.add(body, importance=imp, source=f"diary:{source}", key=tag)
                    if len(self.data["memories"]) > before:
                        added += 1
                    else:
                        merged += 1
                continue
            # [fish-patch 2026-11-02] 兼容模型把「给下一个我」包进【】的写法，
            # 否则它会掉进下面的兜底分支，变成一张 38 分的普通卡（1-2 天就烂掉）。
            m_self = re.match(r"^[【\[]?\s*给下一个我\s*[：:]\s*(.*?)\s*[】\]]?\s*$", line)
            if m_self:
                body = m_self.group(1).strip()
                if not body:
                    continue
                before = len(self.data["memories"])
                self.add(
                    body,
                    importance=90,
                    pinned=True,
                    source=f"diary:{source}",
                    key="叮嘱",
                )
                if len(self.data["memories"]) > before:
                    added += 1
                else:
                    merged += 1
            else:
                # 没按格式写（没有【常驻|标签】也没写叮嘱）：仍然收，靠相似度合并
                if line.startswith("#") or re.fullmatch(r"\d{4}-\d{2}-\d{2}", line):
                    continue
                body = line.strip()
                if len(body) >= 6 and not any(w in body for w in COMMAND_JUNK):
                    before = len(self.data["memories"])
                    self.add(body, source=f"diary:{source}")
                    if len(self.data["memories"]) > before:
                        added += 1
                    else:
                        merged += 1
        return added, merged



# ---------------- 偏好档案（3.0） ----------------

class PrefStore:
    """她"定下的喜欢"，存在 memory/prefs.json。

    偏好不是记忆而是档案：不参与衰减、不进记忆卡、不占 inject_chars 预算，
    只在她每天第一次对话时单独注入一块【我喜欢】。
    """

    RESERVED = ("主人", "姐姐",
                "名字", "称呼", "关系", "人设")

    def __init__(self, memory_dir: str, cfg: dict | None = None):
        self.dir = memory_dir
        self.path = os.path.join(memory_dir, "prefs.json")
        self.cfg = cfg or {}
        self.data = self._load()

    # ---- 存取 ----
    def _load(self) -> dict:
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict) and isinstance(d.get("items"), list):
                    return d
            except Exception:
                pass
        return {"version": 1, "items": []}

    def save(self):
        os.makedirs(self.dir, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    @property
    def max_items(self) -> int:
        try:
            return int(self.cfg.get("prefs_max_items", 36) or 36)
        except Exception:
            return 36

    # ---- 工具 ----
    @staticmethod
    def _topic(t: str) -> str:
        return re.sub(r"\s+", "", str(t or ""))[:8]

    def _reserved(self, topic: str) -> bool:
        return any(w in topic for w in self.RESERVED)

    def find(self, topic: str):
        nt = self._topic(topic)
        for it in self.data["items"]:
            if self._topic(it.get("topic")) == nt:
                return it
        return None

    # ---- 写 ----
    def upsert(self, topic: str, text: str) -> str:
        """返回 added / updated / skipped。"""
        nt = self._topic(topic)
        body = (text or "").strip()[:60]
        if not nt or not body:
            return "skipped"
        if self._reserved(nt):
            return "skipped"
        today = _day(_now())
        it = self.find(nt)
        if it:
            it["text"] = body
            it["updated"] = today
            self.save()
            return "updated"
        # 方案 C：不做自动淘汰。偏好是"她定下的"，删不删由人决定，不交给时间。
        # 满了就拒收新主题（改口仍然允许，因为上面已 return）。
        if len(self.data["items"]) >= self.max_items:
            return "full"
        self.data["items"].append(
            {"topic": nt, "text": body, "since": today, "updated": today})
        self.save()
        return "added"

    def bump(self, topic: str) -> bool:
        """刷新 updated（只记"最近动过"，不参与排序和淘汰）。目前没有自动调用点。"""
        it = self.find(topic)
        if not it:
            return False
        it["updated"] = _day(_now())
        self.save()
        return True

    def delete(self, topic: str) -> bool:
        it = self.find(topic)
        if not it:
            return False
        self.data["items"].remove(it)
        self.save()
        return True

    # ---- 读 ----
    def items(self) -> list:
        """按 since 升序：先定下的永远在前（沉默不代表不重要）。

        方案 C：排序不看 updated —— 偏好本来就该长期沉默，用"最近提到"排序
        会让稳定偏好被新来的临时喜好挤到后面。
        """
        return sorted(self.data["items"],
                      key=lambda x: (x.get("since") or "9999", x.get("topic") or ""))

    def render(self, limit_chars: int = 240) -> str:
        its = self.items()
        if not its:
            return ""
        head = "【我喜欢】\n"
        parts, total = [], len(head)      # 标题也算进预算，免得实际输出超上限
        for it in its:
            line = f"- {it['topic']}：{it['text']}"
            if total + len(line) + 1 > limit_chars:
                break
            parts.append(line)
            total += len(line) + 1
        if not parts:
            return ""
        return head + "\n".join(parts)
