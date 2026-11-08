# -*- coding: utf-8 -*-
"""第二轮审计（4 个子代理）发现的 bug 的回归测试。

每条 bug 一个能复现它的测试 —— 光修代码不加测试，下一个同类 bug 还会溜过去。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(PLUGIN / "tests"))
sys.path.insert(0, str(PLUGIN.parent))
import _env  # noqa: E402
sys.path.insert(0, str(_env.app()))

import importlib  # noqa: E402

from core import compat  # noqa: E402
from core.graph import GraphStore  # noqa: E402
from core.groups import MemoryGroups  # noqa: E402
from core.proactive import ProactiveScheduler  # noqa: E402

try:
    plugin_main = importlib.import_module(f"{PLUGIN.name}.main")
except Exception as e:  # pragma: no cover
    from run_tests import Skip
    raise Skip(f"无法导入插件入口：{e!r}")

Neo = plugin_main.ChatCoreNeoPlugin
from test_decision_chain import FakeEvent, _neo  # noqa: E402


# ═══════════════ P0-1：事件模板抽取 ═══════════════

class _Meta:
    def __init__(self, name, id_):
        self.name = name
        self.id = id_


class _Obj:
    def __init__(self, sender=None, group_id=""):
        self.sender = sender
        self.group_id = group_id


class _Sender:
    user_id = "42"
    nickname = "小明"


class _RealishEvent:
    """模拟真实 AstrMessageEvent：**只有 getter 方法**，没有那些属性。"""

    def __init__(self, umo="Sui:GroupMessage:1", group_id="1", private=False):
        self.unified_msg_origin = umo
        self.message_obj = _Obj(sender=_Sender(), group_id=group_id)
        self.session_id = group_id
        self.platform_meta = _Meta("aiocqhttp", umo.split(":", 1)[0])
        self._private = private

    def get_platform_id(self):
        return self.platform_meta.id

    def get_platform_name(self):
        return self.platform_meta.name

    def get_self_id(self):
        return "999"

    def get_group_id(self):
        return self.message_obj.group_id

    def get_session_id(self):
        return self.session_id

    def get_message_type(self):
        from astrbot.core.platform.message_type import MessageType
        return (MessageType.FRIEND_MESSAGE if self._private
                else MessageType.GROUP_MESSAGE)


def _store():
    return compat.EventTemplateStore(Path(tempfile.mkdtemp()) / "t.json")


def test_template_records_platform_id_not_repr():
    """模板的 platform 必须是 platform_meta.**id**。

    旧实现读 `getattr(event, "platform")` 拿到的是整个 PlatformMetadata
    对象的 repr 字符串，重建 UMO 时完全对不上。
    """
    st = _store()
    tpl = st.ensure_from_event(_RealishEvent())
    assert tpl is not None
    assert tpl.platform == "Sui", f"platform 应为适配器 id，实际 {tpl.platform!r}"
    assert "PlatformMetadata" not in tpl.platform


def test_template_records_group_and_self_and_sender():
    """self_id / group_id / sender 都必须真的取到（旧实现全是空）。"""
    st = _store()
    tpl = st.ensure_from_event(_RealishEvent())
    assert tpl.self_id == "999", tpl.self_id
    assert tpl.group_id == "1", tpl.group_id
    assert tpl.sender.get("user_id") == "42", tpl.sender
    assert tpl.sender.get("nickname") == "小明"
    assert tpl.message_type == "GroupMessage", tpl.message_type


def test_template_private_has_no_group():
    st = _store()
    tpl = st.ensure_from_event(
        _RealishEvent(umo="Sui:FriendMessage:5", group_id="", private=True))
    assert tpl.group_id == ""
    assert tpl.message_type == "FriendMessage"


def test_find_adapter_prefers_id():
    """Banyan / Sui 两个适配器的 name 都是 aiocqhttp，必须按 id 选。"""
    from core.proactive import _find_adapter

    class _A:
        def __init__(self, name, id_):
            self._m = _Meta(name, id_)

        def meta(self):
            return self._m

    class _Ctx:
        platform_manager = type("M", (), {
            "platform_insts": [_A("aiocqhttp", "Banyan"), _A("aiocqhttp", "Sui")]})

    assert _find_adapter(_Ctx(), "Sui").meta().id == "Sui"
    assert _find_adapter(_Ctx(), "Banyan").meta().id == "Banyan"


def test_synthesize_event_uses_umo_for_adapter():
    """`synthesize_event` 必须能靠 umo 找到正确适配器（两处调用都传了 umo）。"""
    import inspect
    from core import proactive as pr
    sig = inspect.signature(pr.synthesize_event)
    assert "umo" in sig.parameters, "synthesize_event 应接受 umo"
    src = (PLUGIN / "main.py").read_text(encoding="utf-8")
    calls = [l for l in src.splitlines() if "synthesize_event(tpl" in l]
    assert calls, "main.py 应调用 synthesize_event"
    for c in calls:
        assert "umo=" in c, f"调用点没传 umo: {c.strip()}"


# ═══════════════ P0-2：/reset 私聊 uid ═══════════════

def test_reset_uses_empty_uid_in_private():
    """私聊状态存在 `{umo}/{persona}.json`，/reset 用 sender_id 会写错文件。"""
    src = (PLUGIN / "main.py").read_text(encoding="utf-8")
    i = src.find("async def on_after_message")
    seg = src[i:i + 2000]
    assert 'uid = "" if is_private' in seg or 'if is_private else self._sender_id' in seg, \
        "/reset 必须按私聊/群聊区分 uid（私聊用空串）"


def test_recent_msgs_accepts_uid():
    """群聊里判定器要看到最近对话，必须按 uid 读成员状态。"""
    import inspect
    fn = Neo.__dict__.get("_recent_msgs")
    assert fn is not None
    assert "uid" in inspect.signature(fn).parameters, "_recent_msgs 应接受 uid"


def test_recent_msgs_reads_member_state():
    """群聊成员有日志时，_recent_msgs(umo, uid) 必须真的读到。

    注意人格 id 要用 `_persona_for` 真正返回的那个（不是随便编一个）。
    """
    p = _neo()
    umo = "Sui:GroupMessage:1"
    doc = Neo._persona_for(p, umo)
    if doc is None:
        from run_tests import Skip
        raise Skip("没有人格数据")
    st = p.state.load(umo, doc.id, "42")
    st.append_user("你好呀", "42")
    p.state.save(umo, doc.id, "42", st)
    got = Neo._recent_msgs(p, umo, "42")
    assert got, f"带 uid 时必须能读到成员状态日志（persona={doc.id}）"
    # 不带 uid 读的是会话级文件，群聊里不存在 → 应为空（这正是旧 bug）
    assert Neo._recent_msgs(p, umo) == [], "不带 uid 时群聊应读不到（对照）"


# ═══════════════ P0-3：/reset 必须清插件自维护的会话运行时数据 ═══════════════

def test_reset_clears_session_runtime_caches():
    """/reset 只清框架历史不够。

    框架只会清 `req.contexts`；插件自己维护的**每会话**运行时缓存若不清，
    bot 仍会"记得上下文"—— 实测群聊 reset 后仍引用 reset 前的群消息，
    来源就是注入为「群里最近发言」的 `_live` 流水。
    """
    p = _neo()
    umo = "Banyan:GroupMessage:200000001"
    key = p.groups.group_of(umo)
    p.memory.drop_pending(key)          # 先清干净，避免受既有数据干扰

    # 各容器都塞进该会话的数据
    p._live[umo] = [{"u": "1", "n": "甲", "t": "给我超超", "ts": 1}]
    p.merge.note(umo, "在吗")
    p.attention.record_interaction(umo)
    p._intent_last[umo] = 1.0
    p._active_triggers[umo] = "mid"
    p._persona_switch_notice[umo] = "切人格"
    p.memory.note_turn(key, "你好", "好呀")
    assert p.memory.pending_size(key) == 1

    p._clear_session_caches(umo)

    assert umo not in p._live, "群聊发言流水没清 → reset 后仍记得群消息"
    assert p.merge.peek(umo) == 0, "待合并批次没清"
    assert p.attention._states.get(umo) is None, "注意力状态没清"
    assert umo not in p._intent_last
    assert umo not in p._active_triggers
    assert umo not in p._persona_switch_notice
    assert p.memory.pending_size(key) == 0, "未固化缓冲没清 → 之后仍会被写成记忆"


# ═══════════════ P1-2：forget 用记忆组键 ═══════════════

def test_forget_uses_group_key():
    src = (PLUGIN / "main.py").read_text(encoding="utf-8")
    i = src.find('if sub == "forget"')
    seg = src[i:i + 500]
    assert "group_of" in seg, \
        "/persona forget 必须用记忆组键（记忆是按组键存的）"


# ═══════════════ P1-5：GraphStore 置脏 ═══════════════

def test_graph_upsert_marks_dirty():
    """更新既有节点必须置脏，否则 metadata 合并在 save 时丢失。"""
    d = Path(tempfile.mkdtemp()) / "g.json"
    g = GraphStore(d)
    from core.graph.model import GraphNode
    g.upsert_node(GraphNode("topic", "X", metadata={"a": 1}))
    g.save(force=True)
    g2 = GraphStore(d)
    g2.upsert_node(GraphNode("topic", "X", metadata={"hits": 9}))
    g2.save()                      # 不 force：靠 _dirty
    # canonicalize() 会把纯 ASCII 转小写，所以 node_key 是 topic:x 而不是 topic:X
    key = "topic:x"
    assert key in g2.nodes, list(g2.nodes)
    g3 = GraphStore(d)
    assert g3.nodes[key].metadata.get("hits") == 9, \
        "更新分支没置脏 → metadata 丢失"


def test_graph_upsert_edge_marks_dirty():
    d = Path(tempfile.mkdtemp()) / "g.json"
    g = GraphStore(d)
    from core.graph.model import GraphEdge
    g.upsert_edge(GraphEdge("a", "b", "rel", 1, weight=1.0))
    g.save(force=True)
    g2 = GraphStore(d)
    g2.upsert_edge(GraphEdge("a", "b", "rel", 1, weight=5.0))
    g2.save()
    g3 = GraphStore(d)
    assert g3.edges[list(g3.edges)[0]].weight == 5.0


# ═══════════════ P1-6：terminate 各自 try ═══════════════

def test_terminate_saves_are_independent():
    """一个保存失败不能连累其它保存（旧实现共用一个 try）。"""
    src = (PLUGIN / "main.py").read_text(encoding="utf-8")
    i = src.find("async def terminate")
    seg = src[i:i + 1600]
    # 实现形式是"jobs 列表 + 循环内单独 try"，所以断言看结构而不是 try 的个数
    assert "jobs = [" in seg, "应把各项保存列成 jobs"
    assert "for name, fn in jobs:" in seg, "应逐项执行"
    assert "try:" in seg and "logger.warning" in seg, \
        "单项失败必须记日志（旧的共用一个 try 且静默）"
    assert "self.memory.flush()" in seg and "self.templates.save()" in seg


# ═══════════════ P0-3：provider 回退留痕 + 维度告警 ═══════════════

def test_provider_fallback_records_reason():
    from test_providers import _Ctx, _Prov
    from core.memory.embedder import Embedder
    e = Embedder(_Ctx(emb=[_Prov("real_id", "openai_embedding")]),
                 provider_id="不存在的")
    assert e._resolve() is not None
    assert e._last_error, "回退必须留原因"


def test_provider_model_reads_embedding_model():
    """真实 embedding provider 的模型名在 `embedding_model`，不是 model_config.model。"""
    from core.memory.embedder import provider_model

    class _P:
        provider_config = {"id": "BAAI_embedding",
                           "embedding_model": "BAAI/bge-m3"}

    assert provider_model(_P()) == "BAAI/bge-m3"


def test_cosine_dim_mismatch_is_not_silent():
    """维度不一致时要告警（换了 embedding 模型 → 旧向量全失配）。"""
    import logging
    from core.memory.store import cosine, _DIM_WARNED
    _DIM_WARNED.clear()
    recs: list = []

    class _H(logging.Handler):
        def emit(self, r):
            recs.append(r)

    lg = logging.getLogger("astrbot")
    h = _H()
    lg.addHandler(h)
    try:
        assert cosine([1.0, 2.0], [1.0, 2.0, 3.0]) == 0.0
    finally:
        lg.removeHandler(h)
    assert recs, "维度不一致时必须告警，否则用户只看到『记忆突然全忘了』"


# ═══════════════ 结构性：不再犯 @staticmethod 用 self ═══════════════

def test_no_staticmethod_uses_self_anywhere_in_plugin():
    """全插件扫描（不只是 main.py）：@staticmethod 里不许出现 self。"""
    import ast
    bad = []
    for f in PLUGIN.rglob("*.py"):
        rel = f.relative_to(PLUGIN)
        if "__pycache__" in str(f) or rel.parts[0] in ("tests", "migrations"):
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            for fn in cls.body:
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if not any(isinstance(d, ast.Name) and d.id == "staticmethod"
                           for d in fn.decorator_list):
                    continue
                if any(isinstance(n, ast.Name) and n.id == "self"
                       for n in ast.walk(fn)):
                    bad.append(f"{rel.as_posix()}:{cls.name}.{fn.name}")
    assert not bad, "以下 @staticmethod 用了 self（会 NameError）:\n  " + "\n  ".join(bad)
