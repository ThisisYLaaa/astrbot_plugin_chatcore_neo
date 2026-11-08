# -*- coding: utf-8 -*-
"""离线评测harness（`tests/memory_eval.py`）自身的回归测试。

harness 是"改排序前后的裁判"，它自己错了会让后面所有对比都失去意义
——比如把没召回的算成命中、或者把被闸门拦下的查询也计入指标。
这里用一份**合成的小库**（不碰真实数据）把指标算法钉死。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = TESTS_DIR.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from core.memory.embedder import Embedder                     # noqa: E402
from core.memory.store import MemoryStore                     # noqa: E402

sys.path.insert(0, str(TESTS_DIR))
import memory_eval                                            # noqa: E402
from memory_eval import DEFAULT_CASES, evaluate, load_cases    # noqa: E402


def _build(tmp: Path) -> Path:
    """造一个 3 维的小库：3 条记忆 + 3 条查询向量。"""
    db = tmp / "memory.db"
    store = MemoryStore(tmp, db_path=db)
    store.add("K", "用户喜欢打游戏", [1.0, 0.0, 0.0], importance=0.8)
    store.add("K", "用户讨厌早起", [0.0, 1.0, 0.0], importance=0.8)
    store.add("K", "群友聊到游戏帧数", [0.7, 0.7, 0.0], importance=0.4)
    e = Embedder(context=None, db_path=db)
    for text, vec in (("打游戏", [1.0, 0.0, 0.0]),
                      ("讨厌早起", [0.0, 1.0, 0.0]),
                      ("嗯", [1.0, 0.0, 0.0])):
        e.cache_put(Embedder.key_of(text), vec)
    return db


def test_eval_scores_hits_and_noise():
    with tempfile.TemporaryDirectory() as td:
        db = _build(Path(td))
        res = evaluate(db, [
            {"key": "K", "query": "打游戏", "expect_any": ["打游戏"]},
            {"key": "K", "query": "讨厌早起", "expect_any": ["讨厌早起"]},
        ])
    s = res["summary"]
    assert s["graded"] == 2, res["rows"]
    assert s["final"]["hit@5"] == 1.0
    assert s["final"]["mrr"] == 1.0
    # 两条查询都召回了 2 条、各命中 1 条 → precision@5 都是 0.5
    assert s["final"]["precision@5"] == 0.5, s
    assert s["raw"]["hit@5"] == 1.0
    assert res["db"]["current_dim"] == 3
    assert res["db"]["unreachable"] == 0


def test_eval_reports_mmr_redundancy_drop():
    """去冗余的效果必须体现在 `redundancy` 上（否则这项改动没法验证）。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db = tmp / "memory.db"
        store = MemoryStore(tmp, db_path=db)
        # 向量要两两余弦 < 0.95，否则会被 add() 当重复直接合并掉
        store.add("K", "德丽莎说自己刚煮了蛋包饭，蛋包饭在桌上", [1.0, 0.0, 0.0],
                  importance=0.8)
        store.add("K", "德丽莎说自己刚煮了蛋包饭，蛋包饭在桌上呢",
                  [0.8, 0.6, 0.0], importance=0.8)
        store.add("K", "江一楠问 bot 有没有情人，bot 回答一个都没有",
                  [0.6, 0.0, 0.8], importance=0.8)
        e = Embedder(context=None, db_path=db)
        e.cache_put(Embedder.key_of("蛋包饭"), [1.0, 0.0, 0.0])
        # top_k 必须小于候选数，否则 MMR 只是换个顺序、冗余度不会变
        res = evaluate(db, [{"key": "K", "query": "蛋包饭",
                             "expect_any": ["蛋包饭"]}], top_k=2)
    s = res["summary"]
    assert s["raw"]["redundancy"] > s["final"]["redundancy"], s
    row = res["rows"][0]
    assert row["returned"] == 2, row
    assert row["redundancy_raw"] > row["redundancy"], row


