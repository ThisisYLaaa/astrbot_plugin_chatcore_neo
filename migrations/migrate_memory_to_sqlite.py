# -*- coding: utf-8 -*-
"""W5：记忆层从 JSON 迁到 SQLite（一次性）。

搬什么
------
| 源 | 目标库 | 表 |
|---|---|---|
| `memory/<umo>.json`        | `memory/memory.db` | `atoms` |
| `memory/_embed_cache.json` | `memory/memory.db` | `embed_cache` |
| `memory/graph.json`        | `memory/graph.db`  | `nodes` / `edges` / `entries` |

`_pending.json` **不动** —— 它是尚未固化的对话缓冲，本来就小且是临时的，
跟"记忆库"是两回事。

为什么走 store 的写入接口而不是手写 INSERT
------------------------------------------
手写 SQL 在下一次改 schema 时会静默失配（列对不上、默认值不同）。
这里改用 `MemoryStore.add` / `GraphStore.upsert_*`，
保证迁完的库就是**程序自己会写出的那个状态**。两处刻意的例外：
  * `add(dedup_threshold=2.0)` —— 迁移要 1:1 还原，不能顺手把近重复合并掉
    （默认 0.95 会把余弦 ≥0.95 的记忆折成一条，等于偷偷删数据）
  * `add(atom_id=...)`        —— 保住原 id，否则管理页上的记忆 id 全变

回滚
----
搬完把源文件改名成 `*.migrated-<时间>`（**不删**）。要回滚：
删掉 `memory.db` / `graph.db`，再把后缀改回来即可。

用法
----
    python migrations/migrate_memory_to_sqlite.py --dry-run   # 只统计，不写任何东西
    python migrations/migrate_memory_to_sqlite.py             # 真迁移
    python migrations/migrate_memory_to_sqlite.py --force     # 目标库非空也覆盖
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))

from core.db import connection  # noqa: E402
from core.graph import GraphStore  # noqa: E402
from core.graph.model import GraphEdge, GraphEntry, GraphNode  # noqa: E402
from core.memory.embedder import Embedder  # noqa: E402
from core.memory.store import MemoryStore, cosine  # noqa: E402
from core.paths import MEMORY_DIR  # noqa: E402

REPORT = Path(__file__).resolve().parent / "memory_sqlite_report.json"
SKIP_JSON = ("_pending.json",)


def read_json(path: Path):
    raw = path.read_text(encoding="utf-8").strip()
    return json.loads(raw) if raw else {}


def umo_of(atom: dict, src: Path) -> str:
    """原子自己的 `umo` 优先；没有就从文件名反推（`_` 还原成 `:`）。"""
    umo = str(atom.get("umo") or "").strip()
    return umo or src.stem.replace("_", ":")


def table_rows(db: Path, table: str) -> int:
    if not db.exists():
        return 0
    try:
        with connection(db) as con:
            return int(con.execute(
                f"SELECT count(*) AS n FROM {table}").fetchone()["n"])
    except Exception:
        return 0


# ---------------------------------------------------------------- 三块迁移

def migrate_atoms(root: Path) -> list[dict]:
    store = MemoryStore(root)
    detail: list[dict] = []
    for src in sorted(p for p in root.glob("*.json")
                      if not p.name.startswith("_")):
        data = read_json(src)
        raw_atoms = data.get("atoms") or []
        added = skipped = 0
        for d in raw_atoms:
            if not isinstance(d, dict) or not (d.get("text") or "").strip():
                skipped += 1
                continue
            umo = umo_of(d, src)
            atom = store.add(umo, d["text"], d.get("vec") or [],
                             persona=str(d.get("persona") or ""),
                             uid=str(d.get("uid") or ""),
                             kind=str(d.get("kind") or "dialog"),
                             importance=float(d.get("importance", 0.5) or 0.5),
                             tags=list(d.get("tags") or []),
                             metadata=dict(d.get("metadata") or {}),
                             dedup_threshold=2.0,
                             atom_id=str(d.get("id") or ""))
            # ts / hits / last_hit 是 add() 之后才写进去的，这里补齐
            atom.ts = float(d.get("ts") or atom.ts)
            atom.hits = int(d.get("hits") or 0)
            atom.last_hit = float(d.get("last_hit") or 0)
            store.touch(umo, atom)
            added += 1
        store.save_all()
        detail.append({"file": src.name, "in_json": len(raw_atoms),
                       "migrated": added, "skipped": skipped,
                       "db_total": store.stats()["atoms"]})
    return detail


def migrate_embed(root: Path) -> dict:
    src = root / "_embed_cache.json"
    if not src.exists():
        return {}
    data = read_json(src)
    emb = Embedder(context=None, db_path=root / "memory.db")
    put = 0
    for key, vec in (data.items() if isinstance(data, dict) else []):
        if isinstance(vec, list) and vec:
            emb.cache_put(key, vec)
            put += 1
    return {"file": src.name, "in_json": len(data), "migrated": put}


def migrate_graph(root: Path) -> dict:
    src = root / "graph.json"
    if not src.exists():
        return {}
    data = read_json(src)
    gs = GraphStore(root / "graph.db")

    for d in data.get("nodes") or []:
        n = GraphNode(node_type=d.get("node_type", ""),
                      value=d.get("value", ""),
                      canonical_value=d.get("canonical_value", ""),
                      metadata=d.get("metadata") or {},
                      id=int(d.get("id", 0) or 0),
                      created_at=d.get("created_at", ""),
                      updated_at=d.get("updated_at", ""))
        if n.canonical_value:
            gs.upsert_node(n)

    for d in data.get("edges") or []:
        e = GraphEdge(source_key=d.get("source_key", ""),
                      target_key=d.get("target_key", ""),
                      relation_type=d.get("relation_type", ""),
                      source_memory_id=int(d.get("source_memory_id", 0) or 0),
                      weight=float(d.get("weight", 1.0) or 1.0),
                      confidence=float(d.get("confidence", 0.8) or 0.8),
                      status=d.get("status", "active"),
                      metadata=d.get("metadata") or {},
                      id=int(d.get("id", 0) or 0),
                      created_at=d.get("created_at", ""),
                      updated_at=d.get("updated_at", ""))
        if e.source_key and e.target_key:
            gs.upsert_edge(e)

    new_entries = 0
    for d in data.get("entries") or []:
        en = GraphEntry(entry_type=d.get("entry_type", ""),
                        content=d.get("content", ""),
                        node_keys=list(d.get("node_keys") or []),
                        relation_type=d.get("relation_type"),
                        source_memory_id=int(d.get("source_memory_id", 0) or 0),
                        session_id=d.get("session_id", "") or "",
                        persona_id=d.get("persona_id", "") or "",
                        metadata=d.get("metadata") or {},
                        edge_id=int(d.get("edge_id", 0) or 0),
                        vec=list(d.get("vec") or []),
                        id=int(d.get("id", 0) or 0),
                        created_at=d.get("created_at", ""),
                        updated_at=d.get("updated_at", ""))
        if en.content:
            _o, created = gs.upsert_entry(en)
            new_entries += 1 if created else 0

    return {"file": src.name,
            "in_json": {"nodes": len(data.get("nodes") or []),
                        "edges": len(data.get("edges") or []),
                        "entries": len(data.get("entries") or [])},
            "new_entries": new_entries,
            "db": {"nodes": len(gs.nodes), "edges": len(gs.edges),
                   "entries": len(gs.entries)}}


# ---------------------------------------------------------------- 校验

def verify(root: Path, report: dict) -> tuple[list[str], list[str]]:
    """迁完必须对得上，对不上就明说，不要"看起来成功"。

    返回 (致命问题, 提示)。提示是"数量变少但有正当理由"的情况。
    """
    bad: list[str] = []
    notes: list[str] = []
    store = MemoryStore(root)

    # ① 记忆条数：逐文件对
    for row in report.get("atoms", []):
        src = root / row["file"]
        raw = (read_json(src).get("atoms") or []) if src.exists() else []
        umo = ""
        for d in raw:
            if d.get("umo"):
                umo = umo_of(d, src)
                break
        if not umo:
            umo = src.stem.replace("_", ":")
        got = len(store.load(umo))
        if got != row["migrated"]:
            bad.append(f"atoms {row['file']}: 迁移 {row['migrated']} 条，库里 {got} 条")

    # ② 向量保真：float32 打包有精度损失，余弦应当 ≈1.0
    for src in sorted(root.glob("*_FriendMessage_*.json")):
        raw = read_json(src).get("atoms") or []
        for d in [x for x in raw if len(x.get("vec") or []) >= 16][:3]:
            hit = [a for a in store.load(umo_of(d, src)) if a.id == d["id"]]
            if not hit:
                bad.append(f"{src.name}: 按 id 找不到迁移后的原子 {d['id']}")
                continue
            ab = hit[0]
            if len(ab.vec) != len(d["vec"]):
                bad.append(f"{src.name}: 向量维度变了 "
                           f"{len(ab.vec)} vs {len(d['vec'])}")
                continue
            sim = cosine(ab.vec, d["vec"])
            if sim < 0.99999:
                bad.append(f"{src.name}: 向量失真，余弦 {sim:.6f}")

    # ③ 图谱：重新打开一次，内存态与落盘态必须一致（这才叫"真的写下去了"）
    ent = report.get("graph") or {}
    db = ent.get("db") or {}
    js = ent.get("in_json") or {}
    if db:
        again = GraphStore(root / "graph.db")
        st = again.stats()
        for k in ("nodes", "edges", "entries"):
            if st[k] != db.get(k):
                bad.append(f"graph {k}: 内存 {db.get(k)} → 重载 {st[k]}，没落全")
            elif js and js.get(k) != db.get(k):
                notes.append(f"graph {k}: JSON {js[k]} → 库 {db.get(k)}"
                             f"（entry_key 由内容派生，同键条目会合并）")
        if db.get("entries") and not st.get("tokens"):
            bad.append("graph 倒排索引重建为空，检索会全废")
    return bad, notes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写盘")
    ap.add_argument("--force", action="store_true", help="目标库非空也继续")
    args = ap.parse_args()

    root = Path(MEMORY_DIR)
    mem_db = root / "memory.db"
    gra_db = root / "graph.db"
    print(f"数据根: {root}")

    sources = sorted(p for p in root.glob("*.json")
                     if p.name not in SKIP_JSON)
    if not sources:
        print("没有找到任何待迁的 JSON（可能已经迁过了）")
        return 0

    if not args.dry_run and not args.force:
        have = table_rows(mem_db, "atoms") + table_rows(gra_db, "entries")
        if have:
            print(f"目标库已有 {have} 行数据，拒绝覆盖。确认要重来请加 --force")
            return 2

    if args.dry_run:
        total = 0
        for p in sources:
            if p.name == "graph.json":
                d = read_json(p)
                print(f"  {p.name}: nodes={len(d.get('nodes') or [])} "
                      f"edges={len(d.get('edges') or [])} "
                      f"entries={len(d.get('entries') or [])}")
            elif p.name == "_embed_cache.json":
                print(f"  {p.name}: {len(read_json(p))} 条向量")
            else:
                n = len(read_json(p).get("atoms") or [])
                total += n
                print(f"  {p.name}: {n} 条记忆")
        print(f"合计 {total} 条记忆 + 图谱；dry-run 结束，未写任何东西")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    report: dict = {"at": stamp, "root": str(root)}

    report["atoms"] = migrate_atoms(root)
    print(f"atoms  : {sum(r['migrated'] for r in report['atoms'])} 条 → {mem_db.name}")
    report["embed_cache"] = migrate_embed(root)
    print(f"缓存   : {report['embed_cache'].get('migrated', 0)} 条 → {mem_db.name}")
    report["graph"] = migrate_graph(root)
    print(f"图谱   : {report['graph'].get('db')} → {gra_db.name}")

    problems, notes = verify(root, report)
    report["problems"] = problems
    report["notes"] = notes
    for n in notes:
        print(f"  ~ {n}")
    if problems:
        print("\n校验不通过，**源文件保持原样**，请先看下面这些：")
        for p in problems:
            print(f"  ✗ {p}")
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        return 1

    for p in sources:
        p.replace(p.with_name(f"{p.name}.migrated-{stamp}"))
    report["retired"] = [f"{p.name}.migrated-{stamp}" for p in sources]
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                      encoding="utf-8")

    print("\n校验通过。原文件已改名保留（没有删除）：")
    for name in report["retired"]:
        print(f"  {name}")
    print(f"\n报告: {REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
