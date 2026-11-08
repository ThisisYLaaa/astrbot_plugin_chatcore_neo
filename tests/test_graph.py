# -*- coding: utf-8 -*-
"""图谱层测试：模型 / 存储 / BM25 / 抽取 / RRF / 双路融合 / 记忆固化。"""

from __future__ import annotations

import asyncio
import json
import math
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.graph import (GraphExtractor, GraphStore, canonicalize, dedupe,  # noqa: E402
                        extract_into, rrf_fuse, tokenize)
from core.graph.model import GraphEdge, GraphEntry, GraphNode  # noqa: E402
from core.graph.retriever import RRF_K, GraphRetriever  # noqa: E402
from core.memory.retrieval import (DualRouteRetriever, RouteHit,  # noqa: E402
                                  route_weights)
from core.memory.summarizer import (fallback_consolidation,  # noqa: E402
                                   parse_consolidation)
from core.memory.store import MemoryStore  # noqa: E402


class _Cfg(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)


# ---------------------------------------------------------------- 归一化

def test_canonicalize_matches_old_behaviour():
    """归一化决定去重；改了它同一实体会被存成两个节点。"""
    assert canonicalize("  香奈美  ") == "香奈美"
    assert canonicalize("《卡拉彼丘》") == "卡拉彼丘"
    assert canonicalize("Hello World") == "hello world"   # ASCII 转小写
    assert canonicalize("香奈美, ") == "香奈美"
    assert canonicalize("") == ""
    # 混排串不是纯 ASCII，按旧实现**不转小写**（只有全 ASCII 才转）
    assert canonicalize("ABC 中文") == "ABC 中文"
    assert canonicalize("ABC") == "abc"


def test_dedupe_preserves_order_and_original():
    got = dedupe(["苹果", "苹果 ", "《苹果》", "香蕉"])
    assert got == ["苹果", "香蕉"], got


# ---------------------------------------------------------------- 分词

def test_tokenize_ascii_words():
    assert "hello" in tokenize("Hello World")
    assert "world" in tokenize("Hello World")


def test_tokenize_cjk_bigrams():
    toks = tokenize("香奈美")
    assert toks == ["香奈", "奈美"], toks
    assert tokenize("美") == ["美"]


def test_tokenize_mixed():
    toks = tokenize("香奈美SD画图")
    assert "香奈" in toks and "画图" in toks
    assert "sd" in toks


# ---------------------------------------------------------------- 存储

def _store() -> GraphStore:
    return GraphStore(Path(tempfile.mkdtemp()) / "graph.json")


def test_upsert_node_dedupes_by_key():
    s = _store()
    a = s.upsert_node(GraphNode("topic", "SD画图"))
    b = s.upsert_node(GraphNode("topic", "SD画图 "))
    assert a.node_key == b.node_key == "topic:SD画图"
    assert len(s.nodes) == 1, "归一化后相同实体必须只有一个节点"
    assert a.id == b.id


def test_upsert_node_merges_metadata():
    s = _store()
    s.upsert_node(GraphNode("person", "小明", metadata={"a": 1}))
    s.upsert_node(GraphNode("person", "小明", metadata={"b": 2}))
    n = s.nodes["person:小明"]
    assert n.metadata.get("a") == 1 and n.metadata.get("b") == 2


def test_upsert_edge_and_entry_dedupe():
    s = _store()
    e1 = GraphEdge("topic:x", "fact:y", "describes", 1)
    e2 = GraphEdge("topic:x", "fact:y", "describes", 1)
    s.upsert_edge(e1)
    s.upsert_edge(e2)
    assert len(s.edges) == 1

    en = GraphEntry("fact", "Fact: y. Summary: s", ["fact:y"], "fact", 1)
    _o1, c1 = s.upsert_entry(en)
    _o2, c2 = s.upsert_entry(en)
    assert c1 is True and c2 is False
    assert len(s.entries) == 1


