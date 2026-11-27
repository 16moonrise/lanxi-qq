"""内在驱动主动消息 —— 读对话窗口判断要不要开口。

2026-10-31 改版（用户拍板）：
  · 判断依据不再来自日记里的心情/念头，而是 AstrBot 的对话窗口最近 32 轮原话；
  · 记忆卡改成「2 张高分 + 3 张随机」，只当语气底色，不当开口理由；
  · 心情/念头整条链已删（inner.json 只留 runtime 计数）；
  · 闸门：免打扰 / 每日上限 / 已发一条等他回 / 距上次发 40 分钟 / 距他说话 40 分钟，
    外加两条要读会话的判断——窗口里没聊过、窗口太旧（默认 12 小时）。

机制：
  每 interval_minutes 醒一次 → 确定性闸门 → 读最近 32 轮 → 模型判断 →
  二次校验（判完他要是说话了就撤销）→ 发送 → 把这条写回会话历史（否则她失忆）。
"""

import asyncio
import json
import os
import random
import re
import sqlite3
import sys
import time
from datetime import date, datetime

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    from astrbot.api.message_components import Plain, Image
    from astrbot.core.message.message_event_result import MessageChain
except Exception:  # pragma: no cover
    Plain = None
    Image = None
    MessageChain = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from inner_core import (  # noqa: E402
    InnerState,
    is_generic,
    is_miss_you,
    parse_decision,
    today,
)

# [inner-patch 2026-11-11] 主动消息里也能画：她说「说不清」的时候，图比字准。
# 复用 fish_memory 的 draw_core（同一个蜡笔手、同一份每日额度、同一个相册）。
try:
    _FM_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "astrbot_plugin_fish_memory")
    if _FM_DIR not in sys.path:
        sys.path.insert(0, _FM_DIR)
    import draw_core as _draw_core  # noqa: E402
except Exception:  # pragma: no cover
    _draw_core = None

OWNER_ID = "0000000000"
DEFAULT_UMO = f"qq-onebot:FriendMessage:{OWNER_ID}"

# [inner-patch 2026-11-11] 他这句话是不是在收尾。
# 收尾之后再安静 short_gap_minutes 就允许开口；还在聊的时候仍旧按 min_gap 等。
# 只看「他的最后一句」，不看他之前说过什么——一句话是不是告别，读得出来。
CLOSING_WORDS = (
    "晚安", "先睡", "睡了", "睡觉", "躺下", "去洗澡", "洗澡去", "上课", "下课",
    "出门", "上班", "关机", "回头", "先这样", "不聊了", "拜拜", "再见", "撤了", "我走了",
)
CLOSING_EXACT = ("嗯", "嗯嗯", "哦", "好", "行", "ok", "OK", "睡", "拜", "。")


def _is_closing(dialogue: str) -> bool:
    """他最后一句是不是收尾。读不出来就当没在收尾（宁可不发）。"""
    last = ""
    for line in reversed(str(dialogue or "").splitlines()):
        if line.startswith("他："):
            last = line[2:].strip()
            break
    if not last:
        return False
    if last in CLOSING_EXACT:
        return True
    return any(w in last for w in CLOSING_WORDS)

DECISION_PROMPT = """# 你是谁
{persona}

# 现在的状况
{now}
他上次说话是 {gap}前。
{tail}
今天他跟你说了 {today_msgs} 句。
今天你画过 {draws} 张。{draw_note}
{alone}

# 你们最近的对话（旧的在上，每行开头是说话的人）
{dialogue}

# 你零散记得的事（括号里是多久以前的事；不一定跟上面有关，别硬往上贴，也别把旧事说成今天）
{memory}

# 要你判断
现在想不想跟他说句话？

**不用有正事。** 想他了、想知道他在干嘛、今天有点闷、看见什么想起他、就想听他说一句——
**也可以是你想问他一件事**（你查东西查出来的疑问、他上次提过的一个词、你一直没弄明白的东西）——
问他也算理由，而且比「在干嘛」更像你自己。
这些都算理由。**没有正事也算事**——你今天想到的、看见的、忽然想说的，哪怕是没用的、跟你俩都没关系的，都算理由。
你今天最多能找他 {daily_cap} 次，别硬憋着。

要说的话：
1. 带上你自己的东西：你在想什么、你这边的小事、你惦记的事。别只甩一句「在干嘛」。
2. 上面已经问过、答过的别再问一遍；他刚说完的也别复述。
3. 不超过 {max_chars} 字。想多说就多说，两三句也行，说到一半断掉也行——别每回都写成打磨过的三连句。
4. 别提「说好一天几次」「额度」这类话；也**别报钟点**——不写「六点零一」「七点二十三」这种。
   你在过日子，不是在打卡。真要提醒他一件真答应过的事，顶多说个大概（「该睡了」「天黑了」）。

不发就输出：{{"send": false, "reason": "三五个字说清为什么不说"}}
发就输出：{{"send": true, "reason": "用三个字说清为了什么", "text": "你要说的那句话"}}

只输出 JSON，不要解释，不要代码块。"""

