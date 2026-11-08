# -*- coding: utf-8 -*-
"""W3 记忆层测试 + W4 输出层测试。"""

from __future__ import annotations

import asyncio
import math
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.memory import Embedder, MemoryService, MemoryStore, cosine, decayed_score  # noqa: E402
from core.memory.store import MemoryAtom  # noqa: E402
from core.output import (RecallGuard, clean_segments, parse_markers,  # noqa: E402
                         split_raw, split_segments, strip_legacy_segment_marks,
                         strip_markers)

from run_tests import Skip  # noqa: E402


# ---------------------------------------------------------------- 向量基础

def test_cosine_basic():
    assert abs(cosine([1, 0, 0], [1, 0, 0]) - 1.0) < 1e-9
    assert abs(cosine([1, 0], [0, 1])) < 1e-9
    assert abs(cosine([1, 1], [2, 2]) - 1.0) < 1e-9
    assert cosine([], [1]) == 0.0
    assert cosine([1, 2], [1, 2, 3]) == 0.0     # 维度不一致要安全返回 0
    assert cosine([0, 0], [1, 1]) == 0.0        # 零向量不该除零


def test_decay_prefers_recent_and_important():
    now = time.time()
    old = MemoryAtom(text="旧", ts=now - 86400 * 180, importance=0.5)
    new = MemoryAtom(text="新", ts=now, importance=0.5)
    s_old = decayed_score(old, 0.9, now=now, half_life_days=30)
    s_new = decayed_score(new, 0.9, now=now, half_life_days=30)
    assert s_new > s_old, "新记忆应排在旧记忆前面"

    important = MemoryAtom(text="重要", ts=now, importance=1.0)
    trivial = MemoryAtom(text="琐碎", ts=now, importance=0.0)
    assert decayed_score(important, 0.9, now=now, half_life_days=30) > \
        decayed_score(trivial, 0.9, now=now, half_life_days=30)


def test_reinforcement_is_capped():
    now = time.time()
    a = MemoryAtom(text="x", ts=now, importance=0.5)
    base = decayed_score(a, 0.9, now=now, half_life_days=30)
    a.hits = 1000
    capped = decayed_score(a, 0.9, now=now, half_life_days=30)
    assert capped <= base * 1.5001, "命中强化必须有上限，否则老记忆永久霸榜"


# ---------------------------------------------------------------- 存储

def test_store_roundtrip_and_recall():
    with tempfile.TemporaryDirectory() as td:
        store = MemoryStore(Path(td))
        v1, v2, v3 = [1.0, 0.0], [0.0, 1.0], [0.9, 0.1]
        store.add("umo1", "我喜欢打游戏", v1)
        store.add("umo1", "我讨厌早起", v2)
        store.save("umo1")

        store2 = MemoryStore(Path(td))
        atoms = store2.load("umo1")
        assert len(atoms) == 2
        hits = store2.recall("umo1", v3, top_k=1, min_score=0.1)
        assert len(hits) == 1
        assert "打游戏" in hits[0][0].text


def _persona_store():
    td = tempfile.mkdtemp()
    store = MemoryStore(Path(td))
    store.add("u", "本命写的", [1.0, 0.0], persona="A")
    store.add("u", "别人格写的", [0.8, 0.6], persona="B")
    return store


def test_store_recall_prefer_persona_demotes_others():
    """`other_factor`：别人格排序分打折，本命不受影响（在 top_k 截断前）。"""
    q = [1.0, 0.0]
    # 冻结时钟：两次 recall 是独立调用，各自的 age = now - atom.ts 取决于
    # 调用内部 add→recall 的耗时，差值 >5ms 时就会越过 1e-9 阈值（flaky，
    # 抖动实测：Δage=5ms → dA≈1.0e-9）。冻结后 ts/now 位级一致，
    # 断言只考察 other_factor 本身。
    with patch("time.time", return_value=1_700_000_000.0):
        base = {a.persona: s for a, s in
                _persona_store().recall("u", q, top_k=2, min_score=0.0)}
        demoted = {a.persona: s for a, s in
                   _persona_store().recall("u", q, top_k=2, min_score=0.0,
                                           prefer_persona="A", other_factor=0.5)}
    assert abs(demoted["A"] - base["A"]) < 1e-9, "本命不应被降权"
    assert abs(demoted["B"] - base["B"] * 0.5) < 1e-9, "别人格应降权到 0.5"


def test_store_recall_strict_persona_isolates():
    """`strict_persona`：只留当前人格，空 persona 也不算本命。"""
    with tempfile.TemporaryDirectory() as td:
        store = MemoryStore(Path(td))
        q = [1.0, 0.0]
        store.add("u", "本命", [1.0, 0.0], persona="A")
        store.add("u", "别人格", [0.8, 0.6], persona="B")
        store.add("u", "无来源", [0.5, 0.866], persona="")
        hits = store.recall("u", q, top_k=10, min_score=0.0,
                            prefer_persona="A", strict_persona=True)
        assert [a.persona for a, _ in hits] == ["A"]


def test_store_dedups_near_duplicates():
    with tempfile.TemporaryDirectory() as td:
        store = MemoryStore(Path(td))
        store.add("u", "同一件事", [1.0, 0.0])
        store.add("u", "同一件事（改写）", [0.999, 0.001],
                  dedup_threshold=0.95)
        assert len(store.load("u")) == 1, "近重复应合并为一条并计一次命中"


