# 更新日志

## v2.2.0 — 2026-11-09

> 这一段的代码此前只存在于线上运行目录，本轮整理按「以运行版本为准」把它同步进仓库，
> 并顺延版本号为 **v2.2.0**（`metadata.yaml` 同步）。
> 每条都能在代码里找到对应实现：括号里是函数名或配置项。

### 新增

- **撤回拍一拍**（`enable_recall_poke`，`_spawn_recall_poke` / `_recall_poke_target` / `_poke_user`）：
  识别 `group_recall` / `friend_recall` 后拍一拍被撤回的人，群聊发 `group_poke`、私聊发 `friend_poke`；
  平台不支持就静默跳过；同一会话 3 秒只拍一次，另有 claim 去重（一次撤回可能同时来通知与重投）；
  拍一拍异步发出，不阻塞消息管道。
- **已移除实例的盘点与清理**（`InstanceStore.live_ids` / `orphan_ids`、`webadmin.build_orphans` /
  `_orphan_detail` / `purge_orphan_instance`）：区分「AstrBot 里还在的实例」与「磁盘上残留的实例」，
  能列出每个残留实例还占着什么（会话目录、记忆组、磁盘体积），支持归档或彻底删除；
  插件自己的 `default` 兜底目录不算残留。
- **主动消息参数独立成「功能级默认」一层**（`proactive.load_settings` / `save_settings` /
  `webadmin.set_proactive_defaults`）：不再混在实例配置里，读写走原子替换，损坏文件回落内置默认。
- **主动消息按实例过滤会话**（`proactive.set_scope` / `in_scope`）：判定失败时 fail-open（宁可多发，不静默掐死）。
- **主动消息状态按实例拆分**（一次性迁移 `_migrate_proactive_state`）：旧版多个实例共用一份
  `proactive/state.json`，一个实例里配的分会话覆盖会被别的实例的调度器读到，页面之间还会互相矛盾。
- **会话参数视图补齐「继承」的真实取值**（`session_params`：`default_mode` / `default_label` /
  `default_nsfw` / `overridden`）：页面能直接显示「继承（剧情向）」，并据此决定要不要给「清除」按钮。
- **人格池：空授权表 = 不限制**（`PersonaPool.available_ids` / `is_available` / `grant_view`）：
  新接入、还没配授权的实例不再"一个人格都不可用"（否则 `/persona list` 全空、组人格全部回退）；
  拖拽页对 `unrestricted` 显式提示，空勾选不再被误解成"一个都没授权"。
- **人格池按世界观分层**（`paths.world_dir_name` / `world_for_dir`、`persona.model.world_dir_of`）：
  人格目录支持 `personas/<World>/<persona>/`，扁平布局仍兼容。
- **中断文本识别**（`_is_interruption_text`）：被打断类文本不再当正常输入处理。
- **测试套件与迁移脚本入库**：`tests/` 共 43 个模块（含纯标准库多进程运行器 `run_tests.py`、
  离线记忆评测 `memory_eval.py`），`migrations/` 新增 8 个一次性脚本
  （旧技能树 / 状态 / 角色库导入、人格清单生成 `gen_persona_manifests.py`、正文体检
  `check_persona_structure.py`、装配期样板清理 `strip_persona_boilerplate.py`、与基准逐段对比
  `verify_against_baseline.py`、人格池按世界观重排 `reorg_personas_by_world.py`）。

### 变更

- `_conf_schema.json`：主动消息的 5 项（`enable_proactive`、`proactive_min_interval`、
  `proactive_idle_minutes`、`proactive_quiet_hours`、`proactive_silent_max`）从框架配置移除 ——
  改为管理页「主动消息」页按实例 / 按会话配置；`instance_new_preset` 的说明同步（主动消息不再进重置清单）；
  新增 `enable_recall_poke`。
- **人格框架文本统一**（`core/persona/registry.py` 的 `FRAMEWORK_NOTICE`）：改为"言行举止 / 避免被
  此设定以外的话语带偏"；`templates/persona/SKILL.md` 同步，并注明该段由框架统一提供、人格正文不必抄写
  （如要抄写必须逐字一致，否则 `check_persona_structure.py` 会报不一致）。
- **装配把 `msg_mode` 传进状态段**（`core/assembler.py`）：剧情向与纯信息的内心活动措辞不再混用。
- **场景硬编码判据扩展**（`core/persona/render.py` 的 `_SCENE_HARDCODE_MARKERS`）：新增「qq消息 /
  QQ消息 / 刷QQ」；群聊剧情向改用 `style_narrative.md`。
- **人格组合并校验实例**（`GroupStore._instance_of_meta` + `merge`）：补齐「一组一实例」不变式
  （之前只有 `assign` 有守门），跨实例合并直接报错；合并时**连副人格一起并**。
- **`Privilege` 构造语义收紧**（`core/state/store.py`）：`uid_of` 必须是可调用对象，误传列表直接
  `TypeError`（旧写法会静默失效，让相关断言整片空转）；`Privilege("uid")` 位置传参仍兼容。
