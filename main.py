# -*- coding: utf-8 -*-
"""chatcore_neo —— 整合 chatcore_lite / lili_state / livingmemory / proactive_chat。

架构纪律（方案 3.4 / 3.5 / 11.4）
---------------------------------
1. **唯一生成入口**：除 `proactive` 的合成事件外，任何模块都不直接调 provider。
   本文件里没有一处 `text_chat` / `get_provider_by_id`。
2. **唯一注入点**：`on_llm_request` 里由 `PromptAssembler` 统一写 `req`。
   动态内容一律 `extra_user_content_parts` + `mark_as_temp()`，**绝不写 `req.contexts`**。
3. **零反射耦合**：不查 `star_map`、不按类名找别的插件。
4. **禁字符串嗅探**：`world` / 系列一律来自 `persona.yaml` 的外键。
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star

from .core import compat, paths, world as world_mod
from .core.attention import AttentionManager
from .core.groups import MemoryGroups
from .core.impression import ImpressionSummarizer
from .core.interactions import (MergeGate, PokeHandler, build_quote_block,
                                extract_reply_target, is_poke_event,
                                poke_target)
from .core.judge import Judge
from .core.assembler import AssemblyInput, PromptAssembler
from .core.characters import (DEFAULT_CHARACTERS, CharacterMatcher,
                              CharacterStore)
from .core.costars import CoStarStore
from .core.costars import build_block as build_costar_block
from .core.history_hygiene import strip_system_reminders, strip_tool_results
from .core.inject import build_marker_block
from .core.memory import build_default as build_memory
from .core.output import (clean_segments, fix_literal_mentions,
                         normalize_speaker_segments, parse_markers,
                         split_narrative, split_raw,
                         strip_legacy_segment_marks, strip_markers)
from .core.proactive import ProactiveScheduler, synthesize_event
from .core.segment_agent import SegmentAgent
from .core.session_params import MODE_NARRATIVE, MODE_PLAIN
from .core.persona import PersonaService
from .core.persona import authoring as A
from .core.persona import doctor as doctor_mod
from .core.persona import render as R
from .core.persona.model import PersonaDoc
from .core.sessionmap import JsonMap
from .core.state import Privilege, StateStore, safe_segment
from .core.stream import StreamSplitter
from .core.tts import TTS
from .core import webadmin
from .core.instances import InstanceStore, MINIMAL_PRESET
from .core.persona_pool import PersonaPool

from contextvars import ContextVar

PLUGIN_NAME = "astrbot_plugin_chatcore_neo"

# 当前正在处理的实例 id（UMO 第一段）。由事件入口设置，后台任务显式传参。
_CUR_INST: ContextVar[str] = ContextVar("ccn_inst", default="")


def inst_of_umo(umo: str) -> str:
    """`BotA:GroupMessage:123` -> `BotA`。"""
    return str(umo or "").split(":", 1)[0]


class _MemProxy:
    """按当前实例把调用转发给对应的 MemoryService。

    271 处 `self.memory.*` 调用点因此**不需要改**：实例由 contextvar 决定，
    后台任务里用 `plugin.mem(inst)` 显式取。
    """

    __slots__ = ("_p",)

    def __init__(self, plugin) -> None:
        object.__setattr__(self, "_p", plugin)

    def __getattr__(self, name):
        return getattr(self._p.mem(), name)

    def __setattr__(self, name, value):
        setattr(self._p.mem(), name, value)

# 管道耗时探针挂在事件 extras 上的键
_TRACE_KEY = "ccn_trace_entry_ts"


def _int_or(value, default: int) -> int:
    """转 int；只在 None / 转换失败时用默认值（`0` 是合法值）。"""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

# 事件 extras 的键（在 on_llm_request 里写，在 on_llm_response 里读）
X_UMO = "_ccn_umo"
X_PERSONA = "_ccn_persona"
X_UID = "_ccn_uid"
X_TIER = "_ccn_tier"
X_USER_MSG = "_ccn_user_msg"
X_INJECTED = "_ccn_injected"
# 印象的存储键（= 具体用户）。私聊里 `X_UID` 是空串（状态按会话级存），
# 但印象按**人**存，所以单独记一个键，避免请求/响应两侧算出不同的值。
X_IMP_UID = "_ccn_imp_uid"
# 注意力「软触发」命中后自己把消息叫醒时打的标记。
# 两处用它：① 闸门见它就放行（否则会当成"未命中命令的唤醒消息"拦掉）
#           ② `_decide_reply` 见它就跳过重复掷骰子
X_ATTN_WAKE = "_ccn_attention_wake"

# 流式传输（见 `_stream_deliver`）：三个挂在事件 extras 上的键。
#   X_STREAM_REQ   —— 本轮的流式由插件开关放行（流式下禁用分段子代理的判据）
#   X_STREAM_READY —— `send_streaming` 已被本插件接管（只装一次）
#   X_STREAMED     —— 本轮真的走了接管后的流式发送（输出层据此不再重复处理）
#   X_STREAM_SENT  —— 流式层实际发出的段数（0 = 一个字都没发出去，见兜底补发）
X_STREAM_REQ = "_ccn_stream_req"
X_STREAM_READY = "_ccn_stream_ready"
X_STREAMED = "_ccn_streamed"
X_STREAM_SENT = "_ccn_stream_sent"
# 叫醒词（call_prefix）：消息以它开头时**只触发 LLM 回复，不触发指令**。
# 三处用它：① 闸门见它就放行（它不是"未命中命令的唤醒消息"）
#           ② 剥词后把 is_at_or_wake_command 置真，让消息走 LLM 链路
#           ③ 让 `_is_command_result` 认得"这条结果不是命令产生的"
X_CALL_PREFIX = "_ccn_call_prefix"


def _parse_opts(argv) -> dict:
    """解析 `--key value` / `--flag` 形式的参数（/persona new 等命令使用）。

    返回 dict：`--name 小明` → {"name": "小明"}；
    只有开关没跟值时 → `True`。未知参数忽略（不报错，便于向前兼容）。

    **注意**：这个函数从一开始就被 `/persona new` 调用，却一直没被定义 ——
    该命令会抛 `NameError` 且用户零回复。现已补上，并加了"未定义名字"静态检查。
    """
    out: dict = {}
    argv = list(argv or [])
    i = 0
    while i < len(argv):
        tok = str(argv[i])
        if tok.startswith("--"):
            key = tok[2:].strip()
            if not key:
                i += 1
                continue
            nxt = argv[i + 1] if i + 1 < len(argv) else None
            if nxt is None or str(nxt).startswith("--"):
                out[key] = True
                i += 1
            else:
                out[key] = str(nxt)
                i += 2
        else:
            i += 1
    return out


# 群聊流水行的前缀：`[10-01 周三 13:39]`（30 分钟窗口内，年份无意义）
_LIVE_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _live_stamp(ts: int) -> str:
    """把流水时间戳渲染成行前缀——补回历史 reminder 被清理后丢掉的时间线索。"""
    lt = time.localtime(ts)
    return (f"[{lt.tm_mon:02d}-{lt.tm_mday:02d} "
            f"{_LIVE_WEEKDAYS[lt.tm_wday]} "
            f"{lt.tm_hour:02d}:{lt.tm_min:02d}]")


class ChatCoreNeoPlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        paths.ensure_dirs()

        # ---- 实例化：必须早于任何 _cfg 调用 ----
        self._mem_cache: dict = {}
        try:
            self._inst_store = InstanceStore(context.get_config())
        except Exception:
            self._inst_store = InstanceStore(None)
        if bool(config.get("instance_auto_create", True)):
            self._inst_store.ensure_all(
                template=config,
                use_preset=bool(config.get("instance_new_preset", True)))
        self._default_inst = self._inst_store.default_id()
        self._inst_cfgs: dict = {}
        for _i in self._inst_store.loaded_ids():
            _c = self._inst_store.load_cfg(_i)
            if _c is not None:
                self._inst_cfgs[_i] = _c
        self._persona_pool = PersonaPool(paths.INSTANCES_DIR)

        self.personas = PersonaService()
        self.state = StateStore(
            default_affection=self._cfg("initial_affection", 65),
            default_lewdness=self._cfg("initial_lewdness", 20),
            default_depravity=self._cfg("initial_depravity", 0))
        # 特权用户：每个实例有且只有一名（该实例 config.json 的 privilege_uid）。
        # 实例边界本身就是原来的"分会话前缀"，所以不再需要覆盖表。
        self.privilege = Privilege(
            uid_of=lambda inst: self._cfg("privilege_uid", "", inst=inst))
        self.templates = compat.EventTemplateStore()
        # 跨会话记忆组：记忆按组键存取，多会话共享一份
        self.groups = MemoryGroups()
        # 多人格同台：会话级副人格列表（仅剧情模式生效，见 core/costars.py）
        self.costars = CoStarStore()
        self.assembler = PromptAssembler()

        self._intent_last: dict = {}
        # 本轮触发消息 id（umo -> message_id），供撤回取消匹配
        self._active_triggers: dict = {}
        # 人格切换提醒（umo -> 提醒文本）：/persona 切换后记下，
        # 在下一轮装配时以"系统通知"注入动态段，强制模型切换到新身份。
        # 不记的话，旧人格在历史上下文里刷过几十条，模型会沿惯性继续扮演
        # 旧角色（实测：切到示例人格后仍以"香奈美"自称回复）。
        self._persona_switch_notice: dict = {}
        self._mem_tasks: set = set()
        self._char_seeded: set = set()
        # 分段提示词可编辑覆盖（state/_segment_prompts.json，WebUI「交互」页签）。
        # 必须在 runtime 之前建好：SegmentAgent 构造时要拿到它。
        self._segment_prompts = JsonMap(paths.STATE_DIR / "_segment_prompts.json")

        # ---- 每个实例一套运行时 ----
        # 注意力/判定/分段/戳/合并/TTS/印象/角色库/主动消息/记忆全部按实例隔离；
        # 271 处 `self.xxx` 通过下面的 property 自动落到「当前实例」。
        self._runtimes: dict = {}
        self._rt(self._default_inst)
        self.memory = _MemProxy(self)

        self._session_persona = self.state.session_persona
        self._initialized = self.state.session_initialized
        # 会话级发言参数（发言模式 / 额外规则，见 core/session_params.py）
        # WebUI「会话设置」页签读写；未设置的会话按类型默认。
        self._session_params = JsonMap(paths.STATE_DIR / "_session_params.json")
        # uid → 昵称：管理页下拉框显示「QQ + 昵称」的数据源。
        # 每条消息顺手更新，只在昵称变化时落盘（见 _note_identity）。
        self._user_names = JsonMap(paths.STATE_DIR / "_user_names.json")
        # 群 umo → 群名。平台把 `group_name` 直接带在群消息里，同样白捡；
        # 历史会话和平台不带群名的场合由管理页的「补齐群名与昵称」调
        # `get_group_info` 补（见 webadmin.refresh_names）。
        self._group_names = JsonMap(paths.STATE_DIR / "_group_names.json")
        self._seed_names_from_templates()
        self._import_legacy_names()

        self._stats = {"llm_request": 0, "injected": 0, "stripped": 0,
                       "state_updates": 0, "errors": 0}
        # 流式传输统计（管理页「交互」页签展示；重载即归零）
        self._stream_stats = {"turns": 0, "segments": 0, "chars": 0,
                              "errors": 0, "last_error": "",
                              "last_turn_ms": 0, "degraded": 0}
        self._last_error = ""
        # 主动消息「真伪会话」判定的缓存：(状态根目录 mtime, 会话目录名集合)
        self._known_cache: tuple[int, set[str]] | None = None
        # AstrBot 侧会话存活缓存：DB `conversations` 里仍有对话记录的 UMO
        # 集合（safe 段格式，与 state 目录名同构）。None = 还没拉到/已过期，
        # `_session_known` 在存活集未知时回退目录判定，并按需后台补拉。
        # 用户在 AstrBot 删掉的会话会从 DB 消失但插件目录还在 —— 这正是
        # 「孤儿会话」判定的依据（见 _session_known 与 webadmin.build_sessions）。
        self._alive: set[str] | None = None
        self._alive_ts: float = 0.0        # 上次成功拉取的时刻（monotonic）
        self._alive_kick_ts: float = 0.0   # 上次发起后台拉取的时刻（防刷任务）
        self._alive_ttl: float = 60.0
        # 群聊发言流水（内存，不落盘）：umo → 最近群成员发言 `{u,n,t,ts}`。
        # 框架的群聊 contexts 里所有 user 消息**没有说话人标识**，模型因此
        # 分不清"这句话是谁说的"（群聊认错人）。这里由插件自己按事件标注
        # 说话人并在注入时补充给模型。
        self._live: dict[str, list[dict]] = {}

        # WebUI 管理接口（页面在 pages/admin/index.html）
        self.web_paths: list = []
        if bool(self._cfg("enable_admin_page", True)):
            try:
                self.web_paths = webadmin.register(self)
                logger.info(
                    f"[chatcore_neo] 已注册 {len(self.web_paths)} 个管理接口 "
                    f"({webadmin.API_PREFIX}/*)")
            except Exception as e:
                logger.warning(f"[chatcore_neo] 注册管理接口失败: {e!r}")

        # 角色库键位补齐（首次运行建 characters.json；已有文件只补缺不覆盖）
        self.seed_characters()

        logger.info(
            f"[chatcore_neo] 人格 {len(self.personas)} 个；"
            f"世界观 {world_mod.stats().text()}；"
            f"状态 {self.state.stats()}；数据根 {paths.ROOT}"
        )
        for err in self.personas.errors:
            logger.warning(f"[chatcore_neo] 人格加载异常: {err}")

    # ------------------------------------------------------------------ 生命周期
    async def initialize(self):
        """插件加载完成（含热重载）后启动主动消息调度循环。

        旧实现只在 `__init__` 里 new 了 `ProactiveScheduler`，却从不调用
        `start()` —— 主循环从未运行（state.json 里只有用户活动账本在涨、
        ticks 恒为 0），"半夜主动开口"因此一条都发不出来。
        """
        if not self._cfg("enabled", True):
            logger.info("[chatcore_neo] 插件已禁用，主动消息调度不启动")
            return
        # 每个实例一条调度循环（各自的事件模板与会话表）
        for iid in list((getattr(self, "_inst_cfgs", {}) or {}).keys()) or [self._default_inst]:
            try:
                rt = self._rt(iid)
                rt.scheduler.start(self._emit_proactive)
                if not rt.scheduler.enabled:
                    logger.info(f"[chatcore_neo] {iid}: 主动消息全局开关关闭（未放行）")
                elif not rt.scheduler.running:
                    logger.warning(f"[chatcore_neo] {iid}: 主动消息调度启动失败（无事件循环）")
                else:
                    logger.info(f"[chatcore_neo] {iid}: 主动消息调度已启动")
            except Exception as e:
                logger.warning(f"[chatcore_neo] {iid}: 主动消息调度启动异常：{e!r}")
        # 全局开关（`enable_proactive`）只负责**放行**：它关着就不启动，会话级
        # 配置一律不作数；它开着也只是"允许使用"，不会替任何会话开口
        # ——谁开口由「分会话配置」的 opt-in 决定（见 proactive.should_emit）。

    # ------------------------------------------------------------------ 配置
    @staticmethod
    def _num(value, default):
        """只在值为 `None` 时取默认；`0` / `0.0` 是合法值，必须原样保留。

        旧的 `<v> or <默认>` 写法会把用户显式配置的 0 换成默认值，
        使 schema 里标注"0 = 关闭 / 不限"的项永远做不到
        （例如 `max_history_count: 0` 表示不限、`attention_*: 0` 表示关闭该加成）。
        """
        return default if value is None else value

    def cfg_of(self, inst: str = ""):
        """取某实例的配置对象；未指定时取当前上下文 / 默认实例。

        实例是运行中新增的（新平台接进来）时惰性加载，避免启动后才建目录
        的实例拿到模板配置。
        """
        i = inst or _CUR_INST.get() or getattr(self, "_default_inst", "")
        cfgs = getattr(self, "_inst_cfgs", None)
        if cfgs is None:
            cfgs = self._inst_cfgs = {}
        c = cfgs.get(i)
        if c is None and i:
            try:
                c = self._inst_store.load_cfg(i)
            except Exception:
                c = None
            if c is not None:
                cfgs[i] = c
        return c or self.config

    def _cfg(self, key: str, default=None, inst: str | None = None):
        try:
            v = self.cfg_of(inst or "").get(key, default)
        except Exception:
            return default
        return default if v is None else v

    def mem(self, inst: str = ""):
        """取某实例的 MemoryService（惰性建、按实例缓存）。"""
        return self._rt(inst).memory

    # ---------------------------------------------------------- 实例运行时
    def _rt(self, inst: str = ""):
        """取某实例的运行时（懒建）。没有实例上下文时落默认实例。"""
        i = inst or _CUR_INST.get() or getattr(self, "_default_inst", "")
        table = getattr(self, "_runtimes", None)
        if table is None:
            table = self._runtimes = {}
        r = table.get(i)
        if r is None:
            r = self._make_runtime(i)
            table[i] = r
        return r

    def _make_runtime(self, inst: str):
        """按实例配置装配一套子对象。

        这些对象在旧版是 `__init__` 里用**全局配置**固化出来的；实例化之后
        每个实例必须有自己的那份，否则 BotB 会跑 BotA 的注意力参数。
        """
        from types import SimpleNamespace
        cfg = self.cfg_of(inst)
        n = self._num

        def c(key, default=None):
            return self._cfg(key, default, inst=inst)

        idir = paths.inst_root(inst)
        cpath = idir / "characters.json"
        if not cpath.exists():
            # 只有默认实例继承旧全局名册（它的外号是历史资产）；
            # 其它实例从空白起，由 seed_characters 按自己的人格补。
            try:
                import shutil as _sh
                if inst == getattr(self, "_default_inst", "") \
                        and paths.CHARACTERS_JSON.exists():
                    idir.mkdir(parents=True, exist_ok=True)
                    _sh.copy2(paths.CHARACTERS_JSON, cpath)
            except Exception:
                pass
        chars = CharacterStore(cpath)

        attention = AttentionManager(
            bubble_base=float(n(c("attention_bubble_base"), 0.03)),
            active_cap=float(n(c("attention_active_cap"), 0.30)),
            decay_minutes=float(n(c("attention_decay_minutes"), 3.0)),
            hard_trigger_boost=float(n(c("attention_hard_boost"), 0.10)),
            cool_down_seconds=float(n(c("attention_cooldown"), 20.0)),
            no_action_backoff=float(n(c("attention_backoff"), 0.5)),
            backoff_floor=float(n(c("attention_backoff_floor"), 0.25)),
            followup_boost=float(n(c("attention_followup_boost"), 0.15)),
            others_window_seconds=float(n(c("attention_others_window"), 60.0)),
            others_density_threshold=int(n(c("attention_others_limit"), 6)),
        )
        scheduler = ProactiveScheduler(
            enabled=bool(c("enable_proactive", True)),
            min_interval_min=int(n(c("proactive_min_interval"), 45)),
            idle_threshold_min=int(n(c("proactive_idle_minutes"), 90)),
            quiet_hours=str(c("proactive_quiet_hours", "") or ""),
            silent_max=int(n(c("proactive_silent_max"), 2)),
            known=self._session_known,
        )
        # 每次 tick 出话前强刷一次 AstrBot 会话存活集：孤儿会话一旦被用户
        # 删掉，下一轮 tick 就不会再被当成候选（见 _refresh_alive）。
        scheduler.set_pre_tick(lambda: self._refresh_alive(force=True))

        return SimpleNamespace(
            inst=inst,
            cfg=cfg,
            memory=build_memory(self.context, cfg, inst=inst),
            attention=attention,
            judge=Judge(self.context, cfg),
            segmenter=SegmentAgent(self.context, cfg,
                                   prompts_store=self._segment_prompts),
            poke=PokeHandler(
                enabled=bool(c("poke_enabled", True)),
                probability=float(c("poke_probability", 1.0) or 0.0),
                cooldown=float(c("poke_cool_down_seconds", 60.0) or 0.0),
                only_self=bool(c("poke_only_self", True)),
                reply_mode=str(c("poke_reply_mode", "llm") or "llm"),
                text=str(c("poke_text", "别戳我啦~") or "")),
            merge=MergeGate(
                enabled=bool(c("merge_pending_messages", True)),
                window=float(c("merge_window_seconds", 8.0) or 8.0),
                notice=str(c("merge_notice", "") or "")),
            tts=TTS(self.context,
                    provider_id=str(c("tts_provider", "") or ""),
                    enabled=bool(c("enable_tts", False)),
                    only_private=bool(c("tts_only_private", True)),
                    max_chars=int(c("tts_max_chars", 200) or 200)),
            impressions=ImpressionSummarizer(
                self.context, cfg, self.state,
                threshold=int(c("impression_threshold", 10) or 10)),
            characters=chars,
            char_matcher=CharacterMatcher(chars, self.personas.all()),
            scheduler=scheduler,
        )

    @property
    def attention(self):
        return self._rt().attention

    @property
    def judge(self):
        return self._rt().judge

    @property
    def segmenter(self):
        return self._rt().segmenter

    @property
    def poke(self):
        return self._rt().poke

    @property
    def merge(self):
        return self._rt().merge

    @property
    def tts(self):
        return self._rt().tts

    @property
    def impressions(self):
        return self._rt().impressions

    @property
    def scheduler(self):
        return self._rt().scheduler

    @property
    def characters(self):
        return self._rt().characters

    @property
    def char_matcher(self):
        return self._rt().char_matcher

    def set_inst(self, umo: str) -> str:
        """把「当前实例」压进 contextvar；返回实例 id。"""
        i = inst_of_umo(umo)
        if i:
            _CUR_INST.set(i)
        return i

    def use_inst(self, inst: str = ""):
        """临时把上下文切到某实例（WebAPI 用）。返回 token，配 end_inst 复位。"""
        return _CUR_INST.set(inst or getattr(self, "_default_inst", ""))

    def end_inst(self, token) -> None:
        try:
            _CUR_INST.reset(token)
        except Exception:
            pass

    # ---------------------------------------------------------- 角色名识别

    def seed_characters(self) -> int:
        """首次运行补齐角色库键位（人格名 + 默认名册），不覆盖已有外号。

        每个实例一份 `characters.json`，各自补自己的名册（只补已授权人格）。
        """
        inst = _CUR_INST.get() or self._default_inst
        if inst in self._char_seeded:
            return 0
        try:
            docs = self.personas.all()
            if inst:
                docs = self._persona_pool.available_docs(inst, docs)
            names = [d.display_name for d in docs if d.display_name]
            added = self.characters.ensure([*names, *DEFAULT_CHARACTERS])
            self._char_seeded.add(inst)
            if added:
                logger.info(f"[chatcore_neo] 角色库已补齐 {added} 个角色键位（{inst}）")
            self.char_matcher.reload(self.personas.all())
            return added
        except Exception as e:
            logger.warning(f"[chatcore_neo] 角色库初始化失败: {e!r}")
            return 0

    def reload_characters(self) -> None:
        """重读角色库与索引（改完外号、换完人格数据后调用，全实例生效）。"""
        for rt in (getattr(self, "_runtimes", {}) or {}).values():
            try:
                rt.char_matcher.reload(self.personas.all())
            except Exception:
                pass

    def _character_block(self, msg: str, doc: PersonaDoc | None) -> str:
        """本轮消息命中的角色卡片正文；没命中返回空串。"""
        if not bool(self._cfg("char_mention_enabled", True)):
            return ""
        if not str(msg or "").strip():
            return ""
        self.seed_characters()
        try:
            return self.char_matcher.render_block(
                msg, doc=doc,
                min_len=int(self._num(self._cfg("char_mention_min_len"), 2) or 2),
                max_hits=int(self._num(self._cfg("char_mention_max_hits"), 3) or 3),
                brief_chars=int(self._num(self._cfg("char_mention_brief_chars"), 60) or 60),
                max_chars=int(self._num(self._cfg("char_mention_max_chars"), 240) or 240))
        except Exception as e:
            logger.warning(f"[chatcore_neo] 角色名识别失败: {e!r}")
            return ""

    # ---------------------------------------------------------- 多人格同台

    def _costar_max(self) -> int:
        """同台副人格上限（配置 `costar_max`，钳到 [0, 8]）。"""
        try:
            n = int(self._cfg("costar_max", 3) or 0)
        except (TypeError, ValueError):
            n = 3
        return max(0, min(n, 8))

    def _costar_on(self) -> bool:
        return bool(self._cfg("costar_enabled", True))

    def _pname(self, pid: str) -> str:
        d = self.personas.get(str(pid))
        return d.display_name if d is not None else str(pid)

    def _costar_mode_ok(self, umo: str, is_private: bool) -> bool:
        """本会话当前是否剧情模式（同台的可启用/生效条件）。"""
        try:
            eff_mode, _ = self._resolve_session_params(umo, is_private)
        except Exception:
            return False
        return eff_mode == MODE_NARRATIVE

    def _costar_docs(self, umo: str,
                     main_doc: PersonaDoc | None) -> list[PersonaDoc]:
        """当前会话的副人格文档；解析失败 / 重复 / 未授权给本实例的自动忽略。"""
        out: list[PersonaDoc] = []
        for pid in self.costars.cast_of(umo):
            if main_doc is not None and pid == main_doc.id:
                continue
            d = self.personas.get(pid)
            if d is not None and self._persona_ok(umo, d):
                out.append(d)
        return out

    def _costar_block(self, umo: str, main_doc: PersonaDoc | None, *,
                      is_private: bool, msg_mode: int) -> str:
        """本轮要注入的同台块（temp 动态段）；不满足条件返回空串。"""
        if not self._costar_on():
            return ""
        docs = self._costar_docs(umo, main_doc)
        if not docs or main_doc is None:
            return ""
        try:
            return build_costar_block(main_doc, docs, msg_mode=msg_mode,
                                      is_private=is_private)
        except Exception as e:
            logger.warning(f"[chatcore_neo] 同台装配失败: {e!r}")
            return ""

    def _speaker_names(self, umo: str, is_private: bool) -> list:
        """多人格同台生效时，模型可能写在段尾/段首的**角色名前缀**集合。

        输出层据此把名牌归位到它自己那段的开头
        （见 `core/output.py:normalize_speaker_segments`）。

        闸门与注入侧完全一致（`on_llm_request` → `_costar_block`）：
        **剧情模式 + 本会话确有副人格**。否则返回空列表 = 不做任何处理
        —— 没开同台时模型不会写【角色名】，正文里的方括号不能被当名牌切。
        """
        if not self._costar_on():
            return []
        try:
            if not self._costar_mode_ok(umo, is_private):
                return []
            doc = self._persona_for(umo)
            if doc is None:
                return []
            docs = self._costar_docs(umo, doc)
            if not docs:
                return []
            names = [str(doc.display_name or "")]
            names += [str(d.display_name or "") for d in docs]
        except Exception:
            return []
        return [n for n in dict.fromkeys(names) if n]

    def _costar_resolve(self, names) -> tuple[list[PersonaDoc], list[str]]:
        """名字列表 → (命中文档, 未命中名字)。去重、保序。"""
        ok: list[PersonaDoc] = []
        bad: list[str] = []
        seen: set[str] = set()
        for raw in names or []:
            name = str(raw or "").strip()
            if not name:
                continue
            d = self.personas.resolve(name)
            if d is None:
                bad.append(name)
                continue
            if d.id not in seen:
                seen.add(d.id)
                ok.append(d)
        return ok, bad

    @staticmethod
    def _costar_mode_hint() -> str:
        return ("多人格同台仅在**剧情模式**下可用：先用 /persona msg 1 切到括号描写，"
                "或在 WebUI「会话设置」把本会话设为剧情向，再试。")

    def _costar_view(self, umo: str, doc: PersonaDoc | None, *,
                     mode_ok: bool = False, cap: int = 0) -> str:
        docs = self._costar_docs(umo, doc)
        cap = cap or self._costar_max()
        lines = [f"🎭 多人格同台（{len(docs)}/{cap}"
                 + ("，已关闭" if not self._costar_on() else "") + "）",
                 f"主角：{doc.display_name if doc else '（无）'}"]
        lines.append("登场：" + ("、".join(
            f"{d.display_name}（{d.id}）" for d in docs) if docs else "（无）"))
        lines.append("状态：" + ("生效中（剧情模式）" if docs and mode_ok
                                 else "未生效（本会话不是剧情模式）" if docs
                                 else "未开启"))
        lines.append("用法：/persona cast [add|rm|set|clear] <角色名...>；"
                     "快捷：/persona join <名>、/persona leave <名>")
        return "\n".join(lines)

    def _handle_costar_command(self, umo: str, sub: str, args: list[str],
                               *, is_private: bool) -> str:
        """`/persona cast|join|leave` 的落点，返回要给用户看的文本。"""
        doc = self._persona_for(umo)
        # 全局开关只管「注入」，不影响查看/清理；添加时单独把关。
        mode_ok = self._costar_mode_ok(umo, is_private)
        cap = self._costar_max()

        if sub in ("join", "leave"):
            action, names = ("add" if sub == "join" else "rm"), list(args)
        else:
            names = [str(a) for a in args]
            if names and names[0].lower() in (
                    "add", "rm", "remove", "del", "set", "clear", "list", "ls"):
                action = names.pop(0).lower()
            else:
                action = "add" if names else "list"
        action = {"remove": "rm", "del": "rm", "ls": "list"}.get(action, action)

        if action == "list":
            return self._costar_view(umo, doc, mode_ok=mode_ok, cap=cap)
        if action == "clear":
            had = self.costars.clear(umo)
            if not had:
                return "当前会话没有开启多人格同台。"
            return "已关闭多人格同台。\n" + "\n".join(
                f"「{self._pname(p)}」已离开会话" for p in had)
        if action in ("add", "set") and not mode_ok:
            return self._costar_mode_hint()
        if not names:
            return "用法：/persona cast [add|rm|set|clear] <角色名...>"

        docs, bad = self._costar_resolve(names)
        lines: list[str] = []
        if bad:
            lines.append("没找到这些角色：" + "、".join(bad))
        ids: list[str] = []
        skipped: list[str] = []
        for d in docs:
            if doc is not None and d.id == doc.id:
                skipped.append(d.display_name)
            else:
                ids.append(d.id)
        if skipped:
            lines.append("「%s」是当前主角，已跳过" % "、".join(skipped))
        if not ids:
            if not lines:
                lines.append("没有可操作的角色。")
            return "\n".join(lines)

        cur = self.costars.cast_of(umo)
        if action == "add":
            new = list(cur)
            head: list[str] = []
            for pid in ids:
                if pid in new:
                    lines.append(f"「{self._pname(pid)}」已在会话中")
                elif cap and len(new) >= cap:
                    lines.append(f"已达上限 {cap}，未加入「{self._pname(pid)}」")
                else:
                    new.append(pid)
                    head.append(f"「{self._pname(pid)}」已加入会话")
            self.costars.set(umo, new)
            lines = head + lines
        elif action == "rm":
            removed = [p for p in cur if p in set(ids)]
            self.costars.set(umo, [p for p in cur if p not in set(ids)])
            lines = [f"「{self._pname(p)}」已离开会话" for p in removed] + lines
            if not removed:
                lines.insert(0, "这些角色本就不在会话中。")
        else:  # set
            keep = ids[:cap] if cap else list(ids)
            overflow = ids[cap:] if cap else []
            old = set(cur)
            added = [p for p in keep if p not in old]
            removed = [p for p in cur if p not in set(keep)]
            self.costars.set(umo, keep)
            lines = ([f"「{self._pname(p)}」已加入会话" for p in added]
                     + [f"「{self._pname(p)}」已离开会话" for p in removed]
                     + lines)
            if overflow:
                lines.append("已达上限 %d，未加入：%s"
                             % (cap, "、".join(self._pname(p) for p in overflow)))
        if not lines:
            lines.append("多人格同台已更新（无变化）。")
        lines.append(self._costar_view(
            umo, doc, mode_ok=self._costar_mode_ok(umo, is_private), cap=cap))
        return "\n".join(lines)

    def sync_config(self) -> dict:
        """把配置热同步到**每个实例**的子对象（WebUI 保存配置后调用）。

        绝大多数配置项在读取时走 `self._cfg()` 是天然实时的；只有少数在
        建 runtime 时被固化为子对象参数，这里逐个实例原地更新。
        """
        from .core.proactive import parse_quiet_hours
        applied: list[str] = []
        ids = list((getattr(self, "_inst_cfgs", {}) or {}).keys()) \
            or [self._default_inst]
        for iid in ids:
            try:
                rt = self._rt(iid)
            except Exception:
                continue

            def c(key, default=None, _i=iid):
                return self._cfg(key, default, inst=_i)

            n = self._num
            f = float
            try:
                a = rt.attention
                a.bubble_base = max(0.0, min(f(n(c("attention_bubble_base"), 0.03)), 1.0))
                a.active_cap = max(a.bubble_base, min(f(n(c("attention_active_cap"), 0.3)), 1.0))
                a.decay_minutes = max(0.1, f(n(c("attention_decay_minutes"), 3.0)))
                a.hard_trigger_boost = max(0.0, f(n(c("attention_hard_boost"), 0.1)))
                a.cool_down_seconds = max(0.0, f(n(c("attention_cooldown"), 20.0)))
                a.no_action_backoff = max(0.0, min(f(n(c("attention_backoff"), 0.5)), 1.0))
                a.backoff_floor = max(0.0, min(f(n(c("attention_backoff_floor"), 0.25)), 1.0))
                a.followup_boost = max(0.0, f(n(c("attention_followup_boost"), 0.15)))
                a.others_window_seconds = max(1.0, f(n(c("attention_others_window"), 60.0)))
                a.others_density_threshold = max(1, int(n(c("attention_others_limit"), 6)))
                applied.append(f"{iid}:attention")
            except Exception:
                pass
            try:
                p = rt.poke
                p.enabled = bool(c("poke_enabled", True))
                p.probability = max(0.0, min(f(n(c("poke_probability"), 1.0)), 1.0))
                p.cooldown = max(0.0, f(n(c("poke_cool_down_seconds"), 60.0)))
                p.only_self = bool(c("poke_only_self", True))
                p.reply_mode = str(c("poke_reply_mode", "llm") or "llm").strip().lower()
                p.text = str(c("poke_text", "别戳我啦~") or "")
                applied.append(f"{iid}:poke")
            except Exception:
                pass
            try:
                m = rt.merge
                m.enabled = bool(c("merge_pending_messages", True))
                m.window = max(0.5, f(n(c("merge_window_seconds"), 8.0)))
                m.notice = str(c("merge_notice", "") or "")
                applied.append(f"{iid}:merge")
            except Exception:
                pass
            try:
                t = rt.tts
                t.enabled = bool(c("enable_tts", False))
                t.only_private = bool(c("tts_only_private", True))
                t.max_chars = max(1, int(n(c("tts_max_chars"), 200)))
                pid = str(c("tts_provider", "") or "").strip()
                if pid != t.provider_id:
                    t.provider_id = pid
                    t._provider = None      # 换 provider 必须清缓存
                applied.append(f"{iid}:tts")
            except Exception:
                pass
            try:
                s = rt.scheduler
                s.min_interval = max(1, int(n(c("proactive_min_interval"), 45))) * 60
                s.idle_threshold = max(1, int(n(c("proactive_idle_minutes"), 90))) * 60
                s.quiet = parse_quiet_hours(str(c("proactive_quiet_hours", "") or ""))
                s.silent_max = max(0, int(n(c("proactive_silent_max"), 2)))
                s.apply_enabled(bool(c("enable_proactive", True)))
                applied.append(f"{iid}:scheduler")
            except Exception:
                pass
            try:
                rt.impressions.threshold = max(
                    1, int(n(c("impression_threshold"), 10)))
                applied.append(f"{iid}:impression")
            except Exception:
                pass
        try:
            self._inst_store.invalidate()
            applied.append("privilege")
        except Exception:
            pass
        return {"synced": applied}

    def _persona_ok(self, umo: str, doc) -> bool:
        """该人格是否已授权给 umo 所属实例（无池/无实例时放行）。"""
        inst = inst_of_umo(umo)
        pool = getattr(self, "_persona_pool", None)
        if pool is None or not inst or doc is None:
            return True
        return pool.is_available(inst, getattr(doc, "id", ""))

    def _persona_denied(self, umo: str) -> str:
        inst = inst_of_umo(umo)
        pool = getattr(self, "_persona_pool", None)
        names = []
        if pool is not None and inst:
            names = [d.display_name for d in
                     pool.available_docs(inst, self.personas.all())]
        return (f"❌ 该人格不在本实例（{inst}）的授权池内。\n"
                f"可用：{'、'.join(names) if names else '（空）'}\n"
                "管理员可到 WebUI「人格分配」把人格拖到该实例。")

    def _persona_for(self, umo: str) -> PersonaDoc | None:
        inst = inst_of_umo(umo)
        pool = getattr(self, "_persona_pool", None)

        def _ok(pid: str) -> bool:
            if not pid or pid not in self.personas:
                return False
            return pool is None or not inst or pool.is_available(inst, pid)

        sid = self._session_persona.get(umo)
        if _ok(sid):
            return self.personas.get(sid)
        if sid:
            logger.warning(
                f"[chatcore_neo] 会话 {umo} 记录的人格 {sid!r} 在实例 {inst} "
                f"未授权，按回退链处理")
        # 兜底①：状态目录里**记着这个会话用过哪个人格** —— 比笼统回退到
        # "第一个人格"准得多。旧实现直接 `personas.all()[0]`，导致
        # `_session_persona.json` 里缺条目的会话全部读到别人格的状态
        # （实测 7/18 会话、248 条日志不可见）。
        try:
            used = [p for p in self.state.personas_in(umo) if _ok(p)]
            if len(used) == 1:
                return self.personas.get(used[0])
            if used:
                g0 = str(self._cfg("persona_current", "") or "").strip()
                doc0 = self.personas.resolve(g0) if g0 else None
                if doc0 is not None and doc0.id in used:
                    return doc0          # 配置默认人格确实用过就用它
                return self.personas.get(sorted(used)[0])
        except Exception:
            pass
        g = str(self._cfg("persona_current", "") or "").strip()
        if g:
            doc = self.personas.resolve(g)
            if doc is not None and _ok(doc.id):
                return doc
        if pool is not None and inst:
            picked = pool.resolve_default(inst, self.personas.all(), "")
            if picked is not None:
                return picked
        docs = self.personas.all()
        return docs[0] if docs else None

    # ------------------------------------------------------------------ 昵称/群名
    def _note_identity(self, event: AstrMessageEvent) -> None:
        """把这条消息里白捡的「谁」和「哪个群」记进本地映射表。

        平台把这些信息**直接带在消息体里**，读一下不产生任何额外开销；
        管理页的会话标签与用户下拉全靠这两张表，否则页面上只剩一串 QQ 号。
        """
        try:
            uid = str(event.get_sender_id() or "").strip()
            name = str(event.get_sender_name() or "").strip()
            # 平台偶尔把 QQ 号当昵称返回（`10003` → `"10003"`）。
            # 那不是昵称，存下来只会让页面上出现「10003（10003）」。
            if name == uid:
                name = ""
            if uid and name and self._user_names.get(uid) != name:
                self._user_names.set(uid, name)
        except Exception:
            pass
        self._note_group_name(event)

    def _note_group_name(self, event: AstrMessageEvent) -> str:
        """记下群名。返回本次取到的群名（取不到返回空串）。

        AstrBot 把 OneBot 事件的 `group_name` 放进
        `message_obj.group.group_name`；**不是所有实现都带这个字段**，
        没带的会被适配器填成 `"N/A"` —— 那种值绝不能当成群名存下来，
        否则页面上会排出一串「N/A」。真取不到就留空，交给管理页的
        「补齐群名与昵称」走 `get_group_info`。
        """
        try:
            gid = str(event.get_group_id() or "").strip()
            if not gid:
                return ""
            umo = str(event.unified_msg_origin or "")
            if not umo:
                return ""
            grp = getattr(getattr(event, "message_obj", None), "group", None)
            name = str(getattr(grp, "group_name", "") or "").strip()
            if not name or name.upper() == "N/A" or name == gid:
                return ""
            if self._group_names.get(umo) != name:
                self._group_names.set(umo, name)
                logger.info(f"[chatcore_neo] 记下群名 {umo} → {name!r}")
            return name
        except Exception:
            return ""

    def _impression_uid(self, event: AstrMessageEvent, uid: str,
                        umo: str) -> str:
        """印象的存储键：**一律落到具体用户身上**。

        群聊的 `uid` 就是发送者；私聊的 `uid` 是空串（状态按会话级存），
        旧写法 `uid or umo` 于是把私聊印象写成了
        `_impressions/BotA_FriendMessage_10001.json` —— 既在管理页被
        当成"把 umo 当 uid"的垃圾文件，又让老插件留下的
        `_impressions/10001.json`（真印象）永远读不到。
        私聊里发送者就是对方，用 sender_id 作键才是对的
        （`/impression` 命令早就是 `sender_id or umo`，两边从此一致）。
        """
        if uid:
            return uid
        sid = self._sender_id(event)
        return sid or umo

    def _seed_names_from_templates(self) -> int:
        """用会话模板里存过的 `sender.nickname` 补齐还没见过的昵称。

        模板是**第一条**消息留下的，之后昵称表才逐条累积；补齐它能让
        那些"只在很久以前说过一句话"的用户在下拉框里也有名字。
        """
        n = 0
        try:
            for umo, tpl in self.templates.items():
                uid = str((tpl.get("sender") or {}).get("user_id") or "").strip()
                name = str((tpl.get("sender") or {}).get("nickname") or "").strip()
                if uid and name and not self._user_names.get(uid):
                    self._user_names.set(uid, name, persist=False)
                    n += 1
            if n:
                self._user_names.save()
        except Exception as e:
            logger.debug(f"[chatcore_neo] 从会话模板补齐昵称失败: {e!r}")
        return n

    def _import_legacy_names(self) -> dict:
        """一次性把**旧环境** lili_state 的昵称/群名缓存补进本地映射表。

        老插件的 `_name_cache.json` 里有 15 个群名 + 5805 条「成员 QQ→昵称」，
        新环境凭空造不出来 —— 平台的 `get_group_list` 只给群名，成员昵称还得
        逐群 `get_group_member_list`。这些数据没必要丢。

        三条纪律：
        * **只补缺，绝不覆盖**。旧缓存是 2026-09-02 的快照，比实时采集旧；
          群改名了（如「…丨开学快乐」→「…丨梁文谷」）必须以新的为准。
        * **每次加载都补**，不做"导过了就跳过"的标记。曾经按源文件签名去重，
          结果踩了个坑：这两张表在运行期是**整份内存快照回写**的
          （`_note_identity` 一有变化就 `save()`），离线导入的几千条会被
          在线实例的旧内存版本覆盖；签名却已经写下，于是插件重载时判定
          "导过了"直接跳过，数据就永远回不来了。补缺是幂等的，
          220KB 解析约 10ms，不值得为省这点去冒风险。
        * **fail-open**：旧环境不在、文件坏了，都只是跳过，不影响启动。
        """
        out = {"groups": 0, "users": 0, "skipped": False, "src": ""}
        try:
            src = paths.LEGACY_NAME_CACHE
            if not src.is_file():
                out["skipped"] = True
                return out
            raw = json.loads(src.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                out["skipped"] = True
                return out
            gm_up: dict[str, str] = {}
            un_up: dict[str, str] = {}
            for key, val in raw.items():
                if not isinstance(key, str) or not isinstance(val, list) or not val:
                    continue
                name = val[0]
                if key.startswith("g:") and isinstance(name, str):
                    # `g:实例:群号` → umo
                    parts = key[2:].split(":", 1)
                    if len(parts) == 2 and name.strip():
                        umo = f"{parts[0]}:GroupMessage:{parts[1]}"
                        if not self._group_names.get(umo):
                            gm_up[umo] = name.strip()
                elif key.startswith("ml:") and isinstance(name, dict):
                    for uid, nm in name.items():
                        u, n = str(uid).strip(), str(nm or "").strip()
                        if u and n and n != u and not self._user_names.get(u):
                            un_up[u] = n
                elif key.startswith("u:") and isinstance(name, str):
                    parts = key[2:].split(":", 1)
                    n = name.strip()
                    if (len(parts) == 2 and n and n != parts[1]
                            and not self._user_names.get(parts[1])):
                        un_up[parts[1]] = n
            if gm_up:
                out["groups"] = self._group_names.update_many(gm_up)
            if un_up:
                out["users"] = self._user_names.update_many(un_up)
            out["src"] = str(src)
            if out["groups"] or out["users"]:
                logger.info(
                    f"[chatcore_neo] 已从旧环境补进昵称/群名：群名 {out['groups']}、"
                    f"用户 {out['users']}（只补缺，不覆盖）源={src.name}")
            else:
                out["skipped"] = True
        except Exception as e:
            logger.warning(f"[chatcore_neo] 导入旧环境昵称缓存失败（忽略）: {e!r}")
        return out

    # ------------------------------------------------------------------ 事件骨架
    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def on_any_message(self, event: AstrMessageEvent):
        """记录事件模板，供日后合成主动事件使用。

        参考实现在"该会话从未收到过消息"时只能回退旧路径；
        这里把骨架持久化，冷启动也能重入管道（方案 3.6-2）。
        """
        if compat.is_gutoken(event):
            return
        # 实例绑定：本事件后续所有 _cfg / memory 调用都按这个实例走
        try:
            self.set_inst(str(event.unified_msg_origin or ""))
        except Exception:
            pass
        # 流式传输必须**在 LLM 请求之前**就位：框架在 AgentRequestSubStage
        # 里读一次 `enable_streaming`（internal.py:171），读完再置就晚了。
        # 这个 Handler 挂在 ProcessStage → StarRequestSubStage（先于 LLM 请求），
        # 正好卡在那个读点之前。`on_llm_request` 里还会再兜一次（补装包装）。
        try:
            self._stream_prepare(event)
        except Exception as e:
            logger.debug(f"[chatcore_neo] 流式准备失败（按非流式继续）: {e!r}")
        # 每条消息进来顺手刷新「AstrBot 侧会话存活集」：被误判为孤儿的会话
        # 一旦重新开口，DB 会立刻出现新的对话记录，孤儿判定随即失效。
        try:
            self._kick_alive_refresh()
        except Exception:
            pass
        # 顺手记下「uid → 昵称」「群 umo → 群名」（管理页下拉框/会话标签用）。
        # 只在真的变了的时候写盘，平时零 IO 开销。
        try:
            self._note_identity(event)
        except Exception as e:
            logger.debug(f"[chatcore_neo] 记录昵称/群名失败: {e!r}")
        try:
            self.templates.ensure_from_event(event)
            self.templates.save()
        except Exception as e:
            logger.debug(f"[chatcore_neo] 记录事件模板失败: {e!r}")
        # 撤回取消：OneBot 的 group_recall / friend_recall 通知
        try:
            if self._handle_recall(event):
                return
        except Exception as e:
            logger.debug(f"[chatcore_neo] 撤回处理失败: {e!r}")

        # 戳一戳：独立通知类型，按概率回应
        try:
            # `not is_simulated`：戳一戳 llm 模式回应的方式，就是造一条
            # 正文为 `POKE_NOTICE` 的合成事件重入管道 —— 而 `is_poke_event`
            # 的文本兜底恰好认这个字符串，于是**合成事件会被再判成一次戳一戳**：
            # 一次戳让 `should_reply` 被调用两次（seen 翻倍），并且
            # `poke_cool_down_seconds=0` 时会无限套娃。合成事件不可能是
            # 真实用户戳的，直接排除。
            if (is_poke_event(event) and not compat.is_simulated(event)
                    and self._cfg("enabled", True)):
                umo = event.unified_msg_origin
                # target_is_self 必须真的去读被戳对象 ——
                # 旧实现硬编码 True，导致 poke_only_self 两个取值行为完全相同
                target = poke_target(event)
                self_id = ""
                try:
                    self_id = str(event.get_self_id() or "")
                except Exception:
                    pass
                # 取不到目标时按"戳的是自己"处理（宽松），否则按实际比较
                target_is_self = (not target) or (not self_id) or (target == self_id)
                if self.poke.should_reply(umo, target_is_self=target_is_self):
                    self._spawn(self._reply_poke(event, umo))
                return
        except Exception as e:
            logger.debug(f"[chatcore_neo] 戳一戳处理失败: {e!r}")

        # 连发合并计数
        try:
            if not compat.is_simulated(event):
                self.merge.note(event.unified_msg_origin, event.message_str or "")
        except Exception:
            pass

        # ── 叫醒词分流（call_prefix）──
        # 以叫醒词开头的消息：剥掉词、叫醒进 LLM 链路，但**不触发指令**。
        # 必须放在注意力判定之前：叫醒词是"用户明确在跟机器人说话"，
        # 不该再掷一次骰子（见 `_maybe_call_prefix` 的文档）。
        try:
            self._maybe_call_prefix(event)
        except Exception as e:
            logger.warning(f"[chatcore_neo] 叫醒词处理异常（忽略，按普通消息继续）: {e!r}")

        # ── 注意力触发（软触发）──
        # 普通群消息走不到 on_llm_request，判定必须在这里做；
        # 命中就把消息叫醒（见 `_maybe_attention_wake` 的文档）。
        try:
            self._maybe_attention_wake(event)
        except Exception as e:
            logger.warning(f"[chatcore_neo] 注意力判定异常（忽略，按不接话处理）: {e!r}")

        # 记录用户活动时间，主动消息据此判断"多久没说话了"。
        # **合成事件不算用户说话** —— 否则"戳一戳回应 / 主动开口"自己会把
        # 空闲计时器清零，页面上看到的空闲时间永远是 0，
        # 而且主动消息的 idle 判据会被自己的上一句话重置。
        if not compat.is_simulated(event):
            try:
                doc = self._persona_for(event.unified_msg_origin)
                self.scheduler.note_user_message(
                    event.unified_msg_origin, doc.id if doc else "")
            except Exception:
                pass
            # 群聊发言流水（带说话人，修复"认错人"）
            try:
                self._record_live(event)
            except Exception:
                pass

    # ------------------------------------------------- 命令闸门（原 hook2optimize）
    @filter.on_llm_request(priority=95)
    async def on_llm_request_gate(self, event: AstrMessageEvent,
                                  req: ProviderRequest):
        """群聊里「被唤醒词唤醒、但没 @ 也没引用机器人」的消息**不触发 LLM**。

        对应旧 `chatcore_lite/main.py:581-650` 的 `hook2optimize`：
        用户打了 `/` 开头却**没命中任何命令**时（打错字、命令不存在），
        不该白烧一次 LLM 请求。

        判定三条同时成立才拦：
          ① 群聊（私聊一律放行）
          ② 没 @ 机器人、也没引用机器人的消息
          ③ **是被唤醒前缀唤醒的**

        ③ 用 `is_at_or_wake_command` 而不是 `is_wake`：
        框架在插件 handler 过滤器通过时也会置 `is_wake=True`
        （`waking_check/stage.py:223-224`），而本插件自己就注册了
        `EventMessageType.ALL` 的 `on_any_message` —— 于是**每条群消息**
        都会 `is_wake=True`。拿它当判据会把普通聊天也全拦掉
        （实测：普通聊天 `llm_request +0 / blocked_llm +1`），
        机器人会在群里彻底变哑巴。
        `is_at_or_wake_command` 只在「前缀 / @bot / 引用bot / 私聊」四处置位
        （`stage.py:121 / 142 / 151`），handler 路径不置位 —— 正是我们要的。

        **命中命令的情况不会走到这里** —— 命令处理器会先停止事件。
        """
        try:
            self.set_inst(str(event.unified_msg_origin or ""))
        except Exception:
            pass
        if not self._cfg("enabled", True):
            return
        if not bool(self._cfg("block_unmatched_command", True)):
            return
        if compat.should_skip_side_effects(event):
            return
        try:
            if event.is_private_chat():
                return
            if event.get_extra(X_ATTN_WAKE, False):
                # 注意力主动唤醒的消息：`on_any_message` 已经掷过骰子并命中，
                # 闸门不能再按"未命中命令的唤醒消息"把它拦掉
                # （旧 chatcore_lite 用 `_ALLOW_KEY` 解决同一问题）。
                return
            if event.get_extra(X_CALL_PREFIX, False):
                # 叫醒词（call_prefix）消息：用户明确在跟机器人说话，
                # 剥词后**故意不触发指令**，本来就是要进 LLM 的。
                # 不排除就会被下面第 ③ 条当成"未命中命令的唤醒消息"拦掉。
                return
            if self._bot_at_mentioned(event) or self._bot_quoted(event):
                return                      # 明确在叫机器人 → 放行
            if compat.is_simulated(event):
                # **合成事件**（戳一戳回应 / 主动消息重入管道）是我们自己造的一轮，
                # 不是"用户打了前缀却没命中命令"。`synthesize_event` 必然把
                # `is_at_or_wake_command` 置真，所以不排除就会在群里被下面第 ③ 条拦掉。
                #
                # 线上实录（戳一戳 llm 模式，群里）：
                #   戳一戳已重入管道 umo='...:GroupMessage:10005' 成功=True
                #   闸门拦截：唤醒前缀唤醒，未 @/引用机器人　文本='（对方戳了你一下）'
                # 私聊因为第 ① 条早就放行了，所以**只在群里复现**。
                return
            blocked = ""
            if bool(getattr(event, "is_at_or_wake_command", False)):
                # 被唤醒前缀唤醒（@/引用的已在上面放行）
                blocked = "唤醒前缀唤醒，未 @/引用机器人"
            else:
                # 兜底：万一框架没置位，就看原文（前缀可能已被框架剥掉）
                raw = (event.message_str or "").lstrip()
                for p in (self._wake_prefixes() or ["/"]):
                    if raw.startswith(p):
                        blocked = f"以唤醒前缀「{p}」开头，未 @/引用机器人"
                        break
            if not blocked:
                return
            logger.info(f"[chatcore_neo] 闸门拦截：{blocked}"
                        f"　文本={(event.message_str or '')[:40]!r}")
            try:
                self._stats["blocked_llm"] = self._stats.get("blocked_llm", 0) + 1
            except Exception:
                pass
            event.stop_event()
        except Exception as e:
            logger.debug(f"[chatcore_neo] 命令闸门异常（放行）: {e!r}")

    # ------------------------------------------------------ 管道耗时探针
    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    async def on_trace_entry(self, event: AstrMessageEvent):
        """最先执行的钩子：记下事件进入管道的时刻。

        **必须挂在事件自己身上**，不能按 UMO 存字典：群里多条消息共用同一个
        `unified_msg_origin`，后到的会把先到的覆盖掉，探针就永远只量到最新那条
        （实测因此一次都没告警过）。
        """
        try:
            event.set_extra(_TRACE_KEY, time.time())
        except Exception:
            pass

    @staticmethod
    def _recv_ts(event: AstrMessageEvent) -> float:
        """事件自带的**收信时刻**（适配器写入，见 aiocqhttp 适配器）。

        有它就能算出真实端到端延迟，**不必再去配对日志时间戳** ——
        靠配对推算已经被证明很容易得出假数据。
        """
        try:
            ts = getattr(getattr(event, "message_obj", None), "timestamp", None)
            if ts:
                return float(ts)
        except Exception:
            pass
        return 0.0

    def _trace_settle(self, event: AstrMessageEvent, stage: str) -> None:
        """结算并打印超过阈值的耗时。

        打两个数：
          * `total` = 收信 → 此刻（用户实际等了多久）
          * `inner` = 进入管道（唤醒检查之后）→ 此刻（我这边的链路）
        `total - inner` 就是唤醒检查 + 排队的时间。
        """
        now = time.time()
        try:
            t_in = event.get_extra(_TRACE_KEY, None)
        except Exception:
            t_in = None
        inner = (now - float(t_in)) if t_in else -1.0
        recv = self._recv_ts(event)
        total = (now - recv) if recv else -1.0

        thr = float(self._cfg("pipeline_trace_seconds", 8.0) or 8.0)
        if total < thr and inner < thr:
            return
        # 注意：**必须用 warning**。实测本插件里的 logger.info 不落盘、
        # warning 才落盘 —— 这一点让我今天几次"加了日志却看不到"。
        logger.warning(
            f"[chatcore_neo] 到「{stage}」耗时：收信起 {total:.1f}s / "
            f"入管道起 {inner:.1f}s（差 {max(0.0, total - inner):.1f}s "
            f"是唤醒检查与排队）：{(event.message_str or '')[:40]!r}")

    # ------------------------------------------------------------------ 注入
    @filter.on_llm_request(priority=89)
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self._cfg("enabled", True):
            return
        if compat.should_skip_side_effects(event):
            return
        # 流式开关由 on_any_message 置（必须在 LLM 请求之前）；这里只补装
        # `send_streaming` 包装 —— 万一那条路径提前返回漏装，流式结果会退回
        # 框架自己的硬切发送，插件的标记/分段规则就全丢了。
        try:
            self._stream_prepare(event)
        except Exception as e:
            logger.debug(f"[chatcore_neo] 流式接管失败（按框架默认发送）: {e!r}")
        self._stats["llm_request"] += 1
        self._trace_settle(event, "LLM 请求")
        try:
            umo = event.unified_msg_origin
            doc = self._persona_for(umo)
            if doc is None:
                logger.warning(
                    f"[chatcore_neo] 该会话没匹配到人格，已放弃回复: umo={umo!r}"
                    f"　已载入人格={len(self.personas)}"
                    f"　映射={self._session_persona.get(umo)!r}"
                    f"　会话数={len(self._session_persona)}")
                return
            is_private = event.is_private_chat()
            msg_mode = int(self._cfg("private_msg_mode", 1) or 1)
            # 会话级发言参数：显式覆盖 > 按会话类型默认（私聊=全局mdot模式/开NSFW，群聊=纯信息/关NSFW）
            eff_mode, eff_nsfw = self._resolve_session_params(umo, is_private)
            # 合成事件（主动消息/戳一戳回应）是 bot 自己发起的：不主动开车，
            # 强制关闭 NSFW；发言格式仍跟会话走（剧情向/纯信息）。
            # **例外见 `_nsfw_suppressed`**：persona_switch 的重入是"用户对话的
            # 延续"，NSFW 必须跟会话参数走，否则切换那一轮会莫名丢掉露骨许可。
            proactive = compat.is_simulated(event)
            if self._nsfw_suppressed(event) and self._cfg("enabled", True):
                eff_nsfw = False

            # 解析本轮说话人：群聊用发送者 uid，私聊用会话对象
            uid = self._sender_id(event) if not is_private else ""
            # 特权按**会话**解析：分会话覆盖（`BotB`→朋友的号）优先于全局
            tier = self.privilege.tier(uid, scope=umo) if uid else "intimate"
            tier_label = doc.world_role or ""

            st = self.state.load(
                umo, doc.id, uid,
                default_affection=self._default_affection(
                    is_private=is_private, uid=uid, umo=umo))
            msg = event.message_str or ""

            # ⓪ 群聊接话决策：注意力 + 不回复判定 + 隐性意图
            #    私聊、@、指令属"硬触发"，一律直接回复（不浪费判定调用）
            if not is_private and not compat.is_simulated(event):
                decision = await self._decide_reply(event, umo, uid, doc, msg)
                if not decision:
                    logger.info(
                        "[chatcore_neo] 决策=不回复"
                        f"　umo={umo!r}"
                        f"　hard={self._is_hard_trigger(event)}"
                        f"　force={bool(self._cfg('attention_hard_trigger_force', True))}"
                        f"　is_wake={getattr(event, 'is_wake', None)}"
                        f"　is_at_or_wake_command="
                        f"{getattr(event, 'is_at_or_wake_command', None)}"
                        f"　self_id={event.get_self_id()!r}"
                        f"　comps={self._describe_components(event)}"
                        f"　msg={(event.message_str or '')[:40]!r}")
                    event.stop_event()
                    return

            # ① 幂等：先剔除自己上一轮的注入
            req.system_prompt = self.assembler.strip_previous(
                req.system_prompt or "")

            # ② 剥离被取代的工具结果（flag 管不到 role="tool"）
            keep = max(0, int(self._cfg("tool_result_keep_rounds", 1) or 0))
            before = self._tool_bytes(req.contexts)
            req.contexts = strip_tool_results(req.contexts, keep_recent=keep)
            after = self._tool_bytes(req.contexts)
            if after < before:
                self._stats["stripped"] += 1

            # ②.5 剥掉历史里沉积的 <system_reminder>（框架的 User ID/时间戳
            # 与 group_chat_context 的群聊流水块都随消息落库且永不清理，
            # 实测占整个提示词的 17%）。当轮消息的 reminder 走
            # extra_user_content_parts，不在这里，不受影响。
            # 改写口径：群聊 → 说话人标签；私聊 → 丢身份、留时间。
            if bool(self._cfg("strip_history_reminders", True)):
                try:
                    req.contexts, _rem = strip_system_reminders(
                        req.contexts, private=is_private)
                    if _rem > 0:
                        logger.info(
                            f"[chatcore_neo] 已剥离历史 reminder {_rem} 字符"
                            f"　umo={umo!r}")
                except Exception as e:
                    logger.debug(f"[chatcore_neo] 剥离历史 reminder 失败: {e!r}")

            # ③ 引用回溯 + 连发合并提示
            quote_block = ""
            if bool(self._cfg("enable_quote", True)):
                try:
                    qi = extract_reply_target(
                        event, max_chars=int(self._cfg("quote_max_chars", 400) or 400))
                    quote_block = build_quote_block(qi)
                except Exception as e:
                    logger.debug(f"[chatcore_neo] 引用回溯失败: {e!r}")
            merge_notice = ""
            try:
                merge_notice = self.merge.take_notice(umo)
            except Exception:
                pass

            # 记忆召回（embedding 不可用时返回空列表，不抛异常）
            memories: list[str] = []
            mem_key = self.groups.group_of(umo)
            if self.memory.available:
                _q = event.message_str or ""
                logger.info(
                    f"[chatcore_neo] 记忆召回·请求 umo={umo!r} key={mem_key!r} "
                    f"persona={doc.id!r} uid={uid!r} 查询={_q[:50]!r}")
                _t0 = time.time()
                try:
                    memories = await self.memory.recall_texts(
                        mem_key, _q, persona=doc.id, uid=uid,
                        persona_labels=self._persona_labels(),
                        policy=int(self._cfg("memory_persona_policy", 0) or 0))
                    _ms = (time.time() - _t0) * 1000
                    logger.info(
                        f"[chatcore_neo] 记忆召回·结果 命中 {len(memories)} 条 "
                        f"耗时 {_ms:.0f}ms"
                        + (f" 首条={memories[0][:60]!r}" if memories else "（无命中）"))
                except Exception as e:
                    logger.warning(
                        f"[chatcore_neo] 记忆召回·失败 umo={umo!r} key={mem_key!r} "
                        f"耗时 {(time.time()-_t0)*1000:.0f}ms 错误={e!r}")
            else:
                logger.info(
                    f"[chatcore_neo] 记忆召回·跳过（embedding 不可用）umo={umo!r} "
                    f"key={mem_key!r}")

            # ③.5 @ / 引用协议：把**本轮真实可用的 id** 告诉模型。
            # 之前完全没教过它，于是模型自己发明 `[@昵称]` —— 输出层只认
            # `[[at:QQ号]]`，结果就是既没 @ 成，还把 `[@江一楠 这条呀` 这种
            # 坏文本原样发给了用户。
            #
            # **合成事件（主动消息）没有"本轮说话人"**：它的 sender 是从事件
            # 模板里抄来的（= 该会话历史上第一个说话的人，且模板只写一次、
            # 此后不再更新）。拿它当 @ 目标，就变成"每一条主动消息都 @ 同一个人"
            # —— 群友看到的就是莫名其妙被反复点名。没有真实说话人时把目标留空，
            # `build_marker_block` 会明确告诉模型"本轮没有可用的 @ / 引用目标"。
            _uname = event.get_sender_name() or ""
            _at_uid = "" if proactive else uid
            _mk_uname = "" if proactive else _uname
            # 引用目标 id 必须取**本轮**消息：`_active_triggers[umo]` 要等到本函数
            # 末尾（撤回取消登记点）才更新为本轮 id，这里读到的永远是上一轮的，
            # 模型就会被误导去 `[[reply:上一条]]`（日志实测 reply 偏移早一条）。
            _mid = ""
            if not proactive:
                try:
                    _mid = str(getattr(getattr(event, "message_obj", None),
                                       "message_id", "") or "")
                except Exception:
                    _mid = ""
            marker_block = ""
            if bool(self._cfg("enable_markers", True)):
                try:
                    marker_block = build_marker_block(
                        uid=_at_uid, user_name=_mk_uname, message_id=_mid,
                        at_enabled=bool(self._cfg("enable_markers", True)),
                        reply_enabled=bool(self._cfg("enable_markers", True)))
                except Exception as e:
                    logger.debug(f"[chatcore_neo] 标记协议段生成失败: {e!r}")

            # ④ 装配
            imp_uid = self._impression_uid(event, uid, umo)
            event.set_extra(X_IMP_UID, imp_uid)
            # 人格切换提醒：一次性消费，注入动态段的"系统通知"。
            # `/persona` 是真实指令（非合成事件）才会写入这个槽，所以
            # 无论本轮回合是普通消息还是该会话的主动消息，消费到都成立。
            _switch_notice = self._persona_switch_notice.pop(umo, "")
            _identity_ok = self._identity_block_ok(event)
            # 多人格同台：**仅剧情模式**下注入；否则静默（配置保留，切回即恢复）。
            costar_block = ""
            if eff_mode == MODE_NARRATIVE:
                costar_block = self._costar_block(
                    umo, doc, is_private=is_private, msg_mode=eff_mode)
            inp = AssemblyInput(
                doc=doc, state=st, tier=tier, is_private=is_private,
                msg_mode=eff_mode, uid=uid,
                user_name=_uname,
                marker_block=marker_block,
                user_msg=msg,
                impression=self.state.impression_text(
                    imp_uid, nsfw=bool(self._cfg("enable_nsfw_impression", True))),
                memories=memories,
                quote_block=quote_block,
                merge_notice=merge_notice,
                include_dialog=bool(self._cfg("include_dialog", False)),
                tier_label=tier_label,
                cfg=self.config,
                unrestricted=self._unrestricted(umo),
                identity_confirm=_identity_ok,
                nsfw_enabled=eff_nsfw,
                proactive=proactive,
                live_speakers=self._live_text(umo, uid, msg) if not is_private else "",
                empty_user_text=not str(msg or "").strip(),
                characters=self._character_block(msg, doc),
                costars=costar_block,
                notices=[_switch_notice] if _switch_notice else [],
            )
            result = self.assembler.build(inp)

            if result.system_suffix:
                req.system_prompt = (req.system_prompt or "") + result.system_suffix
                self._stats["injected"] += 1
            if result.dynamic_text:
                self._append_dynamic(req, result.dynamic_text)

            # 图片转述：框架在 build_main_agent 阶段就把 `<image_caption>` 注入了
            # `extra_user_content_parts`，但它自己只打 DEBUG，日志里看不见结果。
            # 这里把转述原文捞出来打 INFO，用于确认转述是否真的生效。
            self._log_image_caption(req, umo)

            # 请求主代理（注入完成、真正发出去之前的最后一站）
            try:
                logger.info(
                    f"[chatcore_neo] 请求主代理 umo={umo!r} persona={doc.id!r} "
                    f"uid={uid!r} 消息={msg[:50]!r} "
                    f"system_prompt={len(req.system_prompt or '')}字符 "
                    f"上下文={len(req.contexts or [])}条 "
                    f"召回记忆={len(memories)}条 "
                    f"引用块={'有' if quote_block else '无'} "
                    f"合并提示={'有' if merge_notice else '无'} "
                    f"状态=好感{st.affection}/淫乱{st.lewdness}/"
                    f"恶堕{st.depravity}/情绪{st.emotion}")
            except Exception as _e:
                logger.debug(f"[chatcore_neo] 主代理日志失败: {_e!r}")

            # ⑤ 把本轮上下文传给 on_llm_response
            event.set_extra(X_UMO, umo)
            event.set_extra(X_PERSONA, doc.id)
            event.set_extra(X_UID, uid)
            event.set_extra(X_TIER, tier)
            event.set_extra(X_USER_MSG, event.message_str or "")
            # 登记本轮触发消息，供撤回取消匹配
            try:
                mid = str(getattr(getattr(event, "message_obj", None),
                                  "message_id", "") or "")
                if mid:
                    self._active_triggers[umo] = mid
            except Exception:
                pass
            # 结巴是一次性的：注入时可能已置位，这里确保落盘
            if st.stutter_done:
                try:
                    self.state.save(umo, doc.id, uid, st)
                except Exception:
                    pass
            event.set_extra(X_INJECTED, True)
            self._last_parts = result.parts
        except Exception as e:
            self._stats["errors"] += 1
            self._last_error = repr(e)
            logger.warning(f"[chatcore_neo] 注入失败: {e!r}")

    @staticmethod
    def _sender_id(event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_id() or "")
        except Exception:
            return ""

    @staticmethod
    def _tool_bytes(contexts) -> int:
        if not isinstance(contexts, list):
            return 0
        n = 0
        for ctx in contexts:
            if isinstance(ctx, dict) and ctx.get("role") == "tool":
                c = ctx.get("content")
                n += len(c.encode("utf-8")) if isinstance(c, str) else 0
        return n

    @staticmethod
    def _append_dynamic(req: ProviderRequest, text: str) -> None:
        """动态内容一律走 extra_user_content_parts + mark_as_temp()。

        不写 `req.prompt`：那会把注入烘焙进 user 消息并持久化累积
        （旧实现的血泪注释，lili_state/main.py:1906-1912）。
        """
        from astrbot.core.agent.message import TextPart
        parts = req.extra_user_content_parts
        if parts is None:
            parts = []
            req.extra_user_content_parts = parts
        parts.append(TextPart(text=text).mark_as_temp())

    @staticmethod
    def _extract_image_captions(req: ProviderRequest) -> list[str]:
        """捞出框架注入的 `<image_caption>` 原文（可能有多个）。

        框架侧只有 `logger.debug`，全局 INFO 级别时完全不可见，所以这里
        主动读 `extra_user_content_parts`——只读不写，不违反注入纪律。
        """
        out: list[str] = []
        for part in (req.extra_user_content_parts or []):
            text = getattr(part, "text", None)
            if not isinstance(text, str) or "<image_caption>" not in text:
                continue
            for seg in text.split("<image_caption>")[1:]:
                body = seg.split("</image_caption>")[0].strip()
                if body:
                    out.append(body)
        return out

    def _log_image_caption(self, req: ProviderRequest, umo: str) -> None:
        """把图片转述模型的输出打出来；有图却没转述时给一条告警。"""
        try:
            caps = self._extract_image_captions(req)
        except Exception as e:
            logger.debug(f"[chatcore_neo] 读取图片转述失败: {e!r}")
            return
        if caps:
            for i, cap in enumerate(caps, 1):
                logger.info(
                    f"[chatcore_neo] 图片转述·结果 umo={umo!r} "
                    f"第{i}/{len(caps)}张 {len(cap)}字：{cap}")
            return
        try:
            marks = [
                p for p in (req.extra_user_content_parts or [])
                if isinstance(getattr(p, "text", None), str)
                and "Image Attachment" in p.text
            ]
        except Exception:
            marks = []
        if marks:
            logger.warning(
                f"[chatcore_neo] 本轮含图片但未收到转述（框架未注入 "
                f"<image_caption>）：umo={umo!r} 图片标记={len(marks)} 条")

    # ------------------------------------------------------------------ 状态更新
    @filter.on_llm_response(priority=90)
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        if not self._cfg("enabled", True):
            return
        if compat.should_skip_side_effects(event):
            return
        if not event.get_extra(X_INJECTED, False):
            return
        try:
            umo = event.get_extra(X_UMO)
            pid = event.get_extra(X_PERSONA)
            uid = event.get_extra(X_UID, "")
            user_msg = event.get_extra(X_USER_MSG, "")
            if not umo or not pid:
                return
            st = self.state.load(umo, pid, uid)
            st.update(resp.completion_text or "", user_msg,
                      enable_affection=bool(self._cfg("enable_affection", True)),
                      enable_lewdness=bool(self._cfg("enable_lewdness", True)))
            # 主动消息/戳一戳回应是**合成事件**：没有真实用户发言。
            # 历史里只留 bot 的回复（append_assistant），绝不把
            # SIMULATED_USER_TEXT 当成用户消息写进账号（污染状态/记忆），
            # 印象 msg_count 也只计真实用户轮。
            simulated = compat.is_simulated(event)
            # 印象的 uid 与轮数**只对真实用户轮有意义**，所以赋值在下面的
            # `if not simulated:` 里。但末尾的印象钩子是**无条件读**这两个名字的
            # —— 合成事件（主动消息 / 戳一戳回应）会跳过赋值那一支，于是读到
            # 未绑定的局部名，抛
            #     UnboundLocalError: cannot access local variable 'imp_count'
            # 每发一条主动消息就踩一次（2026-09-16 线上「最近错误」）。
            # 这里先给默认值，把"合成事件不推进印象"这件事表达成数据而不是分支。
            imp_uid = ""
            imp_count = 0
            if not simulated:
                # 印象轮数计数（存储里持久化，重启不丢）
                imp_uid = (event.get_extra(X_IMP_UID, "")
                           or self._impression_uid(event, uid, umo))
                try:
                    imp = self.state.load_impression(imp_uid)
                    imp["msg_count"] = int(imp.get("msg_count", 0) or 0) + 1
                    self.state.save_impression(imp_uid, imp)
                    imp_count = imp["msg_count"]
                except Exception:
                    imp_count = 0

            if self._cfg("save_conversation_log", True):
                if not simulated:
                    st.append_user(self._store_user_msg(user_msg), uid)
                if self._cfg("save_bot_state_to_history", True):
                    st.append_assistant(
                        self._thought(st, resp.completion_text or ""),
                        content=resp.completion_text or "")
                # `0` 是合法值（0 = 不限 / 0 = 不按时间窗保留），
                # 用 `or` 兜底会把它换成默认值 → schema 写的"0=不限"永远做不到
                st.groom(
                    _int_or(self._cfg("max_history_count"), 30),
                    _int_or(self._cfg("history_timeout_seconds"), 600))
            self.state.save(umo, pid, uid, st)
            self._stats["state_updates"] += 1
            try:
                self.attention.record_reply(umo)
            except Exception:
                pass

            if bool(self._cfg("debug", False)):
                logger.info(f"[chatcore_neo] {st.snapshot()}")

            # 记忆写入放后台任务，不阻塞本轮回复；合成事件无真实用户消息，跳过
            if self.memory.available and user_msg.strip() and not simulated:
                self._spawn(self._remember(
                    umo, pid, uid, user_msg, resp.completion_text or "",
                    event.get_sender_name() or ""))

            # 印象总结同样后台化（到轮数阈值才真的跑）。
            # 合成事件（主动消息/戳一戳回应）没有真实用户发言，`imp_count`
            # 恒为 0，不会在这里触发总结 —— 也就不会给它算印象轮数。
            _doc = self.personas.get(pid)
            if _doc is not None and imp_count:
                self._spawn(self.impressions.maybe_update(
                    umo, imp_uid, _doc.display_name,
                    st.conversation_log, imp_count))
        except Exception as e:
            self._stats["errors"] += 1
            self._last_error = repr(e)
            logger.warning(f"[chatcore_neo] 状态更新失败: {e!r}")

    def _spawn(self, coro) -> None:
        """后台任务统一入口：留下引用，避免被 GC 提前回收。"""
        try:
            task = asyncio.create_task(coro)
        except RuntimeError:
            return
        self._mem_tasks.add(task)

        def _done(t) -> None:
            self._mem_tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                # 旧写法只 discard：异常要等 GC 时由 asyncio 无上下文地打一句
                # "Task exception was never retrieved"，定位不到是哪个会话。
                try:
                    self._stats["errors"] += 1
                    self._last_error = repr(exc)
                except Exception:
                    pass
                logger.warning(f"[chatcore_neo] 后台任务异常: {exc!r}")

        task.add_done_callback(_done)

    def _persona_labels(self) -> dict[str, str]:
        """pid → 显示名，供记忆召回标注"来源人格"。"""
        try:
            return {d.id: d.display_name for d in self.personas.all()}
        except Exception:
            return {}

    def _persona_desc(self, pid: str) -> str:
        """构造总结代理用的角色身份描述。

        第一人称记忆的前提是总结代理知道"我"是谁：这里给出角色名、
        身份与一小段人设摘录；找不到人格时返回空串（退化通用口吻）。
        """
        try:
            d = self.personas.get(pid)
        except Exception:
            return ""
        if d is None:
            return ""
        parts = [f"你扮演的角色是「{d.display_name}」"]
        if d.world_role:
            parts.append(f"身份：{d.world_role}")
        body = (d.skill_body or "").strip()
        if body:
            parts.append("角色人设摘录（帮助把握口吻）：\n" + body[:300])
        return "\n".join(parts)

    async def _remember(self, umo: str, pid: str, uid: str,
                        user_msg: str, reply: str,
                        user_name: str = "") -> None:
        """把一轮对话写进记忆。用独立流程，绝不碰主对话的 req。

        记忆按**记忆组键**写入，因此归入同一组的多个会话共享记忆。
        """
        try:
            key = self.groups.group_of(umo)
            self.memory.note_turn(key, user_msg, reply, persona=pid, uid=uid,
                                  user_name=user_name)
            # 攒够一个窗口才固化：固化会调 LLM 总结 + 抽取图谱 + 给条目补向量，
            # 不能每轮都做；按人格计数，避免把切换前他人格的对话混进本轮记忆
            if self.memory.should_consolidate(key, persona=pid):
                r = await self.memory.consolidate(
                    key, session_id=key, persona_id=pid, umo=umo,
                    persona_desc=self._persona_desc(pid))
                if r:
                    logger.info(
                        f"[chatcore_neo] 记忆固化: {r['facts']} 条事实 / "
                        f"{len(r['topics'])} 个话题"
                        + ("（降级：总结不可用）" if r.get("degraded") else ""))
        except Exception as e:
            logger.debug(f"[chatcore_neo] 记忆写入失败: {e!r}")

    def _thought(self, st, reply: str) -> str:
        """内心活动文本。`bot_thought_mode` 控制详略。"""
        from .core.state.model import build_thought
        short = (reply or "").strip().replace("\n", " ")[:40]
        # 三档与 WebUI 下拉选项一致：内心想法 / 简短 / 具体
        mode = str(self._cfg("bot_thought_mode", "简短") or "简短")
        if mode == "简短":
            return short or build_thought(st)
        if mode in ("具体", "详细"):      # 详细=旧写法，兼容
            return f"{build_thought(st)} | 回复「{short}」 | {st.snapshot()}"
        # 「内心想法」：自然语言感受，最省 token
        return f"{build_thought(st)} | 回复「{short}」"

    def _store_user_msg(self, user_msg: str) -> str:
        """按 `user_msg_max_chars` / `user_msg_store_mode` 处理要落日志的用户消息。"""
        t = (user_msg or "").strip()
        try:
            cap = int(self._cfg("user_msg_max_chars", 200) or 200)
        except (TypeError, ValueError):
            cap = 200
        mode = str(self._cfg("user_msg_store_mode", "总结") or "总结")
        if cap > 0 and len(t) > cap:
            if mode == "总结":
                return f"{t[:cap // 2]}…（共{len(t)}字）"
            return t[:cap]
        return t

    # ------------------------------------------------------------------ 会话重置
    @filter.after_message_sent()
    async def on_after_message(self, event: AstrMessageEvent):
        """响应框架的 /reset 会话清空信号，避免 reset 后串记忆。

        框架**只会**清掉 LLM 侧的历史（`req.contexts`）；chatcore_neo 自己还
        维护了若干**每会话**的运行时数据，不清就会出现"reset 了却还记得"：
          * `_live`：群聊近期发言流水（注入为「群里最近发言」）—— 群聊里最
            直接的"记得上下文"，实测 reset 后仍会引用 reset 前的群消息；
          * `merge`：待合并批次（注入为「用户连发消息」）；
          * `memory._pending`：尚未固化的轮次（下次固化会写成记忆）；
          * `attention`：注意力/冷却状态，以及若干 umo 键的节流表；
          * `costars`：多人格同台的副人格列表（属会话上下文，reset 一并清掉）。

        长期记忆与印象是**跨会话持久**的，不属于会话上下文，仍走
        `/persona forget` 与 WebUI 各自的入口。
        """
        try:
            if not event.get_extra("_clean_group_context_session", False):
                return
            umo = event.unified_msg_origin
            # 运行时缓存的清理与人格是否存在无关，先无条件清一遍
            self._clear_session_caches(umo)
            doc = self._persona_for(umo)
            if doc is not None:
                # 与 on_llm_request 保持一致：**私聊的 uid 必须是空串**。
                # 私聊状态存在 `{umo}/{persona}.json`，群聊才带 uid 子目录；
                # 这里无条件取 sender_id 会写到另一个文件，于是 /reset 看似
                # "已重置"却对真实状态毫无影响，还留下幽灵文件。
                is_private = event.is_private_chat()
                uid = "" if is_private else self._sender_id(event)
                st = self.state.load(umo, doc.id, uid)
                st.emotion = 100
                st.conversation_log = []
                st.stutter_done = False
                self.state.save(umo, doc.id, uid, st)
            logger.info(f"[chatcore_neo] 会话已重置: {umo}")
        except Exception as e:
            logger.debug(f"[chatcore_neo] 会话重置处理失败: {e!r}")

    def _clear_session_caches(self, umo: str) -> None:
        """清掉某会话**所有**插件自维护的运行时上下文（/reset 用）。

        每一项单独 try：任何一项失败都不该阻断其余清理（/reset 必须彻底）。
        """
        for name, action in (
            ("_live", lambda: self._live.pop(umo, None)),
            ("merge", lambda: self.merge.cancel(umo)),
            ("attention", lambda: self.attention.reset(umo)),
            ("costars", lambda: self.costars.clear(umo)),
            ("memory._pending", lambda: self.memory.drop_pending(
                self.groups.group_of(umo))),
        ):
            try:
                action()
            except Exception as e:
                logger.debug(f"[chatcore_neo] reset 清 {name} 失败: {e!r}")
        for attr in ("_intent_last", "_active_triggers",
                     "_persona_switch_notice"):
            try:
                getattr(self, attr).pop(umo, None)
            except Exception:
                pass

    # ------------------------------------------------------------------ 世界工具
    @filter.llm_tool(name="world_list")
    async def world_list_tool(self, event: AstrMessageEvent, world: str = ""):
        """列出世界观资料，或列出某个世界观的文件树。

        想查设定但不知道有哪些世界/文件时先用这个工具。

        Args:
            world (string): 世界观 id；留空则列出全部世界观及体量
        """
        try:
            return world_mod.tool_world_list(world or "")
        except Exception as e:
            return f"world_list 失败：{e}"

    @filter.llm_tool(name="world_search")
    async def world_search_tool(self, event: AstrMessageEvent, query: str,
                                world: str = "", limit: int = 5):
        """在世界观资料里做关键词检索，返回命中文件、行号与片段。

        先检索判断相关性，再用 world_read 读具体行范围，不要一次读整份文件。

        Args:
            query (string): 检索关键词（可用空格分隔多个词）
            world (string): 限定某个世界观 id；留空则跨全部世界观
            limit (number): 最多返回多少条，默认 5
        """
        try:
            return world_mod.tool_world_search(query, world or "", limit or 5)
        except Exception as e:
            return f"world_search 失败：{e}"

    @filter.llm_tool(name="world_read")
    async def world_read_tool(self, event: AstrMessageEvent, path: str,
                              start: int = 1, end: int = 0):
        """读取某个世界观文件，支持行范围（避免整份长文灌进上下文）。

        路径来自 world_list / world_search 的输出，例如 "strinova/factions.md"。

        Args:
            path (string): 相对 worlds/ 的文件路径
            start (number): 起始行号（从 1 开始），默认 1
            end (number): 结束行号；0 表示从 start 起读一段
        """
        try:
            return world_mod.tool_world_read(path, start or 1, end or 0)
        except world_mod.WorldError as e:
            return f"world_read 失败：{e}"
        except Exception as e:
            return f"world_read 失败：{e}"

    # ------------------------------------------------------------- 多人格同台工具

    @filter.llm_tool(name="update_costars")
    async def update_costars_tool(self, event: AstrMessageEvent,
                                  personas: list[str]):
        """设置本次会话「多人格同台」的同时登场角色（覆盖式）。

        剧情模式下需要你**同时扮演多个角色**时调用：传入需要一起登场的完整
        角色名列表（覆盖式设置）；传入空列表 [] 表示全部撤下、回到单角色。
        角色名支持技能目录名 / 显示名 / 别名（如 "香奈美" / "kanami"）。
        **仅剧情模式可用**：非剧情模式会拒绝并提示先切换发言模式。
        设置后从下一次请求起生效，本轮回答仍按原设置进行。

        Args:
            personas (list): 需要同时登场的角色名列表（覆盖式）。
        """
        umo = event.unified_msg_origin
        is_private = event.is_private_chat()
        doc = self._persona_for(umo)
        if not isinstance(personas, list):
            personas = [personas] if personas else []
        if not self._costar_mode_ok(umo, is_private):
            yield event.plain_result(self._costar_mode_hint())
            yield json.dumps({"success": False, "error": "not_narrative"},
                             ensure_ascii=False)
            return
        docs, bad = self._costar_resolve(personas)
        if bad:
            yield event.plain_result("这些角色没找到：" + "、".join(bad))
            yield json.dumps({"success": False, "error": "unknown_personas",
                              "invalid": bad}, ensure_ascii=False)
            return
        cap = self._costar_max()
        pids: list[str] = []
        skipped_main: list[str] = []
        for d in docs:
            if doc is not None and d.id == doc.id:
                skipped_main.append(d.display_name)
            else:
                pids.append(d.id)
        overflow = pids[cap:] if cap else []
        pids = pids[:cap] if cap else pids
        prev = self.costars.cast_of(umo)
        self.costars.set(umo, pids)
        old = set(prev)
        new = set(pids)
        msgs = [f"「{self._pname(p)}」已加入会话" for p in pids if p not in old]
        msgs += [f"「{self._pname(p)}」已离开会话" for p in prev if p not in new]
        if overflow:
            msgs.append("已达上限 %d，未加入：%s"
                        % (cap, "、".join(self._pname(p) for p in overflow)))
        if skipped_main:
            msgs.append("「%s」是当前主角，已跳过" % "、".join(skipped_main))
        if not msgs:
            msgs.append("多人格同台设置已更新（无变化）。"
                        if pids else "已关闭多人格同台。")
        yield event.plain_result("\n".join(msgs))
        yield json.dumps({
            "success": True,
            "main": doc.display_name if doc else "",
            "costars": pids,
            "names": [self._pname(p) for p in pids],
            "message": "；".join(msgs),
        }, ensure_ascii=False)

    @filter.llm_tool(name="show_costars")
    async def show_costars_tool(self, event: AstrMessageEvent):
        """查看当前会话「多人格同台」已登场的额外角色（仅剧情模式有意义）。

        需要确认自己当前是否在同时扮演多个角色时调用。
        """
        umo = event.unified_msg_origin
        is_private = event.is_private_chat()
        doc = self._persona_for(umo)
        docs = self._costar_docs(umo, doc)
        mode_ok = self._costar_mode_ok(umo, is_private)
        return json.dumps({
            "success": True,
            "main": doc.display_name if doc else "",
            "costars": [d.id for d in docs],
            "names": [d.display_name for d in docs],
            "narrative_mode": mode_ok,
            "effective": bool(docs) and mode_ok,
        }, ensure_ascii=False)

    # ------------------------------------------------------------------ 人格工具

    @filter.llm_tool(name="persona_list")
    async def persona_list_tool(self, event: AstrMessageEvent, keyword: str = ""):
        """列出全部可用人格（id + 一句简介），或按关键词筛选。

        用户问「有哪些人格 / 有没有 xx 人格 / 换个角色吧」但没给准确名字时，
        先调用它拿到 id 或名称，再交给 persona_switch 执行切换。

        Args:
            keyword (string): 筛选关键词（匹配 id / 显示名 / 别名 / 简介）；留空列出全部
        """
        try:
            umo = event.unified_msg_origin
            cur = self._persona_for(umo)
            kw = (keyword or "").strip().lower()
            _inst = inst_of_umo(umo)
            _pool = getattr(self, "_persona_pool", None)
            _docs = self.personas.all()
            if _pool is not None and _inst:
                _docs = _pool.available_docs(_inst, _docs)
            items = []
            for d in _docs:
                desc = str((d.skill_frontmatter or {}).get("description")
                           or "").strip()
                if kw:
                    hay = " ".join([d.id, d.display_name, *d.aliases,
                                    desc]).lower()
                    if kw not in hay:
                        continue
                items.append({
                    "id": d.id,
                    "name": d.display_name,
                    "world": d.world,
                    "desc": (desc[:80] + "…") if len(desc) > 80 else desc,
                })
            return json.dumps({
                "success": True,
                "current": cur.id if cur else "",
                "count": len(items),
                "personas": items,
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"success": False, "error": "internal",
                               "message": f"persona_list 失败：{e}"},
                              ensure_ascii=False)

    @filter.llm_tool(name="persona_switch")
    async def persona_switch_tool(self, event: AstrMessageEvent, persona: str):
        """【仅特权用户】把**当前会话**切换为另一个人格，立即生效。

        仅当对方明确要求「换成 / 扮演 / 切换成 xx 人格」时调用；普通成员无权。
        调用成功后本会话由新人格接管：你正在生成的这条回复会被丢弃，随后你会
        以新人格的身份顺着对话继续回复用户——不要在切换后重复道别或解释。
        参数支持人格 id、显示名或别名（不确定时先用 persona_list 查）。

        Args:
            persona (string): 目标人格（id / 显示名 / 别名）
        """
        try:
            umo = event.unified_msg_origin
            is_private = event.is_private_chat()
            uid = "" if is_private else self._sender_id(event)
            tier = self.privilege.tier(uid, scope=umo) if uid else "intimate"
            if tier != "intimate":
                return json.dumps({
                    "success": False, "error": "permission_denied",
                    "message": "只有特权用户（等级 1）可以切换人格。"
                               "以当前角色身份婉拒或正常收场即可，不要切换。",
                }, ensure_ascii=False)
            want = str(persona or "").strip()
            doc = self.personas.resolve(want) if want else None
            if doc is None:
                return json.dumps({
                    "success": False, "error": "not_found",
                    "message": self._not_found(want or "（空）"),
                    "candidates": self.personas.suggest(want) if want else [],
                }, ensure_ascii=False)
            old = self._persona_for(umo)
            if old is not None and old.id == doc.id:
                return json.dumps({
                    "success": True, "already_current": True,
                    "message": f"当前已经是「{doc.display_name}」，无需切换；"
                               "顺着对话正常继续回复即可。",
                }, ensure_ascii=False)
            old_name = old.display_name if old else ""
            if not self._persona_ok(umo, doc):
                return json.dumps({
                    "success": False, "error": "not_granted",
                    "message": self._persona_denied(umo),
                }, ensure_ascii=False)
            # ① 应用切换（与 /persona 命令共用同一套：会话覆盖 + 群名片 + 提醒）
            self._apply_persona_switch(umo, doc, event)
            # ② **先**挂重入任务：本轮随后会被中止，重入必须由独立任务负责，
            #    挂在事件上的协程会连它一起被带走。
            self._spawn(self._reenter_after_persona_switch(event, umo, doc,
                                                           old_name))
            # ③ 请求中止当前轮（含本事件自身）：旧人格那条回复不再产出。
            #    不传 exclude —— 要停的正是正在跑的这一轮。
            try:
                from astrbot.core.utils.active_event_registry import (
                    active_event_registry)
                n = active_event_registry.request_agent_stop_all(umo)
                logger.info(f"[chatcore_neo] persona_switch：已请求中止 "
                            f"{n} 个进行中 agent　umo={umo!r}")
            except Exception as e:
                logger.debug(f"[chatcore_neo] persona_switch 停止请求失败: {e!r}")
            # ④ 工具结果只作兜底：正常链路里本轮会被中止、看不到它
            return json.dumps({
                "success": True,
                "switched_to": doc.id,
                "display_name": doc.display_name,
                "note": "人格已切换，本轮将被中止并由新人格重新回复；"
                        "你不必再输出任何内容。",
            }, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"success": False, "error": "internal",
                               "message": f"persona_switch 失败：{e}"},
                              ensure_ascii=False)

    # ------------------------------------------------------------------ 记忆工具

    @filter.llm_tool(name="memory_search")
    async def memory_search_tool(self, event: AstrMessageEvent, query: str,
                                 limit: int = 5):
        """按语义检索本会话的**长期记忆库**（不是当前对话上下文）。

        什么时候用：对方问「你还记得…吗」「我之前说过什么」「我们聊过 xx 吗」，
        或你需要确认某件事是否真的发生过、是不是记错了时。
        查的是**固化后的长期记忆**（总结过的旧事），当前这轮对话的内容已经
        在你的提示里了，不要用它重复查。

        Args:
            query (string): 要查什么（自然语言，越具体越好；用「他上次说的那个游戏」比「游戏」好）
            limit (int): 最多返回几条（默认 5，上限 20）
        """
        try:
            if not self.memory.enabled:
                return self._tool_json({
                    "success": False, "error": "disabled",
                    "message": "长期记忆功能没开，查不到任何东西。"})
            q = str(query or "").strip()
            if not q:
                return self._tool_json({
                    "success": False, "error": "empty_query",
                    "message": "query 不能为空。"})
            umo = event.unified_msg_origin
            is_private = event.is_private_chat()
            uid = "" if is_private else self._sender_id(event)
            doc = self._persona_for(umo)
            k = max(1, min(20, int(limit or 5)))
            hits = await self.memory.recall_texts(
                self.groups.group_of(umo), q,
                persona=doc.id if doc else "", uid=uid, top_k=k,
                persona_labels=self._persona_labels(),
                policy=int(self._cfg("memory_persona_policy", 0) or 0))
            return self._tool_json({
                "success": True, "count": len(hits), "memories": hits,
                "message": ("没查到相关记忆，就当没印象，别编。"
                            if not hits else "")})
        except Exception as e:
            return self._tool_json({
                "success": False, "error": "internal",
                "message": f"memory_search 失败：{e}"})

    @filter.llm_tool(name="memory_note")
    async def memory_note_tool(self, event: AstrMessageEvent, text: str,
                               kind: str = "fact"):
        """【仅特权用户】把一条信息**立刻写进长期记忆**（重要度拉满、不受遗忘影响）。

        只有对方明确说「记住 / 记下来 / 别忘了 xx」时才调用；普通成员无权调用。
        写入是**即时且精确**的：不经过总结改写，但你写什么它就记什么，所以
        要写成一句能独立成立的陈述（把「这个」换成具体指代）。

        Args:
            text (string): 要记住的内容，一句话陈述（如「他养了只叫小黑的猫」）
            kind (string): 记忆类型：preference（喜好/习惯）/ fact（稳定事实，默认）/ dialog（一时闲聊）/ document（事件记录）
        """
        try:
            umo = event.unified_msg_origin
            is_private = event.is_private_chat()
            uid = "" if is_private else self._sender_id(event)
            tier = self.privilege.tier(uid, scope=umo) if uid else "intimate"
            if tier != "intimate":
                return self._tool_json({
                    "success": False, "error": "permission_denied",
                    "message": "只有特权用户（等级 1）能要求我记东西。"
                               "以当前角色身份婉拒或正常收场即可，不要真的记。"})
            body = str(text or "").strip()
            if not body:
                return self._tool_json({
                    "success": False, "error": "empty",
                    "message": "text 不能为空。"})
            doc = self._persona_for(umo)
            got = await self.memory.remember(
                self.groups.group_of(umo), body, kind=kind or "fact",
                importance=1.0, persona_id=doc.id if doc else "", uid=uid,
                user_name=(event.get_sender_name() or ""))
            if not got.get("ok"):
                return self._tool_json({
                    "success": False, "error": got.get("error", "unknown"),
                    "message": f"没能记下来（{got.get('error')}）；"
                               "照常聊天即可，不要跟对方说内部原因。"})
            return self._tool_json({
                "success": True, "kind": got["kind"],
                "already": bool(got.get("deduped")),
                "message": ("这条我本来就记着，不用重复记。"
                            if got.get("deduped") else "记下了。")})
        except Exception as e:
            return self._tool_json({
                "success": False, "error": "internal",
                "message": f"memory_note 失败：{e}"})

    # ------------------------------------------------------------------ 辅助

    @staticmethod
    def _tool_json(payload: dict) -> str:
        """LLM 工具的统一返回：JSON 字符串，中文不转义（省 token、模型好读）。"""
        return json.dumps(payload, ensure_ascii=False)

    def _handle_recall(self, event: AstrMessageEvent) -> bool:
        """检测 OneBot 撤回通知；返回 True 表示已消费（不是普通消息）。

        `enable_recall_cancel` 打开时，若被撤回的正是**本轮触发消息**，
        就请框架停掉这个会话正在进行中的 agent —— 用户撤回了，
        bot 不该再把那条回复发出去。
        """
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if not isinstance(raw, dict):
            return False
        if str(raw.get("post_type") or "") != "notice":
            return False
        notice = str(raw.get("notice_type") or "")
        if notice not in ("group_recall", "friend_recall"):
            return False
        try:
            mid = str(raw.get("message_id", "") or "")
            umo = event.unified_msg_origin
            if bool(self._cfg("enable_recall_cancel", True)) and mid \
                    and self._active_triggers.get(umo) == mid:
                from astrbot.core.utils.active_event_registry import (
                    active_event_registry)
                n = active_event_registry.request_agent_stop_all(umo, exclude=event)
                self._stats["recalls_cancelled"] = \
                    self._stats.get("recalls_cancelled", 0) + 1
                logger.info(f"[chatcore_neo] 撤回取消: {umo} 消息 {mid} "
                            f"（停止 {n} 个进行中的 agent）")
            self._active_triggers.pop(umo, None)
        except Exception as e:
            logger.debug(f"[chatcore_neo] 撤回取消处理异常: {e!r}")
        return True

    def _maybe_set_group_card(self, event: AstrMessageEvent,
                              doc: PersonaDoc) -> None:
        """切换人格后把群名片改成模板（`group_card_template`）。

        平台相关：走 aiocqhttp 的 `bot.set_group_card`。失败静默跳过
        （有些适配器没有这个能力），不影响人格切换本身。
        """
        try:
            template = str(self._cfg("group_card_template", "") or "").strip()
            if not template:
                return
            group_id = event.get_group_id()
            if not group_id:
                return
            bot = getattr(event, "bot", None)
            if bot is None or not hasattr(bot, "set_group_card"):
                return
            card = template.replace("{name}", doc.display_name)

            async def _set_card() -> None:
                try:
                    await bot.set_group_card(
                        group_id=int(group_id),
                        user_id=int(event.get_self_id()), card=card)
                    logger.info(f"[chatcore_neo] 群名片已设为 {card}")
                except Exception as e:
                    logger.warning(f"[chatcore_neo] 设置群名片 {card} 失败: {e!r}")

            # 日志必须在**任务真正执行之后**打：旧写法在调度前就 log
            # "已设为"，失败了也留下成功日志，排障时会误导。
            self._spawn(_set_card())
        except Exception as e:
            logger.debug(f"[chatcore_neo] 设置群名片失败: {e!r}")

    def _apply_persona_switch(self, umo: str, doc: PersonaDoc,
                              event: AstrMessageEvent) -> PersonaDoc | None:
        """把会话切换为 doc：会话覆盖 + 群名片 + 下一轮切换提醒。

        `/persona <名称>` 命令与 `persona_switch` 工具共用这一套，
        保证两条入口的行为（含提醒文案）完全一致；返回旧人格（没有则 None）。
        """
        old = self._persona_for(umo)
        self._session_persona.set(umo, doc.id)
        self._maybe_set_group_card(event, doc)
        _old_name = old.display_name if old else "之前的角色"
        self._persona_switch_notice[umo] = (
            f"人格已切换：你现在扮演的是「{doc.display_name}」"
            f"（id: {doc.id}），不再是「{_old_name}」。"
            "从现在起请彻底以新角色的身份、口吻、称呼体系回应，"
            "不要再用旧角色的自称或人名。"
        )
        return old

    async def _reenter_after_persona_switch(self, event: AstrMessageEvent, umo: str,
                                            doc: PersonaDoc,
                                            old_name: str = "") -> None:
        """切人格后重入管道：让新人格在**新请求**里继续回复。

        为什么必须重入、而不是在原轮里直接回复：
        工具执行发生在 on_llm_request **之后**——原轮的 system prompt 是
        **旧人格**拼好的，在原轮里塞新人格必然与 system 打架。
        重入一条合成事件后，管道会为新请求重跑钩子、以**新人格**重新装配
        system prompt，冲突自然消失；`_persona_switch_notice` 也会在这一轮
        被消费，注入「人格已切换」通知。

        `delay=1.0` 是给原轮的中止留收尾窗口（EventBus 并发消费事件，
        不等前一条跑完）。
        """
        try:
            from .core.proactive import synthesize_event
            text = (
                f"（系统消息：人格切换已完成——你现在是「{doc.display_name}」"
                f"（id: {doc.id}）"
                + (f"，此前扮演的「{old_name}」已下线" if old_name else "")
                + "。请立刻以新人格的身份、口吻与称呼体系，顺着上面的对话"
                "继续回复用户；不要再用旧角色的自称或人名。）"
            )
            tpl = self.templates.get(umo) or self.templates.ensure_from_event(event)
            if tpl is None:
                logger.warning(
                    f"[chatcore_neo] 人格切换重入失败：无事件模板 umo={umo!r}"
                    "（下一条消息起仍会以新人格装配，不会卡死）")
                return
            ev = synthesize_event(tpl, text, self.context, umo=umo,
                                  sender=self._poke_sender(event),
                                  source="persona_switch")
            if ev is None:
                logger.warning(
                    f"[chatcore_neo] 人格切换重入失败：合成事件为空 umo={umo!r}")
                return
            ok = await compat.reenter_pipeline(ev, context=self.context,
                                               source="persona_switch", delay=1.0)
            logger.info(f"[chatcore_neo] 人格切换已重入管道 → {doc.display_name}"
                        f"　umo={umo!r} 成功={ok}")
        except Exception as e:
            logger.warning(f"[chatcore_neo] 人格切换重入异常: {e!r}")

    def _default_affection(self, *, is_private: bool, uid: str,
                           umo: str = "") -> int:
        """按场景给初始好感度。

        私聊 / 特权用户用 `initial_affection`；
        群聊普通成员用 `group_member_initial_affection`（降档，防乱认主）。
        `umo` 用于按会话解析特权（分会话覆盖优先于全局）。
        """
        if is_private or not uid:
            return int(self._cfg("initial_affection", 65) or 0)
        if self.privilege.tier(uid, scope=umo) == "intimate":
            return int(self._cfg("initial_affection", 65) or 0)
        return int(self._cfg("group_member_initial_affection", 30) or 0)

    def _unrestricted(self, scope: str = "") -> set:
        """不受制约用户 = 该会话生效的特权名单 + 配置的 unrestricted_list。"""
        out = set(self.privilege.effective(scope))
        for u in (self._cfg("unrestricted_list", []) or []):
            s = str(u).strip()
            if s:
                out.add(s)
        return out

    def _poke_sender(self, event: AstrMessageEvent) -> dict | None:
        """戳一戳**发起人**的 sender 骨架（uid + 昵称），供合成事件使用。

        poke 事件通常不带昵称（日志里是 `10001/10001`，平台把 QQ 号
        当昵称返回），所以先取事件自带、缺了再从本地昵称表补、再缺就用 uid
        顶上。取不到 uid 时返回 None，让调用方回退到事件模板。
        """
        try:
            uid = str(event.get_sender_id() or "").strip()
            name = str(event.get_sender_name() or "").strip()
        except Exception:
            return None
        if not uid:
            return None
        if name == uid:            # 平台把 QQ 号当昵称返回，不算昵称
            name = ""
        if not name:
            name = str(self._user_names.get(uid) or "").strip() or uid
        return {"user_id": uid, "nickname": name}

    def _identity_block_ok(self, event: AstrMessageEvent) -> bool:
        """是否注入「本轮对话对象」身份块（"你现在正在回复 X"）。

        **只在真有本轮说话人时注入**：合成事件的 sender 抄自事件模板
        （= 该会话历史上第一个说话的人，写一次后不再更新），拿它当
        "正在回复的人"，主动消息就会莫名其妙反复点名同一个人。
        戳一戳是例外 —— `_reply_poke` 已把发起人的真实身份塞进合成事件。
        """
        if not bool(self._cfg("identity_confirm_inject", True)):
            return False
        return compat.simulated_source(event) in ("", "poke")

    async def _reply_poke(self, event: AstrMessageEvent, umo: str) -> bool:
        """回应戳一戳。text 模式直接发固定文案，llm 模式走管道。

        三条路都必须**留下痕迹**：调用方 `_spawn` 会吞掉返回值，旧写法里
        "重入失败"和"成功"从外面看一模一样（都没有日志、也都没有消息）。
        """
        try:
            if self.poke.reply_mode == "text":
                await event.send(event.plain_result(self.poke.text))
                logger.info(f"[chatcore_neo] 戳一戳已回应（固定文案）umo={umo!r}")
                return True
            # llm 模式：造一条合成事件重入管道，让人格与状态照常生效
            tpl = self.templates.get(umo)
            if tpl is None:
                # 没有事件模板（该会话还没来过真实消息）→ 退回固定文案
                logger.info(
                    f"[chatcore_neo] 戳一戳无事件模板，退回固定文案 umo={umo!r}")
                await event.send(event.plain_result(self.poke.text))
                return True
            from .core.interactions import POKE_NOTICE
            from .core.proactive import synthesize_event
            # 必须带上**戳的人**的身份：事件模板里的 sender 是该会话历史上
            # 第一个说话的人（`ensure_from_event` 只写一次、此后不再更新），
            # 直接复用会让 bot 把发起人认成模板里那个人
            # （实测：江一楠戳的，bot 却喊「泠柒又戳我啦」）。
            ev = synthesize_event(tpl, POKE_NOTICE, self.context, umo=umo,
                                  sender=self._poke_sender(event),
                                  source="poke")
            if ev is None:
                logger.warning(
                    f"[chatcore_neo] 戳一戳合成事件失败，退回固定文案 umo={umo!r}")
                await event.send(event.plain_result(self.poke.text))
                return True
            ok = await compat.reenter_pipeline(ev, context=self.context,
                                               source="poke")
            logger.info(f"[chatcore_neo] 戳一戳已重入管道 umo={umo!r} 成功={ok}")
            return ok
        except Exception as e:
            logger.warning(f"[chatcore_neo] 戳一戳回应失败: {e!r}")
            return False

    # ------------------------------------------------------------------ 接话决策
    @staticmethod
    def _describe_components(event: AstrMessageEvent) -> str:
        """把消息链摊平成 `类型(qq=...)` 串，用于诊断 @ / 引用 识别失败。"""
        out = []
        try:
            for c in event.get_messages() or []:
                name = type(c).__name__
                qq = getattr(c, "qq", None)
                sid = getattr(c, "sender_id", None)
                if qq is not None:
                    out.append(f"{name}(qq={qq!r})")
                elif sid is not None:
                    out.append(f"{name}(sender_id={sid!r})")
                else:
                    out.append(name)
        except Exception as e:
            return f"<读取失败 {e!r}>"
        return "[" + ", ".join(out) + "]"

    @staticmethod
    def _is_command_result(event: AstrMessageEvent) -> bool:
        """这条结果是不是**命令**产生的。

        `WakingCheckStage` 把过滤器通过的 handler 存进
        `event.set_extra("activated_handlers", ...)`（stage.py:244），
        带 `CommandFilter` 的就是指令处理器。该 extra 全程不清除。
        """
        try:
            from astrbot.core.star.filter.command import CommandFilter
        except Exception:
            return False
        try:
            handlers = event.get_extra("activated_handlers", []) or []
        except Exception:
            return False
        for h in handlers:
            for f in (getattr(h, "event_filters", None) or []):
                if isinstance(f, CommandFilter):
                    return True
        return False

    @staticmethod
    def _bot_at_mentioned(event: AstrMessageEvent) -> bool:
        """消息里是否 @ 了机器人自己。"""
        try:
            from astrbot.api.message_components import At
        except Exception:
            return False
        try:
            self_id = str(event.get_self_id() or "")
            if not self_id:
                return False
            for comp in event.get_messages() or []:
                if isinstance(comp, At) and str(getattr(comp, "qq", "")) == self_id:
                    return True
        except Exception:
            pass
        return False

    @staticmethod
    def _bot_quoted(event: AstrMessageEvent) -> bool:
        """消息是否**引用**了机器人自己的发言。"""
        try:
            from astrbot.api.message_components import Reply
        except Exception:
            return False
        try:
            self_id = str(event.get_self_id() or "")
            if not self_id:
                return False
            for comp in event.get_messages() or []:
                if not isinstance(comp, Reply):
                    continue
                sid = getattr(comp, "sender_id", None)
                if sid is not None and str(sid) == self_id:
                    return True
        except Exception:
            pass
        return False

    def _wake_prefixes(self) -> list:
        try:
            cfg = self.context.get_config()
            p = cfg.get("wake_prefix", [])
            if isinstance(p, str):
                p = [p]
            return [str(x) for x in p if x]
        except Exception:
            return []

    def _call_prefixes(self) -> list:
        """叫醒词名单（全局配置项 `call_prefix`，默认空 = 不启用）。

        与 `wake_prefix` 的分工：
          * `wake_prefix`（默认 `/`）= **指令 + 回复**都能触发
          * `call_prefix`（如 `^`）= **只触发回复**，剥词后不当指令匹配

        所以叫醒词绝不能出现在 `wake_prefix` 里 —— 那会让框架先把词剥掉并
        置 `is_at_or_wake_command`，指令过滤器照样能命中（指令匹配用的是
        剥完词后的 `message_str`，`filter/command.py:192`），叫醒词就失效了。
        """
        try:
            p = self._cfg("call_prefix", [])
            if p is None:
                return []
            if isinstance(p, str):
                p = [p]
            # 注意 `if x` 会把 0 也滤掉，但叫醒词只可能是字符串；
            # 这里要滤的是 None / ""，所以判 `str(x)` 非空。
            return [str(x) for x in p if str(x or "").strip()]
        except Exception:
            return []

    def _maybe_call_prefix(self, event: AstrMessageEvent) -> bool:
        """以叫醒词开头的消息：剥词、叫醒进 LLM，但**不触发指令**。

        ## 为什么在 `on_any_message` 里做
        框架的 `WakingCheckStage` 只认 `wake_prefix`，叫醒词不在其列，
        所以这类消息在框架眼里是"普通群消息"：既不会置
        `is_at_or_wake_command`，`ProcessStage` 也不会对它发 LLM 请求
        （`process_stage/stage.py:56-61`）—— 机器人根本不理会。
        必须在指令匹配**之前**自己把它叫醒。

        ## 为什么"不触发指令"能成立
        指令过滤器（`CommandFilter.filter`）的第一件事就是
        `if not event.is_at_or_wake_command: return False`，且匹配用的是
        `event.get_message_str()`。我们把叫醒词从 `message_str` 里剥掉后，
        `^persona` 变成 `persona` —— 但此时 `is_at_or_wake_command` 还是
        False（框架没置过），指令过滤器直接返回 False，**不会命中**。
        于是消息只会走 `ProcessStage` 的 LLM 兜底分支。

        等等 —— 那怎么让 LLM 分支也走？`ProcessStage` 同样要求
        `is_at_or_wake_command` 为真。所以顺序是：**先剥词，再置位**。
        剥词发生在置位之前，指令过滤器（在 `WakingCheckStage` 里就已经
        跑完并写入 `activated_handlers`）看不到这个置位；
        而 `ProcessStage` 在更后面读它，能看见。

        ## 为什么不会被命令闸门拦掉
        `on_llm_request_gate` 会把"被唤醒前缀唤醒、但没 @/引用"的消息
        当成"未命中命令的唤醒消息"拦掉。叫醒词消息满足这个形状，
        所以闸门里必须认 `X_CALL_PREFIX` 放行（已加）。

        ## 私聊
        私聊框架本来就必回（`stage.py:145-152`），叫醒词在这里只做剥词，
        不额外置位 —— 置了反而会让"仅含叫醒词"的消息也进 LLM。
        """
        if compat.is_gutoken(event) or compat.is_simulated(event):
            return False
        if not bool(self._cfg("enabled", True)):
            return False
        prefixes = self._call_prefixes()
        if not prefixes:
            return False
        try:
            if event.get_extra(X_CALL_PREFIX, False):
                return False            # 已处理过（防止重入时二次剥词）
        except Exception:
            pass
        try:
            if bool(getattr(event, "is_at_or_wake_command", False)):
                return False            # 已被框架唤醒（@/wake_prefix）→ 不干预
        except Exception:
            return False
        text = (event.message_str or "").lstrip()
        if not text:
            return False
        # 叫醒词不得与唤醒前缀重叠：若消息同时以唤醒词开头，那是指令路径，
        # 交给框架处理（否则会把 /persona 也变成"只回复"）。
        wake = self._wake_prefixes()
        if wake and text.startswith(tuple(wake)):
            return False
        hit = ""
        for p in prefixes:
            if text.startswith(p):
                hit = p
                break
        if not hit:
            return False

        rest = text[len(hit):]
        # 剥掉叫醒词本身，以及紧随其后的空白（用户打 `^ persona` 也认）。
        # **不 strip 右侧**：结尾的空格/换行可能是用户有意为之。
        stripped = rest.lstrip()
        if not stripped:
            # 只有叫醒词、没有正文 → 与框架的"仅有唤醒前缀"同性质：
            # 不叫醒（否则会拿空 prompt 烧一次 LLM）。
            return False

        try:
            event.message_str = stripped
        except Exception:
            return False
        # `message_obj.message_str` 是适配器写的原文，群聊上下文等下游会读它；
        # 不同步的话注入的群历史里仍带着叫醒词。只改第一个 Plain 段。
        try:
            comps = event.get_messages() or []
            from astrbot.api.message_components import Plain
            for c in comps:
                if isinstance(c, Plain):
                    t = (c.text or "").lstrip()
                    if t.startswith(hit):
                        c.text = t[len(hit):].lstrip()
                    break
        except Exception:
            pass
        try:
            event.message_obj.message_str = stripped
        except Exception:
            pass

        event.set_extra(X_CALL_PREFIX, hit)
        # **置位放在剥词之后**（见文档）：指令过滤器早已跑完，看不见它；
        # ProcessStage 在后面读，看得见。
        event.is_at_or_wake_command = True
        event.is_wake = True
        logger.info(f"[chatcore_neo] 叫醒词「{hit}」命中 → 剥词进 LLM"
                    f"（不触发指令）　文本={stripped[:40]!r}")
        return True

    @staticmethod
    def _addresses_other_user(event: AstrMessageEvent) -> bool:
        """消息 @ 了 bot 以外的某个具体用户时返回 True。

        这类消息是发给别人的（如「@小红 作业写完了吗」），软触发不插话。
        @all（AtAll）和 @bot 自己不算。照 chatcore_lite `main.py:852` 移植。
        """
        try:
            from astrbot.api.message_components import At
        except Exception:
            return False
        try:
            self_id = str(event.get_self_id() or "")
            for comp in event.get_messages() or []:
                if isinstance(comp, At):
                    qq = str(getattr(comp, "qq", "") or "")
                    if qq not in ("all", self_id):
                        return True
        except Exception:
            pass
        return False

    def _maybe_attention_wake(self, event: AstrMessageEvent) -> bool:
        """对「普通群消息」做注意力判定；命中则把消息叫醒进 LLM 链路。

        ## 为什么必须在这里做
        `_decide_reply` 挂在 `on_llm_request` 上，而框架的 `ProcessStage`
        只对 `is_at_or_wake_command=True` 的消息发起 LLM 请求
        （`process_stage/stage.py:56-61`）—— 普通群消息**走不到那个钩子**。
        所以判定必须提前到 `EventMessageType.ALL` 的钩子里自己做，
        命中后手动置位 `is_at_or_wake_command`，把消息"叫醒"。

        ## 只有「普通群消息」才走概率
        * 私聊 → 不在这里管（框架本来必回）
        * 已被框架唤醒（@bot / 前缀）→ 属硬触发，**不走概率**
        * 叫醒词消息 → 已由 `_maybe_call_prefix` 叫醒，是硬触发，**不走概率**
        * 以唤醒前缀开头的指令 → 不参与
        * @ 了别人 → 直接不插话
        * 其余 → **掷一次骰子**（纯数学，`random < prob`）

        **不命中时什么都不做** —— 框架自然跳过这条消息，零开销。
        """
        if compat.is_simulated(event):
            return False
        try:
            if event.is_private_chat():
                return False
        except Exception:
            return False
        # 框架已唤醒 → 硬触发路径，不掷骰子
        if bool(getattr(event, "is_at_or_wake_command", False)):
            return False
        # 叫醒词已叫醒（`_maybe_call_prefix` 先跑，会置位上面那个属性）
        # —— 这条判断是冗余保险，防调用顺序被调整。
        try:
            if event.get_extra(X_CALL_PREFIX, False):
                return False
        except Exception:
            pass
        umo = event.unified_msg_origin
        text = (event.message_str or "").strip()
        # 没有任何内容的消息（纯表情/空消息）不参与 —— 注意力是"看别人在聊什么"，
        # 没内容就无从判断。纯图片消息有组件，仍算有内容（框架会做图片转述），
        # 与 ChatCore `main.py:296` 的 `if not text and not has_img: return` 同口径。
        if not text:
            try:
                comps = [c for c in (event.get_messages() or [])
                         if type(c).__name__ != "Plain"]
            except Exception:
                comps = []
            if not comps:
                return False
        # 指令消息（以唤醒前缀开头）不参与
        prefixes = self._wake_prefixes()
        if prefixes and text.lstrip().startswith(tuple(prefixes)):
            return False

        # 「读空气」的他人消息计数（谁在刷屏）
        self.attention.record_others_message(umo)

        # @ 了别人 → 不插话
        if self._addresses_other_user(event):
            return False
        if not bool(self._cfg("enable_attention", True)):
            return False

        prob = self.attention.current_probability(umo)
        if not self.attention.should_respond(umo):
            # 未命中是常态（冒泡基线只有几个百分点），不打日志
            self.attention.record_soft_miss(umo)
            return False

        # 命中：刷新活跃度、记 soft_hit、把消息叫醒
        self.attention.record_interaction(umo)
        self.attention.record_soft_hit(umo)
        event.set_extra(X_ATTN_WAKE, True)
        # 这两行是让框架走 LLM 链路的关键 —— `ProcessStage` 只看它
        event.is_at_or_wake_command = True
        event.is_wake = True
        try:
            _ex = self.attention.explain(umo)
            logger.info(
                f"[chatcore_neo] 注意力·命中 → 主动接话 umo={umo!r} "
                f"概率={prob:.4f} 空闲={_ex.get('idle_minutes')}分 "
                f"基值={_ex.get('base')} 加成={_ex.get('boosts')} "
                f"退避={_ex.get('backoff')} 时段={_ex.get('time_multiplier')} "
                f"读空气={_ex.get('read_air')} "
                f"未回应={_ex.get('soft_misses')} 消息={text[:40]!r}")
        except Exception:
            logger.info(
                f"[chatcore_neo] 注意力·命中 → 主动接话 umo={umo!r} "
                f"概率={prob:.4f} 消息={text[:40]!r}")
        return True

    def _is_hard_trigger(self, event: AstrMessageEvent) -> bool:
        """被 @ / 唤醒词 / 指令 → 必须回复（硬触发）。

        注意用 isinstance 判组件类型，不做类名字符串匹配
        （硬性约束 6：禁止字符串反射）。
        """
        # **不要**用 `is_wake_up` / `is_wake` 判断硬触发：
        # `is_wake_up()` 就是 `return self.is_wake`（框架
        # `astr_message_event.py`），而 `WakingCheckStage:223-224` 在
        # **任何一个插件的 handler 过滤器通过**时都会置 `is_wake=True` ——
        # 本插件自己注册了 `EventMessageType.ALL` 的 `on_any_message`，
        # 于是**每条群消息**的 `is_wake` 都是 True，拿它当判据会让
        # 每条消息都被当成"硬触发、必须回复"，注意力/判定器/隐性意图
        # 三条链路全部失效（实测 `judge_calls` 恒为 0）。
        # 旧 chatcore_lite 的 `_is_hard_trigger`（`main.py:833-850`）本来
        # 就没有这个判据，是移植时新加进来的回归。
        try:
            from astrbot.api.message_components import At
            self_id = str(event.get_self_id() or "")
            for comp in event.get_messages() or []:
                if isinstance(comp, At) and str(getattr(comp, "qq", "")) == self_id:
                    return True
        except Exception:
            pass
        if (event.message_str or "").lstrip().startswith("/"):
            return True
        # 叫醒词消息：剥词后 is_at_or_wake_command 已被 `_maybe_call_prefix`
        # 置真，上面那条 `/` 判据再也认不出它（词已经没了）。
        # 硬编码 "/" 只覆盖默认唤醒前缀，叫醒词要单独认。
        try:
            if event.get_extra(X_CALL_PREFIX, False):
                return True
        except Exception:
            pass
        # attention_wake_by_role：管理员的普通发言也算硬触发
        if bool(self._cfg("attention_wake_by_role", False)):
            try:
                if event.is_admin():
                    return True
            except Exception:
                pass
        return False

    async def _decide_reply(self, event: AstrMessageEvent, umo: str,
                            uid: str, doc: PersonaDoc, msg: str) -> bool:
        """返回 True 表示应当继续走管道（即真的回复）。"""
        try:
            if self._is_hard_trigger(event):
                self.attention.record_interaction(umo)
                self.attention.record_hard_trigger(umo)
                # attention_hard_trigger_force=False 时，硬触发只抬概率、不强制回复
                if bool(self._cfg("attention_hard_trigger_force", True)):
                    return True
            if not bool(self._cfg("enable_attention", True)):
                return True

            # 别人的普通发言：先记一笔（读空气用），再看注意力概率
            self.attention.record_others_message(umo)
            _prob = self.attention.current_probability(umo)
            if bool(event.get_extra(X_ATTN_WAKE, False)):
                # 这条消息是 `_maybe_attention_wake` 掷中后主动叫醒的，
                # 骰子已经掷过 —— 再掷一次就是双重概率，会放大回复率
                triggered = True
            else:
                triggered = self.attention.should_respond(umo)
            if triggered and not bool(event.get_extra(X_ATTN_WAKE, False)):
                # 只在命中时打，且**不重复**：attention 唤醒的消息
                # 已在 `_maybe_attention_wake` 里打过一条（含各因子）。
                try:
                    _ex = self.attention.explain(umo)
                    logger.info(
                        f"[chatcore_neo] 注意力·命中 umo={umo!r} uid={uid!r} "
                        f"概率={_prob:.4f} "
                        f"空闲={_ex.get('idle_minutes')}分 "
                        f"基值={_ex.get('base')} 加成={_ex.get('boosts')} "
                        f"退避={_ex.get('backoff')} 时段={_ex.get('time_multiplier')} "
                        f"读空气={_ex.get('read_air')} "
                        f"未回应={_ex.get('soft_misses')} 消息={msg[:40]!r}")
                except Exception as _e:
                    logger.debug(f"[chatcore_neo] 注意力日志失败: {_e!r}")

            # 注意力没命中 → 给隐性意图一次机会（捕捉"没点名但在等回应"）
            # 按间隔抽样：同一会话在该间隔内最多分析一次，避免每条都调模型
            if not triggered and bool(self._cfg("enable_intent", True)) \
                    and self._intent_due(umo):
                score = await self.judge.intent_score(umo, msg,
                                prompt=str(self._cfg("intent_prompt", "") or ""))
                if score > 0:
                    self.attention.bump_probability(
                        umo, float(self._cfg("intent_bump", 0.09) or 0.0))
                if score >= int(self._cfg("intent_trigger_score", 70) or 70):
                    triggered = True
                    # 只在隐性意图真的把消息推进"要回复"时才打
                    logger.info(
                        f"[chatcore_neo] 注意力·隐性意图命中 umo={umo!r} "
                        f"得分={score} 阈值="
                        f"{int(self._cfg('intent_trigger_score', 70) or 70)}")

            if not triggered:
                self.attention.record_soft_miss(umo)
                return False
            self.attention.record_soft_hit(umo)

            # 注意力命中的消息再送判定器（可关闭）
            hint = (f"注意力命中，当前概率 "
                    f"{self.attention.current_probability(umo):.3f}")
            skip, why = await self.judge.should_skip(
                event, umo, uid, msg, self._recent_msgs(umo, uid),
                attention_triggered=True, attention_hint=hint)
            if skip:
                self.attention.record_soft_miss(umo)
                logger.info(f"[chatcore_neo] 判定不回复({why}): {msg[:30]!r}")
                return False
            return True
        except Exception as e:
            # 决策本身绝不能吞消息
            try:
                self._stats["errors"] += 1
                self._last_error = repr(e)
            except Exception:
                pass
            logger.warning(f"[chatcore_neo] 接话决策异常，按回复处理: {e!r}")
            return True

    def _intent_due(self, umo: str) -> bool:
        """隐性意图的间隔闸门。interval<=0 表示不限制。"""
        try:
            iv = int(self._cfg("intent_interval_minutes", 30) or 0) * 60
            if iv <= 0:
                return True
            now = time.time()
            last = self._intent_last.get(umo, 0.0)
            if now - last < iv:
                return False
            self._intent_last[umo] = now
            return True
        except Exception:
            return False

    def _recent_msgs(self, umo: str, uid: str = "") -> list:
        """取该会话最近的对话，供判定器参考。

        **必须带 uid**：群聊状态存在 `{umo}/{persona}/{uid}.json`，
        不带 uid 读的是会话级文件（群聊里根本不存在）→ 判定器永远只看到
        空历史。私聊则相反，uid 必须是空串。
        """
        try:
            doc = self._persona_for(umo)
            if doc is None:
                return []
            st = self.state.load(umo, doc.id, uid)
            return list(st.conversation_log)[-10:]
        except Exception:
            return []


    # ---------------------------------------------------------------- 群聊说话人
    _LIVE_WINDOW = 1800          # 流水保留窗口（秒）：30 分钟
    _LIVE_LIMIT = 24             # 每个会话最多保留的群发言条数
    _LIVE_INJECT = 16            # 注入给模型的最多条数

    def _record_live(self, event) -> None:
        """把一条群聊发言记进流水（带 uid + 昵称）。

        只记真实用户消息：私聊/合成事件/gutoken 不计。命令类消息也记
        （不影响正确性，能帮模型理解群内语境）。
        """
        try:
            if event.is_private_chat():
                return
        except Exception:
            return
        uid = str(event.get_sender_id() or "")
        if not uid:
            return
        text = (event.message_str or "").strip()
        if not text or len(text) > 300:
            return
        name = str(event.get_sender_name() or "") or uid
        umo = event.unified_msg_origin
        buf = self._live.setdefault(umo, [])
        buf.append({"u": uid, "n": name, "t": text, "ts": int(time.time())})
        cutoff = time.time() - self._LIVE_WINDOW
        buf[:] = [x for x in buf if x["ts"] >= cutoff][-self._LIVE_LIMIT:]

    def _live_text(self, umo: str, exclude_uid: str = "",
                   exclude_text: str = "") -> str:
        """把流水渲染成带说话人与时间的文本（供注入）。

        行格式 `[10-01 周三 13:39] 昵称（uid）：文本` —— 时间前缀是为了
        补回「历史 reminder 被清理」后丢掉的时间线索（框架原流水块带时间，
        本插件自建的这份原本没有）。

        `exclude_uid/exclude_text` 用于跳过"本轮正在被处理的这条发言"，
        避免模型把它当历史再读一遍。
        """
        buf = self._live.get(umo) or []
        lines = [
            f"{_live_stamp(x['ts'])} {x['n']}（{x['u']}）：{x['t']}"
            for x in buf
            if not (exclude_uid and x["u"] == exclude_uid
                    and x["t"] == exclude_text)
        ]
        return "\n".join(lines[-self._LIVE_INJECT:])

    def _resolve_session_params(self, umo: str, is_private: bool) -> tuple[int, bool]:
        """求某会话最终生效的 (发言模式, 是否启用 NSFW)。

        显式覆盖（_session_params.json）> 会话类型默认：群聊=纯信息/关NSFW，
        私聊=全局 private_msg_mode / 开NSFW。解析细节见 core/session_params.py。
        """
        from .core.session_params import resolve
        try:
            ov = dict(self._session_params.get(umo) or {})
        except Exception:
            ov = {}
        global_mode = int(self._cfg("private_msg_mode", 1) or 1)
        return resolve(is_private, ov, global_mode=global_mode)

    @staticmethod
    def _nsfw_suppressed(event: AstrMessageEvent) -> bool:
        """本轮的合成事件是否应**强制关闭 NSFW**。

        bot 自己发起的轮次（主动消息 / 戳一戳回应）不主动开车 → 压掉 NSFW。
        **`persona_switch` 的重入不算**：它是用户对话的延续（换人格后接着回复
        同一条消息），NSFW 必须跟会话参数走 —— 否则会出现"会话明明开着 NSFW、
        一换人格那一轮就失效"。

        只影响 NSFW；`proactive` 的其它语义保持不变（群聊接话决策跳过、
        状态文案口径等仍按合成事件处理）。
        """
        if not compat.is_simulated(event):
            return False
        return compat.simulated_source(event) != "persona_switch"

    # ------------------------------------------------------------ 会话存活
    def _alive_snapshot(self) -> set[str] | None:
        """同步读「AstrBot 侧会话存活集」；没拉到或已过期返回 None。

        返回的集合是 **safe 段格式**（与 state 目录名同构），可以直接和
        `state.sessions()` 的结果做集合运算。None 表示未知 —— 读取方回退
        到目录判定（fail-open），并按需后台补拉。
        """
        if self._alive is None:
            return None
        if time.monotonic() - self._alive_ts > self._alive_ttl:
            return None
        return self._alive

    def _kick_alive_refresh(self) -> None:
        """后台补拉一次存活集（幂等，5s 内不重复建任务）。"""
        now = time.monotonic()
        if now - self._alive_kick_ts < 5.0:
            return
        self._alive_kick_ts = now
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # 没有事件循环（比如纯同步测试）→ 不拉，等有循环再说
        try:
            asyncio.create_task(self._refresh_alive())
        except Exception:
            pass

    async def _refresh_alive(self, force: bool = False) -> set[str] | None:
        """从 AstrBot DB 拉取「仍有对话记录的会话 UMO 集合」（safe 段格式）。

        谁在调用：主动消息每次 tick 出话前（force）、WebUI 会话/主动消息页
        （force）、以及每条用户消息进来后（TTL 内自动去重）。查询失败保留
        旧值 —— 别因为一次 DB 抖动把会话全判成孤儿。
        """
        if not force and self._alive_snapshot() is not None:
            return self._alive
        try:
            db = self.context.get_db()
            convs = await db.get_conversations()
            alive = {safe_segment(str(c.user_id)) for c in convs
                     if getattr(c, "user_id", None)}
            self._alive = alive
            self._alive_ts = time.monotonic()
            return alive
        except Exception as e:
            logger.debug(f"[chatcore_neo] 拉取 AstrBot 会话存活集失败: {e!r}")
            return self._alive

    def _session_known(self, umo: str) -> bool:
        """该会话现在还算不算一个真会话 —— 供主动消息调度器过滤账本。

        判据有**两层**：

        1. **本插件留过状态文件**。`state.save()` 只在 `on_llm_response`
           里调用，也就是"机器人真的在这里回过话"。于是：
           * 正常聊过的会话 → 有状态目录 → 第一层通过；
           * 测试脚本打进来的假会话（`webchat:FriendMessage:webchat!astrbot!
             newapi-balance-e2e-*`、`BotB:GroupMessage:1`）只在 `on_any_message`
             里被记了账本，没有状态目录 → 未知，不再开口、不再显示、可一键清。

        2. **AstrBot 侧还认这个会话**（DB `conversations` 里还有该 UMO 的
           对话记录）。用户在 AstrBot 删掉会话后，插件目录还在，但 AstrBot
           不会再给它建对话 —— 这类「孤儿会话」第一层通过、第二层不通过，
           同样不再开口、不再显示、可一键清（见 webadmin.build_sessions）。

        存活集未拉取/已过期时回退到第一层（fail-open），并按需后台补拉。
        判定异常时同样返回 True：宁可多发一条，也不要因为一次读取失败把
        整条主动消息链路静默掐死。
        """
        try:
            root = self.state.root
            mtime = root.stat().st_mtime_ns
            cache = getattr(self, "_known_cache", None)
            # 一次 tick / 一次页面渲染里会对每个会话问一遍 —— 用状态根目录的
            # mtime 做键缓存，增删会话时目录 mtime 必变，缓存即失效，
            # 既准确又把 O(会话数) 次目录扫描压成 1 次 stat。
            if cache is not None and cache[0] == mtime:
                known = cache[1]
            else:
                known = set(self.state.sessions())
                self._known_cache = (mtime, known)
            # 第二层：AstrBot 侧存活集只做同步读；未知时后台补拉。
            alive = self._alive_snapshot()
            if alive is not None:
                known = {k for k in known if k in alive}
            else:
                self._kick_alive_refresh()
            return safe_segment(umo) in known
        except Exception:
            return True

    async def _emit_proactive(self, umo: str) -> bool:
        """合成事件 → 投回原生队列。**本插件不做任何 provider 调用。**"""
        from .core.proactive import SIMULATED_USER_TEXT
        tpl = self.templates.get(umo)
        if tpl is None:
            # 没有模板就没法合成（该会话还没来过消息）——记录并跳过
            logger.debug(f"[chatcore_neo] 主动消息跳过（无事件模板）: {umo}")
            return False
        ev = synthesize_event(tpl, SIMULATED_USER_TEXT, self.context, umo=umo)
        if ev is None:
            logger.warning("[chatcore_neo] 无法合成事件（平台事件类不匹配），"
                           "主动消息未发出")
            return False
        ok = await compat.reenter_pipeline(ev, context=self.context,
                                           source="proactive")
        if ok:
            self.scheduler.note_proactive(umo)
            logger.info(f"[chatcore_neo] 主动消息已重入管道: {umo}")
        return ok

    # ------------------------------------------------------------------ 流式传输
    #
    # 目标（"流式开启时"的三条约定）：
    #   ① LLM 请求走流式（token 一到就能用）；
    #   ② **实时**做正则处理（`§n` / `[[at:]]` / 句尾标点）并分段、逐条发出；
    #   ③ **不用分段子代理** —— 文本已经在往外发了，等一次 LLM 分段回来
    #      早就晚了；而且子代理只吃"完整回复"，流式里根本没有完整回复。
    #
    # 落地方式：把事件对象上的 `send_streaming` 换成自己的实现
    # （框架在 RespondStage 里调它交付流式结果，见
    # `pipeline/respond/stage.py:224`）。包装只挂在**本事件实例**上，
    # 不影响其它插件、其它平台的事件。
    #
    # 为什么不改框架配置：AstrBot 的 `provider_settings.streaming_response`
    # 是全局的（所有平台、所有插件一起变），而 `event.set_extra
    # ("enable_streaming", …)` 是框架留给调用方的**逐事件**开关
    # （`agent_sub_stages/internal.py:171`），正好用来做插件级开关。

    def _stream_on(self) -> bool:
        """插件级流式开关。"""
        try:
            return bool(self._cfg("enable_streaming", False))
        except Exception:
            return False

    def _stream_prepare(self, event: AstrMessageEvent) -> bool:
        """放行本轮流式 + 接管流式发送；返回是否就位。

        **必须在 LLM 请求之前调用**（挂 `on_any_message`）：`enable_streaming`
        只在 AgentRequestSubStage 开头被读一次，读完再置不会生效。
        """
        if not self._cfg("enabled", True) or not self._stream_on():
            return False
        if compat.should_skip_side_effects(event):
            return False
        try:
            event.set_extra(X_STREAM_REQ, True)
            event.set_extra("enable_streaming", True)
        except Exception as e:
            logger.debug(f"[chatcore_neo] 置流式开关失败: {e!r}")
            return False
        # 只接管**没有原生流式**的平台（aiocqhttp 等 `support_streaming_message
        # = False`）。有原生流式的平台（webchat / 飞书 / Telegram …）自己会把
        # 流式渲染成"一条不断变长的消息"，而它们的 `send_streaming` 带着协议
        # 副作用（webchat 要靠末帧 `complete` 收尾）—— 硬接管会让那条消息
        # 永远停在"生成中"。这类平台保持框架原样，只保留"禁用子代理分段"。
        try:
            native = bool(getattr(event.platform_meta,
                                  "support_streaming_message", True))
        except Exception:
            native = True
        if native:
            logger.debug(f"[chatcore_neo] 该平台有原生流式，交给框架发送: "
                         f"{event.unified_msg_origin!r}")
            return False
        return self._stream_install(event)

    def _stream_install(self, event: AstrMessageEvent) -> bool:
        """把本事件的 `send_streaming` 换成插件实现（幂等）。"""
        try:
            if event.get_extra(X_STREAM_READY, False):
                return True
            if not hasattr(event, "send_streaming"):
                self._stream_stats["degraded"] += 1
                logger.warning("[chatcore_neo] 事件不支持 send_streaming，"
                               "流式退回框架默认发送")
                return False

            async def _send(generator, use_fallback: bool = False):
                return await self._stream_deliver(event, generator,
                                                  use_fallback)

            event.send_streaming = _send
            event.set_extra(X_STREAM_READY, True)
            logger.debug(f"[chatcore_neo] 已接管流式发送: "
                         f"umo={event.unified_msg_origin!r}")
            return True
        except Exception as e:
            self._stream_stats["degraded"] += 1
            logger.warning(f"[chatcore_neo] 接管流式发送失败"
                           f"（退回框架默认）: {e!r}")
            return False

    def _stream_env(self, event: AstrMessageEvent) -> dict:
        """一次流式发送所需的环境快照（会话参数 / 说话人 / 限速）。"""
        umo = ""
        try:
            umo = event.unified_msg_origin
        except Exception:
            pass
        try:
            is_private = bool(event.is_private_chat())
        except Exception:
            is_private = True
        mode = MODE_NARRATIVE if is_private else MODE_PLAIN
        try:
            mode = self._resolve_session_params(umo, is_private)[0]
        except Exception:
            pass
        try:
            names = tuple(self._speaker_names(umo, is_private) or ())
        except Exception:
            names = ()
        # 合成事件（主动消息/戳一戳回应）没有真实的本轮说话人：传空值给
        # `fix_literal_mentions` 是安全的（匹配不上就退化成纯文本）。
        if compat.is_simulated(event):
            uid, uname = "", ""
        else:
            try:
                uid = str(event.get_extra(X_UID, "") or event.get_sender_id() or "")
            except Exception:
                uid = ""
            try:
                uname = str(event.get_sender_name() or "")
            except Exception:
                uname = ""
        return {"umo": umo, "is_private": is_private, "mode": mode,
                "names": names, "uid": uid, "uname": uname,
                "split": self._stream_split_ok(event),
                "interval": max(0.0, float(
                    self._cfg("stream_send_interval", 0.8) or 0.0)),
                "markers": bool(self._cfg("enable_markers", True)),
                "last_send": 0.0, "lock": asyncio.Lock()}

    def _stream_split_ok(self, event: AstrMessageEvent) -> bool:
        """流式这条链上要不要分段。

        与非流式输出层保持同一套判据：命令输出默认**不切**
        （`segment_command_output=False`）—— 命令返回的是结构化文本，
        多行结果被切开就散了。流式下只是退化成"整段收尾发一条"。
        """
        if not bool(self._cfg("enable_segmentation", True)):
            return False
        try:
            if self._is_command_result(event) and \
                    not bool(self._cfg("segment_command_output", False)):
                return False
        except Exception:
            pass
        return True

    async def _stream_deliver(self, event: AstrMessageEvent, generator,
                              use_fallback: bool = False):
        """接管流式发送：边收边正则处理 → 分段 → 实时发出。

        **为什么必须接管**：框架的 `send_streaming()` 在 aiocqhttp 上只有
        两条路 ——（a）`unsupported_streaming_strategy=realtime_segmenting`
        （默认）用 `[^。？！~…]+[。？！~…]+` 硬切、每条固定间隔 1.5 秒，
        而且**完全不经过本插件的输出层**（`[[at:]]`/`[[reply:]]` 标记、
        `§n` 残渣、句尾标点清理、剧情向括号规则统统失效）；
        （b）`turn_off` 时整段攒完再发（等于没有流式）。
        这里两条都换掉：切分交给 `core/stream.py`，正则处理与消息链构造
        复用 `_stream_emit()`，与非流式 `on_decorating_result` 同源。

        `use_fallback` 是框架给的"平台不支持流式"提示（aiocqhttp 恒为真），
        在这里**没有意义**：发送由插件自己做，与平台能力无关，故不使用。
        """
        from astrbot.core.message.message_event_result import MessageChain
        from astrbot.api.message_components import Plain

        ctx = self._stream_env(event)
        sp = StreamSplitter(
            max_len=int(self._cfg("segment_max_len", 120) or 120),
            min_len=int(self._cfg("segment_min_len", 8) or 8),
            mode=ctx["mode"], names=ctx["names"], enabled=ctx["split"])
        stats = self._stream_stats
        stats["turns"] += 1
        t0 = time.time()
        acc = {"cut": 0, "sent": 0, "chars": 0, "first": "", "warned": False}
        err = ""

        async def _emit(segs) -> None:
            for raw in segs:
                acc["cut"] += 1
                if not acc["first"]:
                    acc["first"] = (raw or "").strip()[:40]
                try:
                    ok, n = await self._stream_emit(event, raw, ctx)
                except Exception as e:      # 单段失败不拖垮整轮
                    if not acc["warned"]:
                        acc["warned"] = True
                        logger.warning(
                            f"[chatcore_neo] 流式发送失败（本轮首个失败，"
                            f"后续同类不再重复告警）: {e!r}")
                    continue
                if ok:
                    acc["sent"] += 1
                    acc["chars"] += n
                    stats["segments"] += 1
                    stats["chars"] += n

        try:
            async for chain in generator:
                kind = str(getattr(chain, "type", "") or "")
                if kind == "reasoning":
                    continue                    # 思考过程不进正文（框架亦不显示）
                if kind == "break":
                    # 工具调用边界的"分段信号"：把已生成的内容先发出去，
                    # 否则工具跑几十秒，前面那段话会一直卡在缓冲区里。
                    await _emit(sp.flush())
                    continue
                comps = list(getattr(chain, "chain", None) or [])
                for comp in comps:
                    text = getattr(comp, "text", None)
                    if isinstance(comp, Plain) and isinstance(text, str):
                        await _emit(sp.feed(text))
                    else:
                        # 非文本组件（图片/语音/转发…）：先把攒的文字发掉，
                        # 再原样转发这个组件，保持顺序。
                        await _emit(sp.flush())
                        try:
                            await event.send(MessageChain([comp]))
                            acc["sent"] += 1
                            acc["cut"] += 1
                        except Exception as e:
                            logger.debug(
                                f"[chatcore_neo] 流式转发非文本组件失败: {e!r}")
            await _emit(sp.flush())
        except asyncio.CancelledError:
            # 撤回取消/停轮：已经发出去的不可能收回，剩下的不再补发
            logger.info(f"[chatcore_neo] 流式被取消 umo={ctx['umo']!r} "
                        f"已发 {acc['sent']} 段 {acc['chars']} 字")
            raise
        except Exception as e:
            err = repr(e)
            stats["errors"] += 1
            stats["last_error"] = err
            logger.warning(f"[chatcore_neo] 流式发送异常 umo={ctx['umo']!r} "
                           f"已发 {acc['sent']} 段 {acc['chars']} 字 错误={e!r}")
            # 尽力把已经收到的文字发出去 —— 用户的等待不该白费
            try:
                await _emit(sp.flush())
            except Exception as e2:
                logger.warning(f"[chatcore_neo] 流式收尾也失败了: {e2!r}")
        finally:
            try:
                event.set_extra(X_STREAMED, True)
                event.set_extra(X_STREAM_SENT, int(acc["sent"]))
                event._has_send_oper = True
            except Exception:
                pass
            ms = (time.time() - t0) * 1000
            stats["last_turn_ms"] = int(ms)
            logger.info(
                f"[chatcore_neo] 流式发送完成 umo={ctx['umo']!r} "
                f"切段={acc['cut']} 发出={acc['sent']} 共 {acc['chars']} 字 "
                f"耗时 {ms:.0f}ms "
                f"模式={'剧情向' if ctx['mode'] == MODE_NARRATIVE else '纯信息'} "
                f"分段={'开' if ctx['split'] else '关'} "
                f"残留={len(sp.leftover())}字"
                + (f" 首段={acc['first']!r}" if acc["first"] else "")
                + (f" 错误={err}" if err else ""))

    async def _stream_emit(self, event: AstrMessageEvent, raw: str,
                           ctx: dict) -> tuple:
        """正则处理一个待发段并发出；返回 (是否发出, 发出字符数)。

        与非流式 `on_decorating_result` 用的是**同一批**输出层函数：
        `fix_literal_mentions` → `parse_markers` → `strip_markers`
        → `strip_legacy_segment_marks` → `clean_segments`。
        """
        from astrbot.core.message.message_event_result import MessageChain
        from astrbot.api.message_components import At, Plain, Reply

        comps: list = []
        text = strip_legacy_segment_marks(raw or "")
        if ctx.get("markers"):
            try:
                text, extra = fix_literal_mentions(
                    text, uid=ctx.get("uid", ""), user_name=ctx.get("uname", ""))
            except Exception:
                extra = []
            try:
                marks = parse_markers(text)
            except Exception:
                marks = {}
            for mid in marks.get("reply", []):
                try:
                    comps.append(Reply(id=mid))
                except Exception:
                    pass
            for qq in list(marks.get("at", [])) + list(extra or []):
                if not qq:
                    continue
                try:
                    comps.append(At(qq=qq))
                except Exception:
                    pass
            text = strip_markers(text)
        cleaned = clean_segments([text])
        text = cleaned[0] if cleaned else ""
        if not text and not comps:
            return False, 0

        iv = float(ctx.get("interval") or 0.0)
        if iv > 0 and ctx.get("last_send"):
            gap = iv - (time.time() - float(ctx["last_send"]))
            if gap > 0:
                await asyncio.sleep(gap)        # 平台限速：别把分段打成机关枪
        if text:
            comps.append(Plain(text))
        await event.send(MessageChain(comps))
        ctx["last_send"] = time.time()
        if text and self.tts.enabled:
            # 流式下 TTS 必须是**异步补发**的：合成是秒级阻塞，串在流里
            # 会把后面所有分段一起卡住。锁保证音频与文字同序。
            self._spawn(self._stream_tts(event, text, ctx))
        return True, len(text)

    async def _stream_tts(self, event: AstrMessageEvent, text: str,
                          ctx: dict) -> None:
        """流式模式下的语音：每段文字发出后补一条音频（失败静默降级）。"""
        try:
            if not self.tts.should_synthesize(
                    text, is_private=bool(ctx.get("is_private", True))):
                return
            lock = ctx.get("lock")
            if lock is None:
                rec = await self.tts.synthesize(text)
            else:
                async with lock:
                    rec = await self.tts.synthesize(text)
            if rec is None:
                return
            from astrbot.core.message.message_event_result import MessageChain
            await event.send(MessageChain([rec]))
        except Exception as e:
            logger.debug(f"[chatcore_neo] 流式 TTS 失败（降级为纯文字）: {e!r}")

    async def _stream_rescue(self, event: AstrMessageEvent) -> None:
        """流式一个字都没发出去时的兜底：把最终正文补发一次。

        触发条件是"接管过流式发送、但一个段都没发出去"（`X_STREAM_SENT == 0`）。
        最常见的原因是提供方没真流式 —— 框架在流式模式下**不转发**
        `llm_result` 链，只有 `streaming_delta` 会被交付；于是正文会随着
        STREAMING_FINISH 一起被丢掉，用户那边就是"机器人没说话"。
        宁可补一条整段消息，也不能让回复凭空消失。
        """
        try:
            res = event.get_result()
            chain = list(getattr(res, "chain", None) or [])
        except Exception:
            return
        from astrbot.api.message_components import Plain
        text = "".join(getattr(c, "text", "") or "" for c in chain
                       if isinstance(c, Plain)).strip()
        if not text:
            return
        comps = [c for c in chain if not isinstance(c, Plain)]
        comps.append(Plain(text))
        try:
            from astrbot.core.message.message_event_result import MessageChain
            await event.send(MessageChain(comps))
            logger.warning(
                f"[chatcore_neo] 流式未见任何增量，已补发整段回复 "
                f"umo={event.unified_msg_origin!r} {len(text)}字")
        except Exception as e:
            logger.warning(f"[chatcore_neo] 流式兜底补发失败: {e!r}")

    # ------------------------------------------------------------------ 输出层
    @filter.on_decorating_result(priority=50)
    async def on_decorating_result(self, event: AstrMessageEvent):
        """对最终要发出去的文本做标记解析、分段，并按需附语音。"""
        if not self._cfg("enabled", True):
            return
        # 流式接管过的轮次：文本已经**逐段实时发出**了（见 `_stream_deliver`），
        # 这里再处理一次是纯浪费 —— 框架对 STREAMING_FINISH 的结果也不会
        # 再发给用户（respond 阶段直接 return），改了链子没人看，
        # 却会白跑一次分段子代理、白合成一次 TTS。
        if event.get_extra(X_STREAMED, False):
            logger.debug("[chatcore_neo] 本轮已由流式层实时发送，输出层跳过")
            if int(event.get_extra(X_STREAM_SENT, 0) or 0) == 0:
                # 兜底：上游一个增量都没吐（提供方没真流式 / 首段之前就断了）。
                # 此时流式层一个字都没发出去，而框架也不会再发这条结果 ——
                # 不补的话用户就永远等不到回复。
                await self._stream_rescue(event)
            return
        try:
            res = event.get_result()
            if res is None or not getattr(res, "chain", None):
                return
            from astrbot.api.message_components import At, Plain, Reply
        except Exception as e:
            logger.debug(f"[chatcore_neo] 输出层读取失败: {e!r}")
            return

        # 本轮说话人（兜底转换手写 @ 时要用）
        #
        # **合成事件（主动消息）没有本轮说话人**：`get_sender_id()` /
        # `get_sender_name()` 取到的是事件模板里抄来的那个人（该会话历史上
        # 第一个说话的人）。若拿它去匹配手写的 `[@昵称]`，模型只要写下那个
        # 名字就会被转成真 @ —— 于是每条主动消息都点名同一个人。
        # 传空 uid/名字给 `fix_literal_mentions` 是安全的：它匹配不上时会把
        # `[@某人]` 退化成纯文本，既不产生 @，也不留坏括号。
        synthetic = compat.is_simulated(event)
        try:
            if synthetic:
                _uid, _sname = "", ""
            else:
                _uid = str(event.get_extra(X_UID, "")
                           or event.get_sender_id() or "")
                _sname = str(event.get_sender_name() or "")
        except Exception:
            _uid, _sname = "", ""
        _extra_at: list = []

        # ---- 0) 旧 `§n` 分段标记清理 ----
        # 新环境没有标记协议（分段子代理返回 JSON 段数组），提示词也已不再教它；
        # 但旧对话历史/模型惯性仍可能写出来，而链路上没人删 —— 实测它原样发给了
        # 用户。这里统一在**所有分支之前**清一遍：无论标记/分段开关怎么配，
        # 都不会漏。转成换行（= 分段边界）而不是删除，免得两句被粘成一句。
        try:
            if any(isinstance(c, Plain) and "§" in (getattr(c, "text", "") or "")
                   for c in res.chain):
                res.chain = [
                    Plain(strip_legacy_segment_marks(c.text))
                    if isinstance(c, Plain) else c
                    for c in res.chain]
                logger.info("[chatcore_neo] 已清除模型输出里的旧 §n 分段标记")
        except Exception as e:
            logger.debug(f"[chatcore_neo] §n 清理失败: {e!r}")

        # 命令输出**不做**标记/分段：命令返回的是结构化文本
        # （帮助列表、多行结果），里面的换行是有意义的；按"聊天回复"的规则
        # 去切会把它拆成多条消息、换行也丢了 —— 实测 `/-luck` 的
        # "你今天的运气是: 105\n你今天的运气也太好了吧！" 被切成两条。
        cmd_output = self._is_command_result(event)
        if cmd_output and not bool(self._cfg("segment_command_output", False)):
            pass                                    # 原样保留
        # ---- 1) 标记与分段（各自独立开关）----
        elif self._cfg("enable_segmentation", True) or \
                self._cfg("enable_markers", True):
            try:
                new_chain = []
                for comp in res.chain:
                    if not isinstance(comp, Plain):
                        new_chain.append(comp)
                        continue
                    text = comp.text or ""
                    if bool(self._cfg("enable_markers", True)):
                        # 模型经常无视协议、手写 `[@昵称]`；这里先兜底
                        # 转成正规标记（能对上本轮说话人就变成真 @，
                        # 对不上就退化成纯文本，绝不留坏括号）
                        try:
                            text, _extra = fix_literal_mentions(
                                text, uid=_uid,
                                user_name=_sname)
                            for _q in _extra:
                                if _q and _q not in _extra_at:
                                    _extra_at.append(_q)
                        except Exception:
                            pass
                        marks = parse_markers(text)
                        for qq in marks.get("reply", []):
                            try:
                                new_chain.append(Reply(id=qq))
                            except Exception:
                                pass
                        for qq in list(marks.get("at", [])) + _extra_at:
                            if not qq:
                                continue
                            try:
                                new_chain.append(At(qq=qq))
                            except Exception:
                                pass
                        text = strip_markers(text)
                    if bool(self._cfg("enable_segmentation", True)):
                        # 先试分段子代理（LLM 返回 JSON 段数组）；
                        # 返回空则回退本地标点规则。
                        # 提示词按会话的**发言模式**路由：剧情向走
                        # 「括号与台词分开、说话不细拆」那份（见 segment_agent）。
                        _priv = event.is_private_chat()
                        _mode = self._resolve_session_params(
                            event.unified_msg_origin, _priv)[0]
                        # 多人格同台生效时，模型被要求用【角色名】标说话人
                        # （见 core/costars.py:build_block）；名字集合交给下面的
                        # 归位逻辑用，避免名牌挂错段（见 _speaker_names）。
                        _names = self._speaker_names(
                            event.unified_msg_origin, _priv)
                        segs = []
                        # **流式轮次不调分段子代理**（硬约定）：这一轮的文本
                        # 已经由流式层实时切好发出去了，走到这里只是因为
                        # 框架不支持流式（`turn_off`）而降级成整段回复 ——
                        # 这种情况下也用本地规则切，绝不为了分段再烧一次
                        # LLM（子代理对"正在往外流的文本"本来也帮不上忙）。
                        # 另外 `enabled_for` 里还有一道同源硬闸（`enable_streaming`
                        # 一开就整体停用，WebUI 对应开关会隐藏），这里是双保险。
                        _streamed = bool(event.get_extra(X_STREAM_REQ, False))
                        if self.segmenter.enabled_for(is_private=_priv) \
                                and not _streamed:
                            try:
                                segs = await self.segmenter.segment(
                                    text, is_private=_priv,
                                    umo=event.unified_msg_origin,
                                    msg_mode=_mode)
                            except Exception as e:
                                logger.debug(
                                    f"[chatcore_neo] 分段子代理失败: {e!r}")
                        elif _streamed:
                            logger.debug(
                                "[chatcore_neo] 流式轮次：跳过分段子代理，"
                                "改用本地正则规则")
                        if not segs:
                            if _mode == MODE_NARRATIVE:
                                # 剧情向本地兜底与子代理同一套机械规则：
                                # 括号一段、括号间说话一段、说话不细拆
                                segs = split_narrative(text)
                            else:
                                segs = split_raw(
                                    text,
                                    max_len=int(self._cfg(
                                        "segment_max_len", 120) or 120),
                                    min_len=int(self._cfg(
                                        "segment_min_len", 8) or 8))
                        # 角色名前缀归位（多人格同台）：夹在段中/段尾的
                        # `【艾卡】` 断成新段、挂在它自己那段前面。必须放在
                        # 标点清理**之前** —— 归位后再统一判句尾标点。
                        if _names:
                            segs = normalize_speaker_segments(segs, _names)
                        segs = clean_segments(segs)
                        # 与旧实现一致：**只有真的切成 2 段以上才替换链**。
                        # 单段说明"不需要分段"，此时保留原文（含句尾标点），
                        # 不做标点清理 —— 这是可观察行为，别改。
                        if len(segs) < 2:
                            segs = [text] if text else []
                    else:
                        segs = [text] if text else []
                    for s in segs:
                        new_chain.append(Plain(s))
                if new_chain:
                    res.chain = new_chain
            except Exception as e:
                logger.debug(f"[chatcore_neo] 标记/分段处理失败: {e!r}")

        # ---- 2) TTS（独立于上面两个开关，失败静默降级为纯文字）----
        if self.tts.enabled:
            try:
                text_only = "".join(
                    c.text for c in res.chain
                    if isinstance(c, Plain) and getattr(c, "text", ""))
                if self.tts.should_synthesize(
                        text_only, is_private=event.is_private_chat()):
                    rec = await self.tts.synthesize(
                        text_only[:self.tts.max_chars])
                    if rec is not None:
                        res.chain = list(res.chain) + [rec]
            except Exception as e:
                logger.debug(f"[chatcore_neo] TTS 失败: {e!r}")

    # ------------------------------------------------------------------ /persona
    @filter.command("character")
    async def character_command(self, event: AstrMessageEvent):
        """/character [list|add <角色>|del <角色>|match <文本>|<角色> [外号...]]

        角色名/外号库的编辑入口（数据在 plugin_data/chatcore_neo/characters.json）。
        外号可一次性给多个（空格 / 逗号 / 顿号分隔），**整条覆盖**该角色的外号。
        """
        tokens = event.message_str.strip().split()
        if tokens and tokens[0].lstrip("/") == "character":
            tokens = tokens[1:]
        head = tokens[0] if tokens else ""

        if not head or head in ("list", "ls"):
            entries = self.char_matcher.entries()
            body = [f"🎭 角色库（{len(entries)} 个）："]
            shown = 0
            for h in entries:
                if not h.aliases and not h.brief:
                    continue
                al = ("　外号：" + "、".join(h.aliases)) if h.aliases else ""
                body.append(f"  · {h.name}{al}")
                shown += 1
                if shown >= 40:
                    break
            if shown == 0:
                body.append("  （还没人填外号）")
            body.append("\n设置外号：/character <角色名> 外号1 外号2")
            yield event.plain_result("\n".join(body))
            return

        if head in ("add", "new"):
            if len(tokens) < 2:
                yield event.plain_result("用法：/character add <角色名>")
                return
            name = " ".join(tokens[1:]).strip()
            ok = self.characters.add_character(name)
            self.reload_characters()
            yield event.plain_result(
                f"已新增角色「{name}」" if ok else f"角色「{name}」已存在")
            return

        if head in ("del", "rm", "remove"):
            if len(tokens) < 2:
                yield event.plain_result("用法：/character del <角色名>")
                return
            name = " ".join(tokens[1:]).strip()
            ok = self.characters.delete_character(name)
            self.reload_characters()
            yield event.plain_result(
                f"已删除角色「{name}」及其外号" if ok else f"角色库中没有「{name}」")
            return

        if head == "match":
            text = " ".join(tokens[1:]).strip()
            doc = self._persona_for(event.unified_msg_origin)
            block = self._character_block(text, doc)
            yield event.plain_result(
                f"输入：{text}\n\n{block}" if block else f"输入：{text}\n\n（没有命中任何角色）")
            return

        # `<角色名>` = 查看；`<角色名> <外号...>` = 覆盖设置
        name = tokens[0]
        if len(tokens) == 1:
            hit = self.char_matcher.hit(name, self._persona_for(event.unified_msg_origin))
            if not hit.aliases and not hit.brief and not hit.en:
                yield event.plain_result(
                    f"角色库中没有「{name}」。\n新增：/character add {name}\n"
                    f"设置外号：/character {name} 外号1 外号2")
                return
            lines = [f"🎭 {hit.name}" + (f"（{hit.en}）" if hit.en else "")]
            if hit.brief:
                lines.append(f"简介：{hit.brief}")
            lines.append("外号：" + ("、".join(hit.aliases) if hit.aliases else "（无）"))
            if hit.relation:
                lines.append(f"与当前人格的关系：{hit.relation}")
            yield event.plain_result("\n".join(lines))
            return

        raw = ",".join(tokens[1:])
        if name not in self.characters.load():
            self.characters.add_character(name)      # 允许顺手建新角色
        ok = self.characters.set_aliases(name, raw)
        self.reload_characters()
        if not ok:
            yield event.plain_result(f"设置失败：角色库中没有「{name}」")
            return
        aliases = self.characters.load().get(name) or []
        yield event.plain_result(
            f"「{name}」的外号已设为：" + ("、".join(aliases) if aliases else "（空）"))
        return

    @filter.command("persona")
    async def persona_command(self, event: AstrMessageEvent):
        """/persona [list|current|<名称>|reset|global <名称>|msg 1|2|doctor|preview|new|edit|add|rm|inject|reload]"""
        args = event.message_str.strip().split()
        if args and args[0].lstrip("/") == "persona":
            args = args[1:]
        umo = event.unified_msg_origin
        sub = args[0] if args else ""

        if not sub or sub in ("current", "cur"):
            doc = self._persona_for(umo)
            if doc is None:
                yield event.plain_result("当前没有任何可用人格")
                return
            lines = [f"当前人格：{doc.display_name}（{doc.id}）"]
            if umo in self._session_persona:
                lines.append("（本会话已覆盖，用 /persona reset 恢复全局默认）")
            lines.append(f"世界观：{doc.world or '（未绑定）'}"
                         f"　称谓：{doc.world_role or '-'}")
            lines.append(f"全局默认：{self._cfg('persona_current', '') or '（未设置）'}")
            lines.append(self._persona_sub_help())
            yield event.plain_result("\n".join(lines))
            return

        if sub == "help":
            yield event.plain_result(self._persona_sub_help())
            return

        if sub == "list":
            docs = self.personas.all()
            _inst = inst_of_umo(umo)
            _pool = getattr(self, "_persona_pool", None)
            if _pool is not None and _inst:
                docs = _pool.available_docs(_inst, docs)
            if not docs:
                yield event.plain_result(
                    f"📋 本实例（{_inst}）没有授权任何人格。\n"
                    "请到 WebUI「人格分配」把人格拖到该实例。")
                return
            cur = self._persona_for(umo)
            body = ["📋 可用人格："]
            for d in docs:
                mark = " ← 当前" if cur and d.id == cur.id else ""
                w = f"[{d.world}]" if d.world else "[无世界观]"
                body.append(f"  · {d.display_name}（{d.id}）{w}{mark}")
            body.append(f"\n共 {len(docs)} 个；用 /persona <名称> 切换")
            yield event.plain_result("\n".join(body))
            return

        if sub == "reset":
            self._session_persona.pop(umo)
            doc = self._persona_for(umo)
            yield event.plain_result(
                f"已恢复全局默认人格：{doc.display_name if doc else '（无）'}")
            return

        if sub == "global" and len(args) >= 2:
            doc = self.personas.resolve(args[1])
            if doc is None:
                yield event.plain_result(self._not_found(args[1]))
                return
            if not self._persona_ok(umo, doc):
                yield event.plain_result(self._persona_denied(umo))
                return
            _c = self.cfg_of(inst_of_umo(umo))
            _c["persona_current"] = doc.id
            try:
                _c.save_config()
            except Exception as e:
                logger.warning(f"[chatcore_neo] 保存配置失败: {e!r}")
            yield event.plain_result(
                f"本实例默认人格已设为 {doc.display_name}（{doc.id}）")
            return

        if sub == "msg":
            if len(args) < 2 or str(args[1]) not in ("1", "2"):
                yield event.plain_result(
                    "用法：/persona msg 1|2\n"
                    "1 = 括号描写（允许（神态/动作）描写，可换行分段）\n"
                    "2 = 无括号（格式同群聊，不换行、短句）\n"
                    "说明：这是**全局**私聊发言格式；会话级覆盖见 WebUI「会话设置」")
                return
            mode = int(args[1])
            self.config["private_msg_mode"] = mode
            try:
                self.config.save_config()
            except Exception as e:
                logger.warning(f"[chatcore_neo] 保存配置失败: {e!r}")
            label = "括号描写" if mode == 1 else "无括号（同群聊）"
            yield event.plain_result(f"全局私聊发言格式已设为 {mode} = {label}")
            return

        if sub in ("cast", "join", "leave"):
            yield event.plain_result(self._handle_costar_command(
                umo, sub, args[1:], is_private=event.is_private_chat()))
            return

        if sub == "reload":
            self.personas.reload()
            self.state.invalidate()
            self._char_seeded.clear()
            self.seed_characters()
            self.reload_characters()
            yield event.plain_result(f"已重新加载 {len(self.personas)} 个人格")
            return

        if sub == "doctor":
            rep = doctor_mod.run()
            yield event.plain_result(f"🩺 chatcore_neo doctor\n{rep.text(limit=40)}")
            return

        if sub in ("preview", "diff"):
            doc = (self.personas.resolve(args[1]) if len(args) >= 2
                   else self._persona_for(umo))
            if doc is None:
                yield event.plain_result("找不到人格")
                return
            p = A.preview(doc)
            msg = (f"📐 {doc.display_name}（{doc.id}）装配预览\n"
                   f"  稳定段 {p['stable_bytes']} B"
                   f"（框架 {p['framework_bytes']} + 人格 {p['persona_bytes']}）\n"
                   f"  旧形态 {p['legacy_bytes']} B　"
                   f"每轮省 {p['saved_bytes']} B（{p['saved_pct']}%）")
            if sub == "preview":
                msg += "\n\n--- 稳定段开头 700 字 ---\n" + p["stable_text"][:700]
            yield event.plain_result(msg)
            return

        if sub == "new":
            if len(args) < 2:
                yield event.plain_result(
                    "用法：/persona new <id> [--world <world>] [--name <中文名>] [--role <称谓>]")
                return
            opts = _parse_opts(args[2:])
            res = A.new_persona(args[1], world=opts.get("world", ""),
                                display_name=opts.get("name", ""),
                                world_role=opts.get("role", ""),
                                force=bool(opts.get("force")))
            if res.ok:
                self.personas.reload()
            yield event.plain_result(("✅ " if res.ok else "❌ ") + res.message)
            return

        if sub == "edit":
            if len(args) < 4:
                yield event.plain_result("用法：/persona edit <id> <章节> <新正文>")
                return
            doc = self.personas.resolve(args[1])
            if doc is None:
                yield event.plain_result(self._not_found(args[1]))
                return
            body = event.message_str.split(args[2], 1)[-1].strip()
            res = A.edit_section(doc, args[2], body)
            yield event.plain_result(("✅ " if res.ok else "❌ ") + res.message)
            return

        if sub in ("add", "add-section"):
            if len(args) < 3:
                yield event.plain_result("用法：/persona add <id> <新章节标题> [正文...]")
                return
            doc = self.personas.resolve(args[1])
            if doc is None:
                yield event.plain_result(self._not_found(args[1]))
                return
            rest = event.message_str.split(args[2], 1)[-1].strip()
            body = "" if rest == args[2] else rest
            res = A.add_section(doc, args[2], body)
            yield event.plain_result(("✅ " if res.ok else "❌ ") + res.message)
            return

        if sub in ("rm", "rm-section"):
            if len(args) < 3:
                yield event.plain_result("用法：/persona rm <id> <章节>")
                return
            doc = self.personas.resolve(args[1])
            if doc is None:
                yield event.plain_result(self._not_found(args[1]))
                return
            res = A.remove_section(doc, args[2])
            yield event.plain_result(("✅ " if res.ok else "❌ ") + res.message)
            return

        if sub == "inject":
            if len(args) < 4:
                yield event.plain_result(
                    "用法：/persona inject <id> <章节> <always|on_demand|tier|never>")
                return
            doc = self.personas.resolve(args[1])
            if doc is None:
                yield event.plain_result(self._not_found(args[1]))
                return
            res = A.set_inject(doc, args[2], args[3])
            if res.ok:
                self.personas.reload()
            yield event.plain_result(("✅ " if res.ok else "❌ ") + res.message)
            return

        if sub == "world":
            yield event.plain_result(world_mod.tool_world_list(
                args[1] if len(args) >= 2 else ""))
            return

        if sub == "state":
            doc = self._persona_for(umo)
            uid = "" if ("FriendMessage" in umo or "PrivateMessage" in umo) \
                else self._sender_id(event)
            if doc is None:
                yield event.plain_result("无人格")
                return
            st = self.state.load(umo, doc.id, uid)
            yield event.plain_result(f"📊 {doc.display_name}（{doc.id}）\n{st.snapshot()}"
                                     f"\n日志 {len(st.conversation_log)} 条")
            return

        if sub == "attention":
            r = self.attention.explain(umo)
            yield event.plain_result(
                "🎯 注意力状态（本会话）\n"
                f"当前概率={r['probability']} 冷却中={r['in_cooldown']} "
                f"空闲={r['idle_minutes']}分\n"
                f"基础={r['base']} + 加成={r['boosts']}\n"
                f"退避={r['backoff']} × 时段={r['time_multiplier']} "
                f"× 读空气={r['read_air']}\n"
                f"软触发落空 {r['soft_misses']} 次，他人近况 {r['others_recent']} 条")
            return

        if sub == "graph":
            g = self.memory.graph.stats()
            r = self.memory.graph_retriever.report()
            if len(args) >= 2 and args[1] == "node" and len(args) >= 3:
                key = args[2]
                nb = self.memory.graph.neighbors(key, limit=15)
                ents = self.memory.graph.entries_for_node(key, limit=8)
                yield event.plain_result(
                    f"🔗 节点 {key}\n邻居 {len(nb)} 个：\n"
                    + "\n".join(f"  --{rel}--> {k} ({c:.2f})" for k, rel, c in nb)
                    + f"\n条目 {len(ents)} 条：\n"
                    + "\n".join(f"  · {e.content[:80]}" for e in ents))
                return
            if len(args) >= 2 and args[1] == "find" and len(args) >= 3:
                hits = await self.memory.graph_retriever.search(
                    " ".join(args[2:]), 5)
                if not hits:
                    yield event.plain_result("没找到相关图谱条目")
                    return
                yield event.plain_result(
                    "🔎 图谱检索命中：\n" + "\n".join(
                        f"  [{h.final_score:.3f}] {h.content[:100]}"
                        for h in hits))
                return
            types = "、".join(f"{k}={v}" for k, v in g["node_types"].items())
            rels = "、".join(f"{k}={v}" for k, v in g["relation_types"].items())
            yield event.plain_result(
                "🕸️ 知识图谱\n"
                f"节点 {g['nodes']}（{types or '空'}）\n"
                f"边 {g['edges']}（{rels or '空'}）\n"
                f"条目 {g['entries']}，条目↔节点链接 {g['entry_links']}\n"
                f"倒排词 {g['tokens']}，文件 {g['bytes']} B\n"
                f"检索：{r['searches']} 次（关键词命中 {r['keyword_hits']}，"
                f"向量命中 {r['vector_hits']}）\n"
                f"待固化窗口 {len(self.memory._pending)} 个\n"
                f"用法：/persona graph node <node_key> ｜ /persona graph find <查询>")
            return

        if sub == "segment":
            r = self.segmenter.report()
            # 流式开启时子代理是**整体停用**的（见 segment_agent.enabled_for），
            # 这里得说清楚"为什么两个都是 False"，否则看起来像配置丢了。
            _off = ("流式传输已开启 → 子代理整体停用"
                    "（文本边流边切，等不了 LLM 分段）\n"
                    if self._stream_on() else "")
            yield event.plain_result(
                "✂️ 分段子代理\n"
                + _off
                + f"私聊启用={self.segmenter.enabled_for(is_private=True)} "
                f"阈值={self.segmenter.threshold_for(is_private=True)}\n"
                f"群聊启用={self.segmenter.enabled_for(is_private=False)} "
                f"阈值={self.segmenter.threshold_for(is_private=False)}\n"
                f"调用 {r['calls']} 采用 {r['used']} 回退 {r['fallback']} "
                f"因短跳过 {r['skipped_short']} 错误 {r['errors']}"
                + (f"\n最近错误：{r['last_error']}" if r['last_error'] else ""))
            return

        if sub == "groups":
            if len(args) >= 2 and args[1] == "set" and len(args) >= 4:
                self.groups.assign(args[2], args[3])
                yield event.plain_result(f"已把 {args[2]} 归入组 {args[3]}")
                return
            if len(args) >= 2 and args[1] == "rm" and len(args) >= 3:
                self.groups.remove(args[2])
                yield event.plain_result(f"已把 {args[2]} 移出记忆组")
                return
            g = self.groups.all_groups()
            st = self.groups.stats()
            lines = [f"🧩 跨会话记忆组（{st['groups']} 组 / {st['members']} 会话）"]
            for name, umos in g.items():
                lines.append(f"  · {name}")
                for u in umos:
                    lines.append(f"      {u}")
            lines.append(f"\n当前会话归组：{self.groups.group_of(umo)}")
            lines.append("用法：/persona groups set <umo> <组名> ｜ "
                         "/persona groups rm <umo>")
            yield event.plain_result("\n".join(lines))
            return

        if sub == "admin":
            paths = getattr(self, "web_paths", []) or []
            yield event.plain_result(
                "🖥️ 管理台\n"
                f"页面：WebUI → 插件 → ChatCore Neo → admin\\n"
                f"接口前缀：{webadmin.API_PREFIX}/*（已注册 {len(paths)} 个）\\n"
                + ("\\n".join("  " + p for p in paths) if paths
                   else "（未注册：enable_admin_page 关闭或注册失败）"))
            return

        if sub == "poke":
            r = self.poke.report()
            yield event.plain_result(
                "👉 戳一戳\n"
                f"启用={r['enabled']} 方式={r['reply_mode']} "
                f"概率={r['probability']} 冷却={r['cooldown']}s "
                f"只理自己={r['only_self']}\n"
                f"见到 {r['seen']} 次，回复 {r['replied']} 次"
                f"（跳过：非自己 {r['skipped_self']} / 冷却 "
                f"{r['skipped_cooldown']} / 概率 {r['skipped_prob']}）")
            return

        if sub == "tts":
            r = self.tts.report()
            yield event.plain_result(
                f"🔊 TTS\n启用={r['enabled']} 可用={r['available']} "
                f"provider={r['provider']} 仅私聊={r['only_private']}\n"
                f"调用 {r['calls']} 成功 {r['ok']} 失败 {r['failed']} "
                f"跳过 {r['skipped']}"
                + (f"\n最近错误：{r['last_error']}" if r['last_error'] else ""))
            return

        if sub == "merge":
            r = self.merge.report()
            yield event.plain_result(
                f"📎 连发合并\n启用={r['enabled']} 时间窗={r['window']}s "
                f"触发条数={r['min_count']}\n"
                f"批次数={r['batches']} 已提示={r['notices']} "
                f"待处理={r['pending']}")
            return

        if sub == "impression":
            target_uid = (args[1] if len(args) >= 2
                          else (self._sender_id(event) or umo))
            imp = self.state.load_impression(target_uid)
            r = self.impressions.report()
            yield event.plain_result(
                f"🖊️ 印象（uid={target_uid}）\n"
                f"轮数={imp.get('msg_count', 0)} "
                f"更新时间={imp.get('updated', 0) or '（从未）'}\n"
                f"SFW：{imp.get('sfw') or '（空）'}\n"
                f"NSFW：{imp.get('nsfw') or '（空）'}\n"
                f"人工覆写：SFW={imp.get('sfw_custom') or '（无）'} "
                f"NSFW={imp.get('nsfw_custom') or '（无）'}\n"
                f"总结器：阈值={r['threshold']} 已更新={r['updated']} "
                f"错误={r['errors']}")
            return

        if sub == "judge":
            yield event.plain_result("⚖️ 判定 subagent\n" + str(self.judge.report()))
            return

        if sub == "proactive":
            r = self.scheduler.report()
            yield event.plain_result(
                "⏰ 主动消息调度\n"
                f"启用={r['enabled']} 已发={r['emitted']} 轮询={r['ticks']}\n"
                f"最小间隔={r['min_interval_min']}分 "
                f"空闲阈值={r['idle_threshold_min']}分 "
                f"免打扰={r['quiet_hours'] or '（未设）'}\n"
                f"跟踪会话 {r['tracked_sessions']} 个，当前候选 {r['candidates']} 个\n"
                f"错误 {r['errors']} 次")
            return

        if sub == "memory":
            s = self.memory.stats()
            yield event.plain_result(
                "🧠 记忆层\n"
                f"启用={s['enabled']} 可用={s['available']} provider={s['provider']}\n"
                f"会话 {s['sessions']} 个 / 原子 {s['atoms']} 条 / {s['bytes']} B\n"
                f"召回 top_k={s['top_k']} 下限={s['min_score']} "
                f"半衰期={s['half_life_days']}天\n"
                f"embedding 缓存 {s['cached']} 条，失败 {s['failures']} 次"
                + (f"\n最近错误：{s['last_error']}" if s['last_error'] else ""))
            return

        if sub == "forget":
            # 记忆是按**记忆组键**存的（_remember 用 groups.group_of(umo)），
            # 用 umo 去清会返回 0 条且一条都删不掉 —— 与 webadmin 的口径也不一致。
            key = self.groups.group_of(umo)
            r = self.memory.forget(key)
            self.memory.flush()
            # forget 返回 dict（{"atoms": n}），旧写法把它直接格式化进字符串
            # 会打印成「（{'atoms': 0} 条）」
            n = r.get("atoms", 0) if isinstance(r, dict) else int(r or 0)
            yield event.plain_result(
                f"已清空本会话记忆（{n} 条，记忆键 {key}）")
            return

        if sub == "stats":
            p = getattr(self, "_last_parts", {})
            ws = world_mod.stats()
            yield event.plain_result(
                f"📈 运行统计\n{self._stats}\n"
                f"世界观：{ws.text()}\n状态：{self.state.stats()}\n"
                f"事件模板：{self.templates.stats()['templates']}\n"
                f"最近一次分区块：{p}")
            return

        # 兜底：把最后一个参数当人格名
        doc = self.personas.resolve(args[-1])
        if doc is None:
            yield event.plain_result(self._not_found(args[-1]))
            return
        # 会话覆盖 + 群名片 + 下一轮切换提醒（与 persona_switch 工具共用）
        if not self._persona_ok(umo, doc):
            yield event.plain_result(self._persona_denied(umo))
            return
        self._apply_persona_switch(umo, doc, event)
        yield event.plain_result(f"本会话人格已切换为 {doc.display_name}（{doc.id}）")

    def _persona_sub_help(self) -> str:
        """/persona 可用子命令速览（附在无参/current/help 输出后）。"""
        return (
            "\n\n可用子命令：\n"
            "  人格：list｜<名称> 切换｜reset｜global <名称>｜reload｜new｜"
            "edit｜add｜rm｜inject｜preview｜world\n"
            "  状态：state｜attention｜doctor｜admin\n"
            "  数据：graph｜segment｜groups｜memory｜forget｜impression\n"
            "  同台：cast｜join｜leave（多人格同台，仅剧情模式）\n"
            "  模块：poke｜tts｜merge｜judge｜proactive｜stats\n"
            "（/persona help 查看本清单）"
        )

    def _not_found(self, name: str) -> str:
        cands = self.personas.suggest(name)
        msg = f"找不到人格「{name}」"
        if cands:
            msg += "\n你是不是想找：" + "、".join(cands)
        return msg

    # ------------------------------------------------------------------ 卸载
    async def terminate(self):
        """插件卸载：把还没落盘的东西保存下来。

        每一项**单独 try**：旧实现把 5 个保存塞进同一个 try，
        只要 `memory.flush()` 抛错，后面的 templates 与 3 个状态映射表
        就全都不会保存，而且完全静默 —— 卸载时数据丢失且无任何线索。
        """
        try:
            for rt in (getattr(self, "_runtimes", {}) or {}).values():
                await rt.scheduler.stop()
        except Exception as e:
            logger.warning(f"[chatcore_neo] 卸载时停止主动消息调度失败: {e!r}")
        jobs = [
            ("记忆缓存", lambda: self.memory.flush()),
            ("事件模板", lambda: self.templates.save()),
            ("会话人格表", lambda: self.state.session_persona.save()),
            ("初始化标记", lambda: self.state.session_initialized.save()),
            ("注入覆盖表", lambda: self.state.inject_overrides.save()),
            ("会话发言参数", lambda: self._session_params.save()),
        ]
        for name, fn in jobs:
            try:
                fn()
            except Exception as e:
                logger.warning(f"[chatcore_neo] 卸载时保存「{name}」失败: {e!r}")