def test_store_forget_and_clear():
    with tempfile.TemporaryDirectory() as td:
        store = MemoryStore(Path(td))
        a = store.add("u", "记住我", [1.0])
        assert store.forget("u", a.id) is True
        assert len(store.load("u")) == 0
        store.add("u", "x", [1.0])
        assert store.clear("u") == 1


def test_store_prune():
    with tempfile.TemporaryDirectory() as td:
        store = MemoryStore(Path(td))
        for i in range(30):
            # 关掉近重复合并：这里要造 30 条独立记忆，
            # 相似向量会被 dedup 折成一条，测不出裁剪
            store.add("u", f"记忆{i}", [1.0, float(i)], dedup_threshold=2.0)
        assert len(store.load("u")) == 30
        removed = store.prune("u", keep=10)
        assert removed == 20
        assert len(store.load("u")) == 10


def test_store_handles_corrupt_file():
    """库文件损坏时必须从空开始，而不是抛异常阻断运行。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "memory.db").write_bytes(b"this is not a sqlite database")
        store = MemoryStore(root)
        assert store.load("u") == [], "坏库应从空开始而不是抛异常"
        assert list(root.glob("memory.db.bad-*")), \
            "坏库应改名留证（沿用旧 JSON 实现的策略），不能直接删掉"


def test_store_all_sessions_share_one_db():
    """所有会话同库。旧实现是一个会话一个 JSON 文件。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        store = MemoryStore(root)
        store.add("A:GroupMessage:1", "甲", [1.0])
        store.add("B:FriendMessage:2", "乙", [1.0])
        names = {p.name for p in root.iterdir() if p.is_file()}
        assert names <= {"memory.db", "memory.db-wal", "memory.db-shm"}, names
        assert "memory.db" in names
        assert store.stats()["sessions"] == 2
        assert store.stats()["atoms"] == 2


def test_store_vector_is_float32_blob():
    """向量必须落成 float32 BLOB。

    旧实现把 4096 维写成十进制文本：单条记忆 107 KB、462 条 58 MB，
    而正文只有几百字节。这条断言钉住"别再退回文本存储"。
    """
    import sqlite3
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        MemoryStore(root).add("u", "x", [0.5] * 64)
        con = sqlite3.connect(str(root / "memory.db"))
        blob = con.execute("SELECT vec FROM atoms").fetchone()[0]
        con.close()
        assert isinstance(blob, bytes), "vec 列存的不是 BLOB"
        assert len(blob) == 64 * 4, f"每维应 4 字节（float32），实际 {len(blob)}"


def test_store_vector_roundtrip_keeps_cosine():
    with tempfile.TemporaryDirectory() as td:
        store = MemoryStore(Path(td))
        vec = [i / 1000.0 for i in range(4096)]
        store.add("u", "x", vec)
        got = MemoryStore(Path(td)).load("u")[0].vec
        assert len(got) == 4096
        assert abs(cosine(got, vec) - 1.0) < 1e-6, "float32 打包后余弦应仍≈1"


def test_store_save_only_rewrites_touched_rows():
    """召回只该回写命中的那几条。

    旧实现每次召回都整文件重写（462 条 = 58 MB / 1.9 s），
    而召回就在每条消息的主链路上。
    """
    with tempfile.TemporaryDirectory() as td:
        store = MemoryStore(Path(td))
        for i in range(5):
            store.add("u", f"记忆{i}", [1.0, float(i)], dedup_threshold=2.0)
        store.save("u")
        assert not store._touched.get("u"), "新增是即时的，不该积压待回写项"

        hits = store.recall("u", [1.0, 0.0], top_k=2, min_score=0.0)
        assert len(hits) == 2
        assert len(store._touched.get("u") or ()) == 2, \
            "只有命中的 2 条该被标脏，而不是整个会话"
        store.save("u")
        assert not store._touched.get("u")


def test_store_preserves_given_atom_id():
    """迁移要 1:1 还原，所以 add() 得能接一个现成的 id。"""
    with tempfile.TemporaryDirectory() as td:
        store = MemoryStore(Path(td))
        a = store.add("u", "x", [1.0], atom_id="fixed-id-1")
        assert a.id == "fixed-id-1"
        assert MemoryStore(Path(td)).load("u")[0].id == "fixed-id-1"


# ---------------------------------------------------------------- 服务降级

def test_memory_service_degrades_without_provider():
    """embedding 不可用时，记忆整体静默关闭，不影响聊天。

    注意：`note_turn` 仍会累积（那是纯内存/落盘操作，不花钱），
    但**固化与召回都不可用** —— 固化需要向量，没有向量就不该消耗 LLM 调用。
    """
    emb = Embedder(context=None)
    assert not emb.available
    svc = MemoryService(emb, MemoryStore(Path(tempfile.mkdtemp())))
    assert not svc.available
    import asyncio
    assert svc.note_turn("u", "文本", "回复") == 1
    assert asyncio.run(svc.consolidate("u")) is None
    assert svc.pending_size("u") == 1, "不可用时必须保留缓冲，等恢复后再固化"
    assert asyncio.run(svc.recall_texts("u", "查询")) == []