def test_bm25_ranks_matching_entry_first():
    s = _store()
    s.upsert_entry(GraphEntry("fact", "Fact: 我们在讨论卡拉彼丘的阵营. Summary: x",
                              ["fact:a"], "fact", 1))
    s.upsert_entry(GraphEntry("fact", "Fact: 今天天气很好. Summary: x",
                              ["fact:b"], "fact", 1))
    hits = s.bm25_search("卡拉彼丘")
    assert hits, "应命中"
    assert "卡拉彼丘" in hits[0][0].content


def test_bm25_session_filter():
    s = _store()
    s.upsert_entry(GraphEntry("fact", "Fact: 苹果. Summary: x", ["fact:a"],
                              "fact", 1, session_id="S1"))
    s.upsert_entry(GraphEntry("fact", "Fact: 苹果. Summary: y", ["fact:b"],
                              "fact", 2, session_id="S2"))
    assert len(s.bm25_search("苹果")) == 2
    assert len(s.bm25_search("苹果", session_id="S1")) == 1


def test_bm25_strict_persona_filter():
    """旧 `persona_id` 只丢「明确是别人格」；`strict_persona_id` 才是严格隔离。"""
    s = _store()
    s.upsert_entry(GraphEntry("fact", "Fact: 苹果. Summary: a", ["fact:a"],
                              "fact", 1, persona_id="A"))
    s.upsert_entry(GraphEntry("fact", "Fact: 苹果. Summary: b", ["fact:b"],
                              "fact", 2, persona_id="B"))
    s.upsert_entry(GraphEntry("fact", "Fact: 苹果. Summary: c", ["fact:c"],
                              "fact", 3, persona_id=""))
    assert len(s.bm25_search("苹果")) == 3
    assert len(s.bm25_search("苹果", persona_id="A")) == 2, "旧语义保留空 persona"
    assert len(s.bm25_search("苹果", strict_persona_id="A")) == 1


def test_graph_search_demotes_other_persona():
    """本命优先：别人格条目在融合前被 other_factor 打折。"""
    s = _store()
    s.upsert_entry(GraphEntry("fact", "Fact: 苹果. Summary: mine", ["fact:a"],
                              "fact", 1, persona_id="A"))
    s.upsert_entry(GraphEntry("fact", "Fact: 苹果. Summary: other", ["fact:b"],
                              "fact", 2, persona_id="B"))
    r = GraphRetriever(s, None, _Cfg())

    def _final(res, suffix):
        return next(x.final_score for x in res if x.content.endswith(suffix))

    base = asyncio.run(r.search("苹果", 10))
    demoted = asyncio.run(r.search("苹果", 10, prefer_persona="A",
                                   other_factor=0.5))
    assert _final(base, "Summary: other") > 0
    assert math.isclose(_final(demoted, "Summary: mine"),
                        _final(base, "Summary: mine"), rel_tol=1e-6)
    assert math.isclose(_final(demoted, "Summary: other"),
                        _final(base, "Summary: other") * 0.5, rel_tol=1e-6)


def test_graph_search_strict_persona_isolates():
    s = _store()
    s.upsert_entry(GraphEntry("fact", "Fact: 苹果. Summary: mine", ["fact:a"],
                              "fact", 1, persona_id="A"))
    s.upsert_entry(GraphEntry("fact", "Fact: 苹果. Summary: other", ["fact:b"],
                              "fact", 2, persona_id="B"))
    r = GraphRetriever(s, None, _Cfg())
    out = asyncio.run(r.search("苹果", 10, prefer_persona="A",
                               strict_persona=True))
    assert len(out) == 1
    assert out[0].content.endswith("Summary: mine")


def test_neighbors_and_expand():
    s = _store()
    for k in ("person:a", "person:b", "fact:x"):
        s.upsert_node(GraphNode(*k.split(":", 1)))
    s.upsert_edge(GraphEdge("person:a", "person:b", "co_occurs_with", 1))
    s.upsert_edge(GraphEdge("person:b", "fact:x", "mentioned_in", 1))
    nb = s.neighbors("person:a")
    assert any(k == "person:b" for k, _r, _c in nb)
    seen = s.expand(["person:a"], hops=2)
    assert "person:b" in seen and "fact:x" in seen, seen


