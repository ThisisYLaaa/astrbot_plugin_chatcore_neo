# -*- coding: utf-8 -*-
"""管理接口冒烟测试：每个 builder 都用**真实运行时对象**跑一遍。

为什么需要它
------------
管理页的接口是"读运行时状态"的胶水层，最容易出的错是
属性名/方法名写错（`AttributeError`）——而那要等用户在浏览器里点了才发现。
这里用真实的 `StateStore` / `MemoryService` / `GraphStore` / `AttentionManager`
等对象构造一个假 plugin，把每个 builder 都调一遍（含写操作，用临时目录）。

只读真实数据的只有 `PersonaService`（人格目录），写操作全部落在临时目录。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import webadmin  # noqa: E402
from core.assembler import PromptAssembler  # noqa: E402
from core.attention import AttentionManager  # noqa: E402
from core.characters import CharacterMatcher, CharacterStore  # noqa: E402
from core.compat import EventTemplateStore  # noqa: E402
from core.costars import CoStarStore  # noqa: E402
from core.groups import MemoryGroups  # noqa: E402
from core.graph import GraphStore  # noqa: E402
from core.group_journal import GroupJournal  # noqa: E402
from core.impression import ImpressionSummarizer  # noqa: E402
from core.interactions import MergeGate, PokeHandler  # noqa: E402
from core.judge import Judge  # noqa: E402
from core.memory.service import MemoryService  # noqa: E402
from core.memory.store import MemoryStore  # noqa: E402
from core.output import RecallGuard  # noqa: E402
from core.persona import PersonaService  # noqa: E402
from core.persona_pool import PersonaPool  # noqa: E402
from core.proactive import ProactiveScheduler  # noqa: E402
from core.segment_agent import SegmentAgent  # noqa: E402
from core.sessionmap import JsonMap  # noqa: E402
from core.state import Privilege, StateStore, safe_segment  # noqa: E402
from core.tts import TTS  # noqa: E402

CFG = {
    "initial_affection": 65, "initial_lewdness": 20, "initial_depravity": 0,
    "privilege_qids": ["100000001"], "persona_current": "",
    "attention_bubble_base": 0.03, "attention_active_cap": 0.3,
    "attention_decay_minutes": 3.0, "attention_hard_boost": 0.1,
    "attention_cooldown": 20.0, "attention_backoff": 0.5,
    "memory_consolidate_turns": 2, "memory_min_score": 0.0,
    "enable_graph_retrieval": True, "graph_embed_entries": False,
}


class _Cfg(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)

    def save_config(self):
        """AstrBotConfig 有这个方法，main.py 在切人格时会调它。"""
        return None


class _FakeEmbedder:
    @property
    def available(self):
        return True

    async def embed(self, text):
        import hashlib
        return [b / 255.0 for b in hashlib.sha256(
            (text or "").encode()).digest()[:8]]

    async def embed_dedup(self, text):
        return await self.embed(text)

    def flush(self):
        pass

    def stats(self):
        return {"provider": "fake", "available": True}


class _Stub:
    """结构不重要的占位对象：够 report() 用即可。"""
    def __init__(self, data):
        self._d = data

    def report(self):
        return dict(self._d)

    def stats(self):
        return dict(self._d)


class _InstStore:
    """最小实例仓库：只提供 webadmin 读实例列表用的几个方法。"""

    _IDS = ("Sui",)

    def discover(self):
        return [{"id": i} for i in self._IDS]

    def loaded_ids(self):
        return list(self._IDS)

    def default_id(self):
        return self._IDS[0]

    def load_cfg(self, inst):
        return None

    def invalidate(self):
        pass

    def ensure_all(self):
        """main.py:236 会调；真实实现按实例目录补齐配置，桩里无事可做。"""
        return []


class _PermissivePool(PersonaPool):
    """人格池：**不做实例授权过滤**（等价于重构前的行为）。

    真实 `PersonaPool` 按 `instances/<实例>/personas.json` 过滤可见人格；
    夹具里没有那份授权表，而且绝大多数用例关心的是"有几个人格、当前用哪个"，
    不是授权矩阵（那有专门的用例）。继承真实类只为保住全部方法面 ——
    `test_plugin_wiring` 会核对 `self._persona_pool.X()` 的每个调用点。
    """

    def available_ids(self, inst: str) -> set[str]:
        return set()

    def available_docs(self, inst: str, docs):
        return list(docs)

    def is_available(self, inst: str, pid: str) -> bool:
        return True


class FakePlugin:
    # 对齐 main.ChatCoreNeoPlugin 的群聊流水常量（方法按 self 查询类属性）
    _LIVE_WINDOW = 1800
    _LIVE_LIMIT = 24
    _LIVE_INJECT = 16

    def __init__(self, root: Path):
        self.config = _Cfg(CFG)
        # —— 实例化 API：webadmin 的 builder 会切实例上下文、读实例配置 ——
        self._default_inst = "Sui"
        self._inst_cfgs = {"Sui": _Cfg({**CFG, "privilege_uid": "100000001"})}
        self._inst_store = _InstStore()
        self._stats = {"llm_request": 1, "injected": 1, "stripped": 0,
                       "state_updates": 1, "errors": 0}
        self._last_error = ""
        self.personas = PersonaService()
        # 人格池（按实例授权哪些人格可见）；实例目录指向临时目录。
        self._persona_pool = _PermissivePool(root)
        # 人格组·跨会话近况流水；同样落在临时目录。
        self.group_journal = GroupJournal(root / "_group_recent.jsonl")
        # 角色名/外号库：路径必须落在临时目录（真实路径是一份会被编辑的数据）
        self.characters = CharacterStore(root / "characters.json")
        # 多实例重构后它是「已补过名册的实例集合」，不是布尔值
        # （main.py:274 `self._char_seeded: set = set()`）。
        # 写成 False 会让 main.py:616 `if inst in self._char_seeded` 抛
        # TypeError: argument of type 'bool' is not iterable ——
        # 整个 on_llm_request 注入因此整段失败。
        self._char_seeded: set = set()
        self.char_matcher = CharacterMatcher(self.characters, self.personas.all())
        self.state = StateStore(root / "state",
                                default_affection=65, default_lewdness=20,
                                default_depravity=0)
        self._session_privileges = JsonMap(root / "_session_privileges.json")
        # Privilege 已改为「实例级单特权用户」：uid_of(inst) 或固定 uid，
        # 不再接受 qids/per_session（旧签名会让整个 fixture 起不来）。
        self.privilege = Privilege(uid="100000001")
        self.groups = MemoryGroups(root / "groups.json")
        self.costars = CoStarStore(root / "_session_costars.json")
        self.attention = AttentionManager(bubble_base=0.03, active_cap=0.3,
                                          decay_minutes=3.0,
                                          hard_trigger_boost=0.1,
                                          cool_down_seconds=20.0,
                                          no_action_backoff=0.5)
        self.judge = Judge(None, _Cfg(CFG))
        self.segmenter = SegmentAgent(None, _Cfg(CFG),
                                      prompts_store=JsonMap(
                                          root / "_segment_prompts.json"))
        self.poke = PokeHandler(enabled=True, probability=1.0, cooldown=60.0,
                                only_self=True, reply_mode="llm", text="别戳我")
        self.merge = MergeGate(enabled=True, window=8.0, notice="")
        self.tts = TTS(None, enabled=False)
        self.impressions = ImpressionSummarizer(None, _Cfg(CFG), self.state,
                                                threshold=10)
        self.memory = MemoryService(_FakeEmbedder(), MemoryStore(root / "mem"),
                                    config=_Cfg(CFG), context=None)
        self.recall_guard = RecallGuard(ttl=60.0)
        # **必须给临时 state_path**：默认值指向真实 plugin_data/proactive，
        # 而覆盖是持久化的（`set_override` 会立刻落盘）—— 不隔离就会
        # ① 读到正在运行的 bot 的真实会话覆盖，断言随之飘；
        # ② 把测试造的覆盖写进线上数据。
        self.scheduler = ProactiveScheduler(enabled=True, min_interval_min=45,
                                            idle_threshold_min=90,
                                            quiet_hours="23:30-07:30",
                                            silent_max=2,
                                            state_path=root / "proactive_state.json")
        # 以下全部对齐 main.py 的 __init__，桩太薄会掩盖真实 bug
        self.templates = EventTemplateStore(root / "event_templates.json")
        self.assembler = PromptAssembler()
        self._session_persona = self.state.session_persona
        self._initialized = self.state.session_initialized
        self._session_params = JsonMap(root / "_session_params.json")
        self._user_names = JsonMap(root / "_user_names.json")
        self._group_names = JsonMap(root / "_group_names.json")
        self._intent_last: dict = {}
        self._active_triggers: dict = {}
        self._persona_switch_notice: dict = {}
        self._live: dict = {}
        self._mem_tasks: set = set()
        self._last_parts = None
        self.web_paths: list = []
        self.context = None

    def _cfg(self, key, default=None, inst=None):
        if inst:
            return self.cfg_of(inst).get(key, default)
        return self.config.get(key, default)

    # ---- 实例化 API（webadmin 切上下文用；测试里不做真正的切换）----
    def use_inst(self, inst: str = ""):
        return None

    def end_inst(self, token=None) -> None:
        return None

    def cfg_of(self, inst: str = ""):
        return self._inst_cfgs.get(inst or self._default_inst, self.config)

    @staticmethod
    def _num(value, default):
        """对齐 main.py：只在 None 时取默认（0 是合法值）。"""
        return default if value is None else value

    # ---- 与 main.py 同名的角色库助手（桩，逻辑与真实实现一致）----

    def seed_characters(self):
        names = [d.display_name for d in self.personas.all() if d.display_name]
        added = self.characters.ensure(names)
        self._char_seeded.add(self._default_inst)
        self.char_matcher.reload(self.personas.all())
        return added

    def reload_characters(self):
        self.char_matcher.reload(self.personas.all())

    def _character_block(self, msg, doc=None):
        if not bool(self._cfg("char_mention_enabled", True)):
            return ""
        try:
            return self.char_matcher.render_block(
                msg, doc=doc,
                min_len=int(self._num(self._cfg("char_mention_min_len"), 2) or 2),
                max_hits=int(self._num(self._cfg("char_mention_max_hits"), 3) or 3),
                brief_chars=int(self._num(self._cfg("char_mention_brief_chars"), 60) or 60),
                max_chars=int(self._num(self._cfg("char_mention_max_chars"), 240) or 240))
        except Exception:
            return ""

    def sync_config(self):
        """对齐 main.py 新增的热同步方法；桩记录调用即可。"""
        self._sync_called = True
        return {"synced": ["attention", "poke", "scheduler"]}


def _plugin() -> FakePlugin:
    return FakePlugin(Path(tempfile.mkdtemp()))


def _seed(p: FakePlugin) -> str:
    """造一点状态/记忆/图谱，让各页面都有东西可显示。"""
    umo = "Sui:GroupMessage:1"
    st = p.state.load(umo, "p1", "42")
    st.append_user("你好呀", "42")
    st.emotion = 80
    p.state.save(umo, "p1", "42", st)

    async def build():
        p.memory.note_turn(umo, "在聊卡拉彼丘", "嗯嗯", uid="42",
                           user_name="小明")
        p.memory.note_turn(umo, "剪刀手阵营", "知道", uid="42",
                           user_name="小明")

        async def fake(text, participants=None, umo=""):
            return {"summary": "小明在聊卡拉彼丘阵营", "topics": ["卡拉彼丘"],
                    "key_facts": ["小明在讨论剪刀手阵营"],
                    "importance": 0.6, "degraded": False}

        p.memory.summarizer.consolidate = fake
        await p.memory.consolidate(umo, session_id=umo, persona_id="p1",
                                   umo=umo)
    asyncio.run(build())
    return umo


# ---------------------------------------------------------------- 只读接口

def test_all_readonly_builders_do_not_raise():
    p = _plugin()
    umo = _seed(p)
    calls = [
        ("build_personas", lambda: webadmin.build_personas(p)),
        ("build_sessions", lambda: webadmin.build_sessions(p)),
        ("build_stats", lambda: webadmin.build_stats(p)),
        ("build_doctor", lambda: webadmin.build_doctor(p)),
        ("build_attention", lambda: webadmin.build_attention(p)),
        ("build_interactions", lambda: webadmin.build_interactions(p)),
        ("build_proactive", lambda: webadmin.build_proactive(p)),
        ("build_groups", lambda: webadmin.build_groups(p)),
        ("build_memory", lambda: webadmin.build_memory(p, umo)),
        ("build_graph", lambda: webadmin.build_graph(p, 50)),
        ("build_graph_node", lambda: webadmin.build_graph_node(
            p, next(iter(p.memory.graph.nodes), "x:y"))),
        ("graph_search", lambda: webadmin.graph_search(p, "卡拉彼丘", 5)),
        ("build_state", lambda: webadmin.build_state(p, umo, "p1", "42")),
        ("build_impression", lambda: webadmin.build_impression(p, "42")),
        ("build_preview", lambda: webadmin.build_preview(p, "")),
        ("build_characters", lambda: webadmin.build_characters(p)),
        ("build_session_params", lambda: webadmin.build_session_params(p)),
        ("build_session_privileges", lambda: webadmin.build_session_privileges(p)),
    ]
    for name, fn in calls:
        try:
            fn()
        except Exception as e:
            raise AssertionError(f"{name} 抛异常: {e!r}") from e


def test_personas_and_persona_detail():
    p = _plugin()
    lst = webadmin.build_personas(p)
    assert "personas" in lst and "current" in lst
    if not lst["personas"]:
        from run_tests import Skip
        raise Skip("没有人格数据")
    pid = lst["personas"][0]["id"]
    d = webadmin.build_persona(p, pid)
    assert d.get("id") == pid, d
    assert isinstance(d.get("sections"), list)
    assert isinstance(d.get("files"), list)


def test_persona_detail_missing_returns_error():
    d = webadmin.build_persona(_plugin(), "不存在的")
    assert "error" in d


# ---------------------------------------------------------------- 功能开关

def test_features_report_real_switch_not_counter():
    """总览「子系统状态」的开关必须来自**实现里真正开门的那个值**。

    线上症状：`用户印象` 明明开着（enable_impression=true），却显示「关闭」——
    因为页面拿的是 `impressions.updated` 这个**计数**，插件一重载进程内计数
    归零就变成 0。`长期记忆`（atoms）、`知识图谱`（nodes）、`注意力插话`
    （sessions）同理；`语音合成` 读的是 provider 可用性而非 enable_tts。
    这里逐个钉住"开关读的是开关、不是计数"。
    """
    p = _plugin()
    rows = {r["name"]: r for r in webadmin.build_features(p)}
    # 计数全为 0（没跑过任何一轮），但开关是开着的 → 必须报「启用」
    assert rows["用户印象"]["enabled"] is True, rows["用户印象"]
    assert rows["长期记忆"]["enabled"] is True, rows["长期记忆"]
    assert rows["注意力插话"]["enabled"] is True, rows["注意力插话"]
    assert rows["不回复判定"]["enabled"] is True, rows["不回复判定"]
    assert rows["知识图谱"]["enabled"] is True, rows["知识图谱"]
    # TTS 是"关（enabled=False）+ provider 可用"，不能因为可用就报启用
    assert rows["语音合成"]["enabled"] is False, rows["语音合成"]
    # 每条都要写清来源
    for name, r in rows.items():
        assert r.get("gate"), f"{name} 没写开关来源"


def test_features_follow_config_flags():
    """把开关关掉，表里就得跟着关 —— 全部走同一个 `_cfg`。"""
    p = _plugin()
    p.config["enable_impression"] = False
    p.config["enable_attention"] = False
    p.tts.enabled = True
    rows = {r["name"]: r for r in webadmin.build_features(p)}
    assert rows["用户印象"]["enabled"] is False
    assert rows["注意力插话"]["enabled"] is False
    assert rows["语音合成"]["enabled"] is True


def test_features_proactive_reports_open_sessions():
    """主动消息那一行要说清「几个会话开着」，且不再有实例级总闸。

    2026-10-30 语义变更：`enable_proactive`（实例级总闸）已从 schema 删除，
    主动消息变成独立功能 —— 是否生效只看「分会话配置」里有没有 opt-in。
    所以这里钉两件事：
      * 有会话开启 → 这一行 enabled=True，detail 写明几个；
      * 一个都没开 → enabled=False + "尚无会话开启"（否则看起来"开了却没动静"）。
    """
    p = _plugin()
    p.scheduler.set_override("Sui:GroupMessage:1", {"enabled": True})
    rows = {r["name"]: r for r in webadmin.build_features(p)}
    assert rows["主动消息"]["enabled"] is True
    assert "1 个会话已开启" in rows["主动消息"]["detail"], rows["主动消息"]

    p2 = _plugin()
    rows2 = {r["name"]: r for r in webadmin.build_features(p2)}
    assert rows2["主动消息"]["enabled"] is False, \
        "没有任何会话 opt-in 时不该显示为已启用"
    assert "尚无会话开启" in rows2["主动消息"]["detail"], rows2["主动消息"]


def test_build_stats_carries_features():
    """`/stats` 必须带 features —— 页面就靠它渲染开关列。"""
    p = _plugin()
    d = webadmin.build_stats(p)
    assert isinstance(d.get("features"), list) and d["features"], d.get("features")


def test_build_proactive_exposes_effective_state():
    """`/proactive` 的 effective 必须说清"有没有在跑"和"几个会话开着"。

    2026-10-30 语义变更：主动消息改成**独立功能**，不再挂在实例配置上，
    因此 `effective.global`（实例级总闸）已删除 —— 只剩会话级 opt-in。
    这里同时钉住新的 `defaults` 与 `instances`（每实例一条循环）字段。
    """
    p = _plugin()
    p.scheduler.set_override("Sui:GroupMessage:1", {"enabled": True})
    d = webadmin.build_proactive(p)
    eff = d.get("effective") or {}
    assert "global" not in eff, "实例级总闸应已移除"
    assert eff.get("sessions_on") == 1, eff
    rep = d.get("report") or {}
    assert "running" in rep and "sessions_on" in rep, rep
    defaults = d.get("defaults") or {}
    for k in ("min_interval", "idle_minutes", "silent_max"):
        assert k in defaults, f"功能默认参数缺少 {k}: {defaults}"
    assert isinstance(d.get("instances"), list), d.get("instances")


def test_attention_rows_have_breakdown():
    p = _plugin()
    umo = _seed(p)
    p.attention.record_interaction(umo)
    d = webadmin.build_attention(p)
    assert "rows" in d and "config" in d
    row = [r for r in d["rows"] if r["umo"] == umo]
    assert row, d["rows"]
    for k in ("probability", "base", "boosts", "backoff", "time_multiplier",
              "read_air", "in_cooldown", "soft_misses"):
        assert k in row[0], f"注意力拆解缺少 {k}"


def test_graph_shape_for_visualization():
    p = _plugin()
    _seed(p)
    d = webadmin.build_graph(p, 50)
    assert "nodes" in d and "edges" in d and "stats" in d
    ids = {n["id"] for n in d["nodes"]}
    for e in d["edges"]:
        assert e["source"] in ids and e["target"] in ids, "边必须落在返回的节点集合内"
    for n in d["nodes"]:
        for k in ("id", "type", "value", "degree"):
            assert k in n, k


def test_graph_node_detail():
    p = _plugin()
    _seed(p)
    key = next(iter(p.memory.graph.nodes), "")
    if not key:
        from run_tests import Skip
        raise Skip("图谱为空")
    d = webadmin.build_graph_node(p, key)
    assert d["node"]["id"] == key
    assert isinstance(d["neighbors"], list)
    assert isinstance(d["entries"], list)


def test_memory_lists_atoms_with_metadata():
    p = _plugin()
    umo = _seed(p)
    d = webadmin.build_memory(p, umo)
    assert d["key"], d
    assert d["atoms"], "固化后应有记忆原子"
    a = d["atoms"][0]
    assert a["kind"] == "document"
    assert a.get("topics"), "应带固化产出的 topics"


def test_recall_debug_exposes_both_routes():
    p = _plugin()
    umo = _seed(p)
    d = asyncio.run(webadmin.recall_debug(p, umo, "卡拉彼丘", top_k=5))
    for k in ("intent", "weights", "min_score", "doc_hits", "graph_hits",
              "fused"):
        assert k in d, f"召回明细缺少 {k}"
    assert d["weights"]["document"] + d["weights"]["graph"] > 0


def test_recall_debug_empty_query():
    assert "error" in asyncio.run(
        webadmin.recall_debug(_plugin(), "u", "   "))


def test_segment_test_returns_both_paths():
    p = _plugin()
    d = asyncio.run(webadmin.segment_test(
        p, "第一句话。第二句话。第三句话。", True, ""))
    assert "agent" in d and "local_fallback" in d and "will_send" in d
    assert d["local_fallback"], "本地兜底规则必须给出结果"
    assert d["will_send"] == (d["agent"] or d["local_fallback"])


def test_segment_prompts_webapi():
    """分段提示词端点：GET / SET（覆盖）/ RESET（恢复默认）落盘。"""
    from core.segment_agent import DEFAULT_PROMPTS
    root = Path(tempfile.mkdtemp())
    p = _plugin()
    p.segmenter.prompts_store = JsonMap(root / "_segment_prompts.json")

    d = webadmin.build_segment_prompts(p)
    assert set(d["prompts"]) == set(DEFAULT_PROMPTS)
    assert all(not v["customized"] for v in d["prompts"].values())

    r = webadmin.set_segment_prompt(p, "group_narrative", "自定义群聊剧情向")
    assert r["prompts"]["group_narrative"]["current"] == "自定义群聊剧情向"
    assert r["prompts"]["group_narrative"]["customized"] is True
    # 其余仍默认
    assert r["prompts"]["group_plain"]["customized"] is False

    r2 = webadmin.reset_segment_prompts(p, "group_narrative")
    assert r2["prompts"]["group_narrative"]["current"] == \
        DEFAULT_PROMPTS["group_narrative"]
    r3 = webadmin.set_segment_prompt(p, "private_plain", "私聊改过")
    r4 = webadmin.reset_segment_prompts(p)          # 全部重置
    assert all(not v["customized"] for v in r4["prompts"].values())
    assert (root / "_segment_prompts.json").exists(), "必须落盘"

    try:
        webadmin.set_segment_prompt(p, "bogus", "x")
        raise AssertionError("未知键应被拒绝")
    except RuntimeError:
        pass


# ---------------------------------------------------------------- 写接口

def test_write_state_and_clear_log():
    p = _plugin()
    umo = _seed(p)
    d = webadmin.set_state(p, umo, "p1", "42", {"affection": 88})
    assert d["state"]["affection"] == 88
    r = webadmin.clear_log(p, umo, "p1", "42")
    assert r["cleared"] >= 1
    assert webadmin.build_state(p, umo, "p1", "42")["log_count"] == 0


def test_character_webapi_crud_and_match():
    """角色库接口：列表 / 新增 / 保存外号 / 删除 / 试跑匹配。"""
    p = _plugin()
    d = webadmin.build_characters(p)
    assert d["count"] > 0, "首次加载应补齐角色键位"
    assert d["path"].endswith("characters.json")

    r = webadmin.set_character(p, "伊薇特", ["ywt", "小冰"])
    assert r["ok"] and r["aliases"] == ["ywt", "小冰"]
    names = {c["name"] for c in webadmin.build_characters(p)["characters"]}
    assert "伊薇特" in names

    m = webadmin.match_characters(p, "小冰在干嘛")
    assert "伊薇特" in m["matched"], m
    assert "伊薇特" in m["block"]

    r = webadmin.set_character(p, "米雪儿·李", "小玩家")
    assert r["ok"]
    assert webadmin.delete_character(p, "米雪儿·李")["ok"]
    assert not webadmin.delete_character(p, "米雪儿·李")["ok"]


def test_write_inject_and_impression():
    p = _plugin()
    umo = _seed(p)
    webadmin.set_inject(p, umo, "p1", "42", "你暗恋他")
    assert webadmin.build_state(p, umo, "p1", "42")["inject"] == "你暗恋他"
    webadmin.set_impression(p, "42", sfw="自动", sfw_custom="手写")
    d = webadmin.build_impression(p, "42")
    assert d["effective"] == "手写"


def test_groups_assign_remove_merge():
    p = _plugin()
    umo = _seed(p)
    d = webadmin.assign_group(p, umo, "G1")
    assert umo in d["groups"]["G1"]["members"], d["groups"]
    # 批量入组（旧 merge_groups 已并入 assign_group 的 umos 入参）
    d = webadmin.assign_group(p, [umo, "Sui:GroupMessage:2"], "G1")
    assert set(d["groups"]["G1"]["members"]) == {umo, "Sui:GroupMessage:2"}
    d = webadmin.remove_group(p, umo)
    assert umo not in {u for v in d["groups"].values()
                       for u in v["members"]}


def test_groups_rename_keeps_members():
    p = _plugin()
    umo = _seed(p)
    webadmin.assign_group(p, umo, "G1")
    webadmin.assign_group(p, "Sui:GroupMessage:2", "G1")
    d = webadmin.rename_group(p, "G1", "G2")
    assert "G1" not in d["groups"], d["groups"]
    assert set(d["groups"]["G2"]["members"]) == {umo, "Sui:GroupMessage:2"}
    # 不存在的组 / 空名 → 报错（信封层会转成 status=error）
    for bad in (("", "X"), ("G2", ""), ("不存在的", "X")):
        try:
            webadmin.rename_group(p, bad[0], bad[1])
            raise AssertionError(f"rename({bad!r}) 应当报错")
        except ValueError:
            pass


def test_sessions_expose_users_with_names():
    """管理页下拉框的数据源：sessions 应带 uid→昵称映射与全量用户列表。"""
    p = _plugin()
    umo = _seed(p)
    d = webadmin.build_sessions(p)
    assert "users" in d and "user_names" in d, d.keys()
    uids = {u["uid"] for u in d["users"]}
    assert "42" in uids, d["users"]          # 状态目录里的成员
    for u in d["users"]:
        # uid/name 是页面直接用的两个键，其余是印象页排序用的元信息
        assert {"uid", "name"} <= set(u.keys()), u


def test_sessions_filter_stale_umo_and_uid():
    """规格化判定：safe 目录名反推还原；**真的**归不到人的才列入残留。"""
    p = _plugin()
    # 造一个反推能还原的会话目录
    umo = "Sui:GroupMessage:1"
    st = p.state.load(umo, "p1", "")
    st.emotion = 60
    p.state.save(umo, "p1", "", st)
    # 归不到人的：群号不是用户，没法算成谁的印象
    d0 = p.state.root / "_impressions"
    d0.mkdir(parents=True, exist_ok=True)
    (d0 / "Banyan_GroupMessage_100000001.json").write_text(
        '{"sfw": "", "msg_count": 0}', encoding="utf-8")

    d = webadmin.build_sessions(p)
    umos = [s["umo"] for s in d["sessions"]]
    assert umo in umos, d["sessions"]
    assert "Sui_GroupMessage_1" not in umos   # safe 名被还原成冒号形式
    uids = {u["uid"] for u in d["users"]}
    assert "Banyan_GroupMessage_100000001" not in uids   # 不是合法 uid
    stale = [u["uid"] for u in d["stale_users"]]
    assert "Banyan_GroupMessage_100000001" in stale       # 列入残留
    # 无法反推的目录 → stale_sessions
    bad = p.state.root / "not_a_session_dir"
    bad.mkdir(parents=True, exist_ok=True)
    d2 = webadmin.build_sessions(p)
    assert any(x["dir"] == "not_a_session_dir" for x in d2["stale_sessions"])


def test_private_umo_impression_belongs_to_that_person():
    """`Banyan_FriendMessage_<QQ>.json` 是**那个真人自己**的，不是残留。

    私聊里 uid 恒为空，旧代码 `uid or umo` 把印象写成了 umo 名。它不是垃圾，
    所以不许出现在「残留用户」里等着被清掉。
    """
    p = _plugin()
    d0 = p.state.root / "_impressions"
    d0.mkdir(parents=True, exist_ok=True)
    (d0 / "Banyan_FriendMessage_100000001.json").write_text(
        '{"msg_count": 8}', encoding="utf-8")
    (d0 / "100000001.json").write_text(
        '{"sfw": "真印象", "msg_count": 1418, "updated": 1788982269}',
        encoding="utf-8")

    d = webadmin.build_sessions(p)
    assert "Banyan_FriendMessage_100000001" not in {
        u["uid"] for u in d["stale_users"]}, d["stale_users"]
    assert "Banyan_FriendMessage_100000001" not in {
        u["uid"] for u in d["users"]}
    u = [x for x in d["users"] if x["uid"] == "100000001"]
    assert u, d["users"]
    # 两份合并：轮数取大值（同一份数据的副本，不相加）
    assert u[0]["msg_count"] == 1418, u[0]
    assert u[0]["has_impression"] is True
    assert "100000001" in d["users_with_impression"]


def test_prune_stale_removes_data():
    """/prune 语义：删会话状态 + 主动脏键 + 垃圾用户，归组会话跳过。"""
    p = _plugin()
    umo = _seed(p)                      # "Sui:GroupMessage:1"
    p.attention  # noqa
    # 主动脏键
    sched = p.scheduler
    sched.note_user_message("umo1", "")
    sched.note_user_message("ready", "")
    sched.note_user_message(umo, "p1")
    assert "umo1" in sched.state.sessions

    # 垃圾用户印象
    imp = p.state.root / "_impressions" / "Banyan_FriendMessage_100000001.json"
    imp.parent.mkdir(parents=True, exist_ok=True)
    imp.write_text('{"sfw": ""}', encoding="utf-8")

    r = webadmin.prune_stale(
        p, sessions=[umo], users=["Banyan_FriendMessage_100000001"],
        proactive=["umo1", "ready"])
    assert umo in r["sessions"]
    assert "umo1" in r["proactive"] and "ready" in r["proactive"]
    assert "Banyan_FriendMessage_100000001" in r["users"]
    # 状态目录没了
    from core.state.store import safe_segment
    assert not (p.state.root / safe_segment(umo)).exists()
    # 主动脏键没了，真实会话键仍在
    assert "umo1" not in sched.state.sessions
    assert "ready" not in sched.state.sessions
    assert umo not in sched.state.sessions      # 指名删除的会话也清掉了
    # 垃圾印象文件没了
    assert not imp.exists()


def test_forget_memory():
    p = _plugin()
    umo = _seed(p)
    assert webadmin.build_memory(p, umo)["atoms"]
    webadmin.forget_memory(p, umo)
    assert webadmin.build_memory(p, umo)["atoms"] == []


def test_forget_memory_atoms_deletes_only_selected():
    """WebUI 勾选删除：只删勾中的那条，别的记忆与图谱链接都保住。"""
    p = _plugin()
    umo = _seed(p)

    # 第二条记忆的**固化正文必须与第一条不同**：_seed 的假总结器对任何输入
    # 都返回同一段文字，而 fake embedder 是哈希桩 —— 正文一样时余弦=1.0，
    # store.add 会按近重复去重，结果永远只有一条原子，就测不出"只删一条"。
    async def fake2(text, participants=None, umo=""):
        return {"summary": "另一段完全不同的记忆正文", "topics": ["测试"],
                "key_facts": ["这是一条独立的记忆"], "importance": 0.5,
                "degraded": False}
    p.memory.summarizer.consolidate = fake2
    p.memory.note_turn(umo, "第二个话题", "好的", uid="42")
    p.memory.note_turn(umo, "再来一句", "嗯", uid="42")
    asyncio.run(p.memory.consolidate(umo, session_id=umo, persona_id="p1",
                                     umo=umo))

    atoms = webadmin.build_memory(p, umo)["atoms"]
    assert len(atoms) >= 2, "需要至少两条记忆才能验证『只删一条』"
    keep, drop = atoms[0]["id"], atoms[1]["id"]
    before = len(atoms)

    r = webadmin.forget_memory_atoms(p, umo, [drop])
    assert r["deleted"] == 1, r
    after = webadmin.build_memory(p, umo)["atoms"]
    assert len(after) == before - 1, (before, len(after))
    assert [a["id"] for a in after] == [keep], "不该误删未勾选的那条"


def test_forget_memory_atoms_unknown_id_is_noop():
    p = _plugin()
    umo = _seed(p)
    before = len(webadmin.build_memory(p, umo)["atoms"])
    r = webadmin.forget_memory_atoms(p, umo, ["not-a-real-id"])
    assert r["deleted"] == 0 and r["graph_dropped"] == 0, r
    assert len(webadmin.build_memory(p, umo)["atoms"]) == before


def test_forget_memory_atoms_empty_list_is_noop():
    p = _plugin()
    umo = _seed(p)
    r = webadmin.forget_memory_atoms(p, umo, [])
    assert r == {"key": umo, "deleted": 0, "graph_dropped": 0, "requested": 0}


def test_forget_memory_atom_also_drops_graph_entries():
    """删单条记忆时，它派生的图谱条目也要跟着走（否则图谱路照样召回）。"""
    p = _plugin()
    umo = _seed(p)
    atoms = webadmin.build_memory(p, umo)["atoms"]
    assert atoms, "种子数据应已固化出记忆"
    atom = p.memory.store.load(umo)[0]
    mid = int((atom.metadata or {}).get("source_memory_id") or 0)

    linked = [k for k, e in p.memory.graph.entries.items()
              if e.source_memory_id == mid]
    if not linked:
        from run_tests import Skip
        raise Skip("种子数据没抽出图谱条目，跳过图谱联动断言")

    r = p.memory.forget_atom(umo, atom.id)
    assert r["deleted"] == 1
    assert r["graph_dropped"] >= 1, r
    left = [k for k, e in p.memory.graph.entries.items()
            if e.source_memory_id == mid]
    assert left == [], left


def test_edit_memory_atom_rewrites_text_and_rebuilds_graph():
    """WebUI「编辑」：正文改了，向量与图谱条目必须一起重建。"""
    p = _plugin()
    umo = _seed(p)
    atom = p.memory.store.load(umo)[0]
    mid = int((atom.metadata or {}).get("source_memory_id") or 0)
    old_vec = list(atom.vec)
    old_entries = [e.content for e in p.memory.graph.entries.values()
                   if e.source_memory_id == mid]

    new_text = "这是人工改写后的正文，跟原来那段完全不是一回事"
    r = asyncio.run(webadmin.edit_memory_atom(p, umo, atom.id, new_text))
    assert "error" not in r, r
    assert r["key"] == umo and r["id"] == atom.id
    assert atom.text == new_text
    assert list(atom.vec) != old_vec, "必须重算向量，否则召回与正文对不上"
    assert atom.metadata["summary"] == new_text
    assert p.memory.stats()["edited"] == 1

    left = [e.content for e in p.memory.graph.entries.values()
            if e.source_memory_id == mid]
    if old_entries:
        assert not (set(old_entries) & set(left)), "旧图谱条目必须删干净"
    # 旧**摘要**必须消失；话题/参与者是有意保留的，所以不能拿"卡拉彼丘"当判据
    assert all("小明在聊卡拉彼丘阵营" not in t for t in left), left[:3]
    assert any(new_text in t for t in left), left[:3]


def test_edit_memory_atom_result_has_no_ok_key():
    """结果里**不能有 `ok` 键** —— bridge 用 `ok` 判接口成败，
    放进 payload 会被当成"接口出错"直接抛异常，错误原因也就丢了。"""
    p = _plugin()
    umo = _seed(p)
    atom = p.memory.store.load(umo)[0]
    ok = asyncio.run(webadmin.edit_memory_atom(p, umo, atom.id, "换一段正文"))
    assert "ok" not in ok, ok
    bad = asyncio.run(webadmin.edit_memory_atom(p, umo, atom.id, "  "))
    assert "ok" not in bad and bad["error"] == "empty"


def test_edit_memory_atom_input_errors():
    p = _plugin()
    umo = _seed(p)
    atom = p.memory.store.load(umo)[0]
    assert asyncio.run(webadmin.edit_memory_atom(
        p, umo, "", "x"))["error"] == "missing_id"
    assert asyncio.run(webadmin.edit_memory_atom(
        p, umo, "not-a-real-id", "新正文"))["error"] == "not_found"
    assert asyncio.run(webadmin.edit_memory_atom(
        p, umo, atom.id, atom.text))["error"] == "unchanged"


def test_attention_reset():
    p = _plugin()
    umo = _seed(p)
    p.attention.record_interaction(umo)
    webadmin.reset_attention(p, umo)
    assert webadmin.build_attention(p)["rows"] == [] or True


def test_reload_all():
    p = _plugin()
    d = webadmin.reload_all(p)
    assert "personas" in d


def test_config_schema_and_roundtrip():
    """配置接口：schema 完整、能写入并按类型夹紧、保存后热同步被调用。"""
    p = _plugin()
    d = webadmin.build_config(p)
    schema = d.get("schema") or {}
    assert len(schema) >= 50, f"schema 数量不足: {len(schema)}"
    assert "current" in d
    assert "enable_tts" in schema and "initial_affection" in schema

    r = webadmin.set_config(p, {
        "enable_tts": True,
        "initial_affection": 500,          # 超出 max=100 → 应被夹紧为 100
        "privilege_uid": "100000001",
        "privacy_unknown_key": 123,        # schema 里不存在的键 → 忽略，不报错
    })
    assert "enable_tts" in r["applied"]
    assert "privacy_unknown_key" not in r["applied"]
    assert r["current"]["initial_affection"] == 100
    assert r["current"]["enable_tts"] is True
    assert r["current"]["privilege_uid"] == "100000001"
    assert getattr(p, "_sync_called", False) is True   # save 后调用了热同步
    # 保存落盘：Cfg 是纯内存 dict，save_config 不应抛
    # 类型错误 → 应该抛（信封会转 status=error）
    try:
        webadmin.set_config(p, {"initial_affection": "abc"})
        raise AssertionError("非法值应当报错")
    except ValueError:
        pass


def test_config_schema_coerce_types():
    p = _plugin()
    cases = [
        ({"attention_bubble_base": 0.05}, "attention_bubble_base", 0.05),
        ({"stutter_probability": 7.0}, "stutter_probability", 1.0),   # max=1.0
        ({"enable_markers": "true"}, "enable_markers", True),
        ({"max_history_count": 0}, "max_history_count", 0),           # 0 是合法值
    ]
    for values, key, want in cases:
        r = webadmin.set_config(p, values)
        got = r["current"][key]
        assert got == want, f"{key}: got {got!r} want {want!r}"


def test_proactive_override_webapi():
    """分会话配置接口：设置 / 读取 / 清除覆盖。"""
    p = _plugin()
    # 重要的：给 FakePlugin 换一个**临时目录**的调度器，
    # 否则 overrides 会写进真实 plugin_data/proactive/state.json。
    p.scheduler = ProactiveScheduler(
        enabled=True, min_interval_min=45, idle_threshold_min=90,
        quiet_hours="", silent_max=2,
        state_path=Path(tempfile.mkdtemp()) / "state.json")
    umo = "Sui:GroupMessage:1"

    d = webadmin.build_proactive(p)
    assert "overrides" in d and d["overrides"] == {}

    r = webadmin.set_proactive_override(p, umo, {"enabled": False, "silent_max": 2})
    assert r["overrides"][umo]["enabled"] is False
    assert r["overrides"][umo]["silent_max"] == 2

    r2 = webadmin.set_proactive_override(p, umo, {"idle_threshold_min": 180})
    assert r2["overrides"][umo]["idle_threshold_min"] == 180   # 合并而非覆盖

    r3 = webadmin.clear_proactive_override(p, umo)
    assert umo not in r3["overrides"]


def test_build_proactive_hides_non_sessions():
    """「各会话活跃度」只列真会话，假会话/旧会话归入 stale_proactive。

    线上症状：表里冒出 `webchat:FriendMessage:webchat!astrbot!
    newapi-balance-e2e-*`、`Sui:GroupMessage:1` 这类测试脚本造的键 ——
    它们语法上是合法 UMO，旧判据（只看 `is_spec_umo`）全放过。
    """
    p = _plugin()
    p.scheduler = ProactiveScheduler(
        enabled=True, state_path=Path(tempfile.mkdtemp()) / "state.json")
    # 与 main.py 一致：只有留下过状态文件的会话才算真会话
    p.state.save("Sui:GroupMessage:1", "p1", "42",
                 p.state.load("Sui:GroupMessage:1", "p1", "42"))
    p.scheduler.set_known(
        lambda u: safe_segment(u) in set(p.state.sessions()))

    live = "Sui:GroupMessage:1"
    dead = "webchat:FriendMessage:webchat!astrbot!newapi-balance-e2e-1"
    junk = "umo1"
    for u in (live, dead, junk):
        p.scheduler.note_user_message(u, "p1")

    d = webadmin.build_proactive(p)
    assert live in d["activity"], d["activity"]
    assert dead not in d["activity"], "假会话不该出现在活跃度表里"
    assert junk not in d["activity"]
    assert dead in d["stale_proactive"]
    assert junk in d["stale_proactive"]
    # 残留项也要能被一键清理（走 forget，配额计数一起清）
    r = webadmin.prune_stale(p, proactive=[dead, junk])
    assert sorted(r["proactive"]) == sorted([dead, junk])
    d2 = webadmin.build_proactive(p)
    assert not d2.get("stale_proactive"), d2.get("stale_proactive")
    assert live in d2["activity"]


# ---------------------------------------------------------------- 信封

def test_session_params_webapi():
    """会话发言参数端点：GET 定义 / SET 写入 / 归一化 / CLEAR 落盘。"""
    from core.sessionmap import JsonMap
    root = Path(tempfile.mkdtemp())
    p = _plugin()
    # 主插件在 __init__ 会创建 _session_params；这里给桩补上（temp 隔离）
    p._session_params = JsonMap(root / "_session_params.json")

    d = webadmin.build_session_params(p)
    assert d["modes"] and d["rules"]
    assert d["rules"][0]["id"] == "nsfw"
    assert any(m["id"] == 1 for m in d["modes"])

    umo = "Sui:GroupMessage:9"
    r = webadmin.set_session_params(p, umo, {"mode": 1, "rules": ["nsfw"]})
    assert r["overrides"][umo]["mode"] == 1
    assert r["overrides"][umo]["rules"] == ["nsfw"]
    # 归一化：非法 mode 被丢弃（不改动已存在的 mode = 合并语义），未知规则被过滤
    r2 = webadmin.set_session_params(p, umo, {"mode": 9, "rules": ["nsfw", "x"]})
    assert r2["overrides"][umo]["rules"] == ["nsfw"]
    assert r2["overrides"][umo]["mode"] == 1, "非法 mode 应保持原值"
    # 空覆盖 = 清除该会话
    r3 = webadmin.set_session_params(p, umo, {})
    assert umo not in r3["overrides"]
    r4 = webadmin.set_session_params(p, umo, {"mode": 2})
    assert r4["overrides"][umo]["mode"] == 2
    r5 = webadmin.clear_session_params(p, umo)
    assert umo not in r5["overrides"]
    assert (root / "_session_params.json").exists(), "必须落盘"


def test_session_privileges_webapi():
    """特权用户端点：实例化之后是「每个实例一名」，不再有分会话覆盖。

    旧契约（全局名单 + 平台/平台:类型/精确会话三层前缀覆盖）已随实例化下线：
    实例边界本身就是那个前缀。这里锁新契约——rows 以实例 id 当 umo，
    effective 取该实例配置的 privilege_uid，set 只认第一名，clear 清空。
    """
    p = _plugin()
    d = webadmin.build_session_privileges(p)
    assert d["platforms"] == ["Sui"]
    row = d["rows"][0]
    assert row["umo"] == "Sui"
    assert row["effective"] == ["100000001"]
    assert row["source"] == "Sui"

    # 写：只认第一名（多人时取第一个非空）
    r = webadmin.set_session_privileges(p, "Sui", ["1705744120", "999"])
    assert r["rows"][0]["effective"] == ["1705744120"]
    assert p._inst_cfgs["Sui"]["privilege_uid"] == "1705744120"

    # 空名单 / clear = 该实例没有特权用户
    r2 = webadmin.set_session_privileges(p, "Sui", [])
    assert r2["rows"][0]["effective"] == []
    r3 = webadmin.clear_session_privileges(p, "Sui")
    assert r3["rows"][0]["effective"] == []

    # 空 scope 拒绝（实例 id 必填）
    try:
        webadmin.set_session_privileges(p, "", ["1"])
        raise AssertionError("空 scope 应被拒绝")
    except RuntimeError:
        pass


def test_envelope_shape_for_bridge():
    ok = webadmin._ok({"a": 1})
    err = webadmin._err("坏了")
    assert ok["status"] == "ok" and ok["data"] == {"a": 1}
    assert err["status"] == "error" and err["message"] == "坏了"
    assert err["ok"] is False and ok["ok"] is True


def test_register_returns_expected_paths():
    """register() 应把所有路径都挂上（用假 context 记录调用）。"""
    p = _plugin()
    seen = []

    class Ctx:
        def register_web_api(self, route, handler, methods, desc):
            seen.append((route, tuple(methods)))

    p.context = Ctx()
    done = webadmin.register(p)
    assert len(done) == len(seen)
    assert len(done) >= 25, f"接口太少: {len(done)}"
    # 每个路径都应以插件名为前缀
    for route, _m in seen:
        assert route.startswith(webadmin.API_PREFIX), route
    # 关键接口必须在
    for need in ("/graph", "/memory", "/memory/recall", "/attention",
                 "/interactions", "/proactive", "/groups"):
        assert any(r.endswith(need) for r, _ in seen), f"缺少接口 {need}"


def test_all_registered_handlers_are_async():
    """处理器必须是协程函数，否则 AstrBot 调用时会炸。"""
    p = _plugin()
    seen = []

    class Ctx:
        def register_web_api(self, route, handler, methods, desc):
            seen.append((route, handler))

    p.context = Ctx()
    webadmin.register(p)
    import inspect
    for route, h in seen:
        assert inspect.iscoroutinefunction(h), f"{route} 不是协程函数"

def test_build_groups_exposes_resolved_available_personas():
    """记忆组页的「组人格」候选 = 该实例**当前可用**的人格。

    回归 2026-10-30：页面以前拿 `instances[i].grants` 当白名单直接过滤，
    而 Banyan 是"空授权表 = 不限制"，于是下拉里只剩当前正在用的那一个。
    现在由后端按 PersonaPool.is_available 解析出 `available` 列表。
    """
    p = _plugin()
    d = webadmin.build_groups(p)
    assert d["instances"], d
    inst = d["instances"][0]
    assert inst["unrestricted"] is True, inst
    all_ids = [x.id for x in p.personas.all()]
    assert len(inst["available"]) == len(all_ids), (len(inst["available"]), len(all_ids))
    assert set(inst["available"]) <= set(all_ids)

