# patches — 对 AstrBot 本体的改动

本项目跑在 **AstrBot 4.x**（开发版本 4.29.3）之上。下面三处改动在插件里做不了，只能落在框架侧；
不应用它们，插件仍能启动，但会缺功能（见每条“不应用会怎样”）。

> ⚠️ AstrBot 采用 **AGPL-3.0-or-later**。这里发布的是**改动说明与代码片段**，不含上游源码。
> 若你分发打过补丁的 AstrBot，属于分发衍生作品，须遵守 AGPL-3.0。

> 为什么给片段而不是 `.patch` 文件：本仓库不附带上游源码，无法生成可信的统一 diff。
> 上游版本之间行号会漂移，请按下文的**位置描述 + 代码块**对照你本地的文件手工应用。

---

## 1. `core/provider/sources/openai_source.py` — 用量计量

**目的**：把每一次真实请求的 token 用量落成本地账本，按用途归因（聊天 / 独处 / 日记 / 视觉 / 主动判断）。
**为什么在 provider 层**：聊天轮有插件 hook，但独处、日记、看片描述、主动判断都是直接 `text_chat`，钩子看不见；只有这里能看到全部请求。
**不应用会怎样**：`tools/usage_report.py` 无数据可读。

### 1a. 新增方法（放在类内任意位置，建议紧邻其它私有方法）

```python
def _fish_meter_usage(self, payloads: dict, resp) -> None:
    """每次真实请求记一条 usage。只落数字，不落 prompt 正文；异常一律吞掉。"""
    try:
        import os as _os
        import time as _t

        u = getattr(resp, "usage", None)
        if u is None:
            return
        # 用途归因：靠 system prompt 的特征串判断。
        # ⚠️ 下面的特征串取自本项目的提示词，换一套提示词时必须同步修改，否则一律归为 chat。
        sys_txt = ""
        has_img = False
        for m in (payloads.get("messages") or [])[:3]:
            c = m.get("content")
            if isinstance(c, str):
                sys_txt += c[:4000]
            elif isinstance(c, list):
                for p in c:
                    if isinstance(p, dict):
                        if p.get("type") == "image_url":
                            has_img = True
                        elif p.get("type") == "text":
                            sys_txt += str(p.get("text") or "")[:2000]
        if "你是一个有自己性子" in sys_txt:
            tag = "inner"
        elif "现在是没人跟你说话的时候" in sys_txt:
            tag = "alone"
        elif has_img:
            tag = "vision"
        elif "日记" in sys_txt:
            tag = "diary"
        else:
            tag = "chat"

        rec = {
            "ts": int(_t.time()),
            "model": str(payloads.get("model") or ""),
            "in_cached": int(getattr(u, "input_cached", 0) or 0),
            "in_other": int(getattr(u, "input_other", 0) or 0),
            "out": int(getattr(u, "output", 0) or 0),
            "tag": tag,
        }
        d = _os.environ.get("FISH_USAGE_DIR") or "memory/usage"
        _os.makedirs(d, exist_ok=True)
        fn = _os.path.join(d, _t.strftime("%Y-%m-%d", _t.localtime()) + ".jsonl")
        with open(fn, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as _e:
        logger.debug("[fish-meter] 记账失败: %s", _e)
```

### 1b. 调用点

在**非流式** `_query` 拿到响应之后、返回之前插一行（本项目插在构造 `LLMResponse` 附近）：

```python
self._fish_meter_usage(payloads, llm_response)
```

> 只覆盖非流式路径。若你开启 `streaming_response`，需在流式收尾处另加一处。

### 1c.（可选）请求体落盘，用于排查

```python
def _fish_dump_payload(self, payloads: dict) -> None:
    """把每次请求体写到 memory/../_work/payloads/last_payload.json，仅用于本地排查。"""
    try:
        import os as _os
        base = _os.environ.get("FISH_PAYLOAD_DIR")
        if not base:
            return
        _os.makedirs(base, exist_ok=True)
        p = _os.path.join(base, "last_payload.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(payloads, f, ensure_ascii=False)
        logger.warning("[fish-dump] 已落盘 %s（messages=%d）", p, len(payloads.get("messages") or []))
    except Exception as _e:
        logger.warning("[fish-dump] 失败: %s", _e)
```

调用点插在发请求之前：`self._fish_dump_payload(payloads)`。
**注意**：它会把完整请求体（含对话原文）写到磁盘，默认关闭（未设 `FISH_PAYLOAD_DIR` 时直接返回）。

---

## 2. `core/pipeline/waking_check/stage.py` — 群聊门槛

**目的**：私聊全放行；群聊必须 **@机器人** 或 **提到角色名** 才唤醒，其余群消息不进模型。
**不应用会怎样**：群里每句话她都会接，且会污染记忆。

在原唤醒判断里加一段（本项目在 `if not ...:` 之前）：

```python
# 私聊全放行；群聊必须有 @机器人，或叫得出名字。
_names = ("__CHARACTER_NAME__",)   # ← 换成你自己的角色名（可多个）
_named = any(n in message_str for n in _names)
```

然后把它并入既有的唤醒条件：`if is_group and not (at_bot or _named): return`（按你本地的结构改写）。

> 说明：`__CHARACTER_NAME__` 是占位符，必须替换；留空字符串会匹配一切消息。

---

## 3. `core/pipeline/respond/stage.py` — 长句不按标点切

**目的**：分段发送时，**较短的单行保持整行**，只有长行才按句末标点再切——否则人设要求的长句会被切碎。
**不应用会怎样**：长句被强行按 `。！？` 拆开，语感变碎。

把阈值从默认值抬高（本项目用 40）：

```python
_threshold = getattr(self, "words_count_threshold", 8)
if len(parts) == 1 and len(parts[0]) >= max(_threshold, 40):
    parts = [p.strip() for p in re.split(r"(?<=[。！？!?…~])", parts[0]) if p.strip()]
```

> 40 是经验值：超过它的单行才值得再切；可按你自己的输出长度调整。

---

## 建议的应用顺序

1. 先打 **2**（群聊门槛）——不做这步，群聊会污染记忆，后面很难回滚。
2. 再打 **3**（分段）——纯显示层，出错也好回滚。
3. 最后打 **1**（计量）——纯旁路，异常全部被吞掉，不影响回复。

改完记得重启 AstrBot；本项目重启后日志能看到 `[fish-meter]` / `[fish-dump]` 生效痕迹。