def test_expand_relation_filter():
    s = _store()
    s.upsert_edge(GraphEdge("a", "b", "describes", 1))
    s.upsert_edge(GraphEdge("a", "c", "mentioned_in", 1))
    nb = s.neighbors("a", relation="describes")
    assert [k for k, _r, _c in nb] == ["b"]


def test_forget_memory_removes_entries_edges_and_orphan_nodes():
    s = _store()
    s.upsert_entry(GraphEntry("fact", "Fact: A. Summary: s", ["fact:A"], "fact", 7))
    s.upsert_edge(GraphEdge("topic:T", "fact:A", "describes", 7))
    assert s.stats()["entries"] == 1
    n = s.forget_memory(7)
    assert n == 1
    st = s.stats()
    assert st["entries"] == 0 and st["edges"] == 0
    assert st["nodes"] == 0, "孤儿节点应被清理"


def test_persistence_roundtrip():
    d = Path(tempfile.mkdtemp()) / "g.json"
    s = GraphStore(d)
    s.upsert_entry(GraphEntry("topic", "Topic: X. Summary: s", ["topic:X"], "topic", 1))
    s.save(force=True)
    s2 = GraphStore(d)
    assert s2.stats()["entries"] == 1
    assert s2.stats()["nodes"] == 1
    # 倒排索引是重建的，不该因持久化而漂移
    assert s2.bm25_search("X"), "重建索引后仍能检索"


def test_corrupt_graph_file_starts_empty():
    d = Path(tempfile.mkdtemp()) / "g.db"
    d.write_bytes(b"this is not a sqlite database")
    s = GraphStore(d)
    assert s.stats()["entries"] == 0
    assert list(d.parent.glob("g.db.bad-*")), \
        "坏库应改名留证（沿用旧 JSON 实现的策略），不能直接删掉"


def test_graph_json_path_maps_to_db():
    """旧调用点传的是 `graph.json`，落盘要统一成 `.db`。"""
    d = Path(tempfile.mkdtemp()) / "graph.json"
    s = GraphStore(d)
    assert s.path.name == "graph.db"
    s.upsert_entry(GraphEntry("fact", "Fact: x. Summary: s", ["fact:x"], "fact", 1))
    assert s.path.exists(), "应按 .db 落盘"
    assert not d.exists(), "不该再写一份 .json"


def test_graph_upsert_persists_without_save():
    """写入是即时的：不该再依赖 save() 才落盘（旧实现攒到 save 才整份重写）。"""
    d = Path(tempfile.mkdtemp()) / "g.db"
    s = GraphStore(d)
    s.upsert_node(GraphNode("topic", "X", metadata={"a": 1}))
    s.upsert_edge(GraphEdge("topic:x", "fact:y", "describes", 1))
    s.upsert_entry(GraphEntry("fact", "Fact: y. Summary: s", ["topic:x"],
                              "fact", 1))
    st = GraphStore(d).stats()          # 新实例，强制从盘上读
    assert (st["nodes"], st["edges"], st["entries"]) == (1, 1, 1), st


def test_graph_entry_vector_is_float32_blob():
    """条目向量同样存 BLOB：326 条带向量的条目从 42 MB 降到 5 MB。"""
    import sqlite3
    d = Path(tempfile.mkdtemp()) / "g.db"
    GraphStore(d).upsert_entry(GraphEntry(
        "fact", "Fact: x. Summary: s", ["fact:x"], "fact", 1,
        vec=[0.25] * 32))
    con = sqlite3.connect(str(d))
    blob = con.execute("SELECT vec FROM entries").fetchone()[0]
    con.close()
    assert isinstance(blob, bytes), "vec 列存的不是 BLOB"
    assert len(blob) == 32 * 4, f"每维应 4 字节（float32），实际 {len(blob)}"