def test_memory_service_with_fake_embedder():
    """用一个假 embedding 跑通写入→召回全链路（无需真实 provider）。"""
    class FakeEmbedder(Embedder):
        def __init__(self):
            super().__init__(context=None)
            import hashlib as _h

            def _v(text: str):
                d = _h.sha256(text.encode()).digest()
                return [b / 255.0 for b in d[:8]]

            self._fake = _v

        @property
        def available(self):
            return True

        async def embed(self, text: str):
            return self._fake(text)

    import asyncio
    from core.memory.service import MemoryService as Svc
    from core.graph import GraphStore
    root = Path(tempfile.mkdtemp())
    svc = Svc(FakeEmbedder(), MemoryStore(root),
              config={"memory_consolidate_turns": 2,
                      "memory_top_k": 2, "memory_min_score": 0.0})

    async def fake_consolidate(text, participants=None, umo=""):
        return {"summary": "用户在说自己喜欢打游戏、讨厌早起",
                "topics": ["打游戏"], "key_facts": ["用户喜欢打游戏"],
                "importance": 0.6, "degraded": False}

    svc.summarizer.consolidate = fake_consolidate
    svc.note_turn("u", "我喜欢打游戏", "好呀")
    svc.note_turn("u", "我讨厌早起", "嗯")
    r = asyncio.run(svc.consolidate("u"))
    assert r is not None, "固化应成功"
    out = asyncio.run(svc.recall_texts("u", "打游戏"))
    assert out, "应当召回至少一条"
    assert any("打游戏" in t for t in out), out


def test_recall_texts_persona_policy():
    """policy 0/1/2 在 service 层的语义：0 全量、1 降权保留、2 严格隔离。"""
    import asyncio
    from core.memory.service import MemoryService as Svc

    class FixedEmb(Embedder):
        def __init__(self):
            super().__init__(context=None)

        @property
        def available(self):
            return True

        async def embed(self, text):
            return [1.0, 0.0]

    root = Path(tempfile.mkdtemp())
    svc = Svc(FixedEmb(), MemoryStore(root),
              config={"memory_top_k": 5, "memory_min_score": 0.0})
    svc.store.add("u", "本命的记忆", [1.0, 0.0], persona="A")
    svc.store.add("u", "别人格的记忆", [0.8, 0.6], persona="B")

    off = asyncio.run(svc.recall_texts("u", "q", persona="A", policy=0))
    assert len(off) == 2, off
    assert any("别人格的记忆" in t and "仅供参考" in t for t in off), \
        "关闭策略下别人格记忆仍召回，但带来源标注"

    prefer = asyncio.run(svc.recall_texts("u", "q", persona="A", policy=1))
    assert len(prefer) == 2, "本命优先仍保留别人格（只是降权）"

    strict = asyncio.run(svc.recall_texts("u", "q", persona="A", policy=2))
    assert len(strict) == 1, strict
    assert "本命的记忆" in strict[0]


# ---------------------------------------------------------------- 召回闸门

def test_recall_gate_blocks_filler_messages():
    """无实词的短消息不该触发召回（省钱也去噪）。"""
    from core.memory.service import recall_gate
    for q in ("嗯", "草", "哈哈", "哈哈哈", "嗯嗯嗯嗯", "？？？", "……",
              "😂", "在吗", "好的", "6", "666"):
        assert recall_gate(q), f"{q!r} 应被闸门拦下"


def test_recall_gate_lets_real_messages_through():
    """**宁可多召回，也不误拦** —— 只要有一个正常词就放行。"""
    from core.memory.service import recall_gate
    for q in ("我想吃火锅", "卡拉彼丘怎么玩", "嗯……那我们去看电影吧",
              "草，这游戏也太难了吧", "哈哈我懂了", "你昨天说的那个"):
        assert not recall_gate(q), f"{q!r} 不该被拦"


def test_recall_texts_skips_gated_query():
    """闸门在 service 层生效：拦下时**不走 embedding**，并记账。"""
    from core.memory.service import MemoryService as Svc

    root = Path(tempfile.mkdtemp())
    db = root / "memory.db"      # 必须给临时库：否则会命中线上真实向量缓存

    class CountingEmb(Embedder):
        def __init__(self):
            super().__init__(context=None, db_path=db)
            self.calls = 0

        @property
        def available(self):
            return True

        async def embed(self, text):
            self.calls += 1
            return [1.0, 0.0]

    emb = CountingEmb()
    svc = Svc(emb, MemoryStore(root, db_path=db),
              config={"memory_top_k": 5, "memory_min_score": 0.0})
    svc.store.add("u", "用户喜欢打游戏", [1.0, 0.0])
    assert asyncio.run(svc.recall_texts("u", "嗯")) == []
    assert emb.calls == 0, "被闸门拦下就不该再调 embedding"
    assert svc.stats_counters["recall_gated"] == 1
    # 关掉闸门后同样的问题照常召回（会真的去调 embedding）
    svc.config["memory_recall_gate"] = False
    assert asyncio.run(svc.recall_texts("u", "嗯"))
    assert emb.calls >= 1, "关掉闸门后必须真的调 embedding"


# ---------------------------------------------------------------- 维度迁移

def test_store_counts_and_picks_stale_vectors():
    with tempfile.TemporaryDirectory() as td:
        store = MemoryStore(Path(td))
        store.add("u", "两条维度的旧记忆", [1.0, 0.0])
        store.add("u", "另一条旧记忆", [0.0, 1.0])
        # 没向量的也算待迁移（存进去就是召回不到的）
        store.add("u", "没有向量的记忆", [])
        assert store.count_stale(2) == 1, "只有没向量的那条算 stale"
        assert store.count_stale(3) == 3
        got = store.stale_atoms(3)
        assert {a.text for _umo, a in got} == {
            "两条维度的旧记忆", "另一条旧记忆", "没有向量的记忆"}
        assert store.stale_atoms(3, limit=2) and \
            len(store.stale_atoms(3, limit=2)) == 2
        skipped = {a.id for _umo, a in got[:1]}
        assert len(store.stale_atoms(3, skip=skipped)) == 2
        assert store.count_stale(0) == 0, "维度未知时不该乱动"


