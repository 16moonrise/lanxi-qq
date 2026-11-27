"""内在状态（inner）—— 纯逻辑，不依赖 AstrBot，可离线测试。

2026-10-31 改版：心情/念头（mood / thoughts）整条链已删除。
判断"要不要主动开口"不再看日记写下的一行心情，而是读 AstrBot 的对话窗口
（最近 32 轮原话，见 main.py）。本文件只保留 runtime 计数与确定性闸门。

文件：memory/self/inner.json
{
  "version": 2,
  "runtime": {
    "day": "2026-10-31",
    "sent_today": 0,
    "unanswered": 0,
    "miss_today": 0,
    "last_sent_ts": 0,
    "last_user_ts": 0
  }
}
"""

import json
import os
import re
import time

DAY = "%Y-%m-%d"

# 一眼就能看出是废话的开场（没有具体事由时不许用）
# 注意：「想你了」不算废话——用户明确要求它能作为开口理由，见 MISS_WORDS
GENERIC = (
    "在干嘛", "在吗", "在不在", "忙吗", "睡了没", "早", "晚安", "早安",
    "没事", "有空吗",
)

# 想念类：允许，但一天只放一次，避免退化成每 40 分钟一句
MISS_WORDS = ("想你", "想主人", "有点想")


def now_ts() -> int:
    return int(time.time())


def today(ts: int | None = None) -> str:
    return time.strftime(DAY, time.localtime(ts if ts is not None else now_ts()))


def _hour(ts: int | None = None) -> int:
    return int(time.strftime("%H", time.localtime(ts if ts is not None else now_ts())))


def in_quiet_hours(ts: int | None = None, spec: str = "1-7") -> bool:
    """免打扰时段，支持跨零点（如 23-7）。格式 "起-止"，小时制。"""
    try:
        a, b = str(spec).split("-")
        start, end = int(a), int(b)
    except Exception:
        return False
    h = _hour(ts)
    if start == end:
        return False
    if start < end:
        return start <= h < end
    return h >= start or h < end


