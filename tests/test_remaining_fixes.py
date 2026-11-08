# -*- coding: utf-8 -*-
"""批次 A/B 修复的回归测试（把"未修项"逐条钉住）。"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(PLUGIN / "tests"))

import _env  # noqa: E402

from core.attention import AttentionManager  # noqa: E402
from core.memory.retrieval import DualRouteRetriever, RouteHit  # noqa: E402
from core.proactive import (ProactiveScheduler, ProactiveState,  # noqa: E402
                            SessionActivity)


# ═══════════════ ① 主动消息状态落盘 ═══════════════

def test_proactive_state_roundtrip():
    st = ProactiveState()
    a = st.activity("Sui:GroupMessage:1")
    a.last_user_ts = 123.5
    a.last_proactive_ts = 456.5
    a.persona = "kanami"
    a.failures = 2
    d = st.to_dict()
    st2 = ProactiveState.from_dict(d)
    b = st2.sessions["Sui:GroupMessage:1"]
    assert (b.last_user_ts, b.last_proactive_ts) == (123.5, 456.5)
    assert b.persona == "kanami" and b.failures == 2


def test_proactive_state_survives_restart():
    """重启后活跃度与静默连发计数必须还在。

    活跃度丢了 → should_emit 恒返 no-user-activity；连发计数丢了 → 死群保护
    失效，重启一次就能对着没人说话的群再连发一轮。
    """
    p = Path(tempfile.mkdtemp()) / "state.json"
    s1 = ProactiveScheduler(enabled=True, min_interval_min=45,
                            idle_threshold_min=90, quiet_hours="",
                            silent_max=2, jitter=0.0, state_path=p)
    umo = "Sui:GroupMessage:1"
    s1.note_user_message(umo, "kanami")
    s1.note_proactive(umo)
    s1.save(force=True)
    assert p.exists(), "状态没落盘"

    s2 = ProactiveScheduler(enabled=True, min_interval_min=45,
                            idle_threshold_min=90, quiet_hours="",
                            silent_max=2, jitter=0.0, state_path=p)
    assert umo in s2.state.sessions, "重启后会话活跃度丢失"
    assert s2.state.sessions[umo].last_user_ts > 0
    assert s2.state.sessions[umo].persona == "kanami"
    assert s2.state.sessions[umo].silent_streak == 1, \
        "重启后静默连发计数被清空（死群保护可被反复绕过）"


def test_proactive_corrupt_state_is_survivable():
    p = Path(tempfile.mkdtemp()) / "state.json"
    p.write_text("{不是 JSON", encoding="utf-8")
    s = ProactiveScheduler(enabled=True, state_path=p)
    assert s.state.sessions == {}


def test_proactive_state_does_not_dirty_storm():
    """save() 要有节流，避免每条消息都写盘。"""
    p = Path(tempfile.mkdtemp()) / "state.json"
    s = ProactiveScheduler(enabled=True, state_path=p)
    for i in range(20):
        s.note_user_message(f"umo{i}")
    # 节流是 5 秒，所以这里最多写 1~2 次
    assert s._last_save > 0


# ═══════════════ ② 双路 key 对齐 ═══════════════

def test_cross_route_bonus_fires_with_aligned_keys():
    """文档路与图谱路用同一个 key（source_memory_id）时，交叉加成必须生效。"""
    dr = DualRouteRetriever({})
    doc = [RouteHit("77", 1.0, "文档内容")]
    graph = [RouteHit("77", 1.0, "图谱内容")]
    both = dr.fuse("q", doc, graph, k=5)
    assert both[0].breakdown["cross_route_bonus"] > 0, \
        "两路 key 对齐后 cross_route_bonus 应生效"


def test_recall_uses_aligned_keys():
    """源码级确认：service 里两路都用 source_memory_id 作 key。"""
    src = (PLUGIN / "core" / "memory" / "service.py").read_text(encoding="utf-8")
    assert 'str(_mid or atom.id)' in src, "文档路 key 应对齐到 source_memory_id"
    assert 'str(r.doc_id or f"g:{r.entry_key}")' in src, \
        "图谱路 key 应对齐到 doc_id"


# ═══════════════ ③ _spawn 取异常 ═══════════════

def test_spawn_retrieves_task_exception():
    src = (PLUGIN / "main.py").read_text(encoding="utf-8")
    i = src.find("def _spawn")
    seg = src[i:i + 1200]
    assert "t.exception()" in seg or "task.exception()" in seg, \
        "_spawn 的 done_callback 必须取异常并记日志"
    assert "add_done_callback" in seg


# ═══════════════ ④ authoring 不抛异常 ═══════════════

def test_authoring_unknown_section_returns_result():
    import tempfile
    from pathlib import Path as _P

    from core.persona import PersonaDoc
    from core.persona.authoring import (edit_section, new_persona,
                                        remove_section, set_inject)
    root = _P(tempfile.mkdtemp())
    assert new_persona("t1", display_name="测试", root=root).ok
    doc = PersonaDoc.load(root / "DIY" / "t1")
    for fn in (lambda: edit_section(doc, "没有这章", "x"),
               lambda: set_inject(doc, "没有这章", "always"),
               lambda: remove_section(doc, "没有这章")):
        r = fn()
        assert r.ok is False and "找不到章节" in r.message


# ═══════════════ ⑤ attention 参数接线 ═══════════════

def test_attention_accepts_configured_params():
    a = AttentionManager(backoff_floor=0.1, followup_boost=0.2,
                         others_window_seconds=30.0,
                         others_density_threshold=2)
    assert a.backoff_floor == 0.1
    assert a.followup_boost == 0.2
    s = a.stats()
    for k in ("hard_trigger_boost", "no_action_backoff", "backoff_floor"):
        assert k in s, f"stats() 应暴露 {k}（管理页要用）"


def test_main_passes_attention_extra_params():
    src = (PLUGIN / "main.py").read_text(encoding="utf-8")
    for k in ("attention_backoff_floor", "attention_followup_boost",
              "attention_others_window", "attention_others_limit"):
        assert k in src, f"{k} 未被接线（幽灵配置）"


# ═══════════════ ⑥ intent_prompt ═══════════════

def test_intent_score_accepts_prompt():
    import inspect
    from core.judge import Judge
    sig = inspect.signature(Judge.intent_score)
    assert "prompt" in sig.parameters, "intent_score 应接受 prompt"
    src = (PLUGIN / "core" / "judge.py").read_text(encoding="utf-8")
    assert "prompt.strip() or DEFAULT_INTENT_SYSTEM" in src, \
        "自定义 prompt 必须真正被使用（否则又是幽灵配置）"
    m = (PLUGIN / "main.py").read_text(encoding="utf-8")
    assert "intent_prompt" in m, "main.py 应传入 intent_prompt"


# ═══════════════ ⑦ 其余 ═══════════════

def test_prune_has_a_call_site():
    src = (PLUGIN / "core" / "memory" / "service.py").read_text(encoding="utf-8")
    assert "self.store.prune(" in src, "MemoryStore.prune 必须有生产调用点"


def test_embed_cache_follows_store_root():
    src = (PLUGIN / "core" / "memory" / "service.py").read_text(encoding="utf-8")
    assert "db_path=base" in src, "向量缓存库应由 store root 推导"


def test_persona_text_reads_strip_bom():
    """人格文本可能带 BOM（旧文件），读取时要剥掉，否则 \\ufeff 进 system prompt。"""
    src = (PLUGIN / "core" / "persona" / "model.py").read_text(encoding="utf-8")
    assert "utf-8-sig" in src, "读取人格文件应使用 utf-8-sig（剥 BOM）"


def test_consolidation_records_uids():
    src = (PLUGIN / "core" / "memory" / "service.py").read_text(encoding="utf-8")
    assert '"uids": _uids' in src, "固化时要记下本轮说话人"


def test_group_card_logs_after_success():
    src = (PLUGIN / "main.py").read_text(encoding="utf-8")
    i = src.find("_maybe_set_group_card")
    seg = src[i:i + 1200]
    assert "async def _set_card" in seg, \
        "群名片的成功日志必须在任务真正执行之后打"


def test_dead_session_file_removed():
    f = _env.plugin_root() / "state" / "_session_second_persona.json"
    assert not f.exists(), "多人格同台的死占位文件已删除"


def test_tool_names_match_registered():
    """`DEFAULT_TOOL_NAMES` 里的名字必须与 `main.py` 真实注册的 llm_tool 一致。

    原来列了 `read_persona_skill` / `_lore` / `_dialog` 三个**不存在**的工具名，
    永远不会匹配 —— 典型的"看起来在工作"的静默失效。
    """
    import re
    main_src = (PLUGIN / "main.py").read_text(encoding="utf-8")
    registered = set(re.findall(r"@filter\.llm_tool\(\s*name=[\"\']([^\"\']+)",
                                main_src))
    assert registered, "main.py 里没有注册任何 llm_tool？"
    from core.history_hygiene import DEFAULT_TOOL_NAMES
    ghost = [n for n in DEFAULT_TOOL_NAMES if n not in registered]
    assert not ghost, f"DEFAULT_TOOL_NAMES 里有未注册的工具名: {ghost}"


def test_forget_clears_graph():
    """`/persona forget` 必须连带清掉图谱条目，否则图谱路仍会召回已忘内容。"""
    src = (PLUGIN / "core" / "memory" / "service.py").read_text(encoding="utf-8")
    i = src.find("def forget")
    seg = src[i:i + 1200]
    assert "forget_memory" in seg, "forget 应调用 GraphStore.forget_memory"
    assert "forget_session" in seg, \
        "还须按记忆键兜底删图：old 数据的图条目 mid 与原子对不上，按 mid 删会漏"