- **管理页扩展**（`pages/admin/index.html`）：实例下拉、人格筛选、拖拽局部更新、残留实例面板、
  知识图谱画布随容器尺寸重排。
- **迁移脚本不再写死本机路径**：插件目录从脚本位置推导，旧环境 / 基准快照走环境变量
  （`CHATCORE_OLD_SKILLS`、`CHATCORE_OLD_STATE`、`CHATCORE_OLD_DATA`、`CHATCORE_BASELINE`、
  `CHATCORE_PREFERRED_QIDS`）。

### 修复

- 一次撤回被拍成连击（重复通知 / 重投）→ 3 秒冷却 + claim 去重。
- 人格组合并时源组的副人格被静默丢弃。
- 「空授权表」被当成"一律不可用"，导致新实例人格列表全空、组人格全部回退。
- `Privilege(["uid"])` 静默失效（测试全绿但什么都没验）。
- 主动消息跨实例串台（共用 `state.json`）。
- 会话参数的「继承」显示与实际取值不符（私聊那行写着「继承（全局设置）」，实际是剧情向）。
- `_recall_poke_at` / `_recall_poke_claims` 原先只在首次触发时 `hasattr` 懒初始化，
  `tests/test_plugin_wiring.py` 的接线静态检查会判定「属性没构造」；改为在 `__init__` 显式构造
  （测试替身 `FakePlugin` 同步补齐），测试从 711 passed / 46 failed 变为 713 passed / 44 failed
  （裸克隆口径，剩余失败均因缺人格库数据）。

## 仓库整理 — 2026-11-09

### 变更

- 以线上运行版本为准同步 `main.py`、`core/`、`pages/`、`_conf_schema.json`、`templates/`、
  `migrations/`、`tests/`、`metadata.yaml`；仓库独有文件（`README.md`、`LICENSE`、`CHANGELOG.md`、
  `.gitignore`）保留。
- `tests/` 首次公开：新增 `tests/_env.py`（从插件位置推导 AstrBot 安装路径，环境变量可覆盖）、
  `tests/conftest.py`（让 pytest 把用例自带的「环境不满足」识别成 skip）；离线评测集
  `tests/data/memory_eval.json` 换成**合成样例**，真实向量快照不入库。
- 隐私脱敏：真实 QQ / 群号 / message_id 统一换成占位（`100000001` / `200000001` / `300000001` 系列），
  测试与迁移脚本里的本机绝对路径改为变量 / 环境变量；`no_reply_judge_exempt_users` 的默认值保持空列表。
- `.gitignore` 补充：`*.bak2-*`、迁移报告、`tests/data/memory_eval_vecs.json`、缓存目录。
- README 重写：定位、核心能力、安装与依赖、按 `_conf_schema.json` 分组的配置说明、WebUI 入口、
  目录结构、数据目录、测试跑法、迁移说明。

## v2.1.0 — 2026-10-26

### 新增

- **人格组 v2**：一个实例可建多个组，每组绑定一个人格 + 任意多会话；组内记忆召回共享、人格与副人格同步、入组即刻对齐（人格 + 副人格 + 群名片 + 切换提醒）；跨实例归组直接拒绝。
- **组内近况注入**：把同一用户在同组**其他会话**、时间窗内的「原话 + bot 回复」注入本轮提示词，配置项 `group_recent_enabled` / `group_recent_window_min` / `group_recent_limit` / `group_recent_max_chars`。
- **每实例单特权用户**：`privilege_uid` 取代旧的全局名单 + 前缀覆盖，实例之间互不串号。
- **按实例的运行时**：注意力、判定、分段、戳一戳、合并、TTS、印象、角色名、调度器全部按实例各一套，配置热同步（`sync_config()`）。
- 管理页「记忆组」重做：组卡片编辑、会话表批量入组 / 移出、一键同步群名片。
- 管理页「人格分配」改为容器级事件委托 + 局部更新，拖拽不再整页重绘。
- 知识图谱页新增**人格筛选**。

### 变更

- 总览 / 记忆 / 注意力 / 交互 / 主动消息 / 知识图谱 / 角色接口全部支持 `?inst=`，默认实例回退读、写操作抛错。
- 群名片对齐批量执行，启动后 20s / 45s / 90s 三次重试（平台适配器晚连）。
- 流式收尾额外输出一条完整回复日志，便于排查分段问题。
- 清理残留支持**归组会话**：清自身状态并从组里摘掉，但不动共享记忆。

### 修复

- `_conf_schema.json` 里 `no_reply_judge_exempt_users` 默认值不再是作者本人的号。
- 屏蔽框架 `pre-send event hook` 在流式下的无害告警（不改框架源码）。
- `.gitignore` 不再误排除 `core/memory/`、`core/state/`。

## v2.0.0 — 2026-10-25

- 首次公开：人格 × 记忆 × 多实例内核（人格目录、SQLite 记忆 + 知识图谱、注意力判定、主动消息、流式分段、角色名识别、印象系统）。
- 按平台实例隔离配置与记忆库，人格池 × 实例授权矩阵，管理页 17 个页签。
