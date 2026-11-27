# astrbot_plugin_inner_voice

主动开口插件：决定「她什么时候想说话、说什么、分几条发」。

它是 `astrbot_plugin_fish_memory` 的搭档——记忆插件负责「她记得什么」，这个插件负责「她什么时候开口」，
两者只通过记忆目录（`memory/`）与人格 ID 耦合，可以单独安装。

## 它做什么

- 按固定间隔做一次**是否开口**的判断，条件不满足就静默跳过（不打扰、不空转）
- 判断通过后，先生成一句「开口理由」，再据此写消息正文
- 正文按句末标点与换行切成多条，逐条发出；超长截断
- 每天有配额、有最小间隔、有安静时段，避免变成刷屏机器人
- 支持「她今天在独处里弄明白了什么」这类内部素材参与判断（读 `memory/inner_brief.json`）

## 配置项（见 `_conf_schema.json`）

| 键 | 作用 |
|---|---|
| `persona_id` | AstrBot 人格 ID（默认 `default`） |
| `umo` | 目标会话 ID，形如 `qq-onebot:FriendMessage:<号码>` |
| `interval_minutes` | 多久判断一次 |
| `min_gap_minutes` | 两次开口的最小间隔 |
| `daily_cap` | 每天最多主动说几条 |
| `quiet_hours` | 安静时段，形如 `1-7` |
| `max_text_chars` | 单条最长字数，超出截断 |

样例见仓库根目录 `examples/astrbot_plugin_inner_voice.example.json`。

## 依赖

- AstrBot 4.x
- 一个可用的对话模型 provider
- 记忆目录里可选的 `inner_brief.json` / `interests.json`（由记忆插件写入；没有也能跑）

## 发消息的注意点

反向 WebSocket 接入下，主动发消息**必须能定位到那条连接**，否则会抛 `ApiNotAvailable`（异常字符串还是空的）。
代码里对此做了取 `self_id` 的处理（见 `main.py` 的 `_bot_send`）——如果你换接入方式，这里是第一个要改的地方。

## 与分条的关系

AstrBot 自带的 `segmented_reply` 与本插件的分条会叠加，造成双重分条。二者只能留一个：
本项目选择了**关掉框架那层、由插件自己发**，所以在 AstrBot 配置里把
`platform_settings.segmented_reply.enable` 设为 `false`。
