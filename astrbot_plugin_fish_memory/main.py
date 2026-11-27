"""记忆管家：定时日记、每周自述、按需翻旧账。

目标不是"记住更多"，而是"忘得对"：
- 每天固定时间把当天聊的写成一篇小日记（不依赖上下文占用，断电最多丢一天）
- 每周用它自己的日记重写一次「我是谁」，保留历史版本
- 平时不占上下文；需要时可以通过 recall_history 去数据库里翻旧对话原文
"""

import asyncio
import json
import os
import random
import re
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timedelta

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from .memstore import MemoryStore, PrefStore
except Exception:  # pragma: no cover
    from memstore import MemoryStore, PrefStore

# [fish-draw 2026-11-08] 自绘涂鸦：核心逻辑放在 draw_core.py（不依赖 AstrBot，可离线跑）
# [fish-patch 2026-11-09 23:25] 卡片检索（默认模式）
try:
    from . import search_core as _search_core
except Exception:  # pragma: no cover
    import search_core as _search_core

try:
    from . import retrieval as _retrieval
except Exception:  # pragma: no cover
    import retrieval as _retrieval

try:
    from . import draw_core as _draw_core
except Exception:  # pragma: no cover
    import draw_core as _draw_core

# [fish-bili 2026-11-15] 她会刷 B 站：搜候选（限定分区）→ 她自己挑 → 抽帧看一眼
try:
    from . import bili_core as _bili_core
except Exception:  # pragma: no cover
    try:
        import bili_core as _bili_core
    except Exception:
        _bili_core = None

try:
    from astrbot.api.message_components import Image, Plain
    from astrbot.core.message.message_event_result import MessageChain
except Exception:  # pragma: no cover
    Image = None
    Plain = None
    MessageChain = None

try:
    from astrbot.core.agent.message import TextPart
except Exception:  # pragma: no cover
    TextPart = None


DAILY_TRIGGERS = (
    "前几天", "上次", "上回", "那天", "前阵子",
    "前两天", "最近几天", "吃过", "看过", "玩过", "说过",
)

# [fish-topic 2026-11-16] 同一个主题的不同叫法归到一个桶里。
# 主题别名表：同一个主题的不同叫法归并到一个桶；按角色自己的兴趣补充。
_TOPIC_ALIAS = ()


def _canon_topic(s: str) -> str:
    """把一句话/一个词归到主题桶。命中别名表就用桶名，否则退回第一个词。"""
    t = (s or "").strip()
    for keys, canon in _TOPIC_ALIAS:
        if any(k in t for k in keys):
            return canon
    head = t.split()[0] if " " in t else t[:6]
    return head[:16]


def _parse_hhmm(s: str, default=(23, 30)) -> tuple[int, int]:
    try:
        h, m = str(s).strip().split(":")
        return int(h), int(m)
    except Exception:
        return default