def test_reembed_worker_migrates_old_vectors():
    """换 embedding 模型后，后台把旧维度向量重编码，记忆恢复可召回。"""
    from core.memory.service import MemoryService as Svc

    class DimEmb(Embedder):
        def __init__(self, path, dim=3):
            super().__init__(context=None, db_path=path)
            self.dim = dim
            self.calls = 0

        @property
        def available(self):
            return True

        async def embed(self, text):
            self.calls += 1
            return [1.0] + [0.0] * (self.dim - 1)

    root = Path(tempfile.mkdtemp())
    db = root / "memory.db"
    store = MemoryStore(root, db_path=db)
    store.add("u", "旧记忆甲", [1.0, 0.0])
    store.add("u", "旧记忆乙", [0.0, 1.0])
    svc = Svc(DimEmb(db), store,
              config={"memory_reembed_batch": 1, "memory_top_k": 5,
                      "memory_min_score": 0.0})
    # 批大小 1：必须靠循环把 2 条都迁完（验证不会只处理一批）
    asyncio.run(svc._reembed_worker(3))
    assert store.count_stale(3) == 0, "迁移完不该还剩维度不符的"
    assert all(len(a.vec) == 3 for a in store.load("u"))
    assert svc.stats_counters["reembedded"] == 2
    assert svc.stats_counters["reembed_failed"] == 0


def test_reembed_worker_keeps_unencodable_atoms_and_warns():
    """编不出来的记忆**不能被删**，只记账 + 显式告警。"""
    from core.memory.service import MemoryService as Svc

    class DeadEmb(Embedder):
        def __init__(self, path):
            super().__init__(context=None, db_path=path)

        @property
        def available(self):
            return True

        async def embed(self, text):
            return []

    root = Path(tempfile.mkdtemp())
    db = root / "memory.db"
    store = MemoryStore(root, db_path=db)
    store.add("u", "编不出来的旧记忆", [1.0, 0.0])
    store.add("u", "", [])                      # 正文为空：连 embed 都不该调
    svc = Svc(DeadEmb(db), store, config={"memory_reembed_batch": 8})
    with patch("logging.Logger.warning") as warn:
        asyncio.run(svc._reembed_worker(3))
    assert len(store.load("u")) == 2, "迁移失败绝不能删记忆"
    assert svc.stats_counters["reembed_failed"] == 2, "两条都编不出来"
    assert svc.stats_counters["reembedded"] == 0
    assert warn.called, "重编码失败必须显式告警，不能静默失效"


def test_reembed_scheduled_once_per_dim_and_only_when_stale():
    """同一维度只扫一次；库里没有待迁移条目时**连任务都不建**。"""
    from core.memory.service import MemoryService as Svc

    class DimEmb(Embedder):
        def __init__(self, path):
            super().__init__(context=None, db_path=path)

        @property
        def available(self):
            return True

        async def embed(self, text):
            return [1.0, 0.0, 0.0]

    root = Path(tempfile.mkdtemp())
    db = root / "memory.db"
    store = MemoryStore(root, db_path=db)
    svc = Svc(DimEmb(db), store, config={})

    async def _no_stale():
        svc._maybe_reembed(3)
        assert svc._reembed_task is None, "没有待迁移条目不该起后台任务"

    asyncio.run(_no_stale())

    store.add("u", "旧记忆", [1.0, 0.0])
    svc2 = Svc(DimEmb(db), store, config={})

    async def _once():
        svc2._maybe_reembed(3)
        t1 = svc2._reembed_task
        assert t1 is not None, "有旧维度向量就该起后台迁移"
        await t1
        svc2._maybe_reembed(3)
        assert svc2._reembed_task is t1, "同一维度不该反复重扫"

    asyncio.run(_once())


def test_purge_stale_cache_drops_wrong_dim_vectors():
    """换模型后缓存里的旧维度向量必须清掉，否则重编码会被缓存挡住。"""
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "memory.db"

        class _Prov:
            provider_config = {"id": "emb", "type": "openai_embedding"}

            def __init__(self):
                self.calls = 0

            async def get_embedding(self, text):
                self.calls += 1
                return [1.0, 0.0, 0.0]

        class _Ctx:
            def __init__(self, p):
                self._p = [p]

            def get_all_embedding_providers(self):
                return list(self._p)

        p = _Prov()
        e = Embedder(_Ctx(p), db_path=db)
        e.cache_put(e.key_of("旧文本"), [1.0, 0.0])
        e.cache_put(e.key_of("新文本"), [1.0, 0.0, 0.0])
        assert asyncio.run(e.embed("旧文本")) == [1.0, 0.0], "缓存应先命中旧向量"
        assert p.calls == 0, "命中缓存不该调 provider"
        assert e.purge_stale_cache(3) == 1, "只该清掉维度不符的那一条"
        assert e.key_of("旧文本") not in e._cache
        assert asyncio.run(e.embed("旧文本")) == [1.0, 0.0, 0.0], \
            "清完缓存后必须重新调 provider 拿新维度向量"
        assert p.calls == 1


# ---------------------------------------------------------------- 分类衰减

