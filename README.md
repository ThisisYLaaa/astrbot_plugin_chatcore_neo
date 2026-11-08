# ChatCore Neo

**给 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 的「人格 × 记忆 × 多实例」聊天内核。**

把散落在 `chatcore_lite` / `lili_state` / `livingmemory` / `proactive_chat` 里的能力合并成一套：
人格状态机、长期记忆、注意力判定、主动消息、流式分段、角色名识别、印象系统，
再加上 **按平台实例隔离的配置与记忆库**、**人格池 × 实例的授权矩阵**。

> 适用 AstrBot `>= 4.27.0`，Python `3.10+`。当前版本 **v2.2.0**（见 [CHANGELOG](CHANGELOG.md)）。
> 许可证 MIT，署名要求见文末。

---

## 目录

- [这是什么](#这是什么)
- [核心能力](#核心能力)
- [安装](#安装)
- [快速开始](#快速开始)
- [配置项说明](#配置项说明)
- [WebUI 管理页](#webui-管理页)
- [命令](#命令)
- [模型工具](#模型工具function-calling)
- [目录结构](#目录结构)
- [数据目录](#数据目录)
- [多实例](#多实例)
- [人格组（跨会话）](#人格组跨会话)
- [测试](#测试)
- [从旧版迁移](#从旧版迁移)
- [版本与更新日志](#版本与更新日志)
- [许可证与署名](#许可证与署名)

---

## 这是什么

一个插件，把「让 bot 像一个人」需要的东西收进同一套状态与数据里：

| 层 | 解决的问题 | 落在哪 |
|---|---|---|
| 人格 | 它是谁、怎么说话、对谁是什么态度 | `personas/<世界观>/<人格>/` + `persona.yaml` |
| 状态 | 好感 / 情绪 / 淫乱 / 恶堕，随对话推进 | `state/`（按 umo + 用户） |
| 记忆 | 记得住、想得起、会淡忘、会被新事实取代 | SQLite `memory.db` + 向量 + 知识图谱 |
| 注意力 | 群聊里该不该接这句话 | `core/attention.py` |
| 输出 | 说人话：流式、分段、@ / 引用、撤回、拍一拍 | `core/stream.py`、`core/output.py` |
| 调度 | 没人说话时自己开口 | `core/proactive.py` |
| 多实例 | 一个进程挂几个号，彼此不串味 | `core/instances.py`、`core/persona_pool.py` |

设计取向：**不碰框架源码**，全部通过 AstrBot 的插件钩子、事件过滤器与 WebAPI 实现；
数据全部落在插件自己的目录下，卸载插件不脏框架。

---

## 核心能力

### 人格与状态机

- **结构化人格目录**：`persona.yaml` + `content/{name, skill, lore, dialog, speech_patterns, style_summary, appearance, tier.*}.md`
- **世界观（world）**：人格绑定 `_worlds/<world>/` 词表，按需注入；人格池按世界观分目录（`personas/<World>/<persona>/`）
- **别名与称呼**：多别名匹配，`/persona 香奈美`、`/persona 小鸟` 都能切
- **注入策略**：`always` / `on_demand` / `never`，逐段可覆盖
- **好感度状态机**：好感 / 情绪 / 淫乱 / 恶堕四条轴，按会话与用户独立推进；
  阈值文案与旧实现逐值一致（换实现不改手感）
- **人格一致性检查**：`/persona doctor` 校验缺段、正文漂移、框架样板残留

### 长期记忆

- **SQLite 单库**：`memory.db` 的 `atoms` 表存全部记忆，向量以 float32 BLOB 内联
- **双路召回**：向量余弦 + 知识图谱，两路权重、RRF 与各项打分系数都可调
- **待固化缓冲**：攒够 N 轮才调 LLM 总结，避免每轮烧钱
- **衰减与取代**：按 kind 衰减；新事实可 supersede 旧记忆并降权
- **MMR 去冗余**：同一件事只留 1~2 条
- **阈值门控 + 人格策略**：跨人格共享 / 本命优先 / 严格隔离
- **离线评测**：`tests/memory_eval.py` 给排序类改动一个 hit@5 / MRR / precision@5 基线

### 注意力与隐性意图

- **接话概率**：气泡模型（基础概率、@ 加成、冷却、退避、他人在说话时的观察窗口）
- **不回复判定**：被唤醒但确实不该说时静默；豁免名单、私聊跳过、只看注意力命中
- **隐性意图**：定时用模型判断"这句是不是在等 bot 接"，命中就加注注意力

### 主动消息

- 会话安静超过阈值后由 bot 发起话题；**按会话 opt-in**，不是全局一言不合就开口
- 免打扰时段（支持跨零点）、最小间隔、**静默连发上限**（群里一直没人时最多连几条）
- 参数按实例一份，可在管理页给单个会话覆盖

### 输出：流式、分段与平台细节

- **真流式**：边生成边发，按最小间隔合并；框架不支持时自动 fallback 到切段
- **分段**：按句/长度切、标记清洗、去重、补发兜底；私聊与群聊可分别用「子代理」二次切分
- **标记解析**：`[[at:uid]]`、`[[reply:mid]]` 容错解析（模型常写坏括号）
- **撤回取消 / 撤回拍一拍**、**戳一戳回应**、**连发合并**、**引用回复**

### 角色名识别与印象

- 消息里出现角色名/外号时注入一张短角色卡（长度、条数、总字数都可限）
- 按用户自动总结画像（印象），可人工覆写；支持 NSFW 印象单独开关

### 多人格同台

- 剧情模式下同台扮演多个角色，副人格上限可配
- `show_costars` / `update_costars` 工具让模型自己调度

### 人格池 × 实例授权矩阵

- 所有人格放在一个池子里，**拖到哪个实例就哪个实例能用**；同一个人格可授权给多个实例
- 未授权的人格对该实例不可见、不可切换

---

## 安装

### 1. 放进插件目录

```bash
cd <AstrBot>/data/plugins
git clone https://github.com/ThisisYLaaa/astrbot_plugin_chatcore_neo.git
```

> `<AstrBot>` 是你的 AstrBot 安装目录；插件必须位于它的 `data/plugins/` 下，
> 插件会从自身位置推导所有数据目录（见 `core/paths.py`），不需要配 `ASTRBOT_ROOT`。

### 2. 依赖

| 依赖 | 说明 |
|---|---|
| AstrBot `>= 4.27.0` | 提供插件钩子、事件管线、WebAPI、provider 管理 |
| Python `3.10+` | 使用了 `X \| Y` 类型语法与 `match` 之外的新特性 |
| `numpy` | **可选**。AstrBot 后端自带；缺失时召回退回纯 Python 循环，行为一致 |

除此之外**没有第三方依赖**：数据库用标准库 `sqlite3`，HTTP 全部走框架的 provider 抽象，
没有 `requirements.txt`，不会污染 AstrBot 的运行环境。

### 3. 启用

重启 AstrBot，或在 WebUI → 插件管理里「重载」。
首次启动会自动建好数据目录与 `characters.json`，日志里会打印人格数量、世界观数量与数据根。

---

## 快速开始

1. **建人格**：`/persona new <id> --name 中文名 --world sr`，或把现成目录丢进 `plugin_data/chatcore_neo/personas/<World>/`
2. **授权给实例**：管理页 →「人格分配」→ 把人格拖到右侧实例的虚线框
3. **设为默认**：同一行右侧的「默认人格」下拉
4. **填特权用户**：管理页 →「配置」→ 选实例 → `privilege_uid`（**每个实例一名，互不串号**）
5. **调作息**：`enable_attention` / `enable_segmentation` / `enable_proactive` 等按实例开关

---

## 配置项说明

配置分两层，**这很容易搞混，先看这张表**：

| 你在哪改 | 影响谁 | 存在哪 |
|---|---|---|
| WebUI → 插件配置（`_conf_schema.json`，共 9 组 145 项） | **只是模板**：新建实例时复制一次 | 框架配置 |
| 管理页 →「配置」页（选实例） | 该实例**当前**行为 | `instances/<id>/config.json` |

改模板**不会**影响已有实例；要让老实例变，得在管理页里改它的那份。

`_conf_schema.json` 的分组（管理页里同序）：

### ① 总开关

`enabled`（启用插件）、`debug`（调试日志）。

### ② 人格

| 键 | 作用 |
|---|---|
| `persona_current` | 默认人格 id |
| `private_msg_mode` | 私聊发言格式模式（1 剧情向 / 2 纯信息） |
| `privilege_uid` | 特权用户（**本实例唯一一名**） |
| `instance_auto_create` | 为新出现的平台实例自动建配置目录 |
| `instance_new_preset` | 新实例套用「最小可用」预设（关注意力/分段/标记/TTS/流式/判定/意图/图谱/多人格/角色名/状态/戳一戳/合并，并清空 `privilege_uid`） |
| `unrestricted_list` | 不受制约用户 |
| `allow_erotic_content` | 允许回复色情内容 |
| `include_dialog` | 每轮注入角色台词样本 |
| `costar_enabled` / `costar_max` | 多人格同台开关 / 副人格上限 |
| `char_mention_*` | 角色名识别：最短匹配长度、单轮最多注入几个、简介截断、总长上限 |
| `group_card_template` | 切换人格时的群名片模板 |
| `group_recent_*` | 人格组近况注入：开关 / 时间窗 / 条数 / 字数上限 |
| `identity_confirm_inject` | 群聊注入「本轮对话对象」身份强调 |
| `thinking_mode` | 思考过程引导 |
| `stutter_probability` | 结巴触发概率 |

### ③ 状态数值

`enable_affection`、`initial_affection`、`group_member_initial_affection`、
`enable_lewdness`、`initial_lewdness`、`initial_depravity`、
`save_conversation_log`、`max_history_count`、`history_timeout_seconds`、
`save_bot_state_to_history`、`bot_thought_mode`、`inject_conversation_context`、
`conversation_context_entries`、`user_msg_store_mode`、`user_msg_max_chars`。

> 这一组控制「状态日志」：把对话与 bot 内心活动记进状态、并决定注入多少。

### ④ 用户印象

`enable_impression`、`impression_threshold`、`impression_max_chars`、`impression_model`、
`impression_nsfw`、`enable_nsfw_impression`、`impression_timeout`。

### ⑤ 长期记忆（最大的一组）

| 子块 | 键 |
|---|---|
| 基础 | `enable_memory`、`provider_embedding`、`memory_top_k`、`memory_min_score`、`memory_max_atoms` |
| 衰减与取代 | `memory_decay_days`、`memory_kind_decay`、`memory_supersede` |
| 固化 | `memory_consolidate_turns`、`memory_summary_model`、`memory_summary_timeout`、`memory_summary_max_chars` |
| 注入 | `memory_inject_max_chars`、`memory_recall_gate`、`memory_persona_policy` |
| 去冗余 / 迁移 | `memory_mmr`、`memory_reembed_batch` |
| 知识图谱 | `enable_graph_retrieval`、`graph_embed_entries`、`graph_max_embed_per_memory`、`graph_max_topics`、`graph_max_participants`、`graph_max_facts` |
| 双路融合 | `document_route_weight`、`graph_route_weight`、`cross_route_bonus` |
| 图谱打分 | `graph_score_alpha/beta/gamma/delta`、`decay_rate` |

### ⑥ 注意力与判定

| 子块 | 键 |
|---|---|
| 气泡模型 | `enable_attention`、`attention_bubble_base`、`attention_active_cap`、`attention_decay_minutes`、`attention_hard_boost`、`attention_cooldown`、`attention_backoff`、`attention_backoff_floor`、`attention_hard_trigger_force`、`attention_wake_by_role` |
| 读空气 | `attention_followup_boost`、`attention_others_window`、`attention_others_limit` |
| 不回复判定 | `enable_no_reply_judge`、`no_reply_judge_model`、`no_reply_judge_skip_private`、`no_reply_judge_attention_only`、`no_reply_judge_exempt_users`、`no_reply_judge_prompt`、`no_reply_judge_timeout` |
| 隐性意图 | `enable_intent`、`intent_model`、`intent_interval_minutes`、`intent_timeout`、`intent_bump`、`intent_trigger_score`、`intent_prompt` |

### ⑦ 输出

| 子块 | 键 |
|---|---|
| 流式 | `enable_streaming`、`stream_send_interval` |
| 分段 | `enable_segmentation`、`segment_max_len`、`segment_min_len`、`segment_verify_ratio`、`segment_command_output` |
| 分段子代理 | `private_format_agent/model/timeout/threshold`、`group_format_agent/model/timeout/threshold`、`group_format_min_segment` |
| 标记与平台细节 | `enable_markers`、`enable_quote`、`quote_max_chars`、`enable_recall_cancel`、`enable_recall_poke`、`merge_pending_messages`、`merge_window_seconds`、`merge_notice`、`block_unmatched_command`、`call_prefix` |
| 戳一戳 | `poke_enabled`、`poke_only_self`、`poke_probability`、`poke_cool_down_seconds`、`poke_reply_mode`、`poke_text` |
| 历史卫生 | `tool_result_keep_rounds`、`strip_history_reminders`、`pipeline_trace_seconds` |

> `enable_proactive` 等主动消息参数**不在这一层**：主动消息是独立功能，
> 参数在管理页「主动消息」页（按实例 + 按会话覆盖）。

### ⑧ 语音合成

`enable_tts`、`tts_provider`、`tts_only_private`、`tts_max_chars`。
合成失败会静默降级为纯文字。

### ⑨ 管理页

`enable_admin_page`：启用管理接口与内置管理页面。

---

## WebUI 管理页

前端在 `pages/admin/index.html`，后端接口挂在 `/astrbot_plugin_chatcore_neo/*`
（`core/webadmin.py`，前缀 `API_PREFIX`）。

**入口**：AstrBot WebUI → 插件 → ChatCore Neo → **管理页**（admin）。
没看到页面就先确认 `enable_admin_page` 是开的；接口注册失败会在日志里 warning。

17 个页签：

> 概览 / 人格 / 人格分配 / 角色 / 状态 / 印象 / 记忆 / 知识图谱 / 注意力 / 交互 /
> 主动消息 / 会话设置 / 记忆组 / 装配预览 / 自检 / 配置 / 原始数据

几个要点：

- **配置页顶部有实例下拉**，另有「（默认模板·仅新建实例用）」一项
- **人格分配页**是左人格 / 右实例的拖拽授权；容器级事件委托，拖拽不整页重绘
- **记忆组页 = 人格组**：新建组（组名 / 实例 / 人格）、组卡片（改人格、编辑副人格、逐个移出、改名、删除、一键同步群名片）、会话表（搜索 / 全选 / 批量加入或移出）
- **知识图谱页**支持人格筛选，画布随容器尺寸重排
- 所有读接口支持 `?inst=<实例 id>`；默认实例回退读，写操作必须指明实例否则报错

---

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

---

## 目录结构

```
astrbot_plugin_chatcore_neo/
├── main.py                 # 插件入口：事件钩子、命令、工具、运行时（按实例一套）
├── metadata.yaml           # 插件元信息（name/version/author/支持平台）
├── _conf_schema.json       # 框架配置模板（9 组 145 项）
├── core/
│   ├── paths.py            # 所有路径从插件自身位置推导
│   ├── instances.py        # 实例发现、目录、配置加载、「最小可用」预设
│   ├── persona_pool.py     # 人格池 × 实例授权矩阵
│   ├── groups.py           # 人格组：组 = 人格 + 成员会话
│   ├── group_journal.py    # 组内近况流水（JSONL）
│   ├── persona/            # 人格加载、校验、渲染、创作辅助、doctor
│   ├── memory/             # SQLite 存储、embedder、双路召回、固化
│   ├── graph/              # 知识图谱抽取、存储、检索
│   ├── state/              # 好感度状态机、特权
│   ├── attention.py        # 接话概率
│   ├── proactive.py        # 主动消息调度
│   ├── stream.py           # 流式
│   ├── output.py           # 分段与标记清洗
│   ├── assembler.py        # 提示词装配
│   ├── inject.py           # 注入片段
│   ├── webadmin.py         # 管理页后端（WebAPI）
│   └── ...
├── pages/admin/index.html  # 管理页前端（单文件，无构建）
├── templates/persona/      # 新建人格用的骨架
├── migrations/             # 一次性迁移脚本（默认预演，--apply 才真跑）
└── tests/                  # 测试套件（纯标准库可跑，也支持 pytest）
```

## 数据目录

插件第一次启动时在 `<data>/plugin_data/chatcore_neo/` 下自动建：

```
plugin_data/chatcore_neo/
├── personas/<World>/<persona>/   # 人格池（不在 skills/ 下，因此不进框架技能索引）
├── state/                        # 会话级状态（umo 键，跨实例共享）
│   └── _impressions/<uid>.json   # 用户印象
├── instances/<实例 id>/
│   ├── config.json               # 该实例的全部配置
│   ├── personas.json             # 该实例可用的人格 id
│   ├── characters.json
│   └── memory/{memory.db,graph.db}
└── groups.json                   # 人格组
```

> 这些都是**运行时数据**，已在 `.gitignore` 里排除；升级插件不会覆盖。

## 多实例

| 概念 | 说明 |
|---|---|
| 实例 id | UMO 第一段，`BotA:GroupMessage:123` → `BotA` |
| 配置来源 | `instances/<id>/config.json`；框架面板那份只是模板 |
| 新建实例 | 检测到「已启用但没配置目录」的平台时自动创建（`instance_auto_create`） |
| 新实例预设 | 默认套「最小可用」；`instance_new_preset=false` 可关 |
| 记忆隔离 | 每个实例一个 `memory.db`，跨实例归组会被拒绝 |
| 残留清理 | 管理页/`/chatcore` 可列出并清掉已删实例留下的目录 |

## 人格组（跨会话）

- **一个实例可以有多个组**，每组绑定**一个人格** + **任意多个会话**
- 组内共享：记忆召回按组键共享；任一会话切人格 / 加副人格，**全组一起变**
- **入组立刻对齐**：人格、副人格、群名片、切换提醒一次性同步到位
- **组内近况注入**：某人在会话 b 说话时，把他 1 小时内在同组其他会话里触发过 bot 回复的原话 + bot 当时的回复，按时间正序注入本轮提示词
- 未入组的会话彼此独立：记忆、人格都各自一份
- **跨实例归组会被拒绝**：拆库之后同一个组键只能落在一个 `memory.db`

---

## 测试

仓库自带 43 个测试模块（覆盖人格、记忆、图谱、注意力、主动消息、流式分段、管理页、多实例、人格组等）。

### 跑法

```bash
# 官方跑法：纯标准库运行器，按模块多进程并行（推荐）
python tests/run_tests.py

# 只跑名字含关键字的模块 / 强制串行排障 / 列出最慢用例
python tests/run_tests.py persona
python tests/run_tests.py -j 1
python tests/run_tests.py --timing

# 也支持 pytest（tests/conftest.py 会把用例自带的「环境不满足」映射成 skip）
python -m pytest tests -q
```

### 用例在找什么

- **插件目录自身**：`tests/` 在插件里，所有路径从 `Path(__file__)` 推导
- **AstrBot 框架**（`backend/app`，用来 import `astrbot.*`）：从插件位置向上找，找不到就
  逐个盘符探常见安装位置；也可以用环境变量直接指定
- **真实安装的那份插件数据**（人格库、配置、实例目录）：同上。找不到时**大多数**用例会
  自己 skip，但**少数用例没有 skip 分支**（它们直接 `personas.all()[0]` 之类），会以
  `IndexError` / `AssertionError` 的形式失败 —— 裸克隆（没有 `plugin_data/chatcore_neo`）
  大约 46 条，属于缺数据，不是代码问题

```bash
# 装在非典型位置 / CI 里只有仓库时，用环境变量指路
export ASTRBOT_APP=/path/to/AstrBot/backend/app
export ASTRBOT_DATA=/path/to/AstrBot/data
export ASTRBOT_LEGACY_DATA=/path/to/legacy/env/data   # 只有"和旧环境对比"的用例需要
```

> 结论口径：**跑在真实安装上（带人格库）时应当全绿**；只有环境性 skip。
> 想在脱离安装的裸克隆里跑，先接受上面那批"缺数据失败"。
> 逐条 skip 原因：`python tests/run_tests.py -v`。

### 离线记忆评测

```bash
python tests/memory_eval.py           # 报告
python tests/memory_eval.py --json    # 机器可读，用于前后 diff
```

评测集 `tests/data/memory_eval.json` **只是一份合成样例**（演示格式）。
真跑分要换成自己的 case，并用 `--dump-vectors` 导出本机向量快照
（`tests/data/memory_eval_vecs.json`，已列入 `.gitignore`，不要提交）。

## 从旧版迁移

`migrations/` 下是一次性脚本，**默认只预演，`--apply` 才真跑**，跑前会备份：

| 脚本 | 作用 |
|---|---|
| `split_instances.py` | 单份配置/记忆 → 每实例一份 |
| `migrate_memory_to_sqlite.py` | JSON 记忆 → SQLite |
| `attribute_persona_memory.py` | 给旧记忆补人格归属 |
| `merge_lore.py` | 合并旧 lore / brief 到人格正文 |
| `prune_leftover_graph.py` | 清理孤儿图谱条目 |
| `reorg_personas_by_world.py` | 人格池按世界观分层重排 |
| `migrate_skills_tree.py` / `migrate_state.py` / `import_characters.py` | 旧环境技能树 / 状态 / 角色库导入 |
| `gen_persona_manifests.py` | 旧 `skills/pfm_*` → 人格目录 + `persona.yaml` |
| `check_persona_structure.py` / `strip_persona_boilerplate.py` | 人格正文体检 / 清理装配期样板 |
| `verify_against_baseline.py` | 与旧实现逐段对比（需要基准快照） |
| `selfcheck_instances.py` | 实例 / 人格池 / 配置读取的离线自检 |

脚本**不写死路径**：插件目录从脚本位置推导，旧环境/基准快照用环境变量给
（`CHATCORE_OLD_SKILLS`、`CHATCORE_OLD_STATE`、`CHATCORE_OLD_DATA`、`CHATCORE_BASELINE` 等）。

## 版本与更新日志

当前版本 **v2.2.0**（`metadata.yaml` 的 `version` 与 [CHANGELOG](CHANGELOG.md) 顶部条目一致）。

- [CHANGELOG.md](CHANGELOG.md) — 逐版本的新增 / 变更 / 修复
- 兼容性：`astrbot_version >= 4.27.0`；支持 `aiocqhttp`、`telegram`、`discord`、`webchat`

## 许可证与署名

**MIT**。你可以自由使用、修改、分发（包括商用），但**必须保留版权声明与本许可证**：

> Copyright (c) 2026 ThisisYLaaa — https://github.com/ThisisYLaaa/astrbot_plugin_chatcore_neo

如果你发布修改版，请保留上述署名，并建议在 README 里注明改动。完整条款见 [LICENSE](LICENSE)。