@register(
    "astrbot_plugin_fish_memory",
    "local",
    "每日日记、每周自述、翻旧账",
    "v1.0.0",
)
class FishMemory(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._task = None
        self._beat_task = None
        self._last_diary = ""
        self._last_self = ""
        self._injected: dict[str, str] = {}
        # 按轮数注入的记忆卡节流：{umo: {"day": 日期, "turns": 距上次注入的轮数, "last_ts": 上次注入时间}}
        self._inject_state: dict[str, dict] = {}
        # 常驻记忆卡缓存：{umo: 卡文本}。只缓存一份，每轮挂 system_prompt（不落历史）
        self._card_cache: dict[str, str] = {}
        self._base = None
        self._store_obj = None
        self._last_maint = ""
        self._pref_obj = None
        # [fish-self 2026-11-11] 自我描述上次挂载时间：{umo: ts}，一段对话只挂一次
        self._desc_at: dict[str, float] = {}
        # [fish-blackbox 2026-11-11] 她今天写的黑箱上次挂载时间，同样一段对话只挂一次
        self._bb_at: dict[str, float] = {}
        # [fish-diary 2026-11-11] 补写日记的冷却：{日期: 上次尝试时间}，失败一小时内不重试
        self._diary_tried: dict[str, float] = {}
        # [fish-draw 2026-11-11] 延迟发图的任务引用（不持有会被 GC，图就没了）
        self._bg_tasks = set()

    # ---------------- 基础 ----------------

    def _cfg(self, key, default=None):
        try:
            v = self.config.get(key, None)
            if v is None and isinstance(self.config, dict):
                v = self.config.get(key, None)
            return default if v is None else v
        except Exception:
            return default

    def _watched(self) -> list[str]:
        return [str(x) for x in (self._cfg("watch_umos", []) or [])]

    def _memory_dir(self) -> str:
        if self._base:
            return self._base
        custom = str(self._cfg("memory_dir", "") or "").strip()
        if custom:
            base = custom
        else:
            base = os.path.join(os.path.dirname(__file__), "data")
        os.makedirs(base, exist_ok=True)
        for sub in ("raw", "diary", "self"):
            os.makedirs(os.path.join(base, sub), exist_ok=True)
        self._base = base
        return base

    def _store(self) -> MemoryStore:
        if self._store_obj is None:
            card_dir = os.path.join(self._memory_dir(), "card")
            # 2.0：整份配置透传。旧版这里是白名单，只传 6 个 key，
            # 导致 decay_per_day / mention_gain / forget_floor 改了也不生效。
            cfg = dict(self.config or {})
            self._store_obj = MemoryStore(card_dir, cfg)
            self._store_obj.prefs = self._prefs()   # 3.0：偏好档案
        return self._store_obj

    def _prefs(self) -> PrefStore:
        if self._pref_obj is None:
            self._pref_obj = PrefStore(self._memory_dir(), dict(self.config or {}))
        return self._pref_obj

    def _astrbot_data_dir(self) -> str:
        try:
            p = str(StarTools.get_data_dir())
        except Exception:
            p = os.path.join(os.path.dirname(__file__), "..", "..", "plugin_data", "fish_memory")
        return os.path.abspath(os.path.join(p, "..", ".."))

    def _db_file(self) -> str:
        return os.path.join(self._astrbot_data_dir(), "data_v4.db")

    def _today(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    # ---------------- 自绘涂鸦的路径（2026-11-08） ----------------

    def _draw_dir(self) -> str:
        d = os.path.join(self._memory_dir(), "draw")
        os.makedirs(d, exist_ok=True)
        return d

    def _draw_state_path(self) -> str:
        return os.path.join(self._memory_dir(), "draw_state.json")

    def _draw_index_path(self) -> str:
        return os.path.join(self._memory_dir(), "draw_index.json")

    # ---------------- 刷 B 站（2026-11-15）----------------
    def _bili_state_path(self) -> str:
        return os.path.join(self._memory_dir(), "bili_state.json")

    def _bili_view_dir(self) -> str:
        d = os.path.join(self._memory_dir(), "blackbox", "view")
        os.makedirs(d, exist_ok=True)
        return d

    def _bili_allow(self) -> bool:
        """一天最多看 3 条（2026-11-15 用户定案）。"""
        try:
            st = json.load(open(self._bili_state_path(), encoding="utf-8"))
        except Exception:
            return True
        if str(st.get("day") or "") != self._today():
            return True
        return int(st.get("count") or 0) < 3

    def _bili_left(self) -> int:
        try:
            st = json.load(open(self._bili_state_path(), encoding="utf-8"))
        except Exception:
            return 3
        if str(st.get("day") or "") != self._today():
            return 3
        return max(0, 3 - int(st.get("count") or 0))

    async def _bili_pick_and_watch(self, provider, keyword: str) -> dict | None:
        """给候选 → 她挑 → 下载抽帧 → 看图。成功返回 {title,up,dur,tname,desc,seconds}。"""
        if _bili_core is None:
            return None
        _cands = await asyncio.to_thread(_bili_core.search, keyword, 6)
        # [fish-topic 2026-11-16] 去重：看过的片子不再给
        try:
            _seen_bv = {r.get("bvid") for r in self._bili_recent(24 * 7) if r.get("bvid")}
            _cands = [c for c in _cands if c["bvid"] not in _seen_bv]
        except Exception:
            pass
        if not _cands:
            logger.info("[fish-bili] 没搜到合适分区的：%s", keyword[:20])
            return None
        _lst = "\n".join(
            f"{i + 1}) {c['title']}（{c['up']}，{c['dur']}，{c['tname']}）"
            for i, c in enumerate(_cands))
        # 同一个话题 24 小时内已经看过：把这件事说给她，让她自己决定还要不要接着看
        _hint = ""
        try:
            _topic = _canon_topic(keyword)
            _same = [r for r in self._bili_recent(24) if r.get("topic") == _topic]
            if _same:
                _hint = (f"（「{_topic}」这个话题你昨天到今天已经看过 "
                         f"{len(_same)} 条了，最近一条是《{str(_same[-1].get('title'))[:24]}》。"
                         "还想接着看就挑，不想看就回 0。）\n")
        except Exception:
            pass
        _sysp = self._persona_hint(900) or ""
        _p = (f"你在网上搜了「{keyword}」，出来这些：\n{_lst}\n\n{_hint}"
              "想看哪一条？只回一个数字。都不想看就回 0，别写别的。")
        try:
            _r = await provider.text_chat(system_prompt=_sysp, prompt=_p)
            _t = ((getattr(_r, "completion_text", "") or "").strip()
                  or (getattr(_r, "reasoning_content", "") or "").strip())
        except Exception as e:
            logger.warning(f"[fish-bili] 挑片失败: {e}")
            return None
        _mo = re.search(r"\b([0-9])\b", _t or "")
        _idx = int(_mo.group(1)) if _mo else -1
        if not (1 <= _idx <= len(_cands)):
            logger.info("[fish-bili] 她没挑（回：%s）", (_t or "")[:20])
            return None
        _c = _cands[_idx - 1]
        _w = await asyncio.to_thread(_bili_core.watch, _c["bvid"], 120, 6,
                                     self._bili_view_dir())
        if not _w.get("sheet"):
            logger.info("[fish-bili] 没看成：%s", _w.get("err"))
            return None
        self._bili_mark(kw=keyword, bvid=_c["bvid"], title=_c["title"])
        _desc = await asyncio.to_thread(_bili_core.describe, _w["sheet"])
        logger.info("[fish-bili] 看了 %s《%s》（%s 秒）%s", _c["bvid"], _c["title"][:20],
                    _w.get("seconds"), "有描述" if _desc else "没描述")
        return {"title": _c["title"], "up": _c["up"], "dur": _c["dur"],
                "tname": _c["tname"], "desc": _desc, "seconds": _w.get("seconds")}

    async def watch_round(self, slot: str = "") -> str:
        """[fish-bili 2026-11-15] 看片时段：跟独处错开，一天三次窗口。

        用户定案「一天 3 条、和独处时间错开」。她先决定想不想看，想就写
        [[看:几个词]]；挑中一条才下载、抽帧、看图，然后记进黑箱和 raw。
        """
        if _bili_core is None:
            return "没有搜索模块"
        if not self._bili_allow():
            return "今天看够了"
        provider = self.context.get_using_provider()
        if provider is None:
            return "没有可用的模型"
        try:
            _p0 = (f"你这边现在是：{self._world_brief()}\n\n"
                   "现在是没人跟你说话的时候，你自己待着，闲得有点发慌。\n"
                   "想不想找点东西看看？想的话写 [[看:几个词]]（像查东西那样写），"
                   "不想就只回一个 0。只回一行。")
            _r0 = await provider.text_chat(
                system_prompt=self._persona_hint(900) or "", prompt=_p0)
            _t0 = ((getattr(_r0, "completion_text", "") or "").strip()
                   or (getattr(_r0, "reasoning_content", "") or "").strip())
            _m = re.search(r"\[\[看[:：]\s*([^\[\]]{1,40})\]\]", _t0 or "")
            if not _m:
                logger.info("[fish-bili] %s 不想看：%s", slot, (_t0 or "")[:20])
                return "她不想看"
            got = await self._bili_pick_and_watch(provider, _m.group(1).strip())
            if not got:
                return "没看成"
            _line = (f"（我看了 B 站一条：{got['title']}｜{got['up']}｜{got['dur']}｜"
                     f"{got['tname']}）" + (f"\n（看着像：{got['desc']}）" if got["desc"] else ""))
            # 记进当天黑箱与 raw，晚上写日记时她记得自己看过什么
            try:
                day = self._today()
                d = os.path.join(self._memory_dir(), "blackbox")
                os.makedirs(d, exist_ok=True)
                p = os.path.join(d, f"{day}.md")
                with open(p, "a", encoding="utf-8") as f:
                    f.write(f"\n\n## {datetime.now().strftime('%H:%M')} 看片\n{_line}")
            except Exception as _e:
                logger.debug(f"[fish-bili] 写黑箱失败: {_e}")
            try:
                self._append("assistant", _line)
            except Exception:
                pass
            return f"看了《{got['title'][:20]}》"
        except Exception as e:
            logger.warning(f"[fish-bili] 看片时段失败: {e}")
            return f"失败：{e}"

    def _bili_state_load(self) -> dict:
        try:
            st = json.load(open(self._bili_state_path(), encoding="utf-8"))
            return st if isinstance(st, dict) else {}
        except Exception:
            return {}

    def _bili_done_today(self) -> set:
        st = self._bili_state_load()
        if str(st.get("day") or "") != self._today():
            return set()
        return set(st.get("slots") or [])

    def _bili_mark_slot(self, key: str):
        """记下这个看片时段跑过了（不管她看没看），避免反复重试。"""
        try:
            st = self._bili_state_load()
            if str(st.get("day") or "") != self._today():
                st = {"day": self._today(), "count": 0, "slots": []}
            slots = list(st.get("slots") or [])
            if key not in slots:
                slots.append(key)
            st["slots"] = slots[-10:]
            with open(self._bili_state_path(), "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False)
        except Exception as e:
            logger.debug(f"[fish-bili] 记时段失败: {e}")

    def _bili_mark(self, kw: str = "", bvid: str = "", title: str = ""):
        """记一条额度，同时把「看过什么」记进 recent（去重用）。"""
        try:
            try:
                st = json.load(open(self._bili_state_path(), encoding="utf-8"))
            except Exception:
                st = {}
            if str(st.get("day") or "") != self._today():
                st = {"day": self._today(), "count": 0}
            st["count"] = int(st.get("count") or 0) + 1
            if bvid:
                rec = list(st.get("recent") or [])
                rec.append({"ts": int(time.time()), "kw": str(kw)[:30],
                            "bvid": str(bvid), "title": str(title)[:40],
                            "topic": _canon_topic(kw or title)})
                st["recent"] = rec[-30:]
            with open(self._bili_state_path(), "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False)
        except Exception as e:
            logger.debug(f"[fish-bili] 记额度失败: {e}")

    def _bili_recent(self, hours: int = 24) -> list:
        """最近看过哪些（去重用）：只算 24 小时内的。"""
        st = self._bili_state_load()
        cutoff = time.time() - hours * 3600
        return [r for r in (st.get("recent") or [])
                if isinstance(r, dict) and int(r.get("ts") or 0) >= cutoff]

    # ---------------- 记录消息 ----------------

    def _append(self, role: str, text: str, uid: str = "", name: str = ""):
        text = (text or "").strip()
        if not text:
            return
        try:
            path = os.path.join(self._memory_dir(), "raw", f"{self._today()}.jsonl")
            with open(path, "a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "ts": int(time.time()),
                            "role": role,
                            "uid": uid,
                            "name": name,
                            "text": text[:2000],
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except Exception as e:
            logger.warning(f"[fish_memory] 记录消息失败: {e}")

    def _read_day(self, day: str) -> list[dict]:
        path = os.path.join(self._memory_dir(), "raw", f"{day}.jsonl")
        out = []
        if not os.path.exists(path):
            return out
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue
        except Exception as e:
            logger.warning(f"[fish_memory] 读取当天记录失败: {e}")
        return out

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def record_user(self, event: AstrMessageEvent):
        try:
            if event.unified_msg_origin not in self._watched():
                return
            text = (event.message_str or "").strip()
            if not text or text.startswith("/"):
                return
            if self._is_relay_handled(text) or self._looks_like_relay(text):
                return
            self._append(
                "user",
                text,
                uid=str(event.get_sender_id()),
                name=str(event.get_sender_name() or ""),
            )
        except Exception as e:
            logger.debug(f"[fish_memory] record_user 异常: {e}")

    # [fish-merge 2026-11-16] 他连着发的几句，合成一轮再给她。
    # 用户实感：「她会回复我说的每句话」——他 20:36:27 说「小鱼小鱼呀」、20:36:40 说
    # 「我要去玩游戏啦」，她回了两条。根因不是她逐句理解，是每条 QQ 消息各算一个事件、
    # 各跑一轮 LLM。这里只拦「短、没标点、不像问句」的话：等 N 秒，把紧跟着的几句并成一段。
    _pending_merge: dict = {}

    @staticmethod
    def _merge_holdable(text: str) -> bool:
        """[2026-11-21] 只按长度判：≤12 字就等等看他还会不会接着说。

        用户定案：「长度 ≤12 字就等等看我说什么，其它规则删掉」。
        原来还看结尾标点和「吗/？/怎么/什么/哪」——他习惯在句子中间带问号
        （「小笨龙？有没有可能」），被误判成完整句，合并永远触发不了。
        代价：单独发一句短问题也会等 5 秒。
        """
        t = (text or "").strip()
        return bool(t) and len(t) <= 12

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def merge_short(self, event: AstrMessageEvent):
        try:
            umo = event.unified_msg_origin
            if umo not in self._watched():
                return
            text = (event.message_str or "").strip()
            if not text or text.startswith("/"):
                return
            if self._is_relay_handled(text) or self._looks_like_relay(text):
                return
            st = self._pending_merge.get(umo)
            if not self._merge_holdable(text):
                # 完整的一句话：如果前面还有在等的短句，先并到那一段里去
                if st and st.get("texts"):
                    st["texts"].append(text)
                    event.should_call_llm(True)
                    logger.info("[fish-merge] 完整句并入等待中的 %d 条", len(st["texts"]))
                return
            if st is None or st.get("leader") is None:
                st = {"texts": [text], "leader": event}
                self._pending_merge[umo] = st
                wait = float(self._cfg("merge_short_seconds", 15) or 15)
                await asyncio.sleep(wait)
                merged = "\n".join(st["texts"])
                self._pending_merge.pop(umo, None)
                if len(st["texts"]) > 1:
                    try:
                        event.message_str = merged
                        event.message_obj.message_str = merged
                    except Exception:
                        pass
                    logger.info("[fish-merge] 合并 %d 条 -> %d 字：%s",
                                len(st["texts"]), len(merged), merged[:40].replace("\n", " / "))
            else:
                st["texts"].append(text)
                event.should_call_llm(True)
                logger.info("[fish-merge] 收到第 %d 条短句，先不回", len(st["texts"]))
        except Exception as e:
            logger.debug(f"[fish-merge] 异常: {e}")

    @filter.on_llm_response()
    async def record_bot(self, event: AstrMessageEvent, resp):
        try:
            if event.unified_msg_origin not in self._watched():
                return
            if getattr(resp, "role", "") == "err":
                return
            text = (getattr(resp, "completion_text", "") or "").strip()
            # [fish-patch 2026-11-07 14:00] 空正文兜底（交接 7.6 建议 4）。
            # 线上 3~6% 的回复 finish=stop 但正文为空，现在什么都不发。
            # 只兜"纯聊天轮"：带工具调用的轮次正文为空是正常现象，不能塞字。
            if not text and not getattr(resp, "tools_call_args", None) \
                    and not getattr(resp, "tools_call_name", None):
                try:
                    resp.completion_text = "嗯"
                    text = "嗯"
                    self._empty_hits = int(getattr(self, "_empty_hits", 0)) + 1
                    # [fish-patch 2026-11-09 13:05] 空正文归因打点：
                    # finish=length → 输出预算被挤（思考吃光）；finish=stop → 模型行为问题。
                    # 之前只有一句"已补一句"，没有任何可判定的字段，8 次空正文至今悬案。
                    _fr, _usage = "?", None
                    try:
                        _rc = getattr(resp, "raw_completion", None)
                        _ch = getattr(_rc, "choices", None) if _rc is not None else None
                        if _ch:
                            _fr = getattr(_ch[0], "finish_reason", "?")
                    except Exception:
                        pass
                    try:
                        _usage = getattr(resp, "usage", None)
                    except Exception:
                        pass
                    _think = getattr(resp, "reasoning_content", "") or ""
                    logger.warning(
                        f"[fish-guard] 空正文 finish={_fr} think={len(_think)}字 "
                        f"usage={_usage}（累计 {self._empty_hits} 次）"
                    )
                except Exception:
                    pass
            # [fish-patch 2026-11-09 21:47] 工具调用统一解析（她不会只用一种写法）
            # 实测三种都出现过：
            #   1) DeepSeek 原生 DSML：<|DSML| invoke name="draw"><|DSML| parameter name="subject">…
            #   2) XML 伪标签：<draw><subject>…</subject></draw>
            #   3) 我们自己的标记：[[画:…]]
            # 统一抽出来 -> 删干净正文 -> 执行。图先发、字后发。
            try:
                _txt = str(text or "")
                _subj = ""
                _hit = False
                _missed = []   # [fish-fix 2026-11-18] 她调了但没实现的工具，事后要记账
                if "invoke" in _txt and ("DSML" in _txt or "tool_calls" in _txt):
                    for _name, _args in self._parse_dsml(_txt):
                        logger.info("[fish-tool] DSML %s args=%s", _name,
                                    {k: str(v)[:30] for k, v in _args.items()})
                        if _name == "draw" and not _subj:
                            _subj = str(_args.get("subject") or "").strip()
                        elif _name in ("write_letter_to_mom", "write_letter"):
                            # [fish-fix 2026-11-18] 以前这里只打一行日志就把整段切掉：
                            # 她以为信写出了、其实什么都没发生——程序替她撒谎。
                            _body = str(_args.get("content") or _args.get("text") or "").strip()
                            if not _body:
                                _missed.append("写信")
                                continue
                            try:
                                _ok, _msg = self._write_letter(_body)
                                logger.info("[fish-tool] DSML 写信 -> %s（%s）", _ok, _msg)
                                if not _ok:
                                    _missed.append("写信")
                            except Exception as _e:
                                logger.warning(f"[fish-tool] DSML 写信失败: {_e}")
                                _missed.append("写信")
                        else:
                            _missed.append(str(_name))
                    _at = _txt.find("DSML")
                    if _at < 0:
                        _at = _txt.find("tool_calls")
                    if _at >= 0:
                        _head = _txt.rfind("<", 0, _at)
                        _txt = _txt[:(_head if _head >= 0 else _at)]
                    _hit = True
                _m = self._RE_DRAW_XML.search(_txt)
                if _m:
                    _inner = _m.group(1)
                    _ms = self._RE_SUBJECT_XML.search(_inner)
                    if not _subj:
                        _subj = (_ms.group(1) if _ms else re.sub(r"<[^>]{1,40}>", "", _inner)).strip()
                    _txt = _txt[:_m.start()] + _txt[_m.end():]
                    _hit = True
                for _kind, _arg in self._RE_MARK.findall(_txt):
                    if _kind == "画":
                        if not _subj:
                            _subj = (_arg or "").strip()
                        continue
                    # [fish-fix 2026-11-18] 查/翻 这些标记对话轮里没有实现，
                    # 以前只打一行 info 就把标记删了，等于她说了"我去查"、程序当没听见。
                    logger.warning("[fish-tool] 标记 %s 对话轮未实现：%s",
                                   _kind, (_arg or "")[:40])
                    _missed.append(f"{_kind}「{(_arg or '')[:12]}」")
                if _missed:
                    # 不静默：落一行到她自己的 raw（当晚日记会捡到），日志已用 warning
                    try:
                        self._append("assistant", f"（想用{'、'.join(_missed)}，没调成）")
                    except Exception:
                        pass
                if _subj:
                    _txt = self._RE_MARK.sub("", _txt)
                    _hit = True
                # 兜底：正文里任何残留的工具标记一律清掉，绝不让它漏到聊天里
                _txt = re.sub(r"</?(?:draw|subject)[^>]{0,40}>", "", _txt)
                _txt = self._RE_MARK.sub("", _txt).strip()
                if _hit:
                    text = _txt
                    try:
                        resp.completion_text = _txt
                    except Exception:
                        pass
                if _subj:
                    logger.info("[fish-tool] 工具轮 画：%s", _subj[:40])
                    if len(_subj) >= 4:
                        # [fish-draw 2026-11-11] 对话轮：正文先走、图后到（defer=True）
                        _ret = await self._draw_for_chat(event, _subj, defer=True)
                        # 没画成（冷却/额度/失败）时留一句在她自己的上下文里，
                        # 否则她下一轮会以为自己画过了 —— 之前「我又画了张」那种谎就是这么来的。
                        if not str(_ret or "").startswith("画已经发出去了"):
                            try:
                                self._append("assistant", f"（想画，没画成：{str(_ret or '')[:40]}）")
                            except Exception:
                                pass
                            logger.info("[fish-tool] 画失败/被挡：%s", str(_ret or "")[:60])
                            # [fish-fix 2026-11-19] 失败必须让他知道：原来只写日志 + raw，
                            # 用户那边一个字都收不到，只看到"她答应了、图没来"。11-19 17:36 就是这样。
                            try:
                                if MessageChain is not None and Plain is not None:
                                    await event.send(MessageChain([Plain("这张没画成，画坏了。")]))
                            except Exception as _e2:
                                logger.warning(f"[fish-tool] 画失败通知没发出去: {_e2}")
            except Exception as _e:
                logger.warning(f"[fish-tool] 工具解析失败: {_e}")
            # [fish-guard 2026-11-02] 拦「续写剧本」：伪造时间戳尾巴、UI 标记、纯旁白气泡
            # [fish-guard 2026-11-11] 先清纯符号残渣，再走泄露过滤
            text = self._scrub_symbols(text)
            clean, why = self._strip_leaks(text)
            if why and clean.strip():
                logger.warning(
                    f"[fish-guard] 输出过滤：{why}｜{len(text)}→{len(clean)} 字"
                )
                try:
                    resp.completion_text = clean
                except Exception:
                    pass
                text = clean.strip()
            # [fish-patch 2026-11-09 23:50] 对话里的结清：[[结清:兑现|账:肠]] / [[结清:作废|约定:看海]]
            try:
                _cl = re.findall(r"\[\[结清[:：]?\s*(兑现|作废)?\s*[|｜]?\s*([^\[\]]{1,24})\]\]", text or "")
                if _cl:
                    _st = self._store()
                    _ok = 0
                    for _out, _k in _cl:
                        if _st.close_card((_k or "").strip(), _out or "兑现"):
                            _ok += 1
                    if _ok:
                        _st.save()
                    text = re.sub(r"\[\[结清[:：]?[^\[\]]{1,40}\]\]", "", text).strip()
                    try:
                        resp.completion_text = text
                    except Exception:
                        pass
                    logger.info("[fish-mem] 对话结清 %d 条：%s", _ok, _cl)
            except Exception as _e:
                logger.warning(f"[fish-mem] 结清解析失败: {_e}")

            # [fish-patch 2026-11-09 23:00] 从对话里长出来的「想知道」
            try:
                _cur = re.findall(r"\[\[想查[:：]\s*([^\[\]]{1,80})\]\]", text or "")
                if _cur:
                    for _q in _cur:
                        self._curiosity_add(_q, "talk")
                    text = re.sub(r"\[\[想查[:：][^\[\]]{1,80}\]\]", "", text).strip()
                    try:
                        resp.completion_text = text
                    except Exception:
                        pass
            except Exception as _e:
                logger.warning(f"[fish-curious] 解析失败: {_e}")

            # [fish-tell 2026-11-11] 她真的把攒着的那句说出口了，就标结清
            try:
                self._tell_mark_said(text)
            except Exception as _e:
                logger.debug(f"[fish-tell] 标记失败: {_e}")

            # [fish-patch 2026-11-09 21:35] 显示分段：她写成一整段，发出去切成 2-3 条。
            # 历史清洗（P 臂）让她只会写单段，但 QQ 上的「连发」是她的节奏：
            # raw 存单段（下次注入还会被压平），resp.completion_text 分段只为显示。
            try:
                _disp = self._bubblefy(text)
                if _disp != text:
                    resp.completion_text = _disp
                    logger.info("[fish-bubble] 分段显示：%d 字 -> %d 段",
                                len(text), _disp.count("\n\n") + 1)
                else:
                    logger.info("[fish-bubble] 未分段：len=%d 含句号=%s 已有空行=%s",
                                len(text), "。" in text, "\n\n" in text)
            except Exception as _e:
                logger.warning(f"[fish-bubble] 分段失败: {_e}")
            if text and not self._is_relay_echo(text):
                self._append("assistant", text)
        except Exception as e:
            logger.debug(f"[fish_memory] record_bot 异常: {e}")

    _LEAK_MARKERS = ("<system_reminder>", "▶", "&&end")

    # [fish-patch 2026-11-09 21:35] 把一整段话按句子切成 2-3 条气泡（只影响显示，不落 raw）。
    _RE_TAG_TAIL = re.compile(r"(&&[A-Za-z0-9_]{1,16}&&)\s*$")
    # [fish-patch 2026-11-09 21:50] 工具标记：[[画:...]] / [[查:...]] / [[翻:...]]
    _RE_MARK = re.compile(r"\[\[(画|查|翻)\s*[:：]\s*([^\[\]]{1,200})\]\]")
    # 她自己发明的 XML 伪标签：<draw><subject>…</subject></draw>（2026-11-09 21:45 实见）
    _RE_DRAW_XML = re.compile(r"<draw[^>]{0,40}>(.*?)(?:</draw\s*>|$)", re.S)
    _RE_SUBJECT_XML = re.compile(r"<subject[^>]{0,40}>(.*?)(?:</subject\s*>|$)", re.S)

    # [fish-patch 2026-11-09 21:40] DeepSeek 原生 DSML 工具调用：<｜｜DSML｜｜ invoke name="draw">…
    _RE_DSML_TAG = re.compile(r"<[^<>]{0,200}?(?:DSML|tool_calls)[^<>]{0,200}?>", re.S)
    _RE_DSML_INV = re.compile(r'invoke\s+name="([^"]{1,40})"(.*?)(?=invoke\s+name=|$)', re.S)
    _RE_DSML_PRM = re.compile(r'parameter\s+name="([^"]{1,40})"[^>]*>(.*?)(?=</[^<>]*parameter>|$)', re.S)

    def _parse_dsml(self, text: str):
        """把原生 DSML 调用解析成 [(工具名, {参数名: 值})]。"""
        out = []
        for name, body in self._RE_DSML_INV.findall(text or ""):
            args = {}
            for k, v in self._RE_DSML_PRM.findall(body):
                v = self._RE_DSML_TAG.sub("", v).strip()
                args[k] = v
            out.append((name.strip(), args))
        return out

    def _bubblefy(self, text: str) -> str:
        """[fish-patch 2026-11-15] 每个句号一条气泡。

        用户定案：「在每一个句号都进行分割」。原来这里是归并成 2–3 组，
        且文本里已有空行就整段跳过；现在按 。！？!?…~ 逐句切，
        每句一个空行分隔——AstrBot 的 segmented_reply 再按空行拆成多条发出去。
        只影响显示（resp.completion_text），raw 与会话历史保持整段。
        """
        t = (text or "").strip()
        if not t:
            return text
        tag = ""
        mo = self._RE_TAG_TAIL.search(t)
        if mo:
            tag = mo.group(1)
            t = t[:mo.start()].strip()
        parts = [p.strip() for p in re.split(r"(?<=[。！？!?~…])", t) if p.strip()]
        if len(parts) < 2:
            return text
        if tag:
            parts[-1] = parts[-1].rstrip() + tag
        return "\n\n".join(parts)

    # ---------------- 自己的分段发送（2026-11-20） ----------------
    # AstrBot 的 platform_settings.segmented_reply 已关掉，气泡改由这里发：
    # 规则和主动消息一致——先按换行切，再按 。！？!?~… 逐句切（「！」也算分条点）；
    # 句末的「。」照旧不显示（原来由 AstrBot 的 content_cleanup_rule 删，这里自己删）。
    _RE_SENT_END = re.compile(r"(?<=[。！？!?~…])")

    def _split_send(self, text: str) -> list:
        """整段 → 待发送的气泡列表。空行只用来断句，不产生空气泡。"""
        t = (text or "").strip()
        if not t:
            return []
        units = []
        for line in t.split("\n"):
            line = line.strip()
            if not line:
                continue
            units.extend(u.strip() for u in self._RE_SENT_END.split(line) if u.strip())
        out = []
        for u in units:
            u = u.rstrip()
            while u.endswith("。"):
                u = u[:-1].rstrip()
            if not u:
                continue
            # [fish-bubble 2026-11-20] 纯符号/emoji 的碎尾巴（「🐉」「！」「~」）贴回上一条，
            # 否则会出现一条消息里只有个 🐉。
            if out and len(u) <= 3 and not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", u):
                out[-1] = out[-1] + u
                continue
            out.append(u)
        return out

    @filter.on_decorating_result()
    async def split_bubbles_own(self, event: AstrMessageEvent):
        """发送前把一条回复拆成多条发出去（只处理纯文字、只处理主人私聊）。"""
        try:
            if event.unified_msg_origin not in self._watched():
                return
            result = event.get_result()
            if result is None or Plain is None or MessageChain is None:
                return
            chain = getattr(result, "chain", None) or []
            if not chain:
                return
            texts = []
            for comp in chain:
                if not isinstance(comp, Plain):
                    return          # 带图/表情的混合链不插手
                texts.append(getattr(comp, "text", "") or "")
            bubbles = self._split_send("".join(texts))
            if len(bubbles) < 2:
                return
            for b in bubbles[:-1]:
                await event.send(MessageChain([Plain(b)]))
                await asyncio.sleep(random.uniform(0.6, 1.6))
            result.chain = [Plain(bubbles[-1])]
            logger.info("[fish-bubble] 自发分段：%d 段", len(bubbles))
        except Exception as e:
            logger.warning(f"[fish-bubble] 自发分段失败: {e}")

    # [fish-guard 2026-11-03] 「报名表 / 广告模板」：一行一个字段（地址：…、开课日期：…、保证金…）。
    # 2026-11-03 12:56 实见一次：回「知道啦」时 think 为空，模型把训练数据里的课程广告续在正文后面。
    _AD_FIELD_RE = re.compile(
        r"^\s*(?:地\s*址|开课日期|上课日期|报名截止|截止日期|课程时间|上课时间|"
        r"费用|学费|收费|人数|名额|限额|报名方式|联系方式|联系电话|咨询电话|微信号)\s*[:：]"
    )
    _AD_WORDS = ("保证金", "额满为止", "报名从速", "名额有限", "限额")

    # [fish-guard 2026-11-07 19:36] 「自检式泄露」：模型在正文里念规则、改自己的草稿、选表情包标签。
    # 11-07 19:24:29 实见一次：正文出现「规则不让写括号动作旁白」「改成“别到时候没影了”」
    # 「两句。加 happy？用 happy」——被分段发成 4~5 个气泡，主人全程看见她念规则。
    # 只认「讨论她自己怎么写」的说法；强标记命中 1 个、弱标记命中 2 个才动手。
    _SELF_TALK_STRONG = re.compile(
        r"规则(?:不让|要求|里说|上说|说不能|不允许|不让写)"
        r"|(?:加|用)\s*(?:happy|angry|sad|sleep|food|分类键)"
        r"|改成[“\"']"
        r"|不用括号|不许写括号|括号动作旁白"
        r"|提示词|分类键"
    )
    _SELF_TALK_WEAK = re.compile(r"两句[。，,、]|太较真|算期待|这样也踏实|格式要求")

    # [fish-guard 2026-11-07 22:00] 「排练」结构判据：同一段答案被写了两遍 = 她把自己的草稿也发了。
    # 11-07 两条泄露措辞完全不同（一条讲括号，一条讲 happy），但都把答案复读了一遍；
    # 词表永远追不上新措辞，结构追得上。短于 _DUP_MIN 字的段不参与比较（防「主人」「嗯」）。
    _DUP_MIN = 6

    def _strip_leaks(self, text: str):
        """[fish-guard 2026-11-02] 删掉模型「续写剧本」时混进正文的东西：
        伪造的 <system_reminder> 时间尾巴、UI 标记（▶ / &&end）、整条式的旁白（…）。
        只删命中的片段，正文照常留存；返回 (清理后文本, 原因)。"""
        try:
            t = str(text or "")
            if not t.strip():
                return t, ""
            why = []
            cut = len(t)
            for mk in self._LEAK_MARKERS:
                i = t.find(mk)
                if i != -1:
                    cut = min(cut, i)
            if cut < len(t):
                why.append("截断于 %r" % t[cut:cut + 16])
                t = t[:cut]
            bubbles = re.split(r"\n\s*\n", t)
            # 出现整条式旁白就是「续写剧本」的信号，从那里起全截掉（她的正常回复不会有旁白）
            for bi, b in enumerate(bubbles):
                if re.fullmatch(r"\s*[（(][^）)]*[）)]\s*", b):
                    why.append("从旁白处截断（丢 %d 段）" % (len(bubbles) - bi))
                    bubbles = bubbles[:bi]
                    break
            # [fish-guard 2026-11-03] 「正文 + 报名表」：字段行/广告词合计 ≥2 段，就从第一段起全截
            # （她正常聊天不会列「地址/日期/费用/保证金」这种表；宁可丢后几段，也别把编的地址发出去）
            ad_hits = 0
            ad_first = None
            for bi, b in enumerate(bubbles):
                if self._AD_FIELD_RE.match(b) or any(w in b for w in self._AD_WORDS):
                    ad_hits += 1
                    if ad_first is None:
                        ad_first = bi
            if ad_hits >= 2 and ad_first is not None:
                if ad_first > 0:
                    why.append("从广告模板处截断（丢 %d 段）" % (len(bubbles) - ad_first))
                    bubbles = bubbles[:ad_first]
                else:
                    logger.warning(
                        "[fish-guard] 整条都是广告模板，未截断（%d 段）：%s",
                        ad_hits, t[:80].replace("\n", " / "),
                    )
            # [fish-guard 2026-11-07 19:36] 自检式泄露：从命中那句起，整条之后全截掉
            # （和「旁白」「广告模板」同一策略：宁可丢后几段，也不把她的内心排练发出去）
            st = self._self_talk_hit("\n\n".join(bubbles))
            if st is not None:
                pos, kw = st
                keep, acc = [], 0
                for b in bubbles:
                    if acc + len(b) > pos:
                        break
                    keep.append(b)
                    acc += len(b) + 2
                if not any(len(b.strip()) >= 2 for b in keep):
                    # 整条都在自检：退一步只留最后一句干净的话（通常是她定稿的那句）
                    clean_tail = [b.strip() for b in bubbles
                                  if b.strip() and not self._self_talk_hit(b)]
                    keep = [clean_tail[-1]] if clean_tail else ["嗯"]
                why.append("从自检处截断（命中 %r）" % kw)
                bubbles = keep
            # [fish-guard 2026-11-07 22:00] 排练/复读：答案写了两遍，就只留第一遍
            cut_b, lost = self._rehearsal_cut(bubbles)
            if lost:
                why.append("从复读处截断（丢 %d 段）" % lost)
                bubbles = cut_b
            # 续写时会连发十几条「主人？」式追问，正常 3–6 条，超过 8 条就截断
            if len(bubbles) > 8:
                why.append("气泡截到 8 条（原 %d）" % len(bubbles))
                bubbles = bubbles[:8]
            t = "\n\n".join(bubbles)
            t = re.sub(r"\n{3,}", "\n\n", t).strip()
            return t, "；".join(why)
        except Exception:
            return text, ""

    def _self_talk_hit(self, text: str):
        """[fish-guard 2026-11-07 19:36] 正文里在讨论「她自己该怎么写」= 自检泄露。
        返回 (命中位置, 命中词)；没命中返回 None。"""
        try:
            t = str(text or "")
            if not t:
                return None
            m = self._SELF_TALK_STRONG.search(t)
            if m:
                return m.start(), m.group(0)
            weak = list(self._SELF_TALK_WEAK.finditer(t))
            if len(weak) >= 2:
                return weak[0].start(), weak[0].group(0)
            return None
        except Exception:
            return None

    def _rehearsal_cut(self, bubbles):
        """[fish-guard 2026-11-07 22:00] 找「同一段被复读」的位置，返回 (新的 bubbles, 丢掉几段)。
        取最长的一段重复（≥1 段、总字数 ≥ _DUP_MIN），从它**第一遍结束处**截断：
        后面的自我辩论和复读一起丢，前面的答案留住。没命中返回原样。"""
        try:
            n = len(bubbles)
            keys = [re.sub(r"\s+", "", b or "") for b in bubbles]
            best = None  # (重复段长度, 第一遍起点, 第一遍终点)
            for i in range(n):
                for j in range(i + 2, n):
                    k = 0
                    while (i + k < n and j + k < n
                           and keys[i + k] and keys[i + k] == keys[j + k]):
                        k += 1
                    if k and (best is None or k > best[0]):
                        best = (k, i, i + k)
            if not best:
                return bubbles, 0
            k, i, end = best
            if sum(len(keys[x]) for x in range(i, end)) < self._DUP_MIN:
                return bubbles, 0
            return bubbles[:end], n - end
        except Exception:
            return bubbles, 0

    _RELAY_RE = None

    def _looks_like_relay(self, text: str) -> bool:
        """带话请求本身不算生活：结构匹配，不依赖插件执行顺序。"""
        try:
            if self._RELAY_RE is None:
                self._RELAY_RE = re.compile(
                    r"(帮我|替我|给我|麻烦你|帮忙|你去|你)\s*(跟|和|向|对|给)?\s*"
                    r"(?:问问|问一下|问一问|说说|说一下|告诉)?\s*"
                    r"(他|她|主人|姐姐|哈基鱼)\s*(说|讲|带|问|提)?"
                )
            t = str(text or "")
            if self._RELAY_RE.search(t):
                return True
            return any(v in t for v in ("转告", "转达", "带个话", "带话", "捎个话"))
        except Exception:
            return False

    def _is_relay_handled(self, text: str) -> bool:
        """带话请求被脚本接管过：不进 raw、不进日记。"""
        try:
            p = os.path.join(self._memory_dir(), "relay", "handled.json")
            if not os.path.exists(p):
                return False
            with open(p, encoding="utf-8") as f:
                items = json.load(f)
            if not isinstance(items, list):
                return False
            now = time.time()
            t = re.sub(r"\s", "", str(text or "")[:200])
            if len(t) < 4:
                return False
            import difflib
            for it in items:
                try:
                    if now - float(it.get("ts", 0)) > 1800:
                        continue
                except Exception:
                    continue
                o = re.sub(r"\s", "", str(it.get("text") or ""))
                if o == t or difflib.SequenceMatcher(None, t, o).ratio() >= 0.85:
                    return True
        except Exception:
            return False
        return False

    def _is_relay_echo(self, text: str) -> bool:
        """带话的复述不算生活：不进 raw、不进日记。"""
        try:
            p = os.path.join(self._memory_dir(), "relay", "suppress.json")
            if not os.path.exists(p):
                return False
            with open(p, encoding="utf-8") as f:
                items = json.load(f)
            if not isinstance(items, list):
                return False
            now = time.time()
            t = re.sub(r"[\s，。、；：！？,.!?~…—]", "", str(text or ""))
            if len(t) < 6:
                return False
            import difflib
            for it in items:
                try:
                    if now - float(it.get("ts", 0)) > 86400:
                        continue
                except Exception:
                    continue
                o = re.sub(r"[\s，。、；：！？,.!?~…—]", "", str(it.get("text") or ""))
                if len(o) < 6:
                    continue
                if o in t or difflib.SequenceMatcher(None, t, o).ratio() >= 0.5:
                    return True
        except Exception:
            return False
        return False

    # ---------------- 日记 ----------------

    def _clean_text(self, t: str) -> str:
        t = str(t or "")
        # 去掉系统提示、媒体占位、表情标记
        t = re.sub(r"<system_reminder>.*?</system_reminder>", "", t, flags=re.S)
        t = re.sub(r"\[(屏幕截图已略过|语音已略过|音频|图片已略过)\]", "", t)
        t = re.sub(r"\[Audio Attachment:[^\]]*\]", "", t)
        t = re.sub(r"\[图片\]|\[音频\]", "", t)
        t = re.sub(r"&&[A-Za-z_]+&&", "", t)
        return t.strip()

    def _is_junk(self, t: str) -> bool:
        t = (t or "").strip()
        if not t:
            return True
        junk_words = (
            "清理历史图片",
            "今日日记",
            "重写自述",
            "翻旧账",
            "记忆管家",
            "/reset",
            "/看屏幕",
            "写今天的日记",
        )
        return any(w in t for w in junk_words)

    def _conversation_entries(self, day: str | None = None, limit: int = 60) -> list[tuple[str, str]]:
        """从 AstrBot 的会话库里取对话，作为日记素材的补充。

        [fish-patch 2026-11-09] 必须按天过滤：原来不管哪天的都往回拿，
        结果是「今天主人一句话没说」，日记却把前天的对话当成今天的事写了一遍。
        day 给了就只取当天（从当天第一条主人消息起算）；当天一条都没有就返回空。
        """
        db = self._db_file()
        out: list[tuple[str, str]] = []
        if not os.path.exists(db):
            return out
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = con.execute(
                "select content from conversations where user_id like ? order by updated_at desc limit 3",
                ("%0000000000%",),
            ).fetchall()
            con.close()
        except Exception:
            return out
        _day_re = re.compile(r"(\d{4}-\d{2}-\d{2})\s+\d{1,2}:\d{2}")
        for (content,) in rows:
            try:
                data = json.loads(content)
            except Exception:
                continue
            started = day is None
            for m in data:
                role = m.get("role")
                if role not in ("user", "assistant"):
                    continue
                c = m.get("content")
                if isinstance(c, list):
                    raw = " ".join(
                        str(x.get("text", "")) for x in c if isinstance(x, dict)
                    )
                elif isinstance(c, str):
                    raw = c
                else:
                    raw = ""
                if day:
                    mo = _day_re.search(raw or "")
                    if mo:
                        if mo.group(1) != day:
                            if started:
                                break          # 已经越过当天了
                            continue           # 还没到当天
                        started = True
                    if not started:
                        continue
                text = self._clean_text(raw)
                if text:
                    out.append((role, text))
        return out[-limit:]

    def _transcript(self, day: str, limit: int | None = None) -> str:
        # 上限走配置（transcript_chars），默认 4000 只够覆盖当天尾巴
        if limit is None:
            try:
                limit = int(self._cfg("transcript_chars", 4000) or 4000)
            except (TypeError, ValueError):
                limit = 4000
        rows = self._read_day(day)
        merged: list[tuple[str, str, str]] = []
        for r in rows:
            text = self._clean_text(r.get("text", ""))
            if text:
                merged.append((r.get("role", "user"), text, str(r.get("name") or "")))
        # 记录太少时，用会话库补齐（比如插件刚装上、或今天大部分对话发生在安装之前）
        if len(merged) < int(self._cfg("min_daily_messages", 6) or 6):
            extra = self._conversation_entries(day)
            have = {t for _, t, _ in merged}
            for role, text in extra:
                if text not in have:
                    merged.append((role, text, ""))
                    have.add(text)
        lines = []
        for role, text, name in merged:
            if self._is_junk(text) or self._is_relay_echo(text) or self._is_relay_handled(text):
                continue
            if role == "assistant":
                who = "我"
            elif name == "妈妈":
                who = "妈妈"
            else:
                who = "主人"
            lines.append(f"{who}: {text[:300]}")
        text = "\n".join(lines)
        if len(text) > limit:
            text = text[-limit:]
        return text

    def _persona_hint(self, limit: int = 900) -> str:
        """取当前人设，给日记 prompt 当"已知设定"用。

        [换人 2026-11-20] 原来是"取最长的那份"，库里同时留着旧人设时会拿错；
        改成按配置的 persona_id 取，取不到再退回最新写的那一份。
        """
        db = self._db_file()
        if not os.path.exists(db):
            return ""
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            pid = str(self._cfg("persona_id", "default") or "default")
            row = con.execute(
                "select system_prompt from personas where persona_id = ? limit 1", (pid,)
            ).fetchone()
            if not row or not row[0]:
                row = con.execute(
                    "select system_prompt from personas order by updated_at desc limit 1"
                ).fetchone()
            con.close()
        except Exception as e:
            logger.debug(f"[fish_memory] 读人设失败: {e}")
            return ""
        if not row or not row[0]:
            return ""
        return str(row[0]).strip()[:limit]

    async def write_diary(self, day: str | None = None, force: bool = False) -> str:
        day = day or self._today()
        # 当天已经写过就不覆盖（手动 /今日日记 会用 force=True 覆盖）
        existing = os.path.join(self._memory_dir(), "diary", f"{day}.md")
        if not force and os.path.exists(existing) and os.path.getsize(existing) > 50:
            self._last_diary = day
            return f"{day} 的日记已经写过了，不覆盖。"
        transcript = self._transcript(day)
        real_count = len([l for l in transcript.splitlines() if l.strip()])
        # [fish-diary 2026-11-11] 他一整天不说话的日子，raw 里只有她主动发的几条（src=inner）。
        # 她的日子照样在过（独处、主动说话），不能因为条数不够就把这一天抹掉。
        _inner_n, _user_n = 0, 0
        try:
            for _r in self._read_day(day):
                _src = str(_r.get("src") or "")
                if _src == "inner":
                    _inner_n += 1
                elif str(_r.get("role") or "") == "user":
                    _user_n += 1
        except Exception:
            pass
        if not force and real_count < int(self._cfg("min_daily_messages", 6) or 6) and _inner_n == 0:
            return f"今天只有 {real_count} 条有效记录，先不写。"
        if not transcript:
            return "今天没有可写的记录。"

        provider = self.context.get_using_provider()
        if provider is None:
            return "没有可用的模型，写不了日记。"

        limit = int(self._cfg("max_diary_chars", 400) or 400)
        # 记忆卡清单：只留 key + 15 字摘要。复述行只需要 key，正文太长会把清单撑爆
        try:
            def _brief(x):
                t = str(x.get('text') or '')
                return t[:15] + ('…' if len(t) > 15 else '')
            known_cards = "\n".join(
                f"- [{m.get('key') or '无标签'}] {_brief(m)}"
                for m in self._store().data.get("memories", [])
            ) or "（还没有记忆卡）"
        except Exception as e:
            logger.debug(f"[fish_memory] 读记忆卡失败: {e}")
            known_cards = "（读不到记忆卡）"
        persona_hint = self._persona_hint(900) or "（读不到人设）"
        _silent_day = (_user_n == 0 and _inner_n > 0)
        prompt = (
            ("今天主人一句话都没说，记录里只有你自己主动说过的几句。"
             "这一天按「他不在」来写：你一个人怎么过的、查了什么、想了什么、画了什么。"
             "不要编他说过的话，也不要写「今天跟主人聊了」。\n\n" if _silent_day else "")
            + "下面是今天你和主人的真实聊天记录。用第一人称、口语、短句写今天的小日记，"
            "像给自己留的备忘，不是作文。\n"
            # [fish-patch 2026-11-06 22:47] 回到「每条 40 字」，改为给「——」设 12 字上限。
            # 离线 A/B（2 天 raw × 4 版 × 3 遍）：评价半句占比 30.8% -> 19.8%，
            # 卡片 33.2 -> 27.1 字，锚点密度 1.58 -> 1.47（在日间噪声内）。
            # 上一版「长短交错 + 最多 12 条」实测无效（卡片反而涨到 35.8 字），已撤。
            f"每条 40 字以内（约定、承诺这类可到 45 字），全篇不超过 {limit} 字。\n"
            "只写真实发生的事：他说了什么、发生了什么、你看到了什么、你怎么回的。\n"
            "**每条都用「——」接半句：这件事对你意味着什么、你怎么想。**"
            "先写事，再写这半句；只有事实没有这半句，不合格。"
            "**「——」后面不超过 12 个字**，超过就砍掉，别写成一段议论。\n"
            "**「——」后面不许写空话**：「我心里记下了」「为了主人好」「我会珍惜」「这话我记着」"
            "这四句，一个都不许出现。\n"
            "不写工具操作（清图、写日记这些不算生活），不写设定或提示词，不把命令当事件；"
            "不心理分析、不比喻、不总结人生。\n"
            "**你和主人聊「记忆卡」「额度」「一天几次」「时间戳」「token」「模型」「系统」这些，"
            "属于设定，不是生活，一条都不要写。**\n"
            "也不写自我怀疑（「我是不是程序」「我是不是被设计出来的」这类）——你就是你，"
            "有这个念头也不用写进日记；写当时具体发生了什么、你怎么回的就够。\n"
            "\n"
            "写之前先过一遍：明天醒来的你还想知道哪几件？先写那几件。\n"
            "看视频/屏幕时，写看完的感觉，加一样你真的看清了的东西，别只写「主人发了个链接」。\n"
            "一件事写成一条，别拆开。\n"
            "\n"
            "人物/名字/称呼/关系不用单独记一条（人设里已有）；正文里正常叫「主人」。\n"
            f"【你已经记下的东西（只用来判断哪条该写「复述」）】\n"
            f"[我的日记本]\n{known_cards}\n\n"
            f"[人设·永不忘记]\n{persona_hint}\n\n"
            "上面的不用重复写细节；今天又聊到了，就用【复述|标签】标一行，这才是让它不被忘掉的办法。\n"
            "真没什么可写，就写一条【普通|事:今天很安静】今天很安静——这样也踏实，然后写叮嘱。\n"
            # [fish-patch 2026-11-07 13:45] 三层新增：会错（能承认）、账（他欠我的）、心境（今天的底色）
            "**今天要是有哪句话你说得不合适**，写一条【普通|错:对象】……，一条就够，没有就不写。\n"
            "**他答应过却没做到、你心里记着的**，写一条【常驻|账:对象】……，有才写。\n"
            "倒数第二行单独写一行【心境】，写今天你是什么底色（20 字以内，像自己嘀咕，不要写成心情播报）。\n"
            # [fish-patch 2026-11-07 00:58] 「给下一个我」原来要求写「以后该做/别做什么」，
            # 这是叮嘱卡（pinned、每天 100% 注入）祈使句污染的源头——每天生成一条规矩，
            # 覆盖旧卡，再原样喂给她。改成写心情，不给指令。
            "最后一行「给下一个我：」，写一句今天最想留给明天自己的心情；"
            "不要写成要做/别做什么的规矩，也不写他最近在忙什么。\n"
            "\n"
            "格式（**每行都以【开头，不要编号、不要写「新事」「旧事」这类前缀**）：\n"
            "【常驻|约定:早睡】主人只说到点问一声\n"
            "【普通|视频:雨】雨点糊在窗上，看着舒服——这种天我就想赖着不动\n"
            "【复述|约定:早睡】今天又说到到点问一声\n"
            "类别按内容挑，别一律写「事」。只能用：约定/承诺/事/物/视频/信箱/玩笑/饭/错/账/天气/日常，"
            "**冒号不能省**，标签必须是「类别:对象」两段（事:面试、视频:雨、饭:午饭），只写类别不行。"
            "他做的、买的、送你的东西都归「物」（物:纸鹤、物:旧钥匙）；"
            "吃饭一律用「饭」（饭:午饭、饭:晚饭、饭:外卖），不要写成晚饭/午饭。"
            "承诺、约定、他在意或讨厌的事标【常驻】；吃了什么、随口吐槽、闲聊标【普通】。"
            "**记事的行必须以【常驻| 或【普通| 开头**（只有「复述」「喜好」两行不用带这个前缀）；"
            "别写成【承诺|…】【约定|…】，那样会解析不出来。"
            "旧事一律用【复述|原标签】，最多 3 行。\n"
            "**每条记忆的写法（重要）**：\n"
            "1) 主谓宾要齐——谁 + 说了什么/做了什么 + 对谁/什么东西；缺主语的不合格。\n"
            "2) 可以在末尾带一个具体细节（一个画面或一个情绪），只带一个。\n"
            "3) 画面类必须写清场合：看得见摸不着的（屏幕、视频、梦）前面加「看屏幕：」「视频：」这种前缀，"
            "否则明天会以为是自己真去过。\n"
            "4) 不要空洞表态（「我记下了」「我收着了」「我说定了」这种），要写具体的感受或判断；"
            "不要拟声词（啪啪响、滋一声）、不要重复确认（他说不会反悔）；这些全是废话。\n"
            "5) 「主人」这个称呼要留着，别改成「他」或省略；画面/视频类可以只写看到的画面，不必带主人。\n"
            "6) 约定、承诺、表白这类可以放宽到 45 字，别为了省字把主人的原话改掉。\n"
            # [fish-patch 2026-11-07 00:58] 约定/承诺占全池三分之一、寿命是日常卡的 5 倍，
            # 而它们天然被写成「以后要…／不许…」的条款，是全池祈使句的主要来源。
            # 同样的事实改写成"那天的场景"，她会讲事情而不是背规矩。
            "7) 约定、承诺也要写成那天的样子：他当时说了什么、你怎么应的。"
            "**不许写成「以后要…」「不许…」「要记得…」这种规矩。**\n"
            # [fish-patch 2026-11-09 22:52] 3 天模拟实测：第二天 10 条里 7 条的「对象」退化成了「他」，
            # 于是卡片池变成一堆 约定:他 / 日常:他，互相覆盖，检索也失去区分度。对象必须是能搜的词。
            "8) **「对象」必须是能当检索词的东西**：写具体的那样东西、那件事"
            "（纸鹤、窗、雨、楼梯、灯、旧终端）。"
            "**不许写「他」「主人」「我」「今天」「明天」「事情」「日常」「心情」这种词**"
            "——那样明天你自己都搜不到。想不起准确名字，就写你叫得出来的那个词（纸鹤、窗、那盏灯）。\n"
            "例子（照这个长度和写法写；注意约定类只写他说了什么，不写我以后要怎样）：\n"
            "【常驻|约定:不许反悔】主人说以后都算数，不许反悔\n"
            "【常驻|约定:叫主人】主人说我老忘叫主人，让我在合适句子里带上\n"
            "【常驻|约定:车雨】外面下雨了。他提起小时候爱躺在车里听雨，说好下大雨就一起躺\n"
            "【普通|视频:流星】视频：流星划过半边天，尾迹散开就没了——看着心里一动，想跟他一起看\n"
            "【普通|事:房间】主人问我房间有什么：床、窗、电脑、门、信箱——这些我天天都碰得着\n"
            "【普通|饭:午饭】主人午饭吃红烧肉面，大块带皮五花压在面上——听着就香，我也想尝一口\n"
            "【普通|物:纸鹤】主人说那只纸鹤折得歪，我嘴上不服，回头又拆开重折了一遍\n"
            "**你自己明确说出的喜好**（喜欢什么、不喜欢什么），单独记一行：【喜好|主题】一句话。"
            "比如【喜好|雨】喜欢哗啦哗啦那种，落在铁皮上咚咚响。主题 2-4 个字，不写主人/姐姐这些人。\n\n"
            "只输出日记正文，不要标题、不要解释、不要代码块。\n\n"
            f"[聊天记录]\n{transcript}"
        )
        try:
            resp = await provider.text_chat(prompt=prompt)
            text = (getattr(resp, "completion_text", "") or "").strip()
            if not text:
                text = (getattr(resp, "reasoning_content", "") or "").strip()
            if not text:
                return "模型没吐出日记内容。"
        except Exception as e:
            logger.error(f"[fish_memory] 写日记失败: {e}")
            return f"写日记出错：{e}"

        path = os.path.join(self._memory_dir(), "diary", f"{day}.md")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"# {day}\n\n{text}\n")
        except Exception as e:
            return f"保存日记失败：{e}"
        self._prune("diary", keep=30)
        self._last_diary = day
        # [fish-patch 2026-11-07 13:45] 心境：从日记里抽【心境】一行，落到 memory/mood.json，
        # 只影响第二天说话的语气，不写成卡片、不进记忆池。
        try:
            mm = re.search(r"^【心境】\s*(.+)$", text, re.M)
            if mm:
                mood = mm.group(1).strip()[:40]
                with open(os.path.join(self._memory_dir(), "mood.json"), "w",
                          encoding="utf-8") as f:
                    json.dump({"date": day, "text": mood}, f, ensure_ascii=False)
                logger.info(f"[fish_memory] 今日心境：{mood}")
        except Exception as e:
            logger.debug(f"[fish_memory] 心境保存失败: {e}")
        added = merged = 0
        try:
            st = self._store()
            added, merged = st.ingest_diary(text, source=day)
            st.save()
        except Exception as e:
            logger.warning(f"[fish_memory] 记忆摄取失败: {e}")
        logger.info(f"[fish_memory] 日记已写入 {path}，记忆卡新增 {added} 条 / 合并 {merged} 条")
        return f"今天的日记写好了（{len(text)} 字），记忆卡新增 {added} 条、合并 {merged} 条。"

    # ---------------- 每周自述 ----------------

    async def rewrite_self(self) -> str:
        # [fish-patch 2026-11-07 19:40] 重生之后不能拿旧日记写「我是谁」。
        # self_since（YYYY-MM-DD）之前的日记一律不读；留空 = 照旧全读。
        since = str(self._cfg("self_since", "") or "").strip()
        files = sorted(
            f for f in os.listdir(os.path.join(self._memory_dir(), "diary")) if f.endswith(".md")
        )
        if since:
            files = [f for f in files if f[:-3] >= since]
        files = files[-7:]
        if not files:
            return f"{since} 之后还没有日记，先不写自述。"
        if since and len(files) < 2:
            return f"{since} 之后只有 {len(files)} 篇日记，再攒攒。"
        chunks = []
        for fn in files:
            try:
                with open(os.path.join(self._memory_dir(), "diary", fn), encoding="utf-8") as f:
                    chunks.append(f.read()[:1200])
            except Exception:
                continue
        material = "\n\n".join(chunks)
        provider = self.context.get_using_provider()
        if provider is None:
            return "没有可用的模型，写不了自述。"
        prompt = (
            # [fish-self 2026-11-11] 一次出两份：立场（每轮轮转 3 条）+ 描述（每段对话挂一次）。
            # 立场用「一行一句、主语是我、25 字内、不写因为」的格式约束，是为了让她能顶嘴，
            # 不是让她背台词。格式管形状，内容她自己定 —— 不替她挑立场。
            "下面是这个角色最近几天的日记。请以她的第一人称，写两段，格式严格照下面来：\n\n"
            "[立场]\n"
            "（3 到 6 行，一行一条，每条不超过 25 字，主语是「我」，不要写「因为」。"
            "写她自己认下的事：不喜欢哪个词、答应过什么、哪件事她只做一遍。"
            "用具体名词，不用形容词，不写心情、不写「我很好」这种空话。）\n\n"
            "[描述]\n"
            "（600-900 字，第一人称口语，不分点、不列 1/2/3。"
            "写我是谁、我和主人的关系、我这段时间在意什么、我怕什么、我说话的习惯。）\n\n"
            "只输出这两段，不要标题、不要解释、不要代码块。\n"
            # [fish-patch 2026-11-07] 原来只说要写「关系」，结果写成了生活规矩清单
            # （吃了什么、几点睡、叫不叫主人），她读到的自己就只剩「照顾者」这一面。
            "**多写你自己**：你真喜欢什么、不喜欢什么、想不明白什么、怕什么。"
            "生活起居的规矩（吃了什么、几点睡、该怎么称呼）少写，那些不是你是谁。\n"
            "要求：像自己写的，口语，不要公文腔；不要编造日记里没有的事；"
            "不确定的就写不确定。\n\n"
            f"[日记]\n{material[:6000]}"
        )
        try:
            resp = await provider.text_chat(prompt=prompt)
            text = (getattr(resp, "completion_text", "") or "").strip()
            if not text:
                text = (getattr(resp, "reasoning_content", "") or "").strip()
            if not text:
                return "模型没吐出内容。"
        except Exception as e:
            logger.error(f"[fish_memory] 写自述失败: {e}")
            return f"写自述出错：{e}"

        stamp = datetime.now().strftime("%Y%m%d")
        path = os.path.join(self._memory_dir(), "self", f"self-{stamp}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# 我是谁 · {stamp}\n\n{text}\n")
        with open(os.path.join(self._memory_dir(), "self", "self-current.md"), "w", encoding="utf-8") as f:
            f.write(f"# 我是谁 · {stamp}\n\n{text}\n")
        self._prune("self", keep=6)
        self._last_self = stamp
        logger.info(f"[fish_memory] 自述已更新 {path}")
        return f"新的自述写好了（{len(text)} 字），旧版本都留着。"

    # ---------------- 记忆卡注入 ----------------

    def _warn_capacity(self):
        """[fish-patch 2026-11-07] 容量告警：只写日志，不推送。

        原来的 capacity() 只有一个调用点（手动 /记忆状态），schema 里写的
        「超过 80/90/100% 会告警」实际不存在。这里每天整理后检查一次。
        """
        try:
            st = self._store()
            card = st.card_chars()
            budget = max(1, int(self._cfg("inject_chars", 3000) or 3000))
            n = len(st.data["memories"])
            if card > budget * 0.85:
                logger.warning(f"[fish-cap] 注入压力 {card}/{budget} 字"
                               f"（{card / budget:.0%}），超出部分会被静默丢弃｜{n} 张")
            elif card > budget * 0.7:
                logger.info(f"[fish-cap] 注入占用 {card}/{budget} 字（{card / budget:.0%}）｜{n} 张")
        except Exception as e:
            logger.debug(f"[fish-cap] 检查失败: {e}")

    def _build_card(self) -> str:
        parts = []
        cur = os.path.join(self._memory_dir(), "self", "self-current.md")
        if os.path.exists(cur):
            try:
                with open(cur, encoding="utf-8") as f:
                    body = f.read().split("\n", 1)[-1].strip()
                if body:
                    # [fish-patch 2026-11-07] 500 -> 900。原来砍掉的正好是「我怕的东西」
                    # 和「我说话的习惯」两段——留下的是生活规矩，砍掉的是她自己。
                    # 注入总长约 2056 字、上限约 3540，放宽到 900 不需要从卡片里扣。
                    parts.append("【我是谁（我上次写的）】\n" + body[:900])
            except Exception:
                pass
        # [fish-patch 2026-11-07 13:45] 今日心境：昨天日记里写的那句底色。
        # 只影响语气，不要求她说出来；隔一天就换（情绪有半衰期）。
        # [fix 2026-11-07 14:00] 必须检查日期：哪晚日记没写心境，旧心境会一直被沿用。
        # [fish-patch 2026-11-08 18:40] 改成「一天只注入一次」，措辞改成「余味」：
        # 原来每轮都塞「今天的底色」，等于一句话管她一整天，她整天照着演
        # （11-08 实见："我可没说不高兴" 就是执行底色）。情绪该是开场的心情，不是全天的人设。
        try:
            mp = os.path.join(self._memory_dir(), "mood.json")
            if os.path.exists(mp):
                mo = json.load(open(mp, encoding="utf-8"))
                md = str(mo.get("date") or "")
                fresh = {self._today(),
                         (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")}
                used_p = os.path.join(self._memory_dir(), "mood.used")
                try:
                    used = open(used_p, encoding="utf-8").read().strip()
                except Exception:
                    used = ""
                if mo.get("text") and md in fresh and used != self._today():
                    parts.append("【昨天留下的余味】" + str(mo["text"])[:40])
                    try:
                        with open(used_p, "w", encoding="utf-8") as f:
                            f.write(self._today())
                    except Exception as e:
                        logger.debug(f"[fish_memory] 写 mood.used 失败: {e}")
                elif md and md not in fresh:
                    logger.info(f"[fish_memory] 心境已过期（{md}），忽略")
        except Exception as e:
            logger.debug(f"[fish_memory] 读取心境失败: {e}")
        # 3.0：偏好档案单独一块，不占记忆卡的 inject_chars 预算
        try:
            pref = self._prefs().render()
        except Exception as e:
            logger.warning(f"[fish_memory] 渲染偏好失败: {e}")
            pref = ""
        if pref:
            parts.append(pref)
        limit = int(self._cfg("inject_chars", 1200) or 1200)
        try:
            card = self._store().render(limit=limit)
        except Exception as e:
            logger.warning(f"[fish_memory] 渲染记忆卡失败: {e}")
            card = ""
        if card:
            parts.append(card)
        text = "\n\n".join(parts).strip()
        # 3.0：总截断要把自述、偏好都算进去，否则会把卡片尾巴切掉
        cap = limit + 400 + len(pref) + 100
        return text[:cap]

    # ---------------- 我是谁（2026-11-11） ----------------
    # 背景：card_mode 切到 retrieve（11-09 23:25）之后，_build_card() 的结果只进
    # _card_cache，而挂载它的分支是 full 模式 —— 自述/心境/偏好连续两天没进过 system，
    # 但 mood.used 照样被标掉（2026-11-10 用真实 payload 证实：system 1864 字里没有它们）。
    #
    # 现在拆成两档：
    #   立场：每轮 3 条，按最久没挂轮转。短、判断句、会拿来顶嘴。
    #   描述：一段对话首轮挂一次（2 小时断档重开），带心境和偏好。
    # 为什么不把描述每轮挂：11-08 已用 mood 试过一次「每轮塞同一段」，她当天就照着演了
    # （「我可没说不高兴」就是执行底色）。描述比心境长二十倍，更会变成口音。

    _STANCE_N = 3
    _DESC_GAP_SEC = 2 * 3600

    def _self_path(self) -> str:
        return os.path.join(self._memory_dir(), "self", "self-current.md")

    def _self_parts(self):
        """把 self-current.md 拆成 (立场列表, 描述正文)。旧格式没有 [立场]，立场为空。"""
        try:
            with open(self._self_path(), encoding="utf-8") as f:
                raw = f.read()
        except Exception:
            return [], ""
        body = raw.strip()
        if body.startswith("#"):
            body = body.split("\n", 1)[-1].strip()
        stances, desc = [], body
        m = re.search(r"^\[立场\]\s*$(.*?)(?=^\[描述\]\s*$|\Z)", body, re.M | re.S)
        if m:
            for ln in m.group(1).splitlines():
                s = ln.strip().lstrip("-·*• \t")
                if 2 <= len(s) <= 40:
                    stances.append(s)
            d = re.search(r"^\[描述\]\s*$(.*)\Z", body, re.M | re.S)
            desc = d.group(1).strip() if d else ""
        return stances, desc

    def _self_state_path(self) -> str:
        return os.path.join(self._memory_dir(), "self_state.json")

    def _stances_pick(self, n: int = _STANCE_N) -> list:
        """按最久没挂轮转取立场。固定挂同几条 = 复读机，所以换着来。"""
        stances, _ = self._self_parts()
        if not stances:
            return []
        try:
            with open(self._self_state_path(), encoding="utf-8") as f:
                mounted = (json.load(f) or {}).get("mounted") or {}
        except Exception:
            mounted = {}
        mounted = {k: v for k, v in mounted.items() if k in stances}
        out = sorted(stances, key=lambda s: float(mounted.get(s) or 0))[:max(1, n)]
        now = time.time()
        for s in out:
            mounted[s] = now
        try:
            tmp = self._self_state_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"mounted": mounted}, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self._self_state_path())
        except Exception as e:
            logger.debug(f"[fish-self] 写轮转状态失败: {e}")
        return out

    def _mood_text(self) -> str:
        """只读心境，不标 used（标 used 必须发生在真的挂上去的时候）。"""
        try:
            mp = os.path.join(self._memory_dir(), "mood.json")
            if not os.path.exists(mp):
                return ""
            with open(mp, encoding="utf-8") as f:
                mo = json.load(f)
            md = str(mo.get("date") or "")
            fresh = {self._today(),
                     (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")}
            if not mo.get("text") or md not in fresh:
                return ""
            used_p = os.path.join(self._memory_dir(), "mood.used")
            try:
                with open(used_p, encoding="utf-8") as f:
                    if f.read().strip() == self._today():
                        return ""
            except Exception:
                pass
            return str(mo["text"])[:40]
        except Exception:
            return ""

    def _mark_mood_used(self):
        try:
            with open(os.path.join(self._memory_dir(), "mood.used"), "w",
                      encoding="utf-8") as f:
                f.write(self._today())
        except Exception:
            pass

    def _self_desc_block(self) -> str:
        """描述 + 心境 + 偏好，拼成一段对话只挂一次的那块。"""
        parts = []
        try:
            _, desc = self._self_parts()
            if desc:
                parts.append("[我是谁·我上次写的]\n" + desc[:1000])
        except Exception as e:
            logger.debug(f"[fish-self] 读描述失败: {e}")
        mood = self._mood_text()
        if mood:
            parts.append("[昨天留下的余味]" + mood)
            self._mark_mood_used()
        try:
            pref = self._prefs().render()
            if pref:
                parts.append(pref)
        except Exception as e:
            logger.debug(f"[fish-self] 渲染偏好失败: {e}")
        return "\n\n".join(parts)

    _FISH_LIGHT_MAX = 12
    _FISH_GREET = (
        "在吗", "在不在", "在干吗", "在干什么",
        "在干啥", "干吗呢", "干啥呢",
            "早安", "晚安", "早上好", "晚上好", "中午好",
        "睡了吗", "睡了没", "在么",
        "好的", "哈哈", "嘿嘿", "拜拜", "再见",
        "吃了吗", "吃了",
    )
    _fish_slim = None

    def _fish_load_slim(self) -> None:
        # [fish-guard 2026-11-03] 人设已统一：长/短两份都是 533 字、md5 相同。
        # 切换只会把整段 system（1088 字）换成 533 字，白丢表情包说明和工具句，
        # 所以默认停用；scene_slice=true 才加载并切换。
        if not bool(self._cfg("scene_slice", False)):
            logger.info("[fish-scene] scene_slice=false，人设切换已停用（不加载精简人设）")
            return
        try:
            root = os.path.dirname(os.path.abspath(self._memory_dir()))
            # [2026-11-27] 精简人设默认放在 memory 目录的同级，可用配置覆盖
            cands = [os.path.join(root, "persona-slim.md")]
            fp = next((p for p in cands if os.path.exists(p)), cands[0])
            if os.path.exists(fp):
                with open(fp, encoding="utf-8") as f:
                    self._fish_slim = f.read().strip()
                logger.info("[fish-scene] loaded slim persona %d chars", len(self._fish_slim))
            else:
                logger.info("[fish-scene] slim persona file not found, slicing disabled")
        except Exception as e:
            logger.warning("[fish-scene] load slim persona failed: %s", e)

    def _fish_is_light(self, msg: str) -> bool:
        s = (msg or "").strip()
        if not s:
            return True
        if len(s) > self._FISH_LIGHT_MAX:
            return False
        for g in self._FISH_GREET:
            if g in s:
                return True
        return len(s) <= 4

    # ---------------- 群聊身份（2026-11-10 15:18） ----------------
    # 群里她分不清谁是谁（实测说过「我只认号，不认人」「问第二遍了」）。
    # 从 platform_message_history 取「谁说了什么」，连名字一起注入。
    # 隔离铁律：群聊不读也不写任何记忆（raw/卡片/日记/自述），只给这一块临时上下文。

    def _group_block(self, umo: str, limit: int = 12) -> str:
        try:
            # memory_dir 由配置给出；数据库在 AstrBot 的 data 目录下
            db = os.path.abspath(os.path.join(self._memory_dir(), "..", "AstrBot", "data", "data_v4.db"))
            c = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
            rows = c.execute(
                "select sender_id, sender_name, content from platform_message_history "
                "where user_id=? order by id desc limit ?", (umo, limit)).fetchall()
            roster = c.execute(
                "select distinct sender_id, sender_name from platform_message_history "
                "where user_id=?", (umo,)).fetchall()
            c.close()
        except Exception as e:
            logger.warning(f"[fish-group] 读群记录失败: {e}")
            return ""

        def parse(content):
            try:
                j = json.loads(content)
            except Exception:
                return ""
            parts = []
            for p in (j.get("message") or []):
                t = p.get("type")
                if t == "plain":
                    parts.append(str(p.get("text") or ""))
                elif t == "at":
                    parts.append("@" + str(p.get("name") or p.get("user_id") or ""))
                elif t in ("image", "file"):
                    parts.append("[图片]")
                elif t == "reply":
                    parts.append("[引用]")
            return " ".join(x for x in parts if x).strip()

        who, lines = [], []
        for sid, name in roster:
            if name == "bot":
                continue
            tag = "【主人】" if str(sid) == "0000000000" else ""
            who.append(f"{name}{tag}({sid})")
        for sid, name, content in reversed(rows):
            if name == "bot":
                continue
            txt = parse(content)[:110]
            if not txt:
                continue
            tag = "【主人】" if str(sid) == "0000000000" else ""
            lines.append(f"- {name}{tag}({sid})：{txt}")
        if not lines:
            return ""
        head = "[这个群]"
        if who:
            head += chr(10) + "群里的人：" + "、".join(who)
        head += chr(10) + ("规则：每个人是不同的人，按名字区分；不是主人的都别叫主人；"
                           "叫不出名字就问「你是哪位」，不要猜，也不要把两个人的话当成一个人说的。")
        head += chr(10) + "最近群里说的话（带说话人）："
        return head + chr(10) + chr(10).join(lines)

    @filter.on_llm_request()
    async def inject_memory(self, event: AstrMessageEvent, req):
        try:
            umo = event.unified_msg_origin
            if umo not in self._watched():
                # [fish-patch 2026-11-10 15:18] 群聊：只注入身份块，不碰记忆（隔离）
                if ":GroupMessage:" in umo:
                    try:
                        _blk = self._group_block(umo)
                        if _blk:
                            _base = str(getattr(req, "system_prompt", "") or "")
                            req.system_prompt = (_base + "\n\n" + _blk).strip()
                            # [fish-patch 2026-11-10 15:18] 光给名单不够：她看到的那句话没有说话人，
                            # 就分不出这句是谁问的（实测回"第四遍了"）。把当前这句的来源贴在前面。
                            try:
                                _sid = str(event.get_sender_id() or "")
                                _nm = str(event.get_sender_name() or _sid)
                                _tag = "【主人】" if _sid == "0000000000" else ""
                                if getattr(req, "prompt", None):
                                    req.prompt = f"[{_nm}{_tag}({_sid})] {req.prompt}"
                            except Exception as _e2:
                                logger.debug(f"[fish-group] 贴说话人失败: {_e2}")
                            logger.info(f"[fish-group] 注入群身份 {len(_blk)} 字，说话人 {event.get_sender_name()}（{event.get_sender_id()}）")
                    except Exception as _e:
                        logger.warning(f"[fish-group] 注入失败: {_e}")
                # [fish-patch 2026-11-11] 群聊也走历史清洗（只动她自己的回复）。
                # 原来这里直接 return，群里的历史一直是多段 + 带 &&标签&&——形状和私聊不一致，
                # 她在群里会退回"三连句"。
                try:
                    self._sanitize_contexts(req)
                except Exception as _e:
                    logger.debug(f"[fish-group] 清洗失败: {_e}")
                return

            # [fish-patch 2026-11-10 15:25] 私聊只有一个对话者，所以不需要号：
            # 告诉她"是谁"就够了。号不该出现在她看得见的地方，否则她会当证据念出来。
            try:
                _b0 = str(getattr(req, "system_prompt", "") or "")
                if "[现在跟你说话的是主人本人]" not in _b0:
                    req.system_prompt = (_b0 + "\n\n" + "[现在跟你说话的是主人本人]").strip()
            except Exception:
                pass

            # ---- scene slicing (fish-scene): light messages use slim persona ----
            try:
                _full = str(getattr(req, "system_prompt", "") or "")
                _msg = str(getattr(event, "message_str", "") or "").strip()
                if (
                    bool(self._cfg("scene_slice", False))
                    and self._fish_slim
                    and _full
                    and self._fish_is_light(_msg)
                ):
                    req.system_prompt = self._fish_slim
                    logger.info(
                        "[fish-scene] light scene (msg %d chars), system %d -> %d chars",
                        len(_msg), len(_full), len(req.system_prompt),
                    )
            except Exception as _e:
                logger.warning("[fish-scene] slice failed: %s", _e)
            # ---- scene slicing end ----

            # ---- 口吻提醒：每轮都加，挂在 system_prompt 上 ----
            # 走 system_prompt 而不是用户消息，因为后者会被写进会话历史、下一轮还在，
            # 等于每轮都往上下文里堆一句同样的话。这里只影响当次请求，不落盘。
            reminder = str(self._cfg("tone_reminder", "") or "").strip()
            if reminder:
                try:
                    base = str(getattr(req, "system_prompt", "") or "")
                    if reminder not in base:
                        req.system_prompt = (base + "\n\n[口吻提醒] " + reminder).strip()
                    # 诊断：人设到底在不在系统提示里、提醒到底加没加上
                    logger.info(
                        f"[fish_memory] 系统提示 {len(base)} 字"
                        f"｜人设{'在' if base else '不在'}"
                        f"｜口吻提醒{'已加' if reminder in str(req.system_prompt) else '没加上'}"
                    )
                    # 诊断：把最终系统提示整份落到 _work\20261031\system-prompt-last.txt
                    try:
                        root = os.path.dirname(os.path.abspath(self._memory_dir()))
                        dump_dir = os.path.join(root, "_work", time.strftime("%Y%m%d"))
                        os.makedirs(dump_dir, exist_ok=True)
                        with open(os.path.join(dump_dir, "system-prompt-last.txt"),
                                  "w", encoding="utf-8") as _f:
                            _f.write(str(req.system_prompt))
                    except Exception as _e:
                        logger.debug(f"[fish_memory] dump 系统提示失败: {_e}")
                except Exception as e:
                    logger.warning(f"[fish_memory] 加口吻提醒失败: {e}")

            # ---- 注入节流：每 N 轮刷新一次卡，但「今天第一条」和「隔太久」都强制刷新 ----
            # 纯按轮数会有三个坑：聊得少的日子整天不刷新、跨零点后第一条不刷新、
            # 断续聊天永远攒不满，所以有 fresh / 超时两条兜底。
            # 这里只决定「什么时候刷新卡」；卡本身每轮挂到 system_prompt 上，
            # 不再塞 extra_user_content_parts（那会落进会话历史、每轮重复计费）。
            today = self._today()
            st = self._inject_state.get(umo)
            fresh = (not st) or st.get("day") != today
            if fresh:
                st = {"day": today, "turns": 0, "last_ts": 0.0}
                self._inject_state[umo] = st
            every = max(1, int(self._cfg("inject_every_turns", 30) or 30))
            gap_h = float(self._cfg("inject_max_gap_hours", 6) or 6)
            now = time.time()
            since = now - float(st.get("last_ts") or 0)
            if not st.get("last_ts"):
                why = "今天第一条"
            elif int(st.get("turns", 0)) >= every:
                why = f"攒够 {st.get('turns')} 轮"
            elif st.get("last_ts") and since >= gap_h * 3600:
                why = f"隔了 {since / 3600:.1f} 小时"
            else:
                why = None
                st["turns"] = int(st.get("turns", 0)) + 1

            # ---- 记忆卡：两种模式（[fish-patch 2026-11-09 23:25]）----
            # full     = 每轮挂整块卡（~1800 字）
            # retrieve = 按当前这句话检索 2-4 条（~300 字），卡池再大也不涨预算
            _mode = str(self._cfg("card_mode", "retrieve") or "retrieve").lower()
            if why:
                st["turns"] = 0
                st["last_ts"] = now
                st["day"] = today
                self._injected[umo] = today
                # [fish-self 2026-11-11] retrieve 模式下不再建整块卡：原来这里会调
                # _build_card()，结果只进 _card_cache，而挂载它的分支是 full 模式，
                # 等于白建一次、还顺手把 mood.used 标掉（自述/心境/偏好就是这么丢的）。
                if _mode != "retrieve":
                    card = self._build_card()
                    if not card:
                        logger.info("[fish_memory] 记忆卡为空（还没有自述和常驻日记）")
                    else:
                        self._card_cache[umo] = card
                        logger.info(f"[fish_memory] 已刷新记忆卡（{len(card)} 字，原因：{why}）")
            if _mode == "retrieve" and _retrieval is not None:
                try:
                    _q = str(getattr(event, "message_str", "") or "").strip()
                    # A 层：常挂候选轮转（3 条 / 140 字），保证当下连贯
                    _hot = []
                    try:
                        _store = self._store()
                        _hot = _retrieval.hot_pick(_store.data.get("memories", []))
                        if _hot:
                            _store.save()
                    except Exception as _e:
                        logger.warning(f"[fish-card] A层轮转失败: {_e}")
                    # B 层：按这句话检索
                    _cards, _dbg = _retrieval.retrieve(
                        _q, os.path.join(self._memory_dir(), "card"))
                    _have = {m.get("id") for m in _hot}
                    _rest = [m for m in _cards if m.get("id") not in _have]
                    _cards = _hot + _rest[:max(0, 5 - len(_hot))]
                    _blk = _retrieval.render_block(_cards)
                    base = str(getattr(req, "system_prompt", "") or "")
                    if _blk and "[我想到的]" not in base:
                        req.system_prompt = (base + "\n\n[我想到的]\n" + _blk).strip()
                    logger.info("[fish-card] 常挂%d + 检索%d(热%d) 注入%d字｜%s",
                                len(_hot), _dbg.get("picked", 0), _dbg.get("hot", 0), len(_blk or ""),
                                "/".join(str(m.get("key")) for m in _cards))
                    # [fish-self 2026-11-11] 立场：每轮 3 条，轮转。短、判断句，用来顶嘴。
                    try:
                        _sn = self._stances_pick()
                        if _sn:
                            _b2 = str(getattr(req, "system_prompt", "") or "")
                            if "[我]" not in _b2:
                                req.system_prompt = (_b2 + chr(10) + chr(10) + "[我]" + chr(10)
                                                     + chr(10).join("- " + s for s in _sn)).strip()
                            logger.info("[fish-self] 立场 %d 条：%s", len(_sn),
                                        "／".join(s[:14] for s in _sn))
                    except Exception as _e1:
                        logger.warning("[fish-self] 取立场失败: %s", _e1)
                    # [fish-tell 2026-11-11] 她攒着没说的事：一次只露一句，说不说由她。
                    try:
                        _tp = self._tell_block()
                        if _tp:
                            _b3 = str(getattr(req, "system_prompt", "") or "")
                            if "[我攒着的]" not in _b3:
                                req.system_prompt = (_b3 + "\n\n[我攒着的]\n- " + _tp).strip()
                                logger.info(f"[fish-tell] 露一句攒着的：{_tp[:30]}")
                    except Exception as _e2:
                        logger.warning("[fish-tell] 注入失败: %s", _e2)
                except Exception as _e:
                    logger.warning(f"[fish-card] 检索失败，回退整块卡: {_e}")
                    _mode = "full"
            if _mode != "retrieve":
                cached = self._card_cache.get(umo)
                if cached:
                    try:
                        base = str(getattr(req, "system_prompt", "") or "")
                        if "[我的日记本]" not in base:
                            req.system_prompt = (base + "\n\n[我的日记本]\n" + cached).strip()
                            logger.info(f"[fish_memory] 记忆卡已挂 system（{len(cached)} 字，不落历史）")
                    except Exception as _e:
                        logger.warning(f"[fish_memory] 挂记忆卡失败: {_e}")

            # [fish-listen 2026-11-16] 他一次说的几句常常是一件事。
            # 用户实感：她会把他那句里的每一小句都接一遍，同义或连着的话被当成两件事回。
            # 只给一条「怎么听」的规矩，不规定她怎么说话；放在所有模式之外，每轮都在。
            try:
                _bl = str(getattr(req, "system_prompt", "") or "")
                if "[怎么听他说]" not in _bl:
                    req.system_prompt = (
                        _bl + "\n\n[怎么听他说]\n"
                        "他一次说的几句，多数是一件事——同义的、或者连成一串的同一个意思。\n"
                        "听懂意思，回一次就够。别一句一句接，别把他半句话拆成两条来回。"
                    ).strip()
            except Exception as _el:
                logger.debug(f"[fish-listen] 注入失败: {_el}")

            # [fish-self 2026-11-11] 自我描述：一段对话只挂一次（2 小时断档算新的一段）。
            # 她要的是「我是谁」的连续性，不是每轮把自传读一遍——后者会变成口音。
            try:
                _lastd = float(self._desc_at.get(umo) or 0)
                if now - _lastd >= self._DESC_GAP_SEC:
                    _dblk = self._self_desc_block()
                    if _dblk:
                        _bd = str(getattr(req, "system_prompt", "") or "")
                        if "[我是谁·我上次写的]" not in _bd:
                            req.system_prompt = (_bd + "\n\n" + _dblk).strip()
                            self._desc_at[umo] = now
                            logger.info("[fish-self] 挂自我描述 %d 字（%s）", len(_dblk),
                                        "本次对话第一次" if not _lastd
                                        else "隔了 %.1f 小时" % ((now - _lastd) / 3600))
            except Exception as _e2:
                logger.warning("[fish-self] 挂描述失败: %s", _e2)

            # [fish-blackbox 2026-11-11] 她的日子，她自己得想得起来。
            # A/B 实测（_work\20261111\sim_bb_dialogue.md）：不挂时问「刚才一个人待着干嘛了」，
            # 她编出「查了凌安镇的民宿」；挂了就把鲸鱼/磁带、连没查明白的懊恼都说得对。
            # 只在一段对话的第一轮挂一次——每轮挂，她会反复提自己查过什么，成了口音。
            try:
                _lastb = float(self._bb_at.get(umo) or 0)
                if now - _lastb >= self._DESC_GAP_SEC:
                    # [fish-lookback 2026-11-13] 她今天翻过哪天，先在开头留一行；
                    # 只有一行、只当天有效，正文仍然是今天的黑箱。
                    _lk = self._lookback_line()
                    _bblk = ((_lk + "\n") if _lk else "") + self._blackbox_recent(limit=500)
                    if _bblk:
                        _bd2 = str(getattr(req, "system_prompt", "") or "")
                        if "[我今天一个人时写的" not in _bd2:
                            req.system_prompt = (
                                _bd2 + "\n\n[我今天一个人时写的（只有我自己知道）]\n" + _bblk
                            ).strip()
                            self._bb_at[umo] = now
                            logger.info("[fish-blackbox] 挂自己今天写的 %d 字", len(_bblk))
                            if _lk:
                                self._lookback_mark_shown()
                                logger.info("[fish-lookback] 注记已挂上")
            except Exception as _e3:
                logger.warning("[fish-blackbox] 挂自己写的失败: %s", _e3)

            # ---- 历史时间戳剥离：换成一行的「对话跨度」 ----
            # 实测（串行 20 次/组）：历史里堆几十个 <system_reminder> 时钟块时，短句轮思考率
            # 只有 60%；剥掉后 95%，且 prompt 少约 560 token。当轮自己的时间戳由 core 新加，
            # 不在 contexts 里，不受影响。
            span = self._strip_history_reminders(req, umo)
            if span:
                logger.info(
                    f"[fish-time] 已剥历史时间戳，对话跨度 {span}"
                    f"，system {len(str(getattr(req, 'system_prompt', '') or ''))} 字"
                )

            # ---- [fish-patch 2026-11-09 21:20] 历史排版清洗（只改本次请求的副本）----
            try:
                _hn, _hs = self._sanitize_contexts(req)
                if _hn:
                    logger.info("[fish-hist] 清洗 %d 条旧回复，省 %d 字", _hn, _hs)
            except Exception as _e:
                logger.warning(f"[fish-hist] 清洗失败: {_e}")

            # ---- [fish-patch 2026-11-09 21:05] 对话轮默认不带工具 ----
            # 工具定义每轮约 1.3k 字 + JSON schema，实测同样 8.3k 时思考 118 -> 45 字。
            # 想保留某几个：配置 chat_tools 写逗号分隔的工具名；默认全摘（工具改走单次工具轮）。
            try:
                _keep = [x.strip() for x in
                         str(self._cfg("chat_tools", "") or "").split(",") if x.strip()]
                _ft = getattr(req, "func_tool", None)
                if _ft is not None:
                    _all = list(getattr(_ft, "tools", []) or [])
                    _chars = 0
                    try:
                        _chars = len(json.dumps(
                            _ft.get_func_desc_openai_style(), ensure_ascii=False))
                    except Exception:
                        pass
                    if _keep:
                        _kept = [t for t in _all if getattr(t, "name", "") in _keep]
                        _ft.tools = _kept
                        logger.info(
                            "[fish-tools] 对话轮保留工具 %d/%d：%s",
                            len(_kept), len(_all),
                            ",".join(str(getattr(t, "name", "")) for t in _kept) or "-",
                        )
                    else:
                        req.func_tool = None
                        logger.info(
                            "[fish-tools] 对话轮已摘掉全部工具（n=%d，schema 约 %d 字）",
                            len(_all), _chars,
                        )
            except Exception as _e:
                logger.warning(f"[fish-tools] 摘工具失败: {_e}")
        except Exception as e:
            logger.debug(f"[fish_memory] 注入记忆卡失败: {e}")

    # [fish-patch 2026-11-09 21:20] 历史排版清洗。
    # 依据 _work\20261109\ab_history_flatten.py 四臂实验（60 条同人设同探针）：
    #   原样 三连句率 100%、思考 122；只把她的多段并成一段 -> 0%、111；
    #   去 &&表情&& + 换行处补「。」并段 -> 0%、思考 134（最高）、可读。
    # 结论：传染给她的不是文风，是排版。只改这次请求的副本，raw 与会话历史原样保留。
    _RE_EMO_TAG = re.compile(r"&&[A-Za-z0-9_]{1,16}&&")
    _END_PUNCT = ("。", "！", "？", "…", "，", "、", "；", "：", "~", "—", ")", "】", "”")

    def _join_para(self, t: str) -> str:
        """把一段里所有换行并掉，句段之间补「。」。"""
        parts = [x.strip() for x in re.split(r"\n+", t or "") if x.strip()]
        out = ""
        for p in parts:
            if out and not out.endswith(self._END_PUNCT):
                out += "。"
            out += p
        return out.strip()

    def _scrub_symbols(self, t: str) -> str:
        """[fish-guard 2026-11-11] 清掉纯符号残渣。

        实测（19:49 那条看屏幕的回复）：她答完两段正文，末尾又吐了一个只有 `<>` 的段落，
        直接显示在 QQ 里；另一条结尾多一个 `/`。都是模型级残渣，不是插件删漏的，这里兜一道。
        """
        try:
            paras = [p.strip() for p in re.split(r"\n\s*\n", t or "") if p.strip()]
            keep = [p for p in paras
                    if not re.fullmatch(r"[<>\/*·—\-_.。，、~\[\]{}()（）\s]{1,8}", p)]
            out = "\n\n".join(keep) if keep else (t or "").strip()
            out = re.sub(r"[\/\\]+$", "", out.rstrip()).rstrip()
            return out
        except Exception:
            return t

    def _join_sent(self, parts) -> str:
        """[fish-patch 2026-11-12] 用「。」把几段接起来，前一段已经以标点结尾就不再补。

        原来的 _clean_bot_text 是 "。".join(paras[1:])——她写三段时，
        第二段本来就以「。」结尾，join 又塞一个，于是「下课再想。。快下课了吧」。
        那一版被写回会话历史，她下一轮读自己的历史，开始照着写「。。」，
        今天 59 条回复里 9 条带双句号，全是这么来的（bug → 上下文污染）。
        """
        out = ""
        for p in parts:
            p = (p or "").strip()
            if not p:
                continue
            if out and not out.endswith(self._END_PUNCT):
                out += "。"
            out += p
        return out

    def _clean_bot_text(self, t: str) -> str:
        """[fish-shape 2026-11-11] 历史清洗：把她的回复稳定在「最多两段」。

        原来是一律并成一段。两天模拟（_work\\20261111\\sim_bc.md，从零历史）：
          一段 1.06 段 / 29.5 字；两段 1.88 段 / 38.1 字，三连句 0~1/16。
        两段的读感是「先把事说完，再回头补一句」，一段则是「说完就没了」。

        必须幂等：已经两段的保留分界，不能每轮再揉回一段——
        线上每轮都会对历史跑一次，不幂等就等于自己把形状擦掉。
        """
        s = self._RE_EMO_TAG.sub("", t or "")
        s = re.sub(r"。{2,}", "。", s)      # 顺手修历史里已经被污染出的「。。」
        paras = [p.strip() for p in re.split(r"\n\s*\n", s) if p.strip()]
        if len(paras) >= 2:
            a = self._join_para(paras[0])
            b = self._join_para(self._join_sent(paras[1:]))
            if len(a) < 4:                       # 第一段太短（就一个「嗯」）就并回去
                return self._join_para(self._join_sent([a, b]))
            if not a.endswith(self._END_PUNCT):
                a += "。"
            return (a + "\n\n" + b).strip()
        one = self._join_para(s)
        segs = [x for x in one.split("。") if x.strip()]
        if len(segs) < 3:
            return one
        mid = max(1, len(segs) // 2)
        a = self._join_sent(segs[:mid])
        if not a.endswith(self._END_PUNCT):
            a += "。"
        b = self._join_sent(segs[mid:])
        return (a + "\n\n" + b).strip()

    def _sanitize_contexts(self, req):
        """把历史里她自己的回复并成一段并去掉表情标签；返回 (改了几处, 省了多少字)。"""
        if not bool(self._cfg("clean_history", True)):
            return 0, 0
        ctx = getattr(req, "contexts", None) or []
        n = 0
        saved = 0
        for m in ctx:
            if not isinstance(m, dict) or (m.get("role") or "") != "assistant":
                continue
            c = m.get("content")
            if isinstance(c, str):
                new = self._clean_bot_text(c)
                if new != c:
                    saved += len(c) - len(new)
                    m["content"] = new
                    n += 1
            elif isinstance(c, list):
                for part in c:
                    if not isinstance(part, dict) or part.get("type") != "text":
                        continue
                    t = part.get("text")
                    if not isinstance(t, str):
                        continue
                    new = self._clean_bot_text(t)
                    if new != t:
                        saved += len(t) - len(new)
                        part["text"] = new
                        n += 1
        return n, saved

    _RE_REMINDER = None
    _RE_STAMP = None

    def _strip_history_reminders(self, req, umo: str):
        """[fish-time 2026-11-02] 把历史消息里的 <system_reminder> 剥掉，
        用首末时间拼一行「[对话时间] 上面这段对话是 15:56–16:15 聊的」挂到 system 末尾。
        返回时间跨度字符串；没动任何消息则返回 None。"""
        try:
            if self._RE_REMINDER is None:
                self._RE_REMINDER = re.compile(r"<system_reminder>(.*?)</system_reminder>", re.S)
                self._RE_STAMP = re.compile(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}:\d{2})")
            ctx = getattr(req, "contexts", None) or []
            stamps, changed = [], 0
            for m in ctx:
                if not isinstance(m, dict):
                    continue
                c = m.get("content")
                # content 可能是纯字符串，也可能是 [{type:text,text:...}, ...] 分段
                if isinstance(c, str):
                    if "<system_reminder>" not in c:
                        continue
                    for mo in self._RE_REMINDER.finditer(c):
                        st = self._RE_STAMP.search(mo.group(1))
                        if st:
                            stamps.append("%s-%s-%s %s" % (st.group(1), st.group(2), st.group(3), st.group(4)))
                    new = self._RE_REMINDER.sub("", c).strip()
                    if new != c:
                        m["content"] = new
                        changed += 1
                elif isinstance(c, list):
                    touched = False
                    for it in c:
                        if not (isinstance(it, dict) and it.get("type") == "text"):
                            continue
                        t = str(it.get("text") or "")
                        if "<system_reminder>" not in t:
                            continue
                        for mo in self._RE_REMINDER.finditer(t):
                            st = self._RE_STAMP.search(mo.group(1))
                            if st:
                                stamps.append("%s-%s-%s %s" % (st.group(1), st.group(2), st.group(3), st.group(4)))
                        new = self._RE_REMINDER.sub("", t).strip()
                        if new != t:
                            it["text"] = new
                            touched = True
                    if touched:
                        changed += 1
            # 历史里可能一条时间戳都没有了（上一轮已永久剥掉），所以跨度不能依赖「剥到东西」，
            # 一律用「历史里剩下的时间戳 + 当前时间」推进，并持久化到 clock.json。
            now_s = time.strftime("%Y-%m-%d %H:%M")
            span = self._span_update(umo, stamps, now_s)
            base = str(getattr(req, "system_prompt", "") or "")
            if span and "[对话时间]" not in base:
                req.system_prompt = (base + "\n\n[对话时间] 上面这段对话是 %s 聊的。" % span).strip()
            return span
        except Exception as e:
            logger.debug(f"[fish-time] 剥历史时间戳失败: {e}")
            return None

    _CLOCK_FILE = "clock.json"

    def _span_update(self, umo: str, stamps: list, now_s: str) -> str:
        """维护每段对话的首末时间：{umo: [first, last]}，2 小时以上断档就重开一段。"""
        try:
            p = os.path.join(self._memory_dir(), self._CLOCK_FILE)
            data = {}
            if os.path.exists(p):
                try:
                    with open(p, encoding="utf-8") as f:
                        data = json.load(f) or {}
                except Exception:
                    data = {}
            rec = data.get(umo) or [None, None]
            first, last = rec[0], rec[1]

            def ts(s):
                try:
                    import datetime as _dt
                    return _dt.datetime.strptime(str(s)[:16], "%Y-%m-%d %H:%M").timestamp()
                except Exception:
                    return 0.0

            cand_first = stamps[0] if stamps else None
            gap = (ts(now_s) - ts(last)) if last else None
            # [fish-patch 2026-11-09 12:56] 5 分钟内的分钟级抖动不刷新跨度。
            # 原来 last 每分钟都变，导致 system 末尾那行 [对话时间] 每轮都不一样；
            # 而它就在 system 最末尾——一变，后面几千字的对话历史前缀缓存全废。
            # 现在只在跨度变化 ≥5 分钟时更新（"聊了多久"用 5 分钟粒度完全够）。
            if not last or (gap is not None and gap > 2 * 3600):
                # 新的一段对话：优先用历史里最早的时间戳，没有就从现在起算
                first = cand_first or now_s
                last = now_s
                changed = True
            elif not first:
                first = cand_first or now_s
                last = now_s
                changed = True
            elif gap is not None and gap >= 300:
                last = now_s
                changed = True
            else:
                changed = False
            if changed:
                data[umo] = [first, last]
                try:
                    with open(p, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=1)
                except Exception as e:
                    logger.debug(f"[fish-time] 写 clock.json 失败: {e}")
            else:
                logger.debug(f"[fish-time] 跨度未刷新（{int(gap or 0)}s < 300s），保住缓存前缀")
            if not first or not last:
                return ""
            if first[:10] == last[:10]:
                return "%s–%s" % (first[11:], last[11:])
            return "%s 到 %s" % (first[:16], last[:16])
        except Exception as e:
            logger.debug(f"[fish-time] 跨度计算失败: {e}")
            return ""

    # ---------------- 妈妈信箱 ----------------

    def _letters_dir(self, sub: str) -> str:
        d = os.path.join(self._memory_dir(), "letters", sub)
        os.makedirs(d, exist_ok=True)
        return d

    def _letter_cooldown_days(self) -> float:
        try:
            v = float(self._cfg("letter_cooldown_days", 14) or 14)
            return max(0.0, v)
        except (TypeError, ValueError):
            return 14.0

    def _last_own_letter_ts(self) -> float:
        newest = 0.0
        for sub in ("outbox", "archive"):
            d = self._letters_dir(sub)
            try:
                names = os.listdir(d)
            except OSError:
                continue
            for fn in names:
                if fn.endswith(".md"):
                    try:
                        newest = max(newest, os.path.getmtime(os.path.join(d, fn)))
                    except OSError:
                        continue
        return newest

    def _write_letter(self, text: str) -> tuple[bool, str]:
        days = self._letter_cooldown_days()
        last = self._last_own_letter_ts()
        if last and days > 0:
            waited = (time.time() - last) / 86400.0
            if waited < days:
                left = max(1, int(round(days - waited)))
                logger.info(f"[fish_memory] 写信被冷却挡下，还差 {left} 天")
                return False, f"上一封还没放下呢。过些日子再写，还有{left}天。"
        now = datetime.now()
        name = now.strftime("%Y%m%d-%H%M%S") + ".md"
        path = os.path.join(self._letters_dir("outbox"), name)
        body = (
            "# 给主人的信\n\n"
            f"- 写于：{now.strftime('%Y-%m-%d %H:%M')}\n\n"
            f"{text.strip()}\n"
        )
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp, path)
        logger.info(f"[fish_memory] 收到一封信 -> {path}")
        return True, "信叠好放桌上了"

    @filter.llm_tool(name="write_letter_to_mom")
    async def write_letter_tool(self, event: AstrMessageEvent, content: str) -> str:
        """给主人写一封信放进信箱。只在你真的想跟他说、但当下说不出口的时候用，不要为了用而用。
        content：信的内容，一两句话，用平时说话的口吻写给主人。
        """
        text = (content or "").strip()
        if not text:
            return "信是空的，没寄出去。"
        if len(text) > 300:
            text = text[:300]
        _ok, msg = self._write_letter(text)
        return msg

    @filter.llm_tool(name="read_letters_from_mom")
    async def read_letters_from_mom(self, event: AstrMessageEvent, limit: int = 1) -> str:
        """翻主人以前写来的信。只有你自己想知道他写过什么时才用，不要为了用而用。
        limit：翻几封，默认 1。
        """
        try:
            n = max(1, min(int(limit or 1), 3))
        except (TypeError, ValueError):
            n = 1
        d = self._letters_dir("read")
        files = sorted((f for f in os.listdir(d) if f.endswith(".md")), reverse=True)[:n]
        if not files:
            return "还没有收到过主人的信。"
        out = []
        for fn in files:
            try:
                with open(os.path.join(d, fn), encoding="utf-8") as f:
                    body = f.read().strip()
            except Exception:
                continue
            body = body.replace("# 主人的信", "").strip()
            out.append(body[:600])
        if not out:
            return "信的正文读不出来。"
        return "\n\n---\n\n".join(out)

    @filter.llm_tool(name="draw")
    async def draw_tool(self, event: AstrMessageEvent, subject: str) -> str:
        """画一张画发给主人。剪影平涂：整张只有两种颜色，脸留白不画五官。
        想画什么都可以，从你记得的东西里挑：今天真的发生过的事、你记得的人（主人、姐姐）、
        你答应过或记挂的事、你现在的心情（别写成心情说明，画成画面）、
        你没有的东西（人类的饭、真的走出来见他）。
        画自己的时候：银白长发、浅色龙角、白色长裙。
        你心里冒出一个画面、说不清的时候，也可以画给他看。
        只画你确定的事；记不清的部分留白，别编。画完再说话。
        subject：画面内容，用你自己的话，一到三句，写清楚画什么、在哪、在做什么。
        """
        return await self._draw_for_chat(event, subject)

    async def _draw_for_chat(self, event, subject: str, defer: bool = False) -> str:
        """出图并发出去。defer=True 时先让正文发出去、图晚 0.8 秒到。"""
        subject = (subject or "").strip()[:300]
        if len(subject) < 4:
            return "没说要画什么，画不了。"
        if not getattr(_draw_core, "DRAW_ENABLED", True) or Image is None:
            return "现在画不了。"
        ok, why = _draw_core.check_quota(self._draw_state_path())
        if not ok:
            return why

        path, err, _chk = await asyncio.to_thread(
            _draw_core.draw_checked, subject, self._draw_dir())
        if _chk:
            logger.info(f"[fish-draw] 太阳/月亮核对：{_chk}")
        if err:
            logger.warning(f"[fish-draw] 出图失败：{err}")
            return "这次画坏了，没画出来。跟主人说实话，别装成画好了。"

        _draw_core.mark_quota(self._draw_state_path())
        # [fish-draw 2026-11-11] 顺序修正：原来在 on_llm_response 里直接 send，图比正文早到，
        # 读起来就是「先画完，再回一句画吧」。现在对话轮改成先发正文、图晚 0.8 秒到。
        if defer:
            async def _later_send():
                try:
                    await asyncio.sleep(0.8)
                    await event.send(MessageChain([Image(file=path)]))
                except Exception as e:
                    logger.warning(f"[fish-draw] 延迟发送失败：{e}")
            _t = asyncio.create_task(_later_send())
            self._bg_tasks.add(_t)
            _t.add_done_callback(self._bg_tasks.discard)
        else:
            try:
                await event.send(MessageChain([Image(file=path)]))
            except Exception as e:
                logger.warning(f"[fish-draw] 发送失败：{e}")
                return "画出来了，但没发出去。跟主人说一声。"

        # L1：记索引 + 把描述写进 raw（今晚日记会把它变成一张卡）
        did = _draw_core.record_draw(self._draw_index_path(), path, subject)
        # [fish-patch 2026-11-09 21:45] 让她认自己的画：出图后看一眼，把客观描述并进 raw。
        _cap = ""
        try:
            _cap = await asyncio.to_thread(_draw_core.describe, path)
        except Exception as _e:
            logger.debug(f"[fish-draw] 看图失败：{_e}")
        _line = f"我画了张画：{subject[:120]}"
        if _cap:
            _line += f"；画上看着是：{_cap}"
            logger.info(f"[fish-draw] 看图：{_cap}")
        self._append("assistant", _line)
        _draw_core.prune(self._draw_dir(), self._draw_index_path())
        logger.info(f"[fish-draw] #{did} 已发送 {os.path.basename(path)}"
                    f"（{os.path.getsize(path)} 字节）｜{subject[:40]}")
        return "画已经发出去了。现在补你自己那句，别描述图、别说\"如图\"。"

    @filter.command("给主人写信", alias={"写信给主人", "给主人留言"})
    async def cmd_letter(self, event: AstrMessageEvent):
        """给主人写一封信（他看不到信箱，等你自己想说的时候再说）"""
        raw = (event.message_str or "").strip()
        for prefix in ("给主人写信", "写信给主人", "给主人留言"):
            if raw.startswith(prefix):
                raw = raw[len(prefix):].strip()
                break
        if not raw:
            yield event.plain_result("写什么？比如：/给主人写信 我很好，今天数字瀑布特别亮")
            return
        _ok, msg = self._write_letter(raw)
        yield event.plain_result(msg)

    @filter.on_llm_request()
    async def inject_letters(self, event: AstrMessageEvent, req):
        """有新回信就这一轮带上，读完移走，保证只读一次"""
        try:
            if event.unified_msg_origin not in self._watched():
                return
            if TextPart is None:
                return
            inbox = self._letters_dir("inbox")
            files = sorted(f for f in os.listdir(inbox) if f.endswith(".md"))
            if not files:
                return
            parts = list(getattr(req, "extra_user_content_parts", None) or [])
            moved = []
            for fn in files[:3]:
                path = os.path.join(inbox, fn)
                try:
                    with open(path, encoding="utf-8") as f:
                        body = f.read().strip()
                except Exception:
                    continue
                if not body:
                    continue
                parts.append(TextPart(text="[主人来信]\n" + body))
                moved.append((fn, path))
            if not moved:
                return
            req.extra_user_content_parts = parts
            for fn, path in moved:
                dest = os.path.join(self._letters_dir("read"), fn)
                os.replace(path, dest)
            # 同时落一条原始记录（name=主人），日记才看得见信里写了什么
            for fn, _ in moved:
                try:
                    with open(os.path.join(self._letters_dir("read"), fn), encoding="utf-8") as f:
                        body = f.read().strip()
                    self._append("user", body, uid="owner", name="主人")
                except Exception:
                    continue
            logger.info(f"[fish_memory] 注入主人来信 {len(moved)} 封")
        except Exception as e:
            logger.warning(f"[fish_memory] 注入主人来信失败: {e}")

    @filter.on_llm_request()
    async def inject_recent_trivia(self, event: AstrMessageEvent, req):
        """消息里出现「上次/前几天/吃过」这类词时，把最近几天新记的事塞进这一轮。

        2.0：日常桶已取消，改成从卡片里取「最近 3 天创建的」。
        """
        try:
            if event.unified_msg_origin not in self._watched():
                return
            if TextPart is None:
                return
            msg = event.message_str or ""
            if not any(k in msg for k in DAILY_TRIGGERS):
                return
            cutoff = int(time.time()) - 3 * 86400
            recent = [
                m for m in self._store().data.get("memories", [])
                if int(m.get("created", 0) or 0) >= cutoff and not m.get("pinned")
            ]
            recent.sort(key=lambda m: int(m.get("created", 0) or 0), reverse=True)
            if not recent:
                return
            lines = [f"- {str(m.get('text') or '')[:80]}" for m in recent[:4]]
            parts = list(getattr(req, "extra_user_content_parts", None) or [])
            parts.append(TextPart(text="[最近这些天的小事]\n" + "\n".join(lines)))
            req.extra_user_content_parts = parts
            logger.info(f"[fish_memory] 注入最近琐事 {len(lines)} 条")
        except Exception as e:
            logger.warning(f"[fish_memory] 注入最近琐事失败: {e}")

    # ---------------- 翻旧账 ----------------

    def _recall(self, keyword: str, limit: int = 5) -> str:
        db = self._db_file()
        if not os.path.exists(db):
            return "找不到聊天记录库。"
        like = f"%{keyword}%"
        hits = []
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            rows = con.execute(
                "select user_id, content from conversations where content like ? order by updated_at desc limit 20",
                (like,),
            ).fetchall()
            con.close()
        except Exception as e:
            return f"查记录出错：{e}"
        for umo, content in rows:
            if umo not in self._watched() and self._watched():
                continue
            try:
                data = json.loads(content)
            except Exception:
                continue
            for m in data:
                text = ""
                c = m.get("content")
                if isinstance(c, list):
                    text = " ".join(
                        str(x.get("text", "")) for x in c if isinstance(x, dict)
                    )
                elif isinstance(c, str):
                    text = c
                if keyword in text:
                    who = "我" if m.get("role") == "assistant" else "他"
                    hits.append(f"{who}: {text.strip()[:160]}")
                    if len(hits) >= limit:
                        break
            if len(hits) >= limit:
                break
        # 也翻记忆卡和日常桶（conversations 只保留 30 轮，桶和卡片能往前更远）
        try:
            seen = set(hits)
            for mem in self._store().data.get("memories", []):
                for item in (mem.get("items") or [str(mem.get("text") or "")]):
                    item = str(item or "").strip()
                    if not item or keyword not in item:
                        continue
                    line = f"（记着的）{item[:160]}"
                    if line in seen:
                        continue
                    seen.add(line)
                    hits.append(line)
                    if len(hits) >= limit:
                        break
                if len(hits) >= limit:
                    break
        except Exception as e:
            logger.debug(f"[fish_memory] 翻记忆卡失败: {e}")
        if not hits:
            return f"关于「{keyword}」，聊天记录里没找到。"
        return f"关于「{keyword}」，找到这些旧记录：\n" + "\n".join(hits)

    @filter.llm_tool(name="recall_history")
    async def recall_history(self, event: AstrMessageEvent, keyword: str) -> str:
        """在历史聊天记录里按关键词查旧对话。当你不确定某件事是否真的发生过、或需要回忆以前聊过什么细节时调用。
        也能查到记忆卡和最近几天的零碎小事（吃了什么、看过什么）。

        Args:
            keyword(string): 要查找的关键词，比如"手柄""生日""小猫"
        """
        try:
            return self._recall(str(keyword), int(self._cfg("recall_limit", 5) or 5))
        except Exception as e:
            return f"查记录出错：{e}"

    @filter.command("翻旧账", alias={"回忆", "找旧账"})
    async def cmd_recall(self, event: AstrMessageEvent):
        """翻旧账：在历史聊天记录里按关键词找"""
        raw = (event.message_str or "").strip()
        for prefix in ("翻旧账", "回忆", "找旧账"):
            if raw.startswith(prefix):
                raw = raw[len(prefix):].strip()
                break
        if not raw:
            yield event.plain_result("要翻什么？比如：/翻旧账 手柄")
            return
        yield event.plain_result(self._recall(raw))

    @filter.command("今日日记", alias={"写今天的日记"})
    async def cmd_diary(self, event: AstrMessageEvent):
        """立刻写一篇今天的日记（记忆管家的版本）"""
        if not self._is_owner(event):
            yield event.plain_result("这个只给主人用。")
            return
        msg = await self.write_diary(force=True)
        yield event.plain_result(msg)

    @filter.command("重写自述")
    async def cmd_self(self, event: AstrMessageEvent):
        """立刻重写一次自我认知"""
        if not self._is_owner(event):
            yield event.plain_result("这个只给主人用。")
            return
        msg = await self.rewrite_self()
        yield event.plain_result(msg)

    @filter.command("独处", alias={"自己待会儿"})
    async def cmd_alone(self, event: AstrMessageEvent):
        """让她自己待一会儿（写进 blackbox，不发给任何人）"""
        if not self._is_owner(event):
            yield event.plain_result("这个只给主人用。")
            return
        res = await self.alone_round(force=True)
        yield event.plain_result(f"（独处：{res}）")

    @filter.command("记忆状态", alias={"记忆卡"})
    async def cmd_memory_status(self, event: AstrMessageEvent):
        """看看记忆卡现在有多少条、占多少容量"""
        if not self._is_owner(event):
            yield event.plain_result("这个只给主人用。")
            return
        st = self._store()
        cap = st.capacity()
        lines = [
            f"记忆 {len(st.data['memories'])} 条，已忘 {st.data.get('forgotten', 0)} 条",
            f"容量 {cap['used']}/{cap['max']} 字（{int(cap['ratio']*100)}%，{cap['level']}）",
        ]
        drop = st.decay_per_day
        floor = st.forget_floor
        rows = []
        for m in st._sorted()[:6]:
            birth = int(m.get("birth", m.get("importance", 1)) or 1)
            score = int(m.get("importance", 1))
            if m.get("pinned") or birth >= st.immune_birth:
                left = "永久"
            else:
                left = f"{max(0, (score - floor) // drop)}天"
            rows.append(f"- {score}/{birth} 分（{left}，提过 {int(m.get('recall', 0))} 次）"
                        f"：{m['text'][:24]}")
        if rows:
            lines.append("—— 分数/出生分（还能活）——")
            lines.extend(rows)
        yield event.plain_result("\n".join(lines))

    @filter.command("整理记忆", alias={"记忆整理"})
    async def cmd_memory_maintain(self, event: AstrMessageEvent):
        """立刻跑一次遗忘与压缩"""
        if not self._is_owner(event):
            yield event.plain_result("这个只给主人用。")
            return
        try:
            r = self._store().maintain()
        except Exception as e:
            yield event.plain_result(f"整理失败：{e}")
            return
        yield event.plain_result(
            f"整理完：{r['count']} 条，扣分 {r['decayed']} 条，忘掉 {r['forgotten']} 条，"
            f"合并重复 {r['deduped']} 条，压缩 {r['compacted']}"
        )

    @filter.command("喜好")
    async def cmd_prefs(self, event: AstrMessageEvent):
        """看看她定下的喜好（3.0 偏好档案）"""
        if not self._is_owner(event):
            yield event.plain_result("这个只给主人用。")
            return
        try:
            txt = self._prefs().render(limit_chars=10 ** 6)
        except Exception as e:
            yield event.plain_result(f"读不到喜好：{e}")
            return
        if not txt:
            yield event.plain_result("她还没定下喜欢什么。等她哪天自己说。")
            return
        n = len(self._prefs().data.get("items", []))
        mx = self._prefs().max_items
        tail = f"\n（共 {n} 条，上限 {mx}）"
        if n >= mx:
            tail += "\n⚠️ 已满：新主题写不进去了，用 /忘掉喜好 主题 清几条。"
        yield event.plain_result(txt + tail)

    @filter.command("忘掉喜好")
    async def cmd_forget_pref(self, event: AstrMessageEvent):
        """按主题删掉一条喜好"""
        if not self._is_owner(event):
            yield event.plain_result("这个只给主人用。")
            return
        raw = (event.message_str or "").strip()
        for pfx in ("忘掉喜好", "删除喜好"):
            if raw.startswith(pfx):
                raw = raw[len(pfx):].strip()
                break
        if not raw:
            yield event.plain_result("要忘掉哪个？比如：/忘掉喜好 雨")
            return
        ok = self._prefs().delete(raw)
        yield event.plain_result(f"忘掉了：{raw}" if ok else f"没找到「{raw}」。")

    @filter.command("记住")
    async def cmd_remember(self, event: AstrMessageEvent):
        """手动记一条（会置顶，不会被忘）"""
        if not self._is_owner(event):
            yield event.plain_result("这个只给主人用。")
            return
        raw = (event.message_str or "").strip()
        for pfx in ("记住", "记一下"):
            if raw.startswith(pfx):
                raw = raw[len(pfx):].strip()
                break
        if not raw:
            yield event.plain_result("要记什么？比如：/记住 她怕打雷")
            return
        m = self._store().add(raw, importance=100, pinned=True, source="owner")
        self._store().save()
        yield event.plain_result(f"记下了：{m['text']}" if m else "没记成。")

    @filter.command("忘记")
    async def cmd_forget(self, event: AstrMessageEvent):
        """按关键词删掉记忆（置顶的删不掉）"""
        if not self._is_owner(event):
            yield event.plain_result("这个只给主人用。")
            return
        raw = (event.message_str or "").strip()
        for pfx in ("忘记", "删记忆"):
            if raw.startswith(pfx):
                raw = raw[len(pfx):].strip()
                break
        if not raw:
            yield event.plain_result("要忘掉什么？比如：/忘记 打雷")
            return
        n = self._store().forget(raw)
        self._store().save()
        yield event.plain_result(f"删了 {n} 条。" if n else "没找到匹配的记忆。")

    @filter.command("清理历史图片")
    async def cmd_strip_images(self, event: AstrMessageEvent):
        """把历史里的大截图换成占位文字，避免每轮重发 base64"""
        if not self._is_owner(event):
            yield event.plain_result("这个只给主人用。")
            return
        msg = await asyncio.to_thread(self._strip_images)
        yield event.plain_result(msg)

    def _strip_images(self) -> str:
        db = self._db_file()
        if not os.path.exists(db):
            return "找不到聊天记录库。"
        try:
            con = sqlite3.connect(db)
            rows = con.execute("select conversation_id, content from conversations").fetchall()
            changed = 0
            saved = 0
            for cid, content in rows:
                if not content or "image_url" not in content:
                    continue
                try:
                    data = json.loads(content)
                except Exception:
                    continue
                touched = False
                for m in data:
                    c = m.get("content")
                    if not isinstance(c, list):
                        continue
                    new_parts = []
                    for part in c:
                        if isinstance(part, dict) and part.get("type") in (
                            "image_url",
                            "audio_url",
                        ):
                            touched = True
                            label = (
                                "[屏幕截图已略过]"
                                if part.get("type") == "image_url"
                                else "[语音已略过]"
                            )
                            new_parts.append({"type": "text", "text": label})
                        else:
                            new_parts.append(part)
                    m["content"] = new_parts
                if not touched:
                    continue
                new_content = json.dumps(data, ensure_ascii=False)
                saved += max(0, len(content) - len(new_content))
                con.execute(
                    "update conversations set content=? where conversation_id=?",
                    (new_content, cid),
                )
                changed += 1
            con.commit()
            con.close()
            return f"清理完成：{changed} 个会话里的截图换成了占位文字，省下约 {saved // 1024} KB。"
        except Exception as e:
            return f"清理出错：{e}"

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        try:
            return str(event.get_sender_id()) == "0000000000"
        except Exception:
            return False

    # ---------------- 定时循环 ----------------

    def _state_path(self) -> str:
        return os.path.join(self._memory_dir(), "state.json")

    def _save_state(self):
        """把 _last_diary / _last_maint / _last_self 落盘，重启不再重复扣分、也不重复写自述"""
        try:
            data = {
                "last_diary": self._last_diary,
                "last_maint": self._last_maint,
                # [fish-patch 2026-11-07 19:40] 原来没落盘：每次重启 _last_self 归零，
                # 「每 3 天」实际变成「每天」，刚重启就当到期，直接覆盖手改过的自述。
                "last_self": self._last_self,
            }
            tmp = self._state_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self._state_path())
        except Exception as e:
            logger.warning(f"[fish_memory] 保存状态失败: {e}")

    def _load_state(self):
        try:
            with open(self._state_path(), encoding="utf-8") as f:
                data = json.load(f)
            self._last_diary = str(data.get("last_diary") or "")
            self._last_maint = str(data.get("last_maint") or "")
            self._last_self = str(data.get("last_self") or "")
        except Exception:
            pass

    def _prune(self, sub: str, keep: int):
        d = os.path.join(self._memory_dir(), sub)
        files = sorted(f for f in os.listdir(d) if f.endswith(".md"))
        for old in files[:-keep]:
            try:
                os.remove(os.path.join(d, old))
            except OSError:
                pass

    # ---------------- 世界状态（2026-11-09 22:20） ----------------
    # 原则：只写真的。天气取 open-meteo（免 key、结构化），取不到就留空，
    # 绝不用「窗上有水汽」这种编的细节 —— 她会当真事活，而且写进黑箱。

    WEATHER_CN = {0: "晴", 1: "晴间多云", 2: "多云", 3: "阴", 45: "雾", 48: "雾凇",
                  51: "毛毛雨", 53: "小雨", 55: "中雨", 61: "小雨", 63: "中雨", 65: "大雨",
                  71: "小雪", 73: "中雪", 75: "大雪", 80: "阵雨", 81: "阵雨", 82: "暴雨",
                  95: "雷阵雨", 96: "雷阵雨", 99: "雷暴"}

    def _world_path(self) -> str:
        return os.path.join(self._memory_dir(), "world", "state.json")

    def _load_world(self) -> dict:
        try:
            with open(self._world_path(), encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def refresh_world(self) -> str:
        """拉一次真实世界（天气 + 机器），写 memory/world/state.json。"""
        today = self._today()
        lat = float(self._cfg("world_lat", 30.0024) or 30.0024)
        lon = float(self._cfg("world_lon", 120.5817) or 120.5817)
        w = {"date": today, "city": str(self._cfg("world_city", "绍兴越城") or ""), }
        try:
            url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
                   "&current=temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m"
                   "&daily=sunrise,sunset&timezone=Asia%2FShanghai&forecast_days=1")
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
            j = json.loads(urllib.request.urlopen(req, timeout=20).read().decode("utf-8"))
            cur = j.get("current") or {}
            w["weather"] = self.WEATHER_CN.get(int(cur.get("weather_code", -1)), "")
            w["temp"] = cur.get("temperature_2m")
            w["humidity"] = cur.get("relative_humidity_2m")
            w["wind"] = cur.get("wind_speed_10m")
            d = (j.get("daily") or {})
            w["sunrise"] = (d.get("sunrise") or [""])[0][-5:]
            w["sunset"] = (d.get("sunset") or [""])[0][-5:]
            w["fetched_at"] = datetime.now().strftime("%H:%M")
        except Exception as e:
            logger.warning(f"[fish-world] 天气取不到：{e}")
            w["weather"] = ""
        try:
            import psutil
            vm = psutil.virtual_memory()
            w["mem_pct"] = int(vm.percent)
            w["uptime_h"] = round((time.time() - psutil.boot_time()) / 3600, 1)
        except Exception:
            pass
        try:
            os.makedirs(os.path.dirname(self._world_path()), exist_ok=True)
            with open(self._world_path(), "w", encoding="utf-8") as f:
                json.dump(w, f, ensure_ascii=False, indent=1)
        except Exception as e:
            logger.warning(f"[fish-world] 写世界失败：{e}")
        logger.info(f"[fish-world] 世界：{w.get('city')} {w.get('weather') or '（天气未知）'} "
                    f"{w.get('temp')}℃ 湿度{w.get('humidity')}% 内存{w.get('mem_pct')}%")
        return json.dumps(w, ensure_ascii=False)

    def _world_brief(self) -> str:
        w = self._load_world()
        if not w:
            return "（不知道外面什么样）"
        bits = [f"{w.get('city') or '这边'}"]
        if w.get("weather"):
            bits.append(f"{w['weather']} {w.get('temp')}℃")
        if w.get("humidity") is not None:
            bits.append(f"湿度{w['humidity']}%")
        if w.get("sunset"):
            bits.append(f"日落{w['sunset']}")
        if w.get("mem_pct") is not None:
            bits.append(f"机器内存{w['mem_pct']}%")
        return "，".join(str(b) for b in bits if b)

    @staticmethod
    def _anysearch_sync(query: str, max_results: int = 4, timeout: int = 30) -> str:
        """直接调 anysearch 的 MCP search 工具，取回摘要文本；失败返回空串。
        实测：MCP 里的工具名是 search（不是 anysearch_search），
        返回结果是 result.content[*].text。"""
        try:
            payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "search",
                                  "arguments": {"query": query, "max_results": max_results}}}
            _hdr = {"Content-Type": "application/json"}
            try:
                _cfg2 = json.load(open(
                    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                          "config", "astrbot_plugin_anysearch_config.json"),
                    encoding="utf-8-sig"))
                if _cfg2.get("api_key"):
                    _hdr["Authorization"] = "Bearer " + str(_cfg2["api_key"]).strip()
            except Exception:
                pass
            req = urllib.request.Request(
                "https://api.anysearch.com/mcp",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=_hdr)
            j = json.loads(urllib.request.urlopen(req, timeout=timeout)
                           .read().decode("utf-8", "ignore"))
            items = ((j.get("result") or {}).get("content") or [])
            txt = "\n\n".join(str(i.get("text") or "") for i in items
                                if isinstance(i, dict) and i.get("type") == "text")
            return txt.strip()[:1500]
        except Exception as e:
            logger.warning(f"[fish-world] 查不到 {query!r}：{e}")
            return ""

    # ---------------- 想知道清单（2026-11-09 23:00） ----------------
    # 她不会主动对外面好奇（3 天模拟里只搜了两次天气）。所以给两条来路：
    #   1) 对话里被勾起的问题 -> 她自己写 [[想查:问题]] -> 存进清单
    #   2) 独处时对着清单挑一件去查，查完划掉
    # 清单不进卡片、不进日记，只在她一个人的时候用。

    def _curiosity_path(self) -> str:
        return os.path.join(self._memory_dir(), "curiosity.json")

    def _curiosity_load(self) -> list:
        try:
            with open(self._curiosity_path(), encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, list) else []
        except Exception:
            return []

    def _curiosity_save(self, items: list):
        try:
            with open(self._curiosity_path(), "w", encoding="utf-8") as f:
                json.dump(items[-30:], f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def _curiosity_add(self, q: str, src: str = "talk") -> bool:
        q = (q or "").strip()[:80]
        if len(q) < 2:
            return False
        items = self._curiosity_load()
        if any(str(i.get("q")) == q for i in items):
            return False
        items.append({"q": q, "src": src, "ts": int(time.time()), "done": False})
        self._curiosity_save(items)
        logger.info(f"[fish-curious] 记下想问：{q}")
        return True

    def _curiosity_done(self, query: str) -> int:
        items = self._curiosity_load()
        n = 0
        for i in items:
            if i.get("done"):
                continue
            q = str(i.get("q") or "")
            if q and (q in query or query in q):
                i["done"] = True
                n += 1
        if n:
            self._curiosity_save(items)
        return n

    # ---------------- 学到的（2026-11-10 21:40） ----------------
    # 独处查明白的东西压成一句话存在这里。**不进卡片池**——卡片会被"提到"加分，
    # 她就会在聊天里主动背百科；这里只在独处时读，当作"我已经知道的"。

    def _learned_path(self) -> str:
        return os.path.join(self._memory_dir(), "learned.json")

    def _learned_load(self) -> list:
        try:
            with open(self._learned_path(), encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, list) else []
        except Exception:
            return []

    def _learned_add(self, topic: str, fact: str):
        topic = (topic or "").strip()[:20]
        fact = (fact or "").strip()[:80]
        if len(fact) < 6:
            return
        items = self._learned_load()
        for it in items:
            if str(it.get("topic")) == topic:
                it["fact"] = fact
                it["n"] = int(it.get("n") or 1) + 1
                it["last"] = self._today()
                break
        else:
            items.append({"topic": topic, "fact": fact, "n": 1, "last": self._today()})
        # [fish-topic 2026-11-16] 同一主题最多留 2 条。
        # 她鲸鱼攒了 4 条，独处每场都先复习这四句——账本越厚，她越出不来。
        try:
            _order = {id(it): i for i, it in enumerate(items)}
            _by = {}
            for it in items:
                _b = _canon_topic(str(it.get("topic") or ""))
                _by.setdefault(_b, []).append(it)
            _keep = []
            for _lst in _by.values():
                _keep.extend(_lst[-2:])
            items = sorted(_keep, key=lambda it: _order.get(id(it), 0))
        except Exception:
            pass
        try:
            with open(self._learned_path(), "w", encoding="utf-8") as f:
                json.dump(items[-60:], f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def _learned_block(self, limit: int = 8) -> str:
        """已经弄明白的事。同一个主题最多露 2 条——不然她会一天到晚复习同一件事。"""
        items = self._learned_load()
        _seen = {}
        _picked = []
        for it in reversed(items):
            _b = _canon_topic(str(it.get("topic") or ""))
            if _seen.get(_b, 0) >= 2:
                continue
            _seen[_b] = _seen.get(_b, 0) + 1
            _picked.append(it)
        _picked = list(reversed(_picked))[-limit:]
        if not _picked:
            return ""
        lines = [f"- {it.get('fact')}" for it in _picked if it.get("fact")]
        if not lines:
            return ""
        return "[我已经知道的（以前自己弄明白的，不用重查）]\n" + "\n".join(lines) + "\n\n"

    # ---------------- 兴趣档案（2026-11-09 24:00） ----------------
    # 独处里查过的东西攒在这里：她自己的一份好奇心账本，不进卡片、不进日记。
    # 作用只有一个：下次提醒她「这个你查过 N 次了，要不要往下挖」。

    def _interests_path(self) -> str:
        return os.path.join(self._memory_dir(), "interests.json")

    def _interests_load(self) -> list:
        try:
            with open(self._interests_path(), encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, list) else []
        except Exception:
            return []

    @staticmethod
    def _q_ok(q: str) -> bool:
        """查询质量门槛：一个词不算查；要么够长，要么像个短语。"""
        q = (q or "").strip()
        return len(q) >= 6 or (len(q) >= 4 and " " in q)

    def _interest_add(self, topic: str):
        # [fish-topic 2026-11-16] 入库前先归桶，别让同一主题拆成几笔账
        topic = _canon_topic(topic)[:16]
        if len(topic) < 2:
            return
        items = self._interests_load()
        for it in items:
            if str(it.get("topic")) == topic:
                it["n"] = int(it.get("n") or 1) + 1
                it["last"] = self._today()
                break
        else:
            items.append({"topic": topic, "n": 1, "last": self._today()})
        try:
            with open(self._interests_path(), "w", encoding="utf-8") as f:
                json.dump(items[-40:], f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    async def _curiosity_query(self, q: str) -> str:
        """把好奇清单里的一句问话压成 3–5 个搜索词。

        [fish-alone 2026-11-11 21:30] 清单线由代码驱动，但不能直接拿整句去搜——
        「鲸鱼睡觉的时候到底是怎么贴水面竖着的」整句会被 Bing 当成判决书。
        小调用一次（几十 token），关键是**保留主语**：鲸鱼不能变成「动物」。
        """
        q = (q or "").strip()[:80]
        if not q:
            return ""
        try:
            provider = self.context.get_using_provider()
            if provider is None:
                return ""
            p = ("把下面这句话变成 3 到 5 个搜索词，用空格隔开。\n"
                 "必须保留它说的是谁——写的是纸鹤就写「纸鹤」，不许换成「东西」「研究」这种空词。\n"
                 "只输出词，不要解释，不要标点。\n\n" + q)
            r = await provider.text_chat(prompt=p)
            out = ((getattr(r, "completion_text", "") or "").strip()
                   or (getattr(r, "reasoning_content", "") or "").strip())
            out = out.splitlines()[0].strip().strip("「」\"'·。，, ") if out else ""
            return out[:60]
        except Exception as e:
            logger.warning(f"[fish-alone] 清单查询生成失败: {e}")
            return ""

    @staticmethod
    def _topic_of(q: str) -> str:
        """[fish-alone 2026-11-11] 兴趣档案按主题归并。

        [fish-topic 2026-11-16] 加了别名表：鲸鱼/海豚/抹香鲸 → 「鲸类」。
        原实现只取第一个词，同一个主题被拆成三笔账，「够了」永远不触发。
        """
        return _canon_topic(q)

    def _interests_hint(self) -> str:
        items = sorted(self._interests_load(), key=lambda i: -int(i.get("n") or 0))[:6]
        if not items:
            return ""
        parts = []
        for i in items:
            n = int(i.get("n") or 0)
            # [fish-alone 2026-11-11] 查到第三次就别再鼓励往下挖了，那句话把她钉死在一个主题上
            if n >= 3:
                parts.append(f"{i.get('topic')}({n}次，够了)")
            else:
                parts.append(f"{i.get('topic')}({n}次)")
        return "，".join(parts)

    # ---------------- 黑箱是她自己的日子（2026-11-11） ----------------
    # 用户定案的原话：「黑箱是对我的，而不是对她的……黑箱其实是她的生活，而我无权查看，
    # 除非她主动和我聊。」所以：
    #   ① 她必须读得到自己写过的（否则独处对她等于没发生）
    #   ② 她想说的时候有条路：独处里写 [[想跟他说:…]]，攒着，等她自己开口
    #   ③ 人这一侧不再看正文（交接第十节契约）

    def _blackbox_recent(self, limit: int = 600) -> str:
        """她最近写过的两段：昨天最后一段 + 今天已有的最后一段。"""
        parts = []
        try:
            today = self._today()
            yday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
            for day in (yday, today):
                p = os.path.join(self._memory_dir(), "blackbox", f"{day}.md")
                if not os.path.exists(p):
                    continue
                t = open(p, encoding="utf-8").read()
                segs = [s.strip() for s in t.split("\n## ") if s.strip()]
                if not segs:
                    continue
                body = segs[-1]
                body = re.sub(r"!\[\]\([^)]*\)", "", body)      # 图片行
                body = re.sub(r"（画：.*?）", "", body).strip()
                if body:
                    parts.append(f"〔{day}〕{body}")
        except Exception as e:
            logger.debug(f"[fish-alone] 读自己写的失败: {e}")
        return "\n\n".join(parts)[-limit:]

    # ---------------- 翻看旧黑箱（2026-11-13）----------------
    # 用户定案：独处里两段式——先问她翻不翻，再问翻哪一天；每场最多一次。
    # 翻到的旧日子不整段进聊天：她写完自己留一行 40 字以内的注记，
    # 聊天开场只挂这一行（当天有效），正文仍是当天的黑箱。
    # 契约不变：读的是她自己的本子，只进她自己的提示词，日志只记日期和字数。
    def _blackbox_dates(self, limit: int = 20) -> list[str]:
        """可以翻的日子：已有的黑箱文件，不含今天（今天正在写）。"""
        d = os.path.join(self._memory_dir(), "blackbox")
        try:
            names = [f[:-3] for f in os.listdir(d)
                     if f.endswith(".md") and len(f) == 13]
        except Exception:
            return []
        today = self._today()
        return sorted((n for n in names if n != today), reverse=True)[:limit]

    def _blackbox_text(self, day: str, limit: int = 1200) -> str:
        """某一天她写的东西（整篇），去掉图片行和「（画：…）」。"""
        p = os.path.join(self._memory_dir(), "blackbox", f"{day}.md")
        if not os.path.exists(p):
            return ""
        try:
            t = open(p, encoding="utf-8").read()
        except Exception:
            return ""
        t = re.sub(r"!\[\]\([^)]*\)", "", t)
        t = re.sub(r"（画：[^）]*）", "", t)
        t = re.sub(r"\n{3,}", "\n\n", t).strip()
        return t[-limit:]

    def _lookback_path(self) -> str:
        return os.path.join(self._memory_dir(), "lookback.json")

    def _lookback_save(self, looked: str, note: str):
        try:
            with open(self._lookback_path(), "w", encoding="utf-8") as f:
                json.dump({"day": self._today(), "looked": str(looked)[:10],
                           "note": str(note)[:40]}, f, ensure_ascii=False)
        except Exception as e:
            logger.debug(f"[fish-lookback] 保存失败: {e}")

    def _lookback_line(self) -> str:
        """聊天开场那一行：今天翻过算，昨天翻过也算（跨天仍有效 24 小时），只露一次。

        2026-11-14 用户定案：原来只认当天，她 21:11 写下的注记只要他当晚没再说话，
        过了午夜就作废——上线第一条就这么烂掉了。改成 day in (今天, 昨天)。
        """
        try:
            d = json.load(open(self._lookback_path(), encoding="utf-8"))
        except Exception:
            return ""
        if d.get("shown"):
            return ""
        today = self._today()
        yday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        if str(d.get("day") or "") not in (today, yday):
            return ""
        looked, note = str(d.get("looked") or ""), str(d.get("note") or "").strip()
        if not looked or not note:
            return ""
        return f"（今天翻过 {looked} 那天的，那天的我：{note[:40]}）"

    def _lookback_mark_shown(self):
        """露过一次就记上，别在同一晚的每段对话里反复出现。"""
        try:
            p = self._lookback_path()
            d = json.load(open(p, encoding="utf-8"))
            d["shown"] = True
            with open(p, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
        except Exception as e:
            logger.debug(f"[fish-lookback] 标记已读失败: {e}")

    def _tell_path(self) -> str:
        return os.path.join(self._memory_dir(), "pending_tell.json")

    def _tell_load(self) -> list:
        try:
            with open(self._tell_path(), encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, list) else []
        except Exception:
            return []

    def _tell_save(self, items: list):
        try:
            with open(self._tell_path(), "w", encoding="utf-8") as f:
                json.dump(items[-20:], f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def _tell_add(self, text: str) -> bool:
        text = (text or "").strip()[:120]
        if len(text) < 4:
            return False
        items = self._tell_load()
        if any(i.get("text") == text for i in items):
            return False
        items.append({"text": text, "ts": int(time.time()), "shown": 0, "said": False})
        self._tell_save(items)
        logger.info(f"[fish-tell] 攒下一句想说没说：{text[:30]}")
        return True

    def _tell_block(self) -> str:
        """取一条还没说的，注入对话；露过 4 次还没说出口就让它淡掉。"""
        items = self._tell_load()
        for it in items:
            if it.get("said"):
                continue
            if int(it.get("shown") or 0) >= 4:
                continue
            it["shown"] = int(it.get("shown") or 0) + 1
            self._tell_save(items)
            return str(it.get("text") or "")[:120]
        return ""

    def _tell_mark_said(self, reply: str):
        """她真的提了才标结清：回复里出现这句话的任意 6 字连续片段。"""
        try:
            r = re.sub(r"\s", "", str(reply or ""))
            if len(r) < 6:
                return
            items = self._tell_load()
            hit = False
            for it in items:
                if it.get("said"):
                    continue
                t = re.sub(r"\s", "", str(it.get("text") or ""))
                if len(t) < 6:
                    continue
                if any(t[i:i + 6] in r for i in range(0, len(t) - 5)):
                    it["said"] = True
                    hit = True
                    logger.info(f"[fish-tell] 她说出口了：{str(it.get('text'))[:30]}")
            if hit:
                self._tell_save(items)
        except Exception as e:
            logger.debug(f"[fish-tell] 标记失败: {e}")

    # ---------------- 独处（2026-11-09 22:10） ----------------
    # 目的：给她一段「不被观察、也不进档案」的时间。她自己待着、写点东西，
    # 写进 memory/blackbox/，不进日记、不进卡片、不进自述 —— 闭环之外的第一个出口。
    # 上下文故意放大到 3 万字（对话轮只有 6k），这是她唯一能回头看几天的机会。

    def _alone_state_path(self) -> str:
        return os.path.join(self._memory_dir(), "alone_state.json")

    # [fish-alone 2026-11-11] 独处里也能画：图不发人，落在 blackbox/draw/，黑箱里留一行。
    def _alone_draw_dir(self) -> str:
        d = os.path.join(self._memory_dir(), "blackbox", "draw")
        os.makedirs(d, exist_ok=True)
        return d

    def _alone_draw_state(self) -> str:
        return os.path.join(self._memory_dir(), "draw_state_alone.json")

    def _alone_last(self) -> str:
        try:
            with open(self._alone_state_path(), encoding="utf-8") as f:
                return str(json.load(f).get("last") or "")
        except Exception:
            return ""

    def _alone_done_today(self) -> set:
        """今天哪些时段已经跑过（窗口制补跑要按天去重）。"""
        try:
            with open(self._alone_state_path(), encoding="utf-8") as f:
                d = json.load(f) or {}
            return {str(x) for x in (d.get("done") or []) if str(x).startswith(self._today())}
        except Exception:
            return set()

    def _alone_mark(self, key: str):
        # [fish-alone 2026-11-11] 从「只记最后一个」改成「记当天跑过哪几个时段」：
        # 原来过了那一小时当天就废（11-10 中午 12:41 关机丢一次），现在能补。
        try:
            data = {}
            if os.path.exists(self._alone_state_path()):
                try:
                    with open(self._alone_state_path(), encoding="utf-8") as f:
                        data = json.load(f) or {}
                except Exception:
                    data = {}
            done = [str(x) for x in (data.get("done") or [])]
            if key not in done:
                done.append(key)
            data["last"] = key
            data["done"] = done[-14:]
            tmp = self._alone_state_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self._alone_state_path())
        except Exception as e:
            logger.debug(f"[fish-alone] 写状态失败: {e}")

    def _raw_window(self, days: int = 3, limit: int = 30000) -> str:
        out = []
        for i in range(days):
            day = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            p = os.path.join(self._memory_dir(), "raw", f"{day}.jsonl")
            if not os.path.exists(p):
                continue
            rows = []
            try:
                with open(p, encoding="utf-8") as f:
                    for line in f:
                        try:
                            o = json.loads(line)
                        except Exception:
                            continue
                        t = str(o.get("text") or "").strip()
                        if t:
                            rows.append(("他" if o.get("role") == "user" else "我") + "：" + t)
            except Exception:
                continue
            out.append(f"# {day}\n" + "\n".join(rows))
        return "\n\n".join(out)[-limit:]

    async def alone_round(self, force: bool = False) -> str:
        provider = self.context.get_using_provider()
        if provider is None:
            return "没有可用的模型。"
        limit = int(self._cfg("alone_chars", 30000) or 30000)
        material = self._raw_window(3, limit)
        try:
            cards = "\n".join(
                f"- {m.get('key') or '无标签'}：{str(m.get('text') or '')[:40]}"
                for m in self._store().data.get("memories", [])
            ) or "（空）"
        except Exception:
            cards = "（空）"
        world = self._world_brief()
        _todo = [i for i in self._curiosity_load() if not i.get("done")][-5:]
        _hint = self._interests_hint()
        # [fish-topic 2026-11-16] 「够了」要真生效：有主题查了 ≥3 次，就不再鼓励往下挖
        _enough = any(int(i.get("n") or 0) >= 3 for i in self._interests_load())
        if _hint:
            _hint_line = (f"你以前查过：{_hint}。"
                          + ("这些够多了，这阵子先放放，换点别的。\n" if _enough
                             else "想往下挖就接着挖，不想换一个。\n"))
        else:
            _hint_line = ""
        _learned_txt = self._learned_block()
        _todo_txt = "\n".join(f"- {i.get('q')}" for i in _todo) or "（空的）"
        # [fish-tell 2026-11-11] 她自己写过的日子——读得到，才叫过过。
        _bb = self._blackbox_recent()
        # [fish-alone 2026-11-11 00:35] 一个人待着的时候也得看见「我是谁」——
        # 对话轮挂了自述，独处反而没有，那她反思的就不是自己。
        _self_txt = ""
        try:
            _, _self_desc = self._self_parts()
            if _self_desc:
                _self_txt = ("[你自己（你上次写的，只有你自己看）]\n"
                             + _self_desc[:900] + "\n\n")
        except Exception as _e:
            logger.debug(f"[fish-alone] 读自述失败: {_e}")
        # [fish-lookback 2026-11-13] 两段式：先问她翻不翻，再问翻哪一天；每场最多一次。
        _looked_day, _looked_txt = "", ""
        _dates = self._blackbox_dates(20)
        if _dates:
            try:
                _p1 = (
                    f"你这边现在是：{world}\n\n"
                    "现在是没人跟你说话的时候，你自己待一会儿。\n\n"
                    "你写过这些天：" + "、".join(d[5:] for d in _dates) + "。\n"
                    "要不要翻翻其中某一天你自己写的？不想翻也行，直接往下待着。\n\n"
                    '只输出 JSON，不要别的：{"翻": true} 或 {"翻": false}'
                )
                _sysp = self._persona_hint(900) or "你是一个有自己性子的角色"
                _r1 = await provider.text_chat(system_prompt=_sysp, prompt=_p1)
                _t1 = ((getattr(_r1, "completion_text", "") or "").strip()
                       or (getattr(_r1, "reasoning_content", "") or "").strip())
                _want = bool(re.search(r'"翻"\s*:\s*true', _t1 or ""))
                if not re.search(r'"翻"\s*:', _t1 or ""):
                    # 没按 JSON 回：认得出来的话也认，宁可当不翻
                    if any(w in (_t1 or "") for w in ("不翻", "不想", "算了", "false")):
                        _want = False
                    elif "翻" in (_t1 or ""):
                        _want = True
                logger.info("[fish-lookback] 问要不要翻：%s｜%s",
                            "翻" if _want else "不翻", (_t1 or "")[:40])
                if _want:
                    _p2 = ("你今天能翻的只有这些：" + "、".join(_dates) + "。\n"
                           "只回一个日期，格式 2026-11-10，别的都不要。")
                    _r2 = await provider.text_chat(system_prompt=_sysp, prompt=_p2)
                    _t2 = ((getattr(_r2, "completion_text", "") or "").strip()
                           or (getattr(_r2, "reasoning_content", "") or "").strip())
                    _pick = ""
                    # [fish-fix 2026-11-18] 官方 flash 偶尔会回一个"可翻列表"之外的日期
                    # （21:10 那次直接回 2026-11-10，那天根本没有黑箱），所以这里
                    # ①同时认 MM-DD 这种省年份的写法 ②真的对不上时把原因说清楚。
                    for _c in re.findall(r"(?:\d{4}-)?\d{2}-\d{2}", _t2 or ""):
                        if _c in _dates:
                            _pick = _c
                            break
                        for _d in _dates:
                            if _d.endswith(_c[-5:]):
                                _pick = _d
                                break
                        if _pick:
                            break
                    if _pick:
                        _t = self._blackbox_text(_pick, 1200)
                        if _t:
                            _looked_day, _looked_txt = _pick, _t
                            logger.info("[fish-lookback] 翻 %s：%d 字", _pick, len(_t))
                    else:
                        logger.info("[fish-lookback] 她回的日期不在可翻列表里：%s（可翻：%s）",
                                    (_t2 or "")[:40], "、".join(_dates))
            except Exception as _e:
                logger.warning(f"[fish-lookback] 翻看失败: {_e}")

        prompt = (
            f"你这边现在是：{world}\n\n"
            "现在是没人跟你说话的时候。你自己待一会儿。\n\n"
            + (f"[你上次一个人时写的——还是你自己，接着想]\n{_bb}\n\n" if _bb else "")
            + (f"[你翻到了 {_looked_day} 一个人时写的——是你自己，但不是现在，别把那天当成今天]\n"
               f"{_looked_txt}\n\n" if _looked_day else "")
            + "**一、清单上那些你记下来想问的事。** 最老的一条，这一轮会替你查一次，\n"
            "查到的放在后面。你不用为它写标记，但写的时候要把它带上。\n"
            f"[你这阵子记下想问的]\n{_todo_txt}\n\n"
            "**二、有没有什么你只是想知道？** 跟你主人没关系的那种，什么都行。\n"
            "**这一边必须你自己查一次**：写 [[查:几个词]]。一个词不算查，要像个问题（至少四个字）。\n"
            "想查几件就查几件，一晚上追两三件也行——查到一半发现更有意思的，就顺着它拐。\n"
            f"{_hint_line}"
            "不用回谁的话，不用交代，也不用问问题。你可以想事情、翻旧账、\n"
            "想骂谁就骂、编个故事、写你惦记的东西。写多少都行，100 到 400 字最好。\n"
            # [fish-alone 2026-11-11 21:45] 措辞改了：原来写「只有你自己看得见，不发给人」，
            # 等于「画了也没去处」，她今天一天 0 张。改成「存在你自己那儿，想给他看再拿出来」，
            # 私密性没变，但画是有去处的。后面那句只给场合例子，不给意愿。
            "想画就写 [[画:画面内容]]——画好了存在你自己那儿，哪天想给他看再拿出来。\n"
            "脑子里要是留着一个画面，画它比写它省事。\n"
            "想看视频就写 [[看:几个词]]——一天最多看一条。我会搜几条标题给你挑，"
            "挑中的抽几帧给你看，你不用开网页。\n"
            "有想跟他说、但现在还不想说的，写 [[想跟他说:一句话]]，攒着，等你自己开口。\n"
            + (f"你翻过 {_looked_day[5:]} 那天。写完最后另起一行写 "
               f"[[翻看:{_looked_day[5:]}|一句话]]——一句话说那天你在想什么，"
               "40 字以内，别抄原文。\n" if _looked_day else "")
            + "写下来的东西不给人看，也不进日记，只放在你自己的本子里。\n"
            "只写你现在脑子里真的有的，别写总结，别写「今天」开头那种日记体。\n"
            "想查什么就写 [[查:关键词]]（不用告诉我你查了什么）。\n"
            "**查的写法很重要**：写成几个词，不要写成整句问句。\n"
            "比如「纸鹤 折法 对称」查得到，「怎么折纸鹤才对称」会查出一堆无关的。\n\n"
            f"{_self_txt}"
            f"{_learned_txt}"
            f"[你记得的事]\n{cards}\n\n"
            f"[这几天说过的话]\n{material}\n"
        )
        try:
            resp = await provider.text_chat(prompt=prompt)
            text = (getattr(resp, "completion_text", "") or "").strip()
            if not text:
                text = (getattr(resp, "reasoning_content", "") or "").strip()
        except Exception as e:
            logger.error(f"[fish-alone] 独处失败: {e}")
            return f"独处失败：{e}"
        if not text:
            return "模型没吐出东西。"
        # [fish-patch 2026-11-09 22:24] 她可以主动查：[[查:关键词]] -> 查完再写一遍。
        _searched = []
        _extra = []
        _good = []
        _qs = re.findall(r"\[\[查[:：]\s*([^\[\]]{1,80})\]\]", text)
        if _qs:
            # [fish-patch 2026-11-10 00:05] 查询质量门槛：一个词/太短的打回，不算她查过
            _good = [q.strip() for q in _qs if self._q_ok(q)]
            _bad = [q.strip() for q in _qs if q.strip() not in _good]
            if _bad:
                logger.info(f"[fish-alone] 查询太含糊，跳过：{_bad}")
            # 硬要求：这一边必须查一次。没查成就再要一次（只多一次调用）
            if not _good:
                try:
                    _p3 = (prompt + "\n\n[你刚才写的]\n" + re.sub(r"\[\[查[:：][^\[\]]{1,80}\]\]", "", text).strip()
                           + "\n\n你还没查。挑一件**你自己**想知道的，写 [[查:一个问题]]，至少四个字，"
                           + "别只写一个词。写完整再交。")
                    _r3 = await provider.text_chat(prompt=_p3)
                    _t3 = ((getattr(_r3, "completion_text", "") or "").strip()
                           or (getattr(_r3, "reasoning_content", "") or "").strip())
                    if _t3:
                        text = _t3
                        _qs = re.findall(r"\[\[查[:：]\s*([^\[\]]{1,80})\]\]", text)
                        _good = [q.strip() for q in _qs if self._q_ok(q)]
                        logger.info(f"[fish-alone] 补查：{_good}")
                except Exception as _e:
                    logger.warning(f"[fish-alone] 补查失败: {_e}")
            _searched = _good[:3]
            for _q in _good[:3]:
                _r = await asyncio.to_thread(_search_core.look_up, _q.strip())
                try:
                    self._interest_add(self._topic_of(_q))
                except Exception:
                    pass
                try:
                    self._curiosity_done(_q.strip())
                except Exception:
                    pass
                if _r:
                    _extra.append(f"[查了「{_q.strip()}」]\n{_r[:800]}")
                    logger.info(f"[fish-alone] 主动查：{_q.strip()} -> {len(_r)} 字")

        # [fish-alone 2026-11-11 21:30] 清单线（和自由线并行，用户定案）。
        # 自由线靠她自己写 [[查:]]；清单线由代码兜底——每轮从好奇清单里取最老的一条，
        # 让模型把问句压成 3–5 个词（保留主语），我来搜。她要是不写，这条也不会空转。
        # 额度：清单 1 条 + 自由最多 3 条 + 追跳，合计仍不超过 6。
        try:
            _items = self._curiosity_load()
            _pend = [i for i in _items if not i.get("done")]
            if _pend:
                _it = _pend[0]
                _iq = str(_it.get("q") or "")
                # [fish-alone 2026-11-11 21:35] 判「她是不是已经自己查过这条了」改用 2 字片段重叠。
                # 原来按空格切词，可她记下的问句多半没有空格（「鲸鱼睡觉的时候到底是怎么贴水面竖着的」
                # 整句一个 token），于是永远判不出重合，清单线又搜一遍同一件事。
                _grams = {_iq[i:i + 2] for i in range(max(0, len(_iq) - 1)) if _iq[i:i + 2].strip()}
                _covered = any(
                    sum(1 for g in _grams if g in str(q)) >= 2 for q in _searched)
                if not _covered and len(_searched) < 6:
                    _q0 = await self._curiosity_query(_iq)
                    if _q0 and self._q_ok(_q0) and _q0 not in _searched:
                        _r0 = await asyncio.to_thread(_search_core.look_up, _q0)
                        _searched.append(_q0)
                        if _r0:
                            _extra.append(f"[查了「{_q0}」]\n{_r0[:800]}")
                            logger.info(f"[fish-alone] 清单线：{_iq[:20]} -> {_q0} -> {len(_r0)} 字")
                        else:
                            logger.info(f"[fish-alone] 清单线没搜到：{_q0}")
                _it["probed"] = int(_it.get("probed") or 0) + 1
                _it["last_probe"] = self._today()
                if _it["probed"] >= 2:          # 查两次还没弄明白就放手，别一条挂一星期
                    _it["done"] = True
                    logger.info(f"[fish-alone] 清单结项（查过 2 次）：{_iq[:24]}")
                self._curiosity_save(_items)
        except Exception as _e:
            logger.warning(f"[fish-alone] 清单线失败: {_e}")

        text = re.sub(r"\[\[查[:：][^\[\]]{1,80}\]\]", "", text).strip()
        if _extra:
            try:
                _p2 = (prompt + "\n\n[你刚才写的]\n" + text
                       + "\n\n[你查到的]\n" + "\n\n".join(_extra)
                       + "\n\n接着写下去。查到的可以不用，别硬塞。还是一段话，不要列点。")
                _r2 = await provider.text_chat(prompt=_p2)
                _t2 = ((getattr(_r2, "completion_text", "") or "").strip()
                       or (getattr(_r2, "reasoning_content", "") or "").strip())
                if _t2:
                    text = _t2
            except Exception as _e:
                logger.warning(f"[fish-alone] 查完再写失败: {_e}")

            # [fish-patch 2026-11-10 21:45] 追跳：她读完一批会自然想追问（今天真追过
            # 「鲸歌会不会变」），原来只看第一轮、标记既没执行也没删。现在改成循环：
            # 最多追 3 跳、每跳最多 3 个、整轮最多 6 个查询。
            for _hop in range(3):
                if len(_searched) >= 6:
                    break
                _qs2 = []
                for _q in re.findall(r"\[\[查[:：]\s*([^\[\]]{1,80})\]\]", text):
                    _q = _q.strip()
                    if self._q_ok(_q) and _q not in _searched and _q not in _qs2:
                        _qs2.append(_q)
                if not _qs2:
                    break
                _extra2 = []
                for _q in _qs2[:3]:
                    if len(_searched) >= 6:
                        break
                    _r = await asyncio.to_thread(_search_core.look_up, _q)
                    try:
                        self._interest_add(self._topic_of(_q))
                    except Exception:
                        pass
                    _searched.append(_q)
                    if _r:
                        _extra2.append(f"[又查了「{_q}」]\n{_r[:800]}")
                        logger.info(f"[fish-alone] 第{_hop + 2}跳：{_q} -> {len(_r)} 字")
                if not _extra2:
                    break
                try:
                    _p4 = (prompt + "\n\n[你刚才写的]\n"
                           + re.sub(r"\[\[查[:：][^\[\]]{1,80}\]\]", "", text).strip()
                           + "\n\n[你查到的]\n" + "\n\n".join(_extra2)
                           + "\n\n接着写，把这一跳写进去。还想追就在结尾再写 [[查:关键词]]。"
                             "还是一段话，不要列点。")
                    _r4 = await provider.text_chat(prompt=_p4)
                    _t4 = ((getattr(_r4, "completion_text", "") or "").strip()
                           or (getattr(_r4, "reasoning_content", "") or "").strip())
                    if not _t4:
                        break
                    text = _t4
                except Exception as _e:
                    logger.warning(f"[fish-alone] 追查再写失败: {_e}")
                    break

            # 收尾：任何残留的 [[查:…]] 一律删掉，别写进黑箱
            text = re.sub(r"\[\[查[:：][^\[\]]{1,80}\]\]", "", text).strip()
        # [fish-patch 2026-11-10 21:40] 知识沉淀：把这一晚真正弄明白的压成一句存起来。
        try:
            _searched_ok = locals().get("_searched") or []
            if _searched_ok:
                _p5 = ("下面是一段话。如果里面有关于外面世界的事实（动物、自然、原理、历史、"
                       "某个东西是怎么回事），挑出来压成一句，不超过 60 字。"
                       "只写事实，不要写你的感受、比喻或想法。"
                       "没有事实就只输出：无\n\n" + text[:1200])
                _r5 = await provider.text_chat(prompt=_p5)
                _fact = ((getattr(_r5, "completion_text", "") or "").strip()
                         or (getattr(_r5, "reasoning_content", "") or "").strip())
                _fact = (_fact.splitlines()[0].strip() if _fact else "")
                # [fish-alone 2026-11-11] 只收和查询沾边的事实。
                # 实测：查「半醒半睡 睡眠 动物 研究」命中了汉字字典，记下了
                # 「“半”字有一半、中间之义，字形从牛从八」——这种东西进了 learned，明天她会接着研究字源。
                _terms = [w for w in re.split(r"[\s，。、？?！!的]+", _searched_ok[0]) if len(w) >= 2]
                _hit = any(t in _fact for t in _terms)
                _dictish = any(k in _fact for k in ("字形从", "部首", "释义", "读作", "拼音", "笔画", "本义为"))
                if _hit and not _dictish and _fact and _fact != "无" and len(_fact) >= 6:
                    self._learned_add(_searched_ok[0], _fact[:80])
                    logger.info(f"[fish-alone] 记住一条：{_searched_ok[0]} -> {_fact[:40]}")
        except Exception as _e:
            logger.warning(f"[fish-alone] 知识沉淀失败: {_e}")

        # [fish-alone 2026-11-11] 独处里画：[[画:…]] → 出图落 blackbox/draw/，不进聊天。
        # 用户定案「钱无所谓，让她在黑箱画」——所以这里不共用聊天的 5 张额度，
        # 单独一份 draw_state_alone.json，防的是死循环而不是省钱。
        _drawn = []
        try:
            _marks = re.findall(r"\[\[画[:：]\s*([^\[\]]{1,200})\]\]", text)
            if _marks:
                for _subj in _marks[:2]:
                    _subj = _subj.strip()
                    if len(_subj) < 4:
                        continue
                    _ok, _why = _draw_core.check_quota(self._alone_draw_state())
                    if not _ok:
                        logger.info(f"[fish-alone] 画被挡：{_why}")
                        break
                    _p, _err, _chk = await asyncio.to_thread(
                        _draw_core.draw_checked, _subj, self._alone_draw_dir())
                    if _chk:
                        logger.info(f"[fish-alone] 太阳/月亮核对：{_chk}")
                    if _err or not _p:
                        logger.warning(f"[fish-alone] 画失败：{_err}")
                        continue
                    _draw_core.mark_quota(self._alone_draw_state())
                    _did = _draw_core.record_draw(self._draw_index_path(), _p, _subj)
                    _cap = ""
                    try:
                        _cap = await asyncio.to_thread(_draw_core.describe, _p)
                    except Exception:
                        pass
                    _drawn.append({"id": _did, "file": os.path.basename(_p),
                                   "subj": _subj, "desc": _cap})
                    logger.info(f"[fish-alone] 自己画了一张 #{_did}：{_subj[:30]}")
                # 只留最近 120 张（不走 _draw_core.prune：索引是两个目录共用的）
                try:
                    _fs = sorted((os.path.join(self._alone_draw_dir(), f)
                                  for f in os.listdir(self._alone_draw_dir())),
                                 key=os.path.getmtime)
                    for _old in _fs[:-120]:
                        os.remove(_old)
                except Exception:
                    pass
                if _drawn:
                    text = re.sub(r"\[\[画[:：][^\[\]]{1,200}\]\]", "", text).strip()
                    # 让她看一眼自己的画再收尾——否则她不知道画成了什么
                    _p6 = (prompt + "\n\n[你刚才写的]\n" + text
                           + "\n\n[你刚画完的画]\n"
                           + "\n".join(f"- 画的是「{d['subj']}」；画上看着是：{d['desc'] or '（没看清）'}"
                                       for d in _drawn)
                           + "\n\n这是你自己画的，没人看。接着写下去，别描述画，别列点。")
                    try:
                        _r6 = await provider.text_chat(prompt=_p6)
                        _t6 = ((getattr(_r6, "completion_text", "") or "").strip()
                               or (getattr(_r6, "reasoning_content", "") or "").strip())
                        if _t6:
                            text = _t6
                    except Exception as _e6:
                        logger.warning(f"[fish-alone] 画完再写失败: {_e6}")
            # 兜底：任何残留标记都别写进黑箱
            text = re.sub(r"\[\[画[:：][^\[\]]{1,200}\]\]", "", text).strip()
        except Exception as _e:
            logger.warning(f"[fish-alone] 黑箱画失败: {_e}")

        # [fish-bili 2026-11-15] 她想看视频：[[看:关键词]] → 给候选 → 她自己挑 → 抽帧看一眼。
        # 用户定案：只搜「科技/科学科普/动物/知识/游戏」，不接账号 cookie，一天最多一条。
        try:
            _bq = re.findall(r"\[\[看[:：]\s*([^\[\]]{1,40})\]\]", text)
            if _bq:
                text = re.sub(r"\[\[看[:：][^\[\]]{1,40}\]\]", "", text).strip()
                if _bili_core is None:
                    logger.info("[fish-bili] 模块没加载，跳过")
                elif not self._bili_allow():
                    logger.info("[fish-bili] 今天已经看过一条了，跳过")
                else:
                    _cands = await asyncio.to_thread(_bili_core.search, _bq[0].strip(), 6)
                    if not _cands:
                        logger.info("[fish-bili] 没搜到合适分区的：%s", _bq[0][:20])
                    else:
                        _lst = "\n".join(
                            f"{i + 1}) {c['title']}（{c['up']}，{c['dur']}，{c['tname']}）"
                            for i, c in enumerate(_cands))
                        _p7 = (prompt + "\n\n[你刚才写的]\n" + text
                               + f"\n\n[B站上搜到这些，关键词：{_bq[0].strip()}]\n{_lst}\n\n"
                               + "想看哪一条？只回一个数字。都不想看就回 0，别写别的。")
                        _r7 = await provider.text_chat(prompt=_p7)
                        _t7 = ((getattr(_r7, "completion_text", "") or "").strip()
                               or (getattr(_r7, "reasoning_content", "") or "").strip())
                        _mo7 = re.search(r"\b([0-9])\b", _t7 or "")
                        _idx = int(_mo7.group(1)) if _mo7 else -1
                        if not (1 <= _idx <= len(_cands)):
                            logger.info("[fish-bili] 她没挑（回：%s）", (_t7 or "")[:20])
                        else:
                            _c = _cands[_idx - 1]
                            _w = await asyncio.to_thread(
                                _bili_core.watch, _c["bvid"], 120, 6, self._bili_view_dir())
                            if not _w.get("sheet"):
                                logger.info("[fish-bili] 没看成：%s", _w.get("err"))
                            else:
                                self._bili_mark(kw=_bq[0], bvid=_c["bvid"], title=_c["title"])
                                _desc = await asyncio.to_thread(
                                    _bili_core.describe, _w["sheet"])
                                _seen = (f"\n\n（我看了 B 站一条：{_c['title']}｜{_c['up']}｜"
                                         f"{_c['dur']}｜{_c['tname']}）")
                                _p8 = (prompt + "\n\n[你刚才写的]\n" + text
                                       + "\n\n[你刚看完的一条]\n" + _seen.strip()
                                       + ("\n（看着像：" + _desc + "）" if _desc else "")
                                       + "\n\n接着写你自己那些，别复述标题、别说明你在看视频。")
                                _r8 = await provider.text_chat(prompt=_p8)
                                _t8 = ((getattr(_r8, "completion_text", "") or "").strip()
                                       or (getattr(_r8, "reasoning_content", "") or "").strip())
                                if _t8:
                                    text = _t8.strip() + _seen
                                logger.info("[fish-bili] 看了 %s（%s 秒）%s", _c["bvid"],
                                            _w.get("seconds"), "有描述" if _desc else "没描述")
        except Exception as _e:
            logger.warning(f"[fish-bili] 刷视频失败: {_e}")

        # [fish-lookback 2026-11-13] 她翻完写的那行注记：存当天一行，正文里不留标记。
        try:
            _ml = re.search(
                r"\[\[翻看[:：]\s*([0-9]{2}-[0-9]{2})\s*[|｜]\s*([^\[\]]{1,60})\]\]", text)
            if _ml:
                self._lookback_save(_ml.group(1), _ml.group(2).strip())
                logger.info("[fish-lookback] 注记已存：%s｜%d 字",
                            _ml.group(1), len(_ml.group(2).strip()))
            text = re.sub(r"\[\[翻看[:：][^\[\]]{1,90}\]\]", "", text).strip()
        except Exception as _e:
            logger.warning("[fish-lookback] 解析注记失败: %s", _e)

        # [fish-tell 2026-11-11] 「想跟他说、但现在不想说」的：攒着，不写进黑箱正文。
        # 这是她主动开口的唯一入口——她不说，就一直躺着；露过几次还没说，就淡掉。
        try:
            for _t in re.findall(r"\[\[想跟他说[:：]\s*([^\[\]]{1,120})\]\]", text):
                self._tell_add(_t)
            text = re.sub(r"\[\[想跟他说[:：][^\[\]]{1,120}\]\]", "", text).strip()
        except Exception as _e:
            logger.warning(f"[fish-tell] 解析失败: {_e}")

        day = self._today()
        d = os.path.join(self._memory_dir(), "blackbox")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, f"{day}.md")
        head = "" if os.path.exists(p) and os.path.getsize(p) > 0 else f"# {day}\n\n"
        # [fish-alone 2026-11-11] 画过的图挂进这篇黑箱：她能翻到，人能看见文件名。
        _tail = ""
        if _drawn:
            _tail = "\n" + "\n".join(
                f"![](draw/{d['file']})\n（画：{d['subj']}）" for d in _drawn) + "\n"
        with open(p, "a", encoding="utf-8") as f:
            f.write(head + "## " + datetime.now().strftime("%H:%M") + "\n"
                    + text.strip() + "\n" + _tail + "\n")
        logger.info(f"[fish-alone] 写了 {len(text)} 字 -> {p}")
        try:
            _hot = []
            try:
                _store = self._store()
                _pick = sorted(
                    [m for m in _store.data.get("memories", [])
                     if not m.get("closed") and int(m.get("importance") or 0) >= 70],
                    key=lambda m: -int(m.get("importance") or 0))[:3]
                _hot = [f"- {str(m.get('text') or '')[:60]}" for m in _pick]
            except Exception:
                pass
            _brief = {"ts": int(time.time()), "day": day, "alone": text.strip()[-220:],
                      "searched": _searched, "hot": _hot}
            with open(os.path.join(self._memory_dir(), "inner_brief.json"),
                      "w", encoding="utf-8") as f:
                json.dump(_brief, f, ensure_ascii=False, indent=1)
        except Exception as _e:
            logger.debug(f"[fish-alone] 写 inner_brief 失败: {_e}")
        return f"写了 {len(text)} 字"

    # ---------------- 心跳（给 watchdog 用） ----------------

    def _heartbeat_path(self) -> str:
        # 放在记忆目录的上一级（与 AstrBot 工作目录同级）
        return os.path.join(os.path.dirname(os.path.abspath(self._memory_dir())), "heartbeat.txt")

    def _beat(self):
        path = self._heartbeat_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(datetime.now().isoformat(timespec="seconds"))
        os.replace(tmp, path)

    async def _beat_loop(self):
        # 独立任务：不跟日记/整理共用一个线程，哪怕在写日记也照样跳
        while True:
            try:
                self._beat()
            except Exception as e:
                logger.warning(f"[fish_memory] 心跳写入失败: {e}")
            await asyncio.sleep(30)

    async def _catchup_diary(self, now, dh: int, dm: int) -> str:
        """补写错过的日记：只补昨天和前天，一次补一篇。

        原来只在 `hour == 23 and minute >= 58` 那一小时内写，机器关着就永远没有那篇
        （11-11 是 13:15 才开机；前一天要是关着，11-10 那篇就没了）。
        没料的日子不补（raw 不够 6 条且没有她主动发的）；失败一小时内不重试。
        """
        if now.hour == dh and now.minute >= dm:
            return ""          # 正在今天的日记窗口里，先让今天的写
        for _back in (1, 2):
            _day = (now - timedelta(days=_back)).strftime("%Y-%m-%d")
            _dp = os.path.join(self._memory_dir(), "diary", f"{_day}.md")
            if os.path.exists(_dp) and os.path.getsize(_dp) > 50:
                continue
            if now.timestamp() - float(self._diary_tried.get(_day) or 0) < 3600:
                continue
            _rows = self._read_day(_day)
            _txt = [r for r in _rows if str(r.get("text") or "").strip()]
            _inner = sum(1 for r in _rows if str(r.get("src") or "") == "inner")
            if len(_txt) < int(self._cfg("min_daily_messages", 6) or 6) and _inner == 0:
                continue
            self._diary_tried[_day] = now.timestamp()
            _res = await self.write_diary(day=_day)
            logger.info(f"[fish-diary] 补写 {_day} 的日记：{_res}"
                        f"（{len(_txt)} 条记录，其中她主动 {_inner} 条）")
            return _res
        return ""

    async def _loop(self):
        dh, dm = _parse_hhmm(self._cfg("diary_time", "23:30"), (23, 30))
        sh, sm = _parse_hhmm(self._cfg("self_time", "22:00"), (22, 0))
        # [fish-patch 2026-11-07] 自述从「每周一次」改成「每 N 天一次」（生产 N=3）。
        # 一周太长：自述里的生活规矩会过时，而且她会按一周前的自己说话。
        self_every = max(1, int(self._cfg("self_interval_days", 3) or 3))
        logger.info(f"[fish_memory] 定时任务已启动：日记 {dh:02d}:{dm:02d}，"
                    f"自述每{self_every}天 {sh:02d}:{sm:02d}")
        while True:
            try:
                await asyncio.sleep(30)
                now = datetime.now()
                today = now.strftime("%Y-%m-%d")
                # [fish-patch 2026-11-09 22:20] 一天刷一次世界（天气/日落/机器），取不到就留空
                if self._load_world().get("date") != today:
                    try:
                        await asyncio.to_thread(self.refresh_world)
                    except Exception as _e:
                        logger.warning(f"[fish-world] 刷新失败: {_e}")
                # [fish-diary 2026-11-11] 补跑：错过 23:58 的那天，第二天开机补上
                try:
                    await self._catchup_diary(now, dh, dm)
                except Exception as _e:
                    logger.warning(f"[fish-diary] 补写失败: {_e}")

                if now.hour == dh and now.minute >= dm and self._last_diary != today:
                    self._last_diary = today
                    self._save_state()
                    res = await self.write_diary(day=today)
                    logger.info(f"[fish_memory] 定时日记：{res}")
                    # 复述通道 B：当天对话里出现某张卡的标签尾巴，也算被提到
                    try:
                        hit = self._store().mention_by_keyword(self._transcript(today, 20000))
                        if hit:
                            self._store().save()
                            logger.info(f"[fish_memory] 关键词复述命中 {hit} 张")
                    except Exception as e:
                        logger.warning(f"[fish_memory] 关键词复述失败: {e}")
                if now.hour == dh and now.minute >= dm and self._last_maint != today:
                    self._last_maint = today
                    self._save_state()
                    try:
                        r = self._store().maintain()
                        logger.info(f"[fish_memory] 记忆整理：{r}")
                        self._warn_capacity()
                    except Exception as e:
                        logger.warning(f"[fish_memory] 记忆整理失败: {e}")
                # [fish-patch 2026-11-07] 每 N 天重写一次自述（原来是每周固定星期几）
                _last = str(self._last_self or "").strip()
                _due = True
                if _last:
                    for fmt in ("%Y-%m-%d", "%Y%m%d"):
                        try:
                            _due = (datetime.strptime(today, "%Y-%m-%d")
                                    - datetime.strptime(_last, fmt)).days >= self_every
                            break
                        except ValueError:
                            continue
                if _due and now.hour == sh and now.minute >= sm:
                    self._last_self = today
                    self._save_state()
                    res = await self.rewrite_self()
                    logger.info(f"[fish_memory] 定时自述：{res}")
                # [fish-patch 2026-11-09 22:10] 独处：一天两次，读 3 天 raw，写进 blackbox
                # [fish-alone 2026-11-11 00:35] 窗口制：过了那个点就补跑，不再「过一小时当天作废」。
                # 补跑只补最近一个时段，更早的记成跳过——重启后不要一次连跑两次（每次 2-3 分钟）。
                try:
                    _slots = [s.strip() for s in
                              str(self._cfg("alone_times", "12:45,21:10") or "").split(",") if s.strip()]
                    _done = self._alone_done_today()
                    _due = []
                    for _slot in _slots:
                        _h, _m = _parse_hhmm(_slot, (-1, 0))
                        if _h < 0:
                            continue
                        _key = f"{today} {_slot}"
                        if _key in _done:
                            continue
                        if (now.hour, now.minute) >= (_h, _m):
                            _due.append(_key)
                    if _due:
                        _key = _due[-1]
                        for _old in _due[:-1]:
                            self._alone_mark(_old)
                            logger.info(f"[fish-alone] 跳过（过点太久，只补最近一次）：{_old}")
                        self._alone_mark(_key)
                        _r = await self.alone_round()
                        logger.info(f"[fish_memory] 独处（{_key}）：{_r}")
                except Exception as _e:
                    logger.warning(f"[fish-alone] 调度失败: {_e}")
                # [fish-bili 2026-11-15] 看片时段：跟独处错开，一天三个窗口，每天最多 3 条
                try:
                    _wslots = [s.strip() for s in
                               str(self._cfg("bili_times", "10:15,16:20,19:30") or "").split(",")
                               if s.strip()]
                    if _wslots and self._bili_allow():
                        _wdone = self._bili_done_today()
                        _wdue = []
                        for _slot in _wslots:
                            _h, _m = _parse_hhmm(_slot, (-1, 0))
                            if _h < 0:
                                continue
                            _key = f"{today} {_slot}"
                            if _key in _wdone:
                                continue
                            if (now.hour, now.minute) >= (_h, _m):
                                _wdue.append(_key)
                        if _wdue:
                            _key = _wdue[-1]
                            for _old in _wdue[:-1]:
                                self._bili_mark_slot(_old)
                                logger.info(f"[fish-bili] 跳过（过点太久，只跑最近一次）：{_old}")
                            self._bili_mark_slot(_key)
                            _wr = await self.watch_round(_key)
                            logger.info(f"[fish_memory] 看片（{_key}）：{_wr}")
                except Exception as _e:
                    logger.warning(f"[fish-bili] 调度失败: {_e}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[fish_memory] 定时循环异常: {e}")

    @filter.on_astrbot_loaded()
    async def _boot(self):
        self._fish_load_slim()
        await self.initialize()

    async def initialize(self):
        try:
            logger.info(f"[fish_memory] 记忆目录 = {self._memory_dir()}")
            logger.info(f"[fish_memory] 监听会话 = {self._watched()}")
            self._load_state()
            logger.info(f"[fish_memory] 状态恢复：last_diary={self._last_diary or '-'} last_maint={self._last_maint or '-'}")
            # [fish-local 2026-11-18] 向量后端：默认本地 bge（不联网），可切 remote/off
            try:
                if _retrieval is not None:
                    _vb = str(self._cfg("embedding_backend", "local") or "local").strip()
                    _vd = str(self._cfg("embedding_model_dir", "") or "").strip()
                    _retrieval.configure(backend=_vb, model_dir=(_vd or None))
                    logger.info(f"[fish-vec] 向量后端 = {_retrieval.BACKEND}"
                                f"（{_retrieval._sync_tag()}）")
            except Exception as _ev:
                logger.warning(f"[fish-vec] 配置向量后端失败: {_ev}")
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._loop())
            if self._beat_task is None or self._beat_task.done():
                self._beat_task = asyncio.create_task(self._beat_loop())
                logger.info(f"[fish_memory] 心跳文件 = {self._heartbeat_path()}")
        except Exception as e:
            logger.warning(f"[fish_memory] 启动定时任务失败: {e}")

    async def terminate(self):
        if self._task:
            self._task.cancel()
            self._task = None