class InnerState:
    """inner.json 的读写与状态推进（只放 runtime，没有心情/念头）。"""

    def __init__(self, path: str):
        self.path = path
        self.data = {"version": 2, "runtime": {}}
        self.load()

    # ---------- IO ----------

    def load(self) -> dict:
        try:
            if os.path.exists(self.path):
                with open(self.path, encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d, dict):
                    self.data.update(d)
        except Exception:
            pass
        # 老版本文件里的 mood / thoughts 直接丢弃
        self.data.pop("mood", None)
        self.data.pop("thoughts", None)
        self.data["version"] = 2
        self._roll_day()
        return self.data

    def save(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def _roll_day(self):
        """跨天：清当天计数，未回复次数归零。"""
        rt = self.data.setdefault("runtime", {})
        d = today()
        if rt.get("day") != d:
            rt["day"] = d
            rt["sent_today"] = 0
            rt["unanswered"] = 0
            rt["miss_today"] = 0
            rt["msg_today"] = 0
        rt.setdefault("sent_today", 0)
        rt.setdefault("msg_today", 0)
        rt.setdefault("miss_today", 0)
        rt.setdefault("unanswered", 0)
        rt.setdefault("last_sent_ts", 0)
        rt.setdefault("last_user_ts", 0)

    # ---------- 计数 ----------

    def note_user_message(self, ts: int | None = None):
        rt = self.data.setdefault("runtime", {})
        rt["last_user_ts"] = int(ts if ts is not None else now_ts())
        rt["unanswered"] = 0
        self._roll_day()
        rt["msg_today"] = int(rt.get("msg_today", 0)) + 1

    def note_sent(self, ts: int | None = None):
        rt = self.data.setdefault("runtime", {})
        rt["last_sent_ts"] = int(ts if ts is not None else now_ts())
        rt["sent_today"] = int(rt.get("sent_today", 0)) + 1
        rt["unanswered"] = int(rt.get("unanswered", 0)) + 1

    def note_miss(self):
        rt = self.data.setdefault("runtime", {})
        rt["miss_today"] = int(rt.get("miss_today", 0)) + 1

    # ---------- 闸门 ----------

    def gate(self, cfg: dict, ts: int | None = None, closed: bool = False) -> str | None:
        """返回 None 表示可以自省；返回字符串表示跳过原因。

        只放确定性条件。跟对话窗口有关的（有没有聊过、窗口太旧、最后一句是谁）
        在 main.py 里判断，因为它们要读会话，不属于纯逻辑。
        closed=True 表示「他最后一句是收尾」（晚安/去洗澡/上课…），
        这时只需要 short_gap_minutes 的安静就能开口。
        """
        ts = int(ts if ts is not None else now_ts())
        self._roll_day()
        rt = self.data["runtime"]

        if in_quiet_hours(ts, str(cfg.get("quiet_hours", "1-7"))):
            return "quiet"

        cap = int(cfg.get("daily_cap", 6) or 6)
        if int(rt.get("sent_today", 0)) >= cap:
            return "daily_cap"

        # 默认 1：她已经主动发过一条、他还没回，就不再开口。
        # 这比"连发 3 条才收口"自然——没人喜欢被连发。
        ucap = int(cfg.get("unanswered_limit", 1) or 1)
        if int(rt.get("unanswered", 0)) >= ucap:
            return "waiting_for_reply"

        gap = int(cfg.get("min_gap_minutes", 40) or 40) * 60
        if ts - int(rt.get("last_sent_ts", 0) or 0) < gap:
            return "sent_too_soon"

        last_user = int(rt.get("last_user_ts", 0) or 0)
        # [inner-patch 2026-11-11] 「他刚说过话」和「他已经收尾了」不是一回事。
        # 原来两者共用 min_gap：一天聊 30 轮的日子里空档最长 80 分钟，永远够不到 120，
        # 主动开口实际为 0（2026-11-11 全天模拟，4 次判断 3 次想发全被拦）。
        # 现在他最后一句若是收尾，安静 short_gap_minutes（默认 25）就能开口；
        # 还在聊的时候仍旧按 min_gap 等。
        talked = int(cfg.get("min_gap_minutes", 40) or 40)
        if closed:
            talked = int(cfg.get("short_gap_minutes", 25) or 25)
        if last_user and ts - last_user < talked * 60:
            return "talked_recently"

        return None


# ---------- 模型输出的解析 ----------


def _clip_text(text: str, limit: int) -> str:
    """[inner-fix 2026-11-15] 超长时切在最后一个句末标点上，不从字中间砍。

    原来这里是 text[:120]——她按提示词写 300 字，程序只留 120，
    于是 20:51 那条断在「…你要还是没定，也」。历史上正好 120 字的有 6 条。
    """
    t = str(text or "").strip()
    limit = max(20, int(limit or 300))
    if len(t) <= limit:
        return t
    head = t[:limit]
    cut = max(head.rfind(p) for p in "。！？!?…~")
    if cut >= int(limit * 0.5):
        return head[:cut + 1].strip()
    return head.rstrip("，、；：,.… ").strip()


def parse_decision(raw: str, max_chars: int = 300) -> dict:
    """把模型输出解析成 {send, reason, text}。

    接受：纯 JSON、```json 包裹、或前后带说明文字。
    解析不出来时保守地判定为"不发"。
    """
    s = str(raw or "").strip()
    if not s:
        return {"send": False, "reason": "empty", "text": ""}

    s = re.sub(r"^```(?:json)?|```$", "", s, flags=re.M).strip()
    m = re.search(r"\{.*\}", s, flags=re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            if isinstance(d, dict):
                send = bool(d.get("send"))
                text = str(d.get("text") or "").strip()
                reason = str(d.get("reason") or "").strip()
                if send and not text:
                    return {"send": False, "reason": "no_text", "text": ""}
                if send and not reason:
                    return {"send": False, "reason": "no_reason", "text": ""}
                return {
                    "send": send,
                    "reason": reason[:60],
                    "text": _clip_text(text, max_chars),
                }
        except Exception:
            pass

    # 退路：模型只回了一个「不」
    if s in ("不", "不发送", "无", "算了"):
        return {"send": False, "reason": "declined", "text": ""}
    return {"send": False, "reason": "unparsed", "text": ""}


def is_miss_you(text: str) -> bool:
    """是不是纯粹的「想你了」类。"""
    t = str(text or "").strip()
    return any(k in t for k in MISS_WORDS) and not any(k in t for k in GENERIC)


def is_generic(text: str, topics: list[str] | None = None) -> bool:
    """没有具体事由的泛泛开场（「想你了」不算）。"""
    t = str(text or "").strip()
    if not t:
        return True
    if is_miss_you(t):
        return False
    if any(k in t for k in GENERIC) and len(t) <= 12:
        return True
    topics = topics or []
    if topics and not any(k in t for k in topics):
        # 有念头却一个都没提，也不算错，但要够具体（长度超 12 字才放行）
        return len(t) <= 12
    return False
