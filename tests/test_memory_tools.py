# -*- coding: utf-8 -*-
"""memory_search / memory_note 两个 LLM 工具的行为测试。

工具的核心承诺：
  * search：按语义检索**本会话的长期记忆**，没命中要明确说"没印象"（不许编）；
  * note：**仅特权用户**可调；写入即时且精确（不经总结改写）、重要度拉满；
    私聊按特权处理；重复写同一句要能识别出来而不是堆两条。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(PLUGIN.parent))
import _env  # noqa: E402
_APP = _env.app()
if _APP.is_dir():
    sys.path.insert(0, str(_APP))

import importlib  # noqa: E402

plugin_main = importlib.import_module(f"{PLUGIN.name}.main")
from test_admin_api import _plugin  # noqa: E402

Neo = plugin_main.ChatCoreNeoPlugin


class _Ev:
    """最小事件桩：够这两个工具用。"""

    def __init__(self, umo, uid="100000001", name="翼龙", private=False):
        self.umo = umo
        self.uid = uid
        self.name = name
        self.private = private

    def is_private_chat(self):
        return self.private

    @property
    def unified_msg_origin(self):
        return self.umo

    def get_sender_id(self):
        return self.uid

    def get_sender_name(self):
        return self.name

    def get_group_id(self):
        return "200000001"


def _tool_plugin():
    p = _plugin()
    for name in ("_persona_for", "_persona_labels",
                 "memory_search_tool", "memory_note_tool"):
        setattr(p, name, getattr(Neo, name).__get__(p))
    p._sender_id = getattr(Neo, "_sender_id")
    p._tool_json = Neo._tool_json          # staticmethod：直接赋函数
    return p


def _run(coro):
    return asyncio.run(coro)


UMO = "Banyan:GroupMessage:200000001"


# ---------------------------------------------------------------- search

def test_memory_search_returns_hits():
    p = _tool_plugin()
    key = p.groups.group_of(UMO)
    qvec = _run(p.memory.embedder.embed("卡拉彼丘"))
    p.memory.store.add(key, "用户在聊卡拉彼丘的剪刀手阵营", qvec,
                       kind="fact", importance=0.8)
    out = json.loads(_run(p.memory_search_tool(_Ev(UMO), query="卡拉彼丘")))
    assert out["success"] is True
    assert out["count"] >= 1
    assert any("剪刀手" in m for m in out["memories"]), out


def test_memory_search_says_so_when_nothing_found():
    """没查到必须让模型知道"我确实没印象"，否则它会顺着猜。"""
    p = _tool_plugin()
    out = json.loads(_run(p.memory_search_tool(_Ev(UMO), query="完全不存在的往事")))
    assert out["success"] is True and out["count"] == 0
    assert out["memories"] == []
    assert "没印象" in out["message"]


def test_memory_search_rejects_empty_query():
    p = _tool_plugin()
    out = json.loads(_run(p.memory_search_tool(_Ev(UMO), query="   ")))
    assert out["success"] is False and out["error"] == "empty_query"


def test_memory_search_reports_disabled():
    p = _tool_plugin()

    class _Off:
        enabled = False

    p.memory = _Off()
    out = json.loads(_run(p.memory_search_tool(_Ev(UMO), query="x")))
    assert out["success"] is False and out["error"] == "disabled"


# ---------------------------------------------------------------- note

def test_memory_note_denies_non_privileged():
    p = _tool_plugin()
    ev = _Ev(UMO, uid="999", name="路人")
    out = json.loads(_run(p.memory_note_tool(ev, text="他喜欢猫")))
    assert out["success"] is False and out["error"] == "permission_denied"
    assert p.memory.store.load(p.groups.group_of(UMO)) == [], \
        "拒绝时绝不能写进记忆"


def test_memory_note_writes_immediately_and_maxes_importance():
    p = _tool_plugin()
    out = json.loads(_run(p.memory_note_tool(
        _Ev(UMO), text="他养了只叫小黑的猫", kind="fact")))
    assert out["success"] is True and out["kind"] == "fact"
    atoms = p.memory.store.load(p.groups.group_of(UMO))
    assert len(atoms) == 1
    a = atoms[0]
    assert a.text == "他养了只叫小黑的猫"
    assert a.kind == "fact"
    assert a.importance == 1.0, "手动记的要拉满重要度"
    assert a.metadata["manual"] is True
    assert p.memory.stats_counters["manual_notes"] == 1
    # 立刻可被检索到（不需要等固化窗口）
    found = json.loads(_run(p.memory_search_tool(_Ev(UMO), query="小黑")))
    assert found["count"] >= 1, found


def test_memory_note_from_private_chat_counts_as_privileged():
    p = _tool_plugin()
    umo = "Banyan:FriendMessage:100000001"
    out = json.loads(_run(p.memory_note_tool(
        _Ev(umo, private=True), text="他讨厌早起", kind="preference")))
    assert out["success"] is True
    assert p.memory.store.load(p.groups.group_of(umo))[0].kind == "preference"


def test_memory_note_rejects_empty_text():
    p = _tool_plugin()
    out = json.loads(_run(p.memory_note_tool(_Ev(UMO), text="   ")))
    assert out["success"] is False and out["error"] == "empty"


def test_memory_note_falls_back_to_fact_for_unknown_kind():
    p = _tool_plugin()
    out = json.loads(_run(p.memory_note_tool(
        _Ev(UMO), text="随便一句", kind="胡说八道")))
    assert out["success"] is True and out["kind"] == "fact"


def test_memory_note_reports_duplicate_instead_of_stacking():
    p = _tool_plugin()
    first = json.loads(_run(p.memory_note_tool(_Ev(UMO), text="他喜欢猫")))
    again = json.loads(_run(p.memory_note_tool(_Ev(UMO), text="他喜欢猫")))
    assert first["success"] is True and first["already"] is False
    assert again["success"] is True and again["already"] is True
    assert len(p.memory.store.load(p.groups.group_of(UMO))) == 1


def test_memory_note_survives_embedding_failure():
    """embedding 挂了要如实说"没记下来"，而不是假装成功。"""
    p = _tool_plugin()

    class _Dead:
        @property
        def available(self):
            return True

        async def embed(self, text):
            return []

    p.memory.embedder = _Dead()
    out = json.loads(_run(p.memory_note_tool(_Ev(UMO), text="记一下")))
    assert out["success"] is False
    assert out["error"] == "embedding_failed"