def test_graph_save_entries_persists_late_vectors():
    """向量是 upsert 之后才拿到的（embed 要 await），必须能补写。"""
    d = Path(tempfile.mkdtemp()) / "g.db"
    s = GraphStore(d)
    en = GraphEntry("fact", "Fact: x. Summary: s", ["fact:x"], "fact", 1)
    s.upsert_entry(en)
    assert not len(s.entries[en.entry_key].vec)
    en.vec = [0.5] * 16              # 模拟 await 之后才拿到向量
    s.save_entries([en])
    again = GraphStore(d)
    assert len(again.entries[en.entry_key].vec) == 16, \
        "补写的向量没落盘 —— 召回时图谱那条会用不上向量"


def test_forget_session_removes_entries_by_key():
    """按记忆键删图：旧的按 mid 联动删除的兜底。

    历史数据里图条目的 source_memory_id 与其派生原子对不上
    （旧实现两路脱节），按原子 mid 删会漏。session_id 两路同源，
    所以按记忆键删必须把该键的条目、边、孤儿节点全部带走，
    且不碰别的会话。
    """
    d = Path(tempfile.mkdtemp()) / "g.db"
    s = GraphStore(d)
    s.upsert_entry(GraphEntry("fact", "Fact: A. Summary: s", ["fact:A"],
                              "fact", 7, session_id="S1"))
    s.upsert_entry(GraphEntry("fact", "Fact: B. Summary: s", ["fact:B"],
                              "fact", 7, session_id="S1"))
    s.upsert_edge(GraphEdge("topic:T", "fact:A", "describes", 7))
    s.upsert_entry(GraphEntry("fact", "Fact: 别的会话. Summary: s",
                              ["fact:C"], "fact", 8, session_id="S2"))
    assert s.forget_session("S1") == 2
    st = s.stats()
    assert st["entries"] == 1, st               # 只剩别的会话的条目
    assert st["nodes"] == 1, st                 # 只剩别的会话的节点
    assert st["edges"] == 0, st                 # 该键的边删干净
    # 落盘一致性：重新打开还是一样的状态
    again = GraphStore(d).stats()
    assert again["entries"] == 1 and again["nodes"] == 1 and again["edges"] == 0


def test_forget_session_handles_mismatched_mid():
    """mid 对不上（甚至为 0）的孤儿条目也能被按 session 删掉。"""
    d = Path(tempfile.mkdtemp()) / "g.db"
    s = GraphStore(d)
    s.upsert_entry(GraphEntry("fact", "Fact: X. Summary: s", ["fact:X"],
                              "fact", 0, session_id="S1"))
    assert s.forget_session("S1") == 1
    assert s.stats()["entries"] == 0
    assert s.stats()["nodes"] == 0, "孤儿节点应一并清理"


# ---------------------------------------------------------------- 抽取

META = {
    "canonical_summary": "晚上群里在聊 SD 画图",
    "topics": ["SD画图", "画风切换"],
    "participants": ["小明", "小红"],
    "key_facts": ["小明用 bot 画了火花"],
    "importance": 0.6,
    "session_id": "S1",
    "persona_id": "kanami",
}


def test_extract_node_types_and_keys():
    g = GraphExtractor().extract(1, "正文", META)
    keys = {n.node_key for n in g.nodes}
    assert "topic:SD画图" in keys
    assert "person:小明" in keys and "person:小红" in keys
    assert "fact:小明用 bot 画了火花" in keys


def test_extract_entry_types():
    g = GraphExtractor().extract(1, "正文", META)
    types = {e.entry_type for e in g.entries}
    assert {"fact", "topic", "participant", "edge"} <= types


def test_extract_confidence_values_match_old():
    """confidence 是对齐旧实现的常数，改了会影响图谱路排序。"""
    g = GraphExtractor().extract(1, "正文", META)
    by = {}
    for e in g.entries:
        by.setdefault((e.entry_type, e.relation_type), []).append(
            e.metadata["graph_confidence"])
    assert by[("fact", "fact")][0] == 0.9
    assert by[("topic", "topic")][0] == 0.75
    assert by[("participant", "participant")][0] == 0.7
    assert by[("edge", "describes")][0] == 0.82
    assert by[("edge", "mentioned_in")][0] == 0.88
    assert by[("edge", "co_occurs_with")][0] == 0.7