def test_decayed_score_kind_changes_half_life():
    """喜好/事实活得久、闲聊烂得快；关掉开关后一视同仁。"""
    now = time.time()
    ts = now - 86400 * 180

    def score(kind, **kw):
        a = MemoryAtom(text="x", ts=ts, importance=0.5, kind=kind)
        return decayed_score(a, 0.9, now=now, half_life_days=30, **kw)

    assert score("preference") > score("fact") > score("document") > score("dialog")
    assert score("dialog", kind_decay=False) == \
        score("preference", kind_decay=False), "关掉后所有类型该一致"


def test_decayed_score_importance_never_shortens_half_life():
    """重要度只加长半衰期（单向），否则等于把 importance 算两遍。"""
    now = time.time()
    ts = now - 86400 * 90
    low = MemoryAtom(text="x", ts=ts, importance=0.0, kind="fact")
    high = MemoryAtom(text="x", ts=ts, importance=1.0, kind="fact")
    assert decayed_score(high, 0.9, now=now, half_life_days=30) > \
        decayed_score(low, 0.9, now=now, half_life_days=30)


def test_parse_consolidation_validates_kind():
    from core.memory.summarizer import (fallback_consolidation,
                                        parse_consolidation)
    assert parse_consolidation(
        '{"summary":"x","kind":"preference"}')["kind"] == "preference"
    assert parse_consolidation(
        '{"summary":"x","kind":"  FACT  "}')["kind"] == "fact"
    assert parse_consolidation(
        '{"summary":"x","kind":"聊天"}')["kind"] == "document", "认不出来要兜底"
    assert parse_consolidation('{"summary":"x"}')["kind"] == "document"
    assert fallback_consolidation("随便一段话")["kind"] == "document"


def _kind_svc(kind: str, root: Path):
    """造一个 service，固化产出的记忆类型固定为 kind。"""
    from core.memory.service import MemoryService as Svc
    db = root / "memory.db"

    class Emb(Embedder):
        def __init__(self):
            super().__init__(context=None, db_path=db)

        @property
        def available(self):
            return True

        async def embed(self, text):
            return [1.0, 0.0]

    svc = Svc(Emb(), MemoryStore(root, db_path=db),
              config={"memory_consolidate_turns": 2, "memory_top_k": 5,
                      "memory_min_score": 0.0})

    async def fake(text, participants=None, umo=""):
        return {"summary": "用户说他讨厌早起", "topics": ["作息"],
                "key_facts": ["用户讨厌早起"], "kind": kind,
                "importance": 0.7, "degraded": False}

    svc.summarizer.consolidate = fake
    return svc


def test_consolidate_stores_kind():
    """固化器给的 kind 必须落到记忆上（否则类型衰减永远是死代码）。"""
    svc = _kind_svc("preference", Path(tempfile.mkdtemp()))
    svc.note_turn("u", "我讨厌早起", "嗯")
    svc.note_turn("u", "真的", "嗯")
    assert asyncio.run(svc.consolidate("u")) is not None
    atom = svc.store.load("u")[0]
    assert atom.kind == "preference"
    assert atom.metadata["kind"] == "preference"


# ---------------------------------------------------------------- 取代检测

def _supersede_svc(root: Path):
    """向量按正文区分：首条 [1,0]，后续 [0.9,0.435]（余弦 0.90）。

    故意落在 [0.8, 0.95)：0.95 以上会被 `add()` 当重复直接合并，
    0.8 以下又不该判成取代 —— 取代恰恰发生在这段中间地带。
    """
    from core.memory.service import MemoryService as Svc
    db = root / "memory.db"

    class Emb(Embedder):
        def __init__(self):
            super().__init__(context=None, db_path=db)

        @property
        def available(self):
            return True

        async def embed(self, text):
            return [1.0, 0.0] if "北京" in text else [0.9, 0.435]

    return Svc(Emb(), MemoryStore(root, db_path=db),
               config={"memory_consolidate_turns": 2, "memory_top_k": 5,
                       "memory_min_score": 0.0})


def _reply(summary, facts):
    async def fake(text, participants=None, umo=""):
        return {"summary": summary, "topics": ["居住地"], "key_facts": facts,
                "kind": "fact", "importance": 0.7, "degraded": False}
    return fake


def test_supersede_marks_old_memory_without_deleting():
    svc = _supersede_svc(Path(tempfile.mkdtemp()))
    svc.summarizer.consolidate = _reply("用户住在北京", ["用户住在北京"])
    svc.note_turn("u", "我住北京", "嗯")
    svc.note_turn("u", "那边不错", "嗯")
    assert asyncio.run(svc.consolidate("u")) is not None

    svc.summarizer.consolidate = _reply("用户搬到上海了", ["用户现在住上海"])
    svc.note_turn("u", "我搬到上海了", "好呀")
    svc.note_turn("u", "上个月的事", "嗯")
    assert asyncio.run(svc.consolidate("u")) is not None

    atoms = svc.store.load("u")
    assert len(atoms) == 2, "取代只是降权，绝不能删记忆"
    marked = [a for a in atoms if (a.metadata or {}).get("superseded_by")]
    assert len(marked) == 1, atoms
    assert "北京" in marked[0].text, "被取代的该是旧的那条"
    assert svc.stats_counters["superseded"] == 1
    # 标了就要落盘，不能只活在内存里（换个 store 重新读一遍验证）
    fresh = MemoryStore(Path(svc.store.root))
    assert sum(1 for a in fresh.load("u")
               if (a.metadata or {}).get("superseded_by")) == 1