def test_eval_uses_snapshot_vectors_when_cache_is_empty():
    """向量快照优先：换 embedding 模型清了缓存后，评测集照样能跑。"""
    from array import array as _arr

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db = tmp / "memory.db"
        store = MemoryStore(tmp, db_path=db)
        store.add("K", "用户在聊卡拉彼丘", [1.0, 0.0, 0.0], importance=0.8)
        # 故意不往 embed_cache 写任何东西：只能靠快照
        snap = {Embedder.key_of("卡拉彼丘"): _arr("f", [1.0, 0.0, 0.0])}
        res = evaluate(db, [{"key": "K", "query": "卡拉彼丘",
                             "expect_any": ["卡拉彼丘"]}], snapshot=snap)
    assert res["rows"][0]["status"] == "ok", res["rows"]
    assert res["summary"]["vectors"] == "snapshot"
    assert res["db"]["unreachable"] == 0


def test_dump_vectors_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db = _build(tmp)
        out = tmp / "vecs.json"
        got = memory_eval.dump_vectors(db, [{"query": "打游戏"}], out)
        assert got["written"] == 1, got
        back = memory_eval._load_vectors(out)
        assert len(back[Embedder.key_of("打游戏")]) == 3
        assert memory_eval._load_vectors(tmp / "不存在.json") == {}


def test_eval_excludes_gated_and_unvectorized_cases():
    """被闸门拦下的、以及没有缓存向量的 case 都**不计入指标**。"""
    with tempfile.TemporaryDirectory() as td:
        db = _build(Path(td))
        res = evaluate(db, [
            {"key": "K", "query": "打游戏", "expect_any": ["打游戏"]},
            {"key": "K", "query": "嗯", "expect_any": []},          # 闸门拦下
            {"key": "K", "query": "库里没缓存的一句", "expect_any": ["x"]},
            {"key": "别的键", "query": "打游戏", "expect_any": ["打游戏"]},
        ])
    states = [r["status"] for r in res["rows"]]
    assert states == ["ok", "gated", "no_query_vector", "empty_pool"], states
    assert res["summary"]["graded"] == 1, res["rows"]


def test_eval_reports_dimension_mismatch_as_unreachable():
    """维度不符的记忆进不了候选，必须如实标成不可达（而不是算成"没召回到"）。"""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db = tmp / "memory.db"
        store = MemoryStore(tmp, db_path=db)
        store.add("K", "旧模型的记忆", [1.0, 0.0])          # 2 维（旧）
        store.add("K", "新模型的记忆", [1.0, 0.0, 0.0])     # 3 维（新）
        e = Embedder(context=None, db_path=db)
        e.cache_put(Embedder.key_of("查新记忆"), [1.0, 0.0, 0.0])
        res = evaluate(db, [{"key": "K", "query": "查新记忆",
                             "expect_any": ["新模型的记忆"]}])
    assert res["db"]["current_dim"] == 3
    assert res["db"]["unreachable"] == 1, res["db"]
    row = res["rows"][0]
    assert row["reach"] == 1, row
    assert row["status"] == "ok" and row["hit"] is True


def test_eval_does_not_touch_the_source_db():
    """只读：跑完真实库一个字节都不该变（harness 动的是副本）。"""
    with tempfile.TemporaryDirectory() as td:
        db = _build(Path(td))
        before = (db.read_bytes(), db.stat().st_mtime_ns)
        evaluate(db, [{"key": "K", "query": "打游戏", "expect_any": ["打游戏"]}])
        after = (db.read_bytes(), db.stat().st_mtime_ns)
    assert before == after, "评测不该改动被评测的库"


def test_load_cases_skips_malformed_entries():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "c.json"
        p.write_text(
            '{"cases": [{"key": "", "query": "x"}, {"key": "K", "query": ""},'
            ' {"key": "K", "query": " ok ", "expect_any": ["a"]}]}',
            encoding="utf-8")
        got = load_cases(p)
    assert len(got) == 1
    assert got[0] == {"key": "K", "query": "ok", "expect_any": ["a"]}


def test_repo_eval_cases_are_wellformed():
    """随仓库走的评测集必须能解析、且标注自洽。

    "空 expect_any 的 case 一定得是被闸门拦下的"这条不变量很关键：
    否则空标注会被当成 unlabeled 静默跳过，评测集就悄悄缩水了。
    """
    cases = load_cases(DEFAULT_CASES)
    assert len(cases) >= 10, f"评测集太小：{len(cases)} 条"
    for c in cases:
        assert c["key"] and c["query"]
        if not c["expect_any"]:
            assert memory_eval.recall_gate(c["query"]), \
                f"{c['query']!r} 没有标注，也不是无实词消息 —— 标注漏了"