def test_extract_entry_content_templates():
    g = GraphExtractor().extract(1, "正文", META)
    texts = [e.content for e in g.entries]
    assert any(t.startswith("Fact: 小明用 bot 画了火花. Summary:") for t in texts)
    assert any(t.startswith("Topic: SD画图. Summary:") for t in texts)
    assert any(t.startswith("Participant: 小明. Summary:") for t in texts)
    assert any("describes fact" in t for t in texts)
    assert any("is linked to fact" in t for t in texts)
    assert any("co-occurs with" in t for t in texts)


def test_extract_edges_relations():
    g = GraphExtractor().extract(1, "正文", META)
    rels = {e.relation_type for e in g.edges}
    assert rels == {"describes", "mentioned_in", "co_occurs_with"}, rels


def test_extract_falls_back_to_summary_entry():
    g = GraphExtractor().extract(1, "", {"canonical_summary": "只有摘要"})
    types = {e.entry_type for e in g.entries}
    assert "summary" in types or "fact" in types


def test_extract_limits_respected():
    meta = dict(META)
    meta["key_facts"] = ["事实一", "事实二", "事实三"]
    meta["topics"] = ["话题一", "话题二"]
    g = GraphExtractor({"graph_max_topics": 1, "graph_max_facts": 2,
                        "graph_max_participants": 1}).extract(1, "x", meta)
    assert len([e for e in g.entries if e.entry_type == "topic"]) == 1
    assert len([e for e in g.entries if e.entry_type == "fact"]) == 2


def test_extract_into_store():
    s = _store()
    g = extract_into(s, 5, "正文", META)
    assert g.entries, "应有新条目"
    assert s.stats()["entries"] == len(g.entries)
    # 重复写入不新增
    g2 = extract_into(s, 5, "正文", META)
    assert len(g2.entries) == 0


# ---------------------------------------------------------------- RRF

def test_rrf_fuse_orders_by_rank_sum():
    fused = rrf_fuse([["a", "b"], ["b", "a"]])
    assert abs(fused["a"] - fused["b"]) < 1e-9, "两路排名互换应得同分"
    fused2 = rrf_fuse([["a", "b"], ["a", "b"]])
    assert fused2["a"] > fused2["b"]


def test_rrf_formula():
    fused = rrf_fuse([["x"]])
    assert abs(fused["x"] - 1.0 / (RRF_K + 1)) < 1e-12


def test_rrf_only_in_one_list_still_scores():
    fused = rrf_fuse([["a"], ["b"]])
    assert "a" in fused and "b" in fused


# ---------------------------------------------------------------- 图谱路打分

def test_graph_retriever_scoring_formula():
    s = _store()
    meta = {"importance": 0.6, "graph_confidence": 0.9,
            "create_time": 1e12, "last_access_time": 1e12}
    s.upsert_entry(GraphEntry("fact", "Fact: 卡拉彼丘. Summary: s",
                              ["fact:a"], "fact", 1, metadata=meta))
    r = GraphRetriever(s, None, _Cfg())
    hits = asyncio.run(r.search("卡拉彼丘", 5))
    assert hits, "应命中"
    b = hits[0].score_breakdown
    # RRF 归一化后是 1.0（单条），检查权重与最终分一致
    expect = (0.55 * b["graph_rrf_normalized"] + 0.20 * b["graph_importance"]
              + 0.15 * b["graph_recency_weight"] + 0.10 * b["graph_confidence"])
    assert abs(b["graph_final_score"] - round(expect, 4)) < 1e-3, (b, expect)


def test_graph_retriever_empty_and_no_match():
    s = _store()
    r = GraphRetriever(s, None, _Cfg())
    assert asyncio.run(r.search("")) == []
    assert asyncio.run(r.search("不存在的东西")) == []


def test_graph_retriever_report_shape():
    r = GraphRetriever(_store(), None, _Cfg())
    rep = r.report()
    for k in ("searches", "keyword_hits", "vector_hits", "graph"):
        assert k in rep, k


# ---------------------------------------------------------------- 双路融合

def test_route_weights_default():
    d, g, intent = route_weights("随便问问", doc_w=0.65, graph_w=0.35)
    assert abs(d - 0.65) < 1e-9 and abs(g - 0.35) < 1e-9 and intent == "default"


