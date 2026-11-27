# 蓝汐

一个长期住在 QQ 里的角色系统的**后端实现**：不是聊天机器人 demo，而是一套让一个虚设人格「持续活着」的工程结构。

> 这个仓库只含插件与工具代码，**不含**角色人设、记忆数据、聊天记录、配置与凭据。
> 设计层面的取舍见 [`docs/蓝汐qq项目.md`](docs/蓝汐qq项目.md)（脱敏版，讲判断不讲做法）。

---

## 这个仓库里有什么

| 目录 | 内容 |
|---|---|
| `astrbot_plugin_fish_memory/` | 记忆主插件：日记、自述、卡片池、检索注入、独处、看片、画画、写信、群聊隔离 |
| `astrbot_plugin_inner_voice/` | 主动开口插件：什么时候她想说话、说什么、分几条发 |
| `tools/` | 出图命令行、用量报表、两个只数词的指标脚本 |
| `patches/` | 对 AstrBot 本体的 4 处改动（用量计量、群聊唤醒与门槛、时间提示） |
| `examples/` | 配置样例与启动脚本模板 |
| `docs/` | 脱敏设计文档：`蓝汐qq项目.md`（总览，md + pdf）、`记忆系统.md`（记忆分层、遗忘与一致性） |

两个插件都是独立进程内运行的 AstrBot 插件，彼此只通过「记忆目录」和「人格 ID」耦合，可以单独安装。

---

## 运行前提

- **AstrBot 4.x**（本项目在 4.29.3 上开发）。注意 AstrBot 是 **AGPL-3.0-or-later**，见 [NOTICE.md](NOTICE.md)。
- 一个能收发私聊的 QQ 号，以及任意 OneBot v11 实现（**本项目不含也不分发**）。
- Python 3.11+；`numpy` 用于本地向量检索。
- 可选：`ffmpeg`（看片抽帧）、本地句向量模型目录（`bge-small-zh-v1.5` 的 ONNX 版）。

## 安装

1. 把 `astrbot_plugin_fish_memory/`、`astrbot_plugin_inner_voice/` 放进 AstrBot 的 `data/plugins/`。
2. 按 `examples/*.example.json` 建两份插件配置，填入：
   - `memory_dir`：记忆目录（会被创建，里面是日记/卡片/原文）
   - `watch_umos` / `umo`：要监听的会话 ID
   - `persona_id`：AstrBot 人格 ID（不填则用 `default`）
3. 按需应用 `patches/` 里的补丁（见该目录 README）。
4. 重启 AstrBot。

启动后日志里应能看到 `[fish_memory] 记忆目录 = ...` 与 `[fish_vec] 向量后端 = ...` 两行。

---

## 需要你自己填的东西

本仓库已把私有信息全部替换为占位符，**开箱不能直接跑**。至少要改：

| 占位符 | 位置 | 说明 |
|---|---|---|
| `0000000000` | 两个插件的配置与少数默认值 | 主人的 QQ 号（用于“这是主人”判定） |
| `qq-onebot:FriendMessage:0000000000` | 配置 | 要监听的会话 ID |
| 空字符串 `""` | `examples/*.json` | 记忆目录、人格 ID、模型目录 |
| `persona-slim.md` | `fish_memory` | 可选的短人设文件，放记忆目录同级 |

---

## 这套东西的设计取向（三条）

1. **跟结构赌，不跟模型赌**——换模型会退化，但结构可修。
2. **会忘是设计**——只增不减的记忆池最终等于没有记忆。
3. **宁可钝，不要假**——说错一句的代价远大于不回答。

细节见 `docs/`。

---

## 许可

本仓库代码：**MIT**（见 [LICENSE](LICENSE)）。
运行时依赖 AstrBot（AGPL-3.0-or-later），补丁部分须遵守其协议（见 [NOTICE.md](NOTICE.md)）。

## 不在这个仓库里的

角色人设与归档、`memory/` 下的全部生活数据、数据库、配置文件实值、API 凭据、QQ 协议端、任何第三方插件。