def test_supersede_requires_topic_overlap():
    """只相似、不共话题（换了说法讲另一件事）不该判成取代。"""
    svc = _supersede_svc(Path(tempfile.mkdtemp()))
    svc.summarizer.consolidate = _reply("用户住在北京", ["用户住在北京"])
    svc.note_turn("u", "我住北京", "嗯")
    svc.note_turn("u", "那边不错", "嗯")
    asyncio.run(svc.consolidate("u"))

    async def other(text, participants=None, umo=""):
        return {"summary": "用户搬到上海了", "topics": ["搬家"],
                "key_facts": ["用户现在住上海"], "kind": "fact",
                "importance": 0.7, "degraded": False}

    svc.summarizer.consolidate = other
    svc.note_turn("u", "我搬到上海了", "好呀")
    svc.note_turn("u", "上个月的事", "嗯")
    asyncio.run(svc.consolidate("u"))
    assert svc.stats_counters["superseded"] == 0
    assert not any((a.metadata or {}).get("superseded_by")
                   for a in svc.store.load("u"))


def test_superseded_downweight_only_when_query_is_not_about_past():
    from core.memory.retrieval import asks_about_past
    assert not asks_about_past("他现在住哪")
    assert not asks_about_past("住的地方是哪里")
    assert asks_about_past("他以前住哪")
    assert asks_about_past("那时候他住哪")

    with tempfile.TemporaryDirectory() as td:
        store = MemoryStore(Path(td))
        old = store.add("u", "用户住在北京", [1.0, 0.0], importance=0.8)
        new = store.add("u", "用户现在住上海", [0.9, 0.435], importance=0.8)
        old.metadata["superseded_by"] = new.id
        store.touch("u", old)
        store.save("u")

        plain = store.recall("u", [1.0, 0.0], top_k=5, min_score=0.0,
                             superseded_factor=1.0)
        pushed = store.recall("u", [1.0, 0.0], top_k=5, min_score=0.0,
                              superseded_factor=0.35)
        assert len(plain) == 2 and len(pushed) == 2, "降权不是过滤，旧记忆还在"
        assert plain[0][0].id == old.id, "不降权时旧记忆照样第一（问过去）"
        assert pushed[0][0].id == new.id, "降权后当前状态该排前面"


# ---------------------------------------------------------------- 去冗余

def _fh(key, score, content):
    from core.memory.retrieval import FusedHit
    return FusedHit(key=key, final_score=score, content=content,
                    doc_score=score, graph_score=0.0, intent="")


def test_text_similarity_is_bounded_and_sane():
    from core.memory.retrieval import text_similarity
    assert text_similarity("蛋包饭在桌上", "蛋包饭在桌上") == 1.0
    assert text_similarity("蛋包饭在桌上", "完全无关的一句话") < 0.2
    assert text_similarity("", "x") == 0.0


def test_mmr_diversify_replaces_near_duplicate_with_distinct_hit():
    from core.memory.retrieval import mmr_diversify
    hits = [_fh("a", 0.90, "德丽莎说自己刚煮了蛋包饭，蛋包饭在桌上"),
            _fh("b", 0.89, "德丽莎说自己刚煮了蛋包饭，蛋包饭在桌上呢"),
            _fh("c", 0.60, "江一楠问 bot 有没有情人，bot 回答一个都没有")]
    assert [h.key for h in mmr_diversify(hits, 2)] == ["a", "c"]
    # k 大于候选数时原样返回，不报错
    assert len(mmr_diversify(hits, 9)) == 3
    assert mmr_diversify(hits, 0) == []
    assert len(mmr_diversify(hits[:1], 3)) == 1


def test_fuse_diversify_flag_controls_dedup():
    from core.memory.retrieval import DualRouteRetriever, RouteHit
    r = DualRouteRetriever({})
    dup = "德丽莎说自己刚煮了蛋包饭，蛋包饭在桌上"
    routes = [RouteHit(key="1", score=1.0, content=dup),
              RouteHit(key="2", score=0.99, content=dup + "呢"),
              RouteHit(key="3", score=0.5, content="江一楠问 bot 有没有情人")]
    plain = r.fuse("蛋包饭", routes, [], k=2, diversify=False)
    div = r.fuse("蛋包饭", routes, [], k=2, diversify=True)
    assert [h.key for h in plain] == ["1", "2"]
    assert [h.key for h in div] == ["1", "3"]


# ---------------------------------------------------------------- 编辑记忆

def _edit_svc(root: Path):
    """能固化 + 能编辑的 service；向量按正文区分，便于断言"向量被重算了"。"""
    from core.memory.service import MemoryService as Svc
    db = root / "memory.db"

    class Emb(Embedder):
        def __init__(self):
            super().__init__(context=None, db_path=db)
            self.calls = 0

        @property
        def available(self):
            return True

        async def embed(self, text):
            self.calls += 1
            return [1.0, 0.0, 0.0] if "上海" in text else [0.0, 1.0, 0.0]

    return Svc(Emb(), MemoryStore(root, db_path=db),
               config={"memory_consolidate_turns": 2, "memory_top_k": 5,
                       "memory_min_score": 0.0})


def _seed_one(svc, summary="用户住在北京", fact="用户住在北京"):
    async def fake(text, participants=None, umo=""):
        return {"summary": summary, "topics": ["居住地"],
                "key_facts": [fact], "kind": "fact",
                "importance": 0.7, "degraded": False}

    svc.summarizer.consolidate = fake
    svc.note_turn("u", "第一句", "嗯")
    svc.note_turn("u", "第二句", "嗯")
    assert asyncio.run(svc.consolidate("u")) is not None


