# ChatCore Neo

**给 AstrBot 的「人格 × 记忆 × 多实例」聊天内核。**

把散落在 `chatcore_lite` / `lili_state` / `livingmemory` / `proactive_chat` 里的能力合并成一套：
人格状态机、长期记忆、注意力判定、主动消息、流式分段、角色名识别、印象系统，
再加上 **按平台实例隔离的配置与记忆库**、**人格池 × 实例的授权矩阵**。

> 适用 AstrBot `>= 4.27.0`，Python `3.10+`。

---

## 特性

### 人格

- **结构化人格目录**：`persona.yaml` + `content/{name, skill, lore, dialog, speech_patterns, style_summary, appearance, tier.*}.md`
- **世界观（world）**：人格可绑定 `_worlds/<world>/` 词表，按需注入
- **别名与称呼**：多别名匹配；`/persona 香奈美`、`/persona 小鸟` 都能切
- **注入策略**：`always` / `on_demand` / `never`，逐段可覆盖
- **好感度状态机**：好感 / 情绪 / 淫乱 / 恶堕四条轴，按会话与用户独立推进

### 记忆

- **SQLite 单库**：`memory.db` 里 `atoms` 表存全部记忆，向量以 float32 BLOB 存
- **双路召回**：向量余弦 + 知识图谱，权重可调
- **待固化缓冲**：攒够 N 轮才调 LLM 总结，避免每轮烧钱
- **衰减与取代**：按 kind 衰减、新记忆可 supersede 旧记忆
- **MMR 去重**、阈值门控、人格策略（跨人格共享 / 本命优先 / 严格隔离）

### 交互

- **注意力判定**：群聊里按概率决定「该不该接话」，带冷却、退避、读空气
- **不回复判定**：被唤醒但确实不该说时静默
- **主动消息**：按会话 opt-in、空闲阈值、免打扰时段
- **流式 + 分段**：真流式接管或 fallback 切段，标记清洗、去重、补发兜底
- **角色名识别**：消息里出现角色名/外号时注入一张短角色卡
- **印象系统**：按用户自动总结画像，可人工覆写
- **多人格同台**：剧情模式下同台扮演多个角色

### 多实例（v2）

- **实例 = 平台适配器**（`BotA` / `BotB` / …），一个 AstrBot 进程里挂几个号就隔离几套
- 每个实例独立：`config.json`、`personas.json`、`memory/{memory.db,graph.db}`、`characters.json`、主动消息状态
- **特权用户每实例一名**：写在各自的 `privilege_uid`，互不影响
- **人格池授权矩阵**：55 个人格放在一个池子里，拖到哪个实例就哪个实例能用；同一个人格可以授权给多个实例
- **框架面板配置 = 模板**：只在新建实例时被复制一次，改模板不影响已有实例

### 人格组（跨会话）

- **一个实例可以有多个组**，每组绑定**一个人格** + **任意多个会话**
- 组内共享：记忆召回按组键共享；任一会话切人格 / 加副人格，**全组一起变**
- **入组立刻对齐**：人格、副人格、群名片、切换提醒一次性同步到位
- **组内近况注入**：某人在会话 b 说话时，把他 1 小时内在**同组其他会话**里触发过 bot 回复的原话 + bot 当时的回复，按时间正序注入本轮提示词（`group_recent_enabled` / `group_recent_window_min` / `group_recent_limit` / `group_recent_max_chars`）
- 未入组的会话彼此独立：记忆、人格都各自一份
- **跨实例归组会被拒绝**：拆库之后同一个组键只能落在一个 `memory.db`

---

## 安装

```bash
# 把仓库放进 AstrBot 的插件目录
cd <AstrBot>/data/plugins
git clone https://github.com/ThisisYLaaa/astrbot_plugin_chatcore_neo.git
```

重启 AstrBot，或在 WebUI 插件管理里「重载」。

首次启动会自动建目录：

```
data/plugin_data/chatcore_neo/
├── personas/            # 人格池
├── state/               # 会话级状态（umo 键，跨实例共享）
├── instances/
│   └── <实例 id>/
│       ├── config.json  # 该实例的全部配置
│       ├── personas.json# 该实例可用的人格 id 列表
│       ├── characters.json
│       └── memory/{memory.db,graph.db}
└── groups.json          # 跨会话记忆组
```

## 快速开始