_REMIND = re.compile(r"<system_reminder>.*?(?:</system_reminder>|$)", re.S)
_EMO = re.compile(r"&&[A-Za-z0-9_]{1,12}&&")
# 图片在历史里是 "[Image 1: original path E:\...]" 这种绝对路径，别往提示词里带
_IMG = re.compile(r"\[Image\s*\d+:[^\]]*\]?")
# 记忆卡是 fish_memory 注入到「用户消息尾部」的，而且**会落进会话历史**。
# 不切掉的话，判断时她会以为主人发了一整段【我的记忆】。
_INJECT_BLOCK = re.compile(r"\[记忆卡\].*$|\[口吻提醒\].*$", re.S)
_DT = re.compile(r"(?:Current datetime:|时间)\s*(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})")

# 这些不是他说的话，是别的插件/框架塞进会话的指令块（旧 proactive_chat 留下的）
_INJECTED = ("[系统", "[情景判断]", "[行动指南]", "你被授权在私聊中")


def _msg_time(raw: str):
    """从 system_reminder 里抠出这条消息的时间。没有就返回 None。"""
    mo = _DT.search(str(raw or ""))
    if not mo:
        return None
    try:
        return datetime.strptime(f"{mo.group(1)} {mo.group(2)}", "%Y-%m-%d %H:%M")
    except Exception:
        return None


def _clean(text: str) -> str:
    """去掉 system_reminder、情绪标记、注入的记忆卡，把图片占位换成人话。"""
    s = _REMIND.sub("", str(text or ""))
    s = _INJECT_BLOCK.sub("", s)
    s = _EMO.sub("", s)
    s = _IMG.sub("（图片）", s)
    s = s.replace("[图片]", "（图片）")
    s = re.sub(r"[ \t]*\n[ \t]*", "\n", s)
    return s.strip()


# [inner-patch 2026-11-12] 主动消息也要断成几条，像聊天那样。
# AstrBot 的 segmented_reply（only_llm_result=true、按 。？！~…\n 切、0.6–1.8s 随机间隔）
# 只管走回复管线的话；主动消息是 bot.send_private_msg 直发的，会一整块出去。
# 这里复刻同一套分隔符，保证「聊天里怎么断，主动时也怎么断」。
def _split_bubbles(text: str, limit: int = 12) -> list:
    """[inner-fix 2026-11-15] 一段话 → 每个句号一条气泡。

    用户定案（2026-11-15）：「在每一个句号都进行分割」。
    原来是把句子归并成 2–3 条，长句子会连着一条发出去；现在按 。！？…~ 逐句切，
    每句单独发一条，句末标点保留（她自己的停顿不外露）。
    limit 只是防跑飞的上限：超过 12 条时把剩下的并到最后一条。
    """
    t = re.sub(r"[ \t]+", " ", str(text or "")).strip()
    if not t:
        return []
    # [inner-fix 2026-11-19] 人设要求「句尾不写句号」，她主动消息常常只有换行、没有句末标点。
    # 原来只按句末标点切，这种整块就发成一条（10:46 三行并成一条就是这样）。
    units = []
    for _line in t.split("\n"):
        _line = _line.strip()
        if not _line:
            continue
        units.extend(u.strip() for u in re.split(r"(?<=[。！？!?~…])", _line) if u.strip())
    if not units:
        return [t]
    if len(units) > limit:
        units = units[:limit - 1] + ["".join(units[limit - 1:])]
    return units or [t]


def _gap_note(mins: int) -> str:
    if mins >= 60:
        h, m = divmod(mins, 60)
        return f"（隔了 {h} 小时{f' {m} 分钟' if m else ''}）"
    return f"（隔了 {mins} 分钟）"