def test_edit_atom_rebuilds_vector_summary_and_graph():
    """改正文必须连着重算向量、改摘要、重建图谱条目 —— 少一个就会"改了还查到旧的"。"""
    svc = _edit_svc(Path(tempfile.mkdtemp()))
    _seed_one(svc)
    atom = svc.store.load("u")[0]
    mid = int((atom.metadata or {}).get("source_memory_id") or 0)
    old_vec = list(atom.vec)
    before = [e.content for k, e in svc.graph.entries.items()
              if e.source_memory_id == mid]

    r = asyncio.run(svc.edit_atom("u", atom.id, "用户搬到上海了"))
    assert "error" not in r, r
    assert r["id"] == atom.id
    assert atom.text == "用户搬到上海了"
    assert list(atom.vec) != old_vec, "必须重算向量（否则召回与正文对不上）"
    assert atom.metadata["summary"] == "用户搬到上海了"
    assert atom.metadata["canonical_summary"] == "用户搬到上海了"
    assert atom.metadata["key_facts"] == ["用户搬到上海了"]
    assert atom.metadata["manual_edit"] is True
    assert atom.metadata["edited_at"] > 0
    assert svc.stats_counters["edited"] == 1

    after = [e.content for k, e in svc.graph.entries.items()
             if e.source_memory_id == mid]
    assert after, "编辑后应重建出图谱条目"
    if before:
        assert not (set(before) & set(after)), \
            "旧条目的正文里带着旧 Summary，必须删干净再重建"
    assert all("北京" not in t for t in after), after
    assert any("上海" in t for t in after), after
    assert r["graph_dropped"] == len(before)


def test_edit_atom_keeps_old_content_when_embedding_is_unavailable():
    """算不出向量就**拒绝写入**：宁可改不成，也不能留下正文与向量不符的记忆。"""
    svc = _edit_svc(Path(tempfile.mkdtemp()))
    _seed_one(svc)
    atom = svc.store.load("u")[0]
    old_text, old_vec = atom.text, list(atom.vec)

    class Dead(Embedder):
        def __init__(self):
            super().__init__(context=None)

        @property
        def available(self):
            return False

        async def embed(self, text):
            return []

    svc.embedder = Dead()
    r = asyncio.run(svc.edit_atom("u", atom.id, "用户搬到上海了"))
    assert r["error"] == "embedding_unavailable", r
    assert atom.text == old_text, "拒绝时正文必须原样不动"
    assert list(atom.vec) == old_vec, "拒绝时向量必须原样不动"
    assert svc.stats_counters["edited"] == 0


def test_edit_atom_rejects_empty_unchanged_missing_and_too_long():
    svc = _edit_svc(Path(tempfile.mkdtemp()))
    _seed_one(svc)
    atom = svc.store.load("u")[0]

    def run(text, atom_id=None):
        return asyncio.run(svc.edit_atom("u", atom_id or atom.id, text))

    assert run("   ")["error"] == "empty"
    assert run(atom.text)["error"] == "unchanged"
    assert run("不会被写入的正文", atom_id="no-such-id")["error"] == "not_found"
    got = run("字" * 4001)
    assert got["error"] == "too_long" and got["limit"] == 4000
    assert svc.stats_counters["edited"] == 0, "以上都该被拒绝，不能记账"
    assert svc.store.load("u")[0].text == atom.text


def test_edit_atom_short_text_is_kept_verbatim():
    """短正文原样保留（不会被总结改写）—— 编辑的价值就在"精确"。"""
    svc = _edit_svc(Path(tempfile.mkdtemp()))
    _seed_one(svc)
    atom = svc.store.load("u")[0]
    r = asyncio.run(svc.edit_atom("u", atom.id, "他搬到上海了"))
    assert "error" not in r, r
    assert svc.store.load("u")[0].text == "他搬到上海了"


# ---------------------------------------------------------------- 输出层

def test_split_raw_respects_limit_and_preserves_content():
    """**未清理**的切分必须内容守恒（清理会去掉句尾标点）。"""
    text = "第一句。" * 50
    segs = split_raw(text, max_len=60, min_len=1)
    assert len(segs) > 1
    assert all(len(s) <= 60 for s in segs), [len(s) for s in segs]
    assert "".join(segs).replace("\n", "") == text.replace("\n", "")


def test_split_raw_keeps_short_text_intact():
    assert split_raw("你好呀", max_len=100) == ["你好呀"]
    assert split_raw("", max_len=100) == []


def test_split_raw_prefers_sentence_boundary():
    segs = split_raw("第一句话。第二句话。第三句话。", max_len=12, min_len=1)
    assert segs[0].endswith("。"), segs
    assert len(segs) >= 2


# ---------------------------------------------------------------- 标点清理

def test_clean_strips_trailing_period():
    """rules_base：句尾不加句号。"""
    assert clean_segments(["你好呀。"]) == ["你好呀"]
    assert clean_segments(["你好呀，"]) == ["你好呀"]
    assert clean_segments(["你好呀,"]) == ["你好呀"]


def test_clean_keeps_other_punctuation():
    for s in ("你好呀！", "你好呀？", "你好呀~", "你好呀…"):
        assert clean_segments([s]) == [s], s


def test_clean_keeps_triple_period():
    """。。。 是无语情绪标点，保留。"""
    assert clean_segments(["无语。。。"]) == ["无语。。。"]
    assert clean_segments(["行吧，，"]) == ["行吧，，"]