1. **建人格**：`/persona new <id> --name 中文名 --world sr`，或把现成目录丢进 `personas/`
2. **授权给实例**：管理页 →「人格分配」→ 把人格拖到右侧实例的虚线框
3. **设为默认**：同一行右侧的「默认人格」下拉
4. **填特权用户**：管理页 →「配置」→ 选实例 → `privilege_uid`
5. **调作息**：`enable_attention` / `enable_segmentation` / `enable_proactive` 按实例开关

## 多实例说明

| 概念 | 说明 |
|---|---|
| 实例 id | UMO 第一段，`BotA:GroupMessage:123` → `BotA` |
| 配置来源 | `instances/<id>/config.json`；框架面板那份只是模板 |
| 新建实例 | 检测到「已启用但没配置目录」的平台时自动创建（`instance_auto_create`） |
| 新实例预设 | 默认套「最小可用」：关注意力/分段/主动消息/TTS 等，`instance_new_preset=false` 可关 |
| 记忆隔离 | 每个实例一个 `memory.db`，跨实例归组会被拒绝 |

## 命令

| 命令 | 说明 |
|---|---|
| `/persona` | 当前人格 / 列表 / 切换 / reset / global / new / edit / add / rm / inject / reload / doctor / preview / state / attention / graph / segment / cast / join / leave |
| `/character` | 角色外号库：list / set / del / test |
| `/chatcore` | 运行统计、自检、清理残留 |

## 模型工具（Function Calling）

| 工具 | 作用 |
|---|---|
| `persona_list` / `persona_switch` | 查本实例可用人格 / 切换 |
| `show_costars` / `update_costars` | 多人格同台 |
| `memory_search` / `memory_note` | 记忆召回 / 写入 |
| `world_list` / `world_search` / `world_read` | 世界观词表 |

## 管理页

插件自带一个 17 页签的管理页（概览 / 人格 / **人格分配** / 角色 / 状态 / 印象 / 记忆 / 知识图谱 / 注意力 / 交互 / 主动消息 / 会话设置 / 记忆组 / 装配预览 / 自检 / 配置 / 原始数据）。

- 配置页顶部有**实例下拉**，另有「（默认模板·仅新建实例用）」
- 人格分配页是左人格 / 右实例的拖拽授权
- 记忆组页 = **人格组**：新建组（组名 / 实例 / 人格）、组卡片（改人格、编辑副人格、逐个移出成员、改名、删除、一键同步群名片）、会话表（搜索 / 全选 / 勾选批量加入或移出）
- 会话设置页按实例展示特权用户

## 架构

| 模块 | 职责 |
|---|---|
| `core/persona/` | 人格加载、校验、渲染、doctor |
| `core/memory/` | SQLite 存储、向量、召回、固化 |
| `core/graph/` | 知识图谱抽取、存储、检索 |
| `core/instances.py` | 实例发现、目录、配置加载、最小预设 |
| `core/persona_pool.py` | 人格池 × 实例授权矩阵 |
| `core/groups.py` | 人格组 v2：组 = 人格 + 成员会话 |
| `core/group_journal.py` | 组内近况流水（JSONL 追加，供提示词注入） |
| `core/state/` | 好感度状态机、特权 |
| `core/attention.py` | 接话概率 |
| `core/proactive.py` | 主动消息调度 |
| `core/stream.py` / `core/output.py` | 流式与分段清洗 |
| `core/assembler.py` / `core/inject.py` | 提示词装配 |
| `core/webadmin.py` | 管理页后端 |
| `main.py` | 事件钩子、命令、工具、运行时 |

## 从旧版迁移

`migrations/` 下带几个一次性脚本（都会先备份、可预演）：

| 脚本 | 作用 |
|---|---|
| `split_instances.py` | 单份配置/记忆 → 每实例一份；`--apply` 才真跑 |
| `migrate_memory_to_sqlite.py` | JSON 记忆 → SQLite |
| `attribute_persona_memory.py` | 给旧记忆补人格归属 |
| `prune_leftover_graph.py` | 清理孤儿图谱条目 |

## 开发

```bash
python migrations/selfcheck_instances.py   # 实例 / 人格池离线自检
```

> `tests/` 未随仓库发布：里面的向量缓存与会话样本来自真实记忆库，不适合公开。

## 许可证与署名

**MIT**。你可以自由使用、修改、分发（包括商用），但**必须保留版权声明与本许可证**，
即保留作者署名：

> Copyright (c) 2026 ThisisYLaaa — https://github.com/ThisisYLaaa/astrbot_plugin_chatcore_neo

如果你发布修改版，请保留上述署名，并建议在 README 里注明改动。

完整条款见 [LICENSE](LICENSE)。