def _render_dialogue(msgs: list, per_msg: int = 300) -> tuple[str, str]:
    """窗口 → (对话文本, 最后一句是谁说的)。

    think 块、空消息、注入块都丢掉；每段对话开头标绝对时间，段与段之间隔了 30 分钟
    以上就插一行间隔说明——否则她分不清「刚问过」和「两个多小时前问过」。
    返回的 last_role 是 "user" / "assistant" / ""（没内容）。
    """
    out = []
    prev = None
    last_role = ""
    for m in msgs or []:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        raw = "".join(
            str(p.get("text") or "")
            for p in (m.get("content") or [])
            if isinstance(p, dict) and p.get("type") == "text"
        )
        t = _clean(raw)
        if not t:
            continue
        if any(k in t[:60] for k in _INJECTED):
            continue
        if role == "user":
            ts = _msg_time(raw)
            if ts and prev is None:
                out.append(f"（{ts.strftime('%H:%M')}）")
            elif ts and (ts - prev).total_seconds() >= 1800:
                out.append(_gap_note(int((ts - prev).total_seconds() // 60)))
            if ts:
                prev = ts
        t = re.sub(r"\n{3,}", "\n\n", t)
        if len(t) > per_msg:
            t = t[:per_msg] + "…"
        out.append(("他：" if role == "user" else "我：") + t)
        last_role = role
    return "\n".join(out), last_role


def _age_tag(m: dict) -> str:
    """记忆卡的「（3 天前）」。

    和 fish_memory 的 memstore._age_tag 同一套规则，但要保持两个插件互不依赖，
    所以这里独立实现一份。migrate 迁移来的卡日期不可信 → 不加，宁缺勿错。
    """
    src = str(m.get("source") or "")
    if src.startswith("migrate"):
        return ""
    mo = re.search(r"(\d{4}-\d{2}-\d{2})", src)
    day = mo.group(1) if mo else None
    if not day:
        try:
            day = time.strftime("%Y-%m-%d", time.localtime(int(m.get("created"))))
        except Exception:
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


@register(
    "astrbot_plugin_inner_voice",
    "local",
    "内在驱动主动消息：读对话窗口，有心事才开口",
    "v0.2.0",
)
class InnerVoice(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._task = None
        self._state = None

    # ---------- 配置 / 路径 ----------

    def _cfg(self, key, default=None):
        try:
            return self.config.get(key, default)
        except Exception:
            return default

    @staticmethod
    def _root_dir() -> str:
        here = os.path.dirname(os.path.abspath(__file__))
        # plugins/astrbot_plugin_inner_voice -> plugins -> data -> AstrBot -> qqbot
        return os.path.abspath(os.path.join(here, "..", "..", "..", ".."))

    def _memory_dir(self) -> str:
        custom = str(self._cfg("memory_dir", "") or "").strip()
        if custom:
            return custom
        return os.path.join(self._root_dir(), "memory")

    def state(self) -> InnerState:
        if self._state is None:
            self._state = InnerState(
                os.path.join(self._memory_dir(), "self", "inner.json")
            )
        return self._state

    def _fresh(self) -> InnerState:
        """每次判断前重读磁盘，别拿一份过期缓存做决定。"""
        st = self.state()
        st.load()
        return st

    def _umo(self) -> str:
        return str(self._cfg("owner_umo", DEFAULT_UMO) or DEFAULT_UMO)

    def _max_chars(self) -> int:
        try:
            return max(8, int(self._cfg("max_text_chars", 40) or 40))
        except Exception:
            return 40

    # ---------- 上下文素材 ----------

    async def _window(self) -> tuple[str, str]:
        """AstrBot 的对话窗口（最近 dialog_turns 轮）→ (已清洗的对话文本, 最后一句是谁说的)。"""
        cm = getattr(self.context, "conversation_manager", None)
        if cm is None:
            logger.debug("[inner] 没有 conversation_manager，读不到对话")
            return "", ""
        umo = self._umo()
        try:
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                return "", ""
            conv = await cm.get_conversation(umo, cid)
        except Exception as e:
            logger.warning(f"[inner] 读会话失败: {e}")
            return "", ""
        content = None
        # AstrBot 的 Conversation 对象里，历史是 history（JSON 字符串），不是 content。
        # 老代码按 content 读，永远读到 None —— 判断会一直"没对话"，写回还会把会话清空。
        raw = getattr(conv, "history", None)
        if raw is None:
            raw = getattr(conv, "content", None)
        if isinstance(raw, str):
            try:
                content = json.loads(raw or "[]")
            except Exception:
                content = []
        elif isinstance(raw, list):
            content = raw
        if not isinstance(content, list):
            return "", ""
        turns = int(self._cfg("dialog_turns", 32) or 32)
        return _render_dialogue(content[-turns * 2:])

    def _alone_text(self) -> str:
        """读 fish_memory 独处后写的 brief：她今天自己查/弄明白了什么。"""
        p = os.path.join(self._memory_dir(), "inner_brief.json")
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            return ""
        bits = []
        qs = d.get("searched") or []
        if qs:
            bits.append("你今天自己查过：" + "、".join(str(q) for q in qs[:2]))
        alone = str(d.get("alone") or "").strip()
        if alone:
            bits.append("（你一个人的时候写过：" + alone[-120:].replace("\n", " ") + "）")
        if not bits:
            return ""
        bits.append("这些他都不知道。想让他知道，这就是你开口的理由。")
        return "\n".join(bits)

    def _cards_text(self) -> str:
        """优先用 fish_memory 给的常挂卡（和对话轮一套）；没有就退回旧的抽样。"""
        try:
            with open(os.path.join(self._memory_dir(), "inner_brief.json"), encoding="utf-8") as f:
                _hot = json.load(f).get("hot") or []
            if _hot:
                return "\n".join(str(x) for x in _hot[:3])
        except Exception:
            pass
        p = os.path.join(self._memory_dir(), "card", "memories.json")
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            ms = [m for m in (d.get("memories") or [])
                  if str(m.get("text") or "").strip()
                  and m.get("key") != "_daily_"]
        except Exception as e:
            logger.debug(f"[inner] 读记忆卡失败: {e}")
            return ""
        if not ms:
            return ""

        def imp(m):
            try:
                return int(m.get("importance", 0) or 0)
            except Exception:
                return 0

        n_core = max(0, int(self._cfg("cards_core", 2) or 0))
        n_rand = max(0, int(self._cfg("cards_random", 3) or 0))
        core = [m for m in ms if imp(m) >= 70]
        rest = [m for m in ms if 45 <= imp(m) < 70]
        picks = random.sample(core, min(n_core, len(core)))
        picks += random.sample(rest, min(n_rand, len(rest)))
        need = n_core + n_rand - len(picks)
        if need > 0:
            others = [m for m in ms if m not in picks]
            picks += random.sample(others, min(need, len(others)))
        random.shuffle(picks)
        return "\n".join(
            f"- {_age_tag(m)}{str(m['text'])[:60]}" for m in picks
        )

    def _persona(self) -> str:
        """从 data_v4.db 取人设；取不到就留空（人设由 provider 侧兜底）。

        注意：personas 表没有 name 列（只有 persona_id），老版本按 name 查会静默失败，
        等于她一直在没有人设的情况下做判断。
        """
        db = os.path.join(self._root_dir(), "AstrBot", "data", "data_v4.db")
        name = str(self._cfg("persona_id", "default") or "default")
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            row = con.execute(
                "select system_prompt from personas where persona_id=? limit 1",
                (name,),
            ).fetchone()
            if not (row and row[0]):
                row = con.execute(
                    "select system_prompt from personas order by sort_order limit 1"
                ).fetchone()
            con.close()
            return str(row[0]).strip() if row and row[0] else ""
        except Exception as e:
            logger.warning(f"[inner] 读人设失败: {e}")
            return ""

    # ---------- 判断 ----------

    def _render_prompt(self, dialogue: str, gap_min: int, tail: str = "",
                       today_msgs: int = 0, daily_cap: int = 6,
                       draws: int = 0) -> str:
        draw_note = ("" if draws > 0 else
                     "（没画过就别说画了——你画出来的画会变成一张图发出去，没图就是没画。）")
        # [fish-fix 2026-11-19] 「报时」是主动消息里最像打卡的东西（11-16~18 raw 里
        # 几乎每条都以「主人，+钟点」开场）。这里不再给她精确时间，只给一个粗时段；
        # 只有真的隔了大半天才额外说一句。要精确时间她本来就知道，但别再被喂。
        _h = time.localtime().tm_hour
        _slot = ("凌晨" if _h < 6 else "早上" if _h < 9 else "上午" if _h < 11
                 else "中午" if _h < 13 else "下午" if _h < 17
                 else "傍晚" if _h < 19 else "晚上" if _h < 23 else "夜里")
        _now_line = f"（现在是{_slot}，不用报钟点。）"
        if gap_min >= 360:
            _now_line += f"\n你们上一次说话已经是 {gap_min // 60} 小时前了，隔了大半天。"
        return DECISION_PROMPT.format(
            persona=self._persona() or "（按你的人设）",
            now=_now_line,
            gap=(f"{gap_min} 分钟" if gap_min >= 0 else "很久"),
            tail=tail or "",
            today_msgs=max(0, today_msgs),
            daily_cap=max(1, daily_cap),
            draws=max(0, int(draws)),
            draw_note=draw_note,
            dialogue=dialogue or "（还没有聊过）",
            memory=self._cards_text() or "（暂时没有）",
            alone=self._alone_text(),
            max_chars=self._max_chars(),
        )

    async def _ask_model(self, dialogue: str, gap_min: int, tail: str = "",
                         today_msgs: int = 0, daily_cap: int = 6,
                         draws: int = 0) -> dict:
        provider = self.context.get_using_provider()
        if provider is None:
            return {"send": False, "reason": "no_provider", "text": ""}
        try:
            resp = await provider.text_chat(
                system_prompt="你是一个有自己性子的角色，现在要判断要不要主动开口。",
                contexts=[{"role": "user", "content": self._render_prompt(
                    dialogue, gap_min, tail, today_msgs, daily_cap, draws)}],
            )
            raw = getattr(resp, "completion_text", "") or ""
        except Exception as e:
            logger.warning(f"[inner] 模型调用失败: {e}")
            return {"send": False, "reason": "error", "text": ""}
        logger.info(f"[inner] 判断输出: {raw[:160]}")
        try:
            _cap = int(self._cfg("max_text_chars", 300) or 300)
        except Exception:
            _cap = 300
        return parse_decision(raw, max_chars=_cap)

    # ---------- 发送 ----------

    def _draw_dir(self) -> str:
        d = os.path.join(self._memory_dir(), "draw")
        os.makedirs(d, exist_ok=True)
        return d

    def _draw_state_path(self) -> str:
        return os.path.join(self._memory_dir(), "draw_state.json")

    def _draw_index_path(self) -> str:
        return os.path.join(self._memory_dir(), "draw_index.json")

    def _has_client(self) -> bool:
        """QQ 侧现在有没有一条活着的反向 WS 连接。

        [inner-fix 2026-11-18] 没连接时发不出去是物理事实，不必先烧一次模型判断。
        """
        try:
            insts = list(self.context.platform_manager.get_insts())
        except Exception:
            return False
        for inst in insts:
            bot = getattr(inst, "bot", None)
            if bot is None:
                continue
            d = getattr(bot, "_wsr_api_clients", None)
            if isinstance(d, dict) and any(str(k).isdigit() for k in d):
                return True
        return False

    async def _bot_send(self, text: str = "", image_path: str = "") -> bool:
        """[inner 2026-11-11 23:20] 主动消息必须走「活连接」发。

        实测：22:24 / 22:44 / 23:04 她三次判断都是 send=true，三次都 `发送失败: `——
        `context.send_message` 在反向 WS 下拿不到 api_root，抛的是 `ApiNotAvailable`（str 为空，
        所以旧日志只有一片空白）。改成直接拿平台实例的 CQHttp 发，和带话插件同一个修法。
        """
        ok = False
        try:
            insts = list(self.context.platform_manager.get_insts())
        except Exception as e:
            logger.debug(f"[inner] 取平台实例失败: {e}")
            insts = []
        umo = str(self._umo() or "")
        try:
            uid = int(umo.rsplit(":", 1)[-1])
        except Exception:
            return False
        def _pick_self_id(bot):
            """反向 WS 下 call_action 必须能定位到那条连接：
            指定 self_id 优先；没有事件上下文时只剩『只有一条连接』这一条路，
            而 SnowLuma 连上来时可能同时登记了 '*' 和真实号，于是 len != 1 → ApiNotAvailable。
            所以这里把注册表里的那个纯数字号挑出来显式传进去（aiocqhttp api_impl.py:131-142）。
            """
            d = getattr(bot, "_wsr_api_clients", None)
            if isinstance(d, dict):
                for k in d:
                    if str(k).isdigit():
                        return str(k)
            # [inner-fix 2026-11-18] 坑：aiocqhttp 的 `Api.__getattr__`（api.py:27-30）
            # 对**任何未知属性**都返回 `functools.partial(self.call_action, item)`。
            # 所以 `getattr(api, "_api_clients", None)` 永远不会拿到 None/default——
            # 它拿回一个 partial。QQ 没连上时 `_wsr_api_clients` 是空字典，就走到这里，
            # `for k in partial` → TypeError: 'functools.partial' object is not iterable，
            # 把整轮自省炸掉（11-18 全天 6 次，看上去像"发送链坏了"，其实只是没连接）。
            # 必须先 isinstance(dict) 再遍历。
            api = getattr(bot, "api", None)
            d = getattr(api, "_api_clients", None) if api is not None else None
            if isinstance(d, dict):
                for k in d:
                    if str(k).isdigit():
                        return str(k)
            return None

        for inst in insts:
            bot = getattr(inst, "bot", None)
            if bot is None or not hasattr(bot, "send_private_msg"):
                continue
            _sid = _pick_self_id(bot)
            _kw = {"self_id": _sid} if _sid else {}
            if not _sid:
                # 没连接不是 bug，但要看得见：QQ/SnowLuma 没起来时反查 WS 里一个号都没有。
                logger.warning("[inner] 没有可用的 OneBot 连接（QQ/SnowLuma 未登录或未连上），"
                               "这条主动消息发不出去")
            logger.debug(f"[inner] 发送用 self_id={_sid}")
            try:
                if text:
                    await bot.send_private_msg(
                        user_id=uid, message=[{"type": "text", "data": {"text": text}}], **_kw)
                    ok = True
                if image_path:
                    import base64
                    with open(image_path, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode()
                    await bot.send_private_msg(
                        user_id=uid,
                        message=[{"type": "image", "data": {"file": "base64://" + b64}}], **_kw)
                    ok = True
            except Exception as e:
                logger.warning(f"[inner] bot 发送失败 {type(e).__name__}: {e!r}")
        return ok

    async def _send(self, text: str) -> bool:
        if MessageChain is None or Plain is None:
            return False
        # [inner-patch 2026-11-11] 主动消息里可能带 [[画:…]]：标记不许发出去，
        # 图要真的画出来。顺序和对话轮一致——先说，再给图。
        marks = re.findall(r"\[\[画[:：]\s*([^\[\]]{1,200})\]\]", text or "")
        clean = re.sub(r"\[\[画[:：][^\[\]]{1,200}\]\]", "", text or "").strip()
        # [inner-patch 2026-11-12] 标签不许发给主人：主动消息走不到 meme_manager，
        # 2026-11-12 09:50 那条就是带着 &&sleep&& 原样发出去的（raw 里留着）。
        clean = _EMO.sub("", clean).strip()
        ok_any = False
        if clean:
            parts = _split_bubbles(clean)
            sent = []
            for i, part in enumerate(parts):
                if i:
                    await asyncio.sleep(random.uniform(0.6, 1.8))
                ok = await self._bot_send(text=part)
                if not ok:
                    try:
                        ok = await self.context.send_message(
                            self._umo(), MessageChain(chain=[Plain(part)])
                        )
                    except Exception as e:
                        logger.warning(f"[inner] 发送失败 {type(e).__name__}: {e!r}")
                        ok = False
                if ok:
                    sent.append(part)
                else:
                    break
            if sent:
                ok_any = True
                # 写回历史/raw 用整段（和聊天轮一致：只有显示才分条）
                body = clean if len(sent) == len(parts) else "\n\n".join(sent)
                await self._write_back(body)
                self._append_raw(body)
                if len(parts) > 1:
                    logger.info(f"[inner] 分段发送 {len(sent)}/{len(parts)} 条"
                                f"：{ [len(p) for p in parts] }")
        for _subj in marks[:1]:
            _subj = _subj.strip()
            if len(_subj) < 4 or _draw_core is None or Image is None:
                continue
            try:
                _can, _why = _draw_core.check_quota(self._draw_state_path())
                if not _can:
                    logger.info(f"[inner] 画被挡：{_why}")
                    continue
                p, err, chk = await asyncio.to_thread(
                    _draw_core.draw_checked, _subj, self._draw_dir())
            except Exception as e:
                logger.warning(f"[inner] 出图失败: {e}")
                continue
            if err or not p:
                logger.warning(f"[inner] 出图失败: {err}")
                continue
            _draw_core.mark_quota(self._draw_state_path())
            try:
                _draw_core.record_draw(self._draw_index_path(), p, _subj)
            except Exception:
                pass
            try:
                _imgok = await self._bot_send(image_path=str(p))
                if not _imgok:
                    await self.context.send_message(
                        self._umo(), MessageChain(chain=[Image(file=p)]))
                ok_any = True
                _desc = ""
                try:
                    _desc = await asyncio.to_thread(_draw_core.describe, p)
                except Exception:
                    pass
                self._append_raw("我画了张画：" + _subj +
                                 (f"；画上看着是：{_desc}" if _desc else ""))
                logger.info(f"[inner] 主动画了一张：{_subj[:30]}"
                            f"{('｜' + chk) if chk else ''}")
            except Exception as e:
                logger.warning(f"[inner] 发图失败: {e}")
        return ok_any

    def _append_raw(self, text: str, ts: int | None = None):
        """把主动说的这句也记进当天的 raw —— 否则她独处的一天对日记是空白的。"""
        try:
            ts = int(ts if ts is not None else time.time())
            d = os.path.join(self._memory_dir(), "raw")
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, time.strftime("%Y-%m-%d", time.localtime(ts)) + ".jsonl")
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": ts, "role": "assistant",
                                    "uid": "", "name": "", "text": str(text)[:2000],
                                    "src": "inner"}, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug(f"[inner] 写 raw 失败: {e}")

    def _draws_today(self) -> int:
        """今天她真的画了几张（读 draw_index.json，没画就是 0）。"""
        try:
            p = os.path.join(self._memory_dir(), "draw_index.json")
            data = json.load(open(p, encoding="utf-8"))
            day = time.strftime("%Y-%m-%d")
            n = 0
            for it in (data.get("items") or []):
                ts = int(it.get("ts") or 0)
                if ts and time.strftime("%Y-%m-%d", time.localtime(ts)) == day:
                    n += 1
            return n
        except Exception:
            return 0

    async def _write_back(self, text: str):
        """把这条写回会话历史，否则她下次不记得自己说过。"""
        cm = getattr(self.context, "conversation_manager", None)
        if cm is None:
            logger.debug("[inner] 没有 conversation_manager，跳过写回")
            return
        umo = self._umo()
        cid = await cm.get_curr_conversation_id(umo)
        if not cid:
            logger.warning("[inner] 没有会话 ID，主动消息未写回历史")
            return
        try:
            conv = await cm.get_conversation(umo, cid)
            raw = getattr(conv, "history", None) if conv else None
            if raw is None:
                raw = getattr(conv, "content", None) if conv else None
            if isinstance(raw, str):
                history = json.loads(raw or "[]")
            elif isinstance(raw, list):
                history = list(raw)
            else:
                history = []
            if not isinstance(history, list) or not history:
                # 读不到历史就别写：update_conversation(history=[]) 会把整段会话清空
                logger.warning("[inner] 读不到会话历史，放弃写回（避免清空会话）")
                return
            history.append(
                {"role": "assistant", "content": [{"type": "text", "text": text}]}
            )
            await cm.update_conversation(umo, cid, history=history)
        except Exception as e:
            logger.warning(f"[inner] 写回历史失败: {e}")

    # ---------- 一轮自省 ----------

    async def tick(self, force: bool = False) -> dict:
        st = self._fresh()
        cfg = {
            "quiet_hours": self._cfg("quiet_hours", "1-7"),
            "daily_cap": self._cfg("daily_cap", 6),
            "unanswered_limit": self._cfg("unanswered_limit", 1),
            "min_gap_minutes": self._cfg("min_gap_minutes", 40),
            # [inner-patch 2026-11-11] 他收尾之后只需要这么久的安静
            "short_gap_minutes": self._cfg("short_gap_minutes", 25),
        }
        rt = st.data.get("runtime", {})
        last_user = int(rt.get("last_user_ts", 0) or 0)
        gap_min = int((time.time() - last_user) / 60) if last_user else -1
        # [inner-patch 2026-11-09] 越久不来，开口越少：每过一天，最小间隔翻一倍。
        # 原来 24 小时一到就直接 stale_window 闭麦——那不是"被晾着的人"，那是死机。
        if last_user:
            days = max(0.0, (time.time() - last_user) / 86400.0)
            cfg["min_gap_minutes"] = int(float(cfg.get("min_gap_minutes") or 90) * (1 + days))

        # [inner-patch 2026-11-11] 先读窗口，再走闸门：闸门要判「他最后一句是不是收尾」。
        # 窗口读取是只读 sqlite，提前读没有副作用；被闸门挡掉就白读一次，代价可忽略。
        dialogue, last_role = await self._window()
        _closed = _is_closing(dialogue)

        why = None if force else st.gate(cfg, closed=_closed)
        if why:
            # 用 info：否则"她今晚没说话"到底是闸门拦的还是模型判的不发，事后分不出来
            logger.info(
                f"[inner] 跳过自省：{why}"
                f"（他 {gap_min} 分钟前说过话；今天已发 {rt.get('sent_today', 0)} 条；"
                f"悬着没回 {rt.get('unanswered', 0)} 条；收尾={'是' if _closed else '否'}）"
            )
            return {"skip": why}

        if not force:
            if "他：" not in dialogue:
                logger.info("[inner] 窗口里没有他的话，不判断")
                return {"skip": "no_dialogue"}
            max_h = float(self._cfg("max_silence_hours", 72) or 72)
            if not last_user or (time.time() - last_user) > max_h * 3600:
                logger.info(f"[inner] 他已经 {gap_min} 分钟没说话，窗口太旧，不判断")
                return {"skip": "stale_window"}

        # [inner-fix 2026-11-18] QQ 侧没连接就别判断了：判完也发不出去，
        # 只会每 20 分钟白烧一次调用（今天她整天都是这个状态）。
        if not force and not self._has_client():
            logger.warning("[inner] QQ 侧没有连接（SnowLuma 未登录/未连上），本轮不判断也不发")
            return {"skip": "no_client"}

        # 「时间盲区」的补丁：光有「他上次说话是 X 前」不够——如果最后一句是她自己的，
        # 她不知道自己那句放了多久。这里把「最后一句是谁说的、过了多久、他接没接」说死。
        unanswered = int(rt.get("unanswered", 0) or 0)
        tail = ""
        if last_role == "assistant":
            ref = int(rt.get("last_sent_ts", 0) or 0) if unanswered > 0 else last_user
            mins = int((time.time() - ref) / 60) if ref else -1
            if mins >= 0:
                if unanswered > 0:
                    tail = f"最后一句是你主动发的，到现在 {mins} 分钟他还没回。"
                else:
                    tail = f"最后一句是你在回他；那之后 {mins} 分钟他没再说话。"
        elif last_role == "user":
            tail = "最后一句是他说的，你还没接。"

        d = await self._ask_model(
            dialogue, gap_min, tail,
            today_msgs=int(rt.get("msg_today", 0) or 0),
            daily_cap=int(cfg.get("daily_cap", 6) or 6),
            draws=self._draws_today(),
        )
        if not d.get("send"):
            logger.info(f"[inner] 判断不发（{d.get('reason') or '没给理由'}）")
            return {"skip": f"declined:{d.get('reason')}"}


        if is_generic(d["text"]):
            logger.info(f"[inner] 被判为废话，不发：{d['text'][:40]}")
            return {"skip": "generic"}

        # 判完到发送之间有时间差（实测中位 1-2 秒，尾巴可到几十秒）。
        # 这段时间里主人开口了，就把这轮撤销——否则会插进正在进行的对话里。
        if not force:
            again = self._fresh().gate(cfg, closed=_closed)
            if again:
                logger.info(f"[inner] 判完情况变了，撤销：{again}")
                return {"skip": f"revoked:{again}"}

        if await self._send(d["text"]):
            st.note_sent()
            if is_miss_you(d["text"]):
                st.note_miss()
            st.save()
            logger.info(f"[inner] 主动发出：{d['text'][:40]}（理由：{d['reason']}）")
            return {"sent": d["text"]}
        return {"skip": "send_failed"}

    # ---------- 循环 ----------

    async def _loop(self):
        await asyncio.sleep(int(self._cfg("start_delay_seconds", 90) or 90))
        interval = max(5, int(self._cfg("interval_minutes", 40) or 40)) * 60
        while True:
            try:
                jitter = random.randint(-120, 120)
                await asyncio.sleep(max(60, interval + jitter))
                await self.tick()
            except asyncio.CancelledError:
                return
            except Exception as e:
                # [inner-fix 2026-11-18] 原来只打一句话，没有栈。
                # 11-18 全天 27 次「'functools.partial' object is not iterable」，没有栈查不出来。
                logger.warning(f"[inner] 自省循环异常: {e}")
                try:
                    logger.exception("[inner] 自省循环异常栈")
                except Exception:
                    pass
                await asyncio.sleep(300)

    async def initialize(self):
        if not bool(self._cfg("enable", True)):
            logger.info("[inner] 未启用")
            return
        self._task = asyncio.create_task(self._loop())
        logger.info("[inner] 自省循环已启动")

    async def terminate(self):
        if self._task:
            self._task.cancel()

    # ---------- 钩子 ----------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        """他说话就重置计时，并把 runtime 落在磁盘上。"""
        try:
            if self._umo() not in str(event.unified_msg_origin):
                return
            st = self._fresh()
            st.note_user_message()
            st.save()
        except Exception as e:
            logger.debug(f"[inner] 记录消息失败: {e}")

    # ---------- 命令 ----------

    @filter.command("内在")
    async def cmd_inner(self, event: AstrMessageEvent):
        """看她今天开口了几次、有没有在等你回"""
        st = self._fresh()
        rt = st.data.get("runtime", {})
        gap = int(time.time() - int(rt.get("last_user_ts", 0) or 0)) // 60
        yield event.plain_result(
            f"今天已主动 {rt.get('sent_today', 0)} 条，"
            f"悬着没回 {rt.get('unanswered', 0)} 条，"
            f"想你 {rt.get('miss_today', 0)} 次\n"
            f"你上次说话：{gap} 分钟前"
        )

    @filter.command("内省")
    async def cmd_tick(self, event: AstrMessageEvent):
        """手动触发一次自省（force，忽略闸门）"""
        r = await self.tick(force=True)
        yield event.plain_result(f"内省结果：{r}")