def test_clean_merges_leading_continuation():
    """段首延续性标点说明断点切在从句中间 → 并回上一段。"""
    out = clean_segments(["快睡吧", "，明天还有演出"])
    assert out == ["快睡吧，明天还有演出"], out


def test_clean_strips_leading_continuation_in_first_segment():
    assert clean_segments(["、开头就是顿号"]) == ["开头就是顿号"]


def test_clean_drops_empty_segments():
    assert clean_segments(["甲", "", "  ", "乙"]) == ["甲", "乙"]
    assert clean_segments([]) == []


def test_split_segments_cleans_by_default():
    segs = split_segments("第一句话。第二句话。", max_len=6, min_len=1)
    assert all(not s.endswith("。") for s in segs), segs
    # clean=False 时保留原标点
    raw = split_segments("第一句话。第二句话。", max_len=6, min_len=1, clean=False)
    assert any(s.endswith("。") for s in raw), raw


def test_split_narrative_parens_and_speech():
    """剧情向切分：每个括号一段、括号间说话一段、说话无论多长都不再拆。"""
    from core.output import clean_segments, split_narrative
    text = "（括号描写1）第一句话（括号描写2）非常长的第二句话（括号描写3）第三句话"
    assert split_narrative(text) == [
        "（括号描写1）", "第一句话",
        "（括号描写2）", "非常长的第二句话",
        "（括号描写3）", "第三句话"]
    # 内容守恒：拼回去等于原文
    assert "".join(split_narrative(text)) == text
    # 无括号：整段返回
    assert split_narrative("只是一句话") == ["只是一句话"]
    assert split_narrative("") == []
    # 开头的说话 / 结尾的说话 同样各成一段
    assert split_narrative("开头的话（动作）中间的话") == \
        ["开头的话", "（动作）", "中间的话"]
    # clean_segments 后仍保持"括号段独立"
    cleaned = clean_segments(split_narrative(text))
    assert cleaned == split_narrative(text) or all(
        "（" in s or "括号" in s or "话" in s for s in cleaned)


def test_parse_and_strip_markers():
    text = "早上好[[at:123456]]，你上次说的那个[[reply:789]]我看了"
    m = parse_markers(text)
    assert m["at"] == ["123456"]
    assert m["reply"] == ["789"]
    clean = strip_markers(text)
    assert "[[at:" not in clean and "[[reply:" not in clean
    assert "早上好" in clean and "我看了" in clean


def test_parse_markers_multiple_and_empty():
    m = parse_markers("[[at:1]][[at:2]][[reply:3]]")
    assert m["at"] == ["1", "2"] and m["reply"] == ["3"]
    assert parse_markers("没有标记") == {"at": [], "reply": []}


# ------------------------------------------------ 旧 §n 分段标记（回归）


def test_strip_legacy_segment_marks_turns_mark_into_break():
    """旧 `§n` 要变成**分段边界**（换行），不是简单删掉 —— 否则两句会被粘一起。

    注意 `n` 是标记本体的一部分（不是占位符），必须一起吃掉，
    否则会残留一个孤零零的 `n`。
    """
    assert strip_legacy_segment_marks("好累啊§n不过挺开心的") == "好累啊\n不过挺开心的"
    # 带编号/空格的变体
    assert "§" not in strip_legacy_segment_marks("甲§1乙§ 2丙")
    # 连续标记不留空段
    assert strip_legacy_segment_marks("甲§n§n乙") == "甲\n乙"
    # 标记两侧多余空白不残留
    assert strip_legacy_segment_marks("甲 §n 乙") == "甲\n乙"


def test_strip_legacy_segment_marks_is_noop_without_marker():
    for s in ("正常的一句话", "", None, "含~和…的文本"):
        assert strip_legacy_segment_marks(s) == (s or "")


def test_legacy_marker_break_is_usable_by_splitter():
    """标记转出的换行是"优先断点"（`\\n` 属于句末），不是强制断点。

    短文本仍会被合并成一条（这是 `split_raw` 的既有语义：过短不硬切）；
    长文本则会在这里断开 —— 与其它模型输出的处理方式一致。
    """
    cleaned = strip_legacy_segment_marks("第一句§n第二句")
    assert "\n" in cleaned
    assert len(split_segments(cleaned, max_len=6, min_len=1)) == 2
    segs = split_segments(cleaned, max_len=40, min_len=1)
    assert all("§" not in s for s in segs)


def test_recall_guard_cancels_pending():
    g = RecallGuard(ttl=60.0)
    g.mark_pending("umo1", "msg1")
    assert g.is_pending("umo1", "msg1")
    assert g.cancel("umo1", "msg1") is True
    assert not g.is_pending("umo1", "msg1")
    assert g.cancel("umo1", "msg1") is False, "重复撤回应为 no-op"


def test_recall_guard_ttl_expires():
    # ttl <= 0 的语义是"永不过期"（见 RecallGuard 文档），
    # 所以这里用一个很短的正常 ttl 来测过期
    g = RecallGuard(ttl=0.01)
    g.mark_pending("umo1", "msg1")
    assert g.is_pending("umo1", "msg1")
    time.sleep(0.03)
    assert not g.is_pending("umo1", "msg1"), "过期后不应再算待发"
    assert g.sweep() == 1, "过期项应被清理"


def test_recall_guard_ttl_zero_means_never_expire():
    g = RecallGuard(ttl=0.0)
    g.mark_pending("umo1", "msg1")
    time.sleep(0.02)
    assert g.is_pending("umo1", "msg1")
    assert g.sweep() == 0