def test_route_weights_relationship_favours_graph():
    d, g, intent = route_weights("小明和谁认识", doc_w=0.65, graph_w=0.35)
    assert intent == "relationship"
    assert g > 0.35, (d, g)


def test_route_weights_factual_favours_document():
    d, g, intent = route_weights("卡拉彼丘是什么", doc_w=0.65, graph_w=0.35)
    assert "factual" in intent
    assert d > 0.65, (d, g)


def test_route_weights_temporal():
    d, g, intent = route_weights("上次聊到哪了", doc_w=0.65, graph_w=0.35)
    assert "temporal" in intent and g > 0.35


def test_route_weights_fixed_mode():
    d, g, intent = route_weights("小明和谁认识", doc_w=0.65, graph_w=0.35,
                                 dynamic=False)
    assert (d, g, intent) == (0.65, 0.35, "fixed")


def test_dual_route_bonus_for_both_routes():
    dr = DualRouteRetriever(_Cfg())
    doc = [RouteHit("k1", 1.0, "文档内容")]
    graph = [RouteHit("k1", 1.0, "图谱内容")]
    only_doc = dr.fuse("q", doc, [], k=5)
    both = dr.fuse("q", doc, graph, k=5)
    assert both[0].final_score > only_doc[0].final_score, "两路都命中应有加成"
    assert both[0].breakdown["cross_route_bonus"] > 0


def test_dual_route_keeps_graph_only_hits():
    dr = DualRouteRetriever(_Cfg())
    out = dr.fuse("q", [], [RouteHit("g1", 0.9, "图谱独有")], k=5)
    assert len(out) == 1 and out[0].content == "图谱独有"


def test_dual_route_empty():
    assert DualRouteRetriever(_Cfg()).fuse("q", [], [], k=5) == []


# ---------------------------------------------------------------- 固化

def test_parse_consolidation_forms():
    good = '{"summary":"聊了画图","topics":["画图"],"key_facts":["A画了图"],"importance":0.7}'
    r = parse_consolidation(good)
    assert r["summary"] == "聊了画图" and r["importance"] == 0.7
    assert parse_consolidation(f"```json\n{good}\n```")["summary"] == "聊了画图"
    assert parse_consolidation(f"好的：\n{good}\n完毕")["summary"] == "聊了画图"


def test_parse_consolidation_rejects_garbage():
    assert parse_consolidation("我总结不出来") is None
    assert parse_consolidation("") is None
    assert parse_consolidation("{}") is None


def test_parse_consolidation_clamps_and_caps():
    r = parse_consolidation(json.dumps({
        "summary": "x", "topics": [f"t{i}" for i in range(20)],
        "key_facts": [f"f{i}" for i in range(20)], "importance": 9.9}))
    assert len(r["topics"]) <= 6
    assert len(r["key_facts"]) <= 8
    assert r["importance"] == 1.0


def test_fallback_consolidation_keeps_text():
    r = fallback_consolidation("用户：你好\nbot：你也好")
    assert r["degraded"] is True
    assert r["summary"]
    assert r["key_facts"], "降级也要给出事实，否则图谱没输入"


# ---------------------------------------------------------------- 记忆服务

class _FakeEmbedder:
    def __init__(self):
        import hashlib

    @property
    def available(self):
        return True

    async def embed(self, text):
        import hashlib
        d = hashlib.sha256((text or "").encode()).digest()
        return [b / 255.0 for b in d[:8]]

    async def embed_dedup(self, text):
        return await self.embed(text)

    def flush(self):
        pass

    def stats(self):
        return {}


def _service():
    from core.memory.service import MemoryService
    root = Path(tempfile.mkdtemp())
    svc = MemoryService(_FakeEmbedder(), MemoryStore(root),
                        config=_Cfg({"memory_consolidate_turns": 2,
                                     "graph_embed_entries": False}))
    svc.graph = GraphStore(root / "graph.json")
    svc.graph_retriever.store = svc.graph
    svc._pending_path = root / "_pending.json"
    return svc


def test_note_turn_and_threshold():
    svc = _service()
    assert svc.pending_size("K") == 0
    svc.note_turn("K", "你好", "你也好")
    assert svc.pending_size("K") == 1
    assert not svc.should_consolidate("K")
    svc.note_turn("K", "在吗", "在")
    assert svc.should_consolidate("K")


def test_consolidate_builds_document_and_graph():
    svc = _service()

    async def fake_consolidate(text, participants=None, umo=""):
        return {"summary": "聊了 SD 画图与画风", "topics": ["SD画图"],
                "key_facts": ["小明用 bot 画了火花"], "importance": 0.6,
                "degraded": False}

    svc.summarizer.consolidate = fake_consolidate
    svc.note_turn("K", "画个图", "好呀", uid="123", user_name="小明")
    svc.note_turn("K", "换个画风", "换了", uid="123", user_name="小明")
    r = asyncio.run(svc.consolidate("K", session_id="K", persona_id="kanami"))
    assert r is not None
    assert r["topics"] == ["SD画图"]
    # 文档路：固化出一条文档
    assert len(svc.store.load("K")) == 1
    atom = svc.store.load("K")[0]
    assert atom.kind == "document"
    assert atom.metadata["topics"] == ["SD画图"]
    # 图谱路：节点/边/条目都长出来了
    st = svc.graph.stats()
    assert st["nodes"] >= 3, st
    assert st["edges"] >= 1, st
    assert st["entries"] >= 3, st
    # 缓冲已清空
    assert svc.pending_size("K") == 0


def test_consolidate_clears_pending_only_on_success():
    svc = _service()
    svc.note_turn("K", "a", "b")
    svc.note_turn("K", "c", "d")
    svc.summarizer.consolidate = None      # 触发异常路径
    r = asyncio.run(svc.consolidate("K"))
    assert r is None
    assert svc.pending_size("K") == 2, "固化失败必须保留缓冲，不能丢对话"


def test_pending_survives_reload():
    svc = _service()
    svc.note_turn("K", "记住这句", "好")
    svc._save_pending()
    from core.memory.service import MemoryService
    svc2 = MemoryService(_FakeEmbedder(), MemoryStore(Path(tempfile.mkdtemp())),
                         config=_Cfg({}))
    svc2._pending_path = svc._pending_path
    svc2._load_pending()
    assert svc2.pending_size("K") == 1, "未固化的对话应跨重启保留"


def test_recall_uses_both_routes():
    svc = _service()

    async def fake_consolidate(text, participants=None, umo=""):
        return {"summary": "小明和小红在研究卡拉彼丘阵营", "topics": ["卡拉彼丘"],
                "key_facts": ["小明和小红在讨论卡拉彼丘的剪刀手阵营"],
                "importance": 0.7, "degraded": False}

    svc.summarizer.consolidate = fake_consolidate
    svc.note_turn("K", "卡拉彼丘阵营怎么分", "分这几个", uid="1", user_name="小明")
    svc.note_turn("K", "剪刀手呢", "说说看", uid="2", user_name="小红")
    asyncio.run(svc.consolidate("K", session_id="K", persona_id="p"))

    out = asyncio.run(svc.recall_texts("K", "卡拉彼丘", persona="p"))
    assert out, "双路召回应给出结果"
    assert any("卡拉彼丘" in t for t in out)


def test_recall_empty_when_disabled():
    from core.memory.service import MemoryService
    root = Path(tempfile.mkdtemp())
    svc = MemoryService(_FakeEmbedder(), MemoryStore(root),
                        config=_Cfg({"enable_memory": False}))
    assert asyncio.run(svc.recall_texts("K", "任何")) == []


def test_service_stats_shape():
    svc = _service()
    st = svc.stats()
    for k in ("enabled", "graph", "consolidated", "consolidate_turns"):
        assert k in st, k
    assert "nodes" in st["graph"]


def test_forget_clears_document_and_pending():
    svc = _service()
    svc.note_turn("K", "a", "b")
    svc.forget("K")
    assert svc.pending_size("K") == 0
    assert svc.store.load("K") == []
