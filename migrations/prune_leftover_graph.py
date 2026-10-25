# -*- coding: utf-8 -*-
"""清理"无主"的图谱条目（重启后执行，一次性）。

背景
----
旧实现里图谱条目与它的派生原子用同一个 `source_memory_id` 关联，
但历史数据中两路 id 并不同源（某会话 462 条原子的 mid 集合里没有
图中 55 条条目挂着的 `10004`）。于是「删除记忆 → 按原子 mid 删图」
永远匹配不到那批条目 —— 图保留着已遗忘的内容，图谱路照常召回，
AI 表现得"删了记忆还记得"（2026-09-19 实测）。

`GraphStore.forget_session`（按记忆键删图）已修掉新删除的路径；
这个脚本把**历史遗留**清掉：图中所有"现存原子里已找不到对应
source_memory_id"的条目/边/节点。

安全边界
--------
* 只删 `source_memory_id` 在 atoms 库中已不存在的条目；
  原子还活着的**绝不动**。
* `mid=0` 的条目（原子侧也可能没有 mid）**跳过不删**，保守保留。
* 默认 dry-run，加 `--apply` 才真正写库。

用法
----
    python migrations/prune_leftover_graph.py            # 只看，不写
    python migrations/prune_leftover_graph.py --apply    # 真清理
务必在 AstrBot 重启后再跑（否则图的内存态与库不一致）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))

from core.db import connection  # noqa: E402
from core.graph import GraphStore  # noqa: E402
from core.paths import MEMORY_DIR  # noqa: E402


def all_atom_mids() -> set[int]:
    """memory.db 里全部现存原子声明过的 source_memory_id。"""
    alive: set[int] = set()
    with connection(MEMORY_DIR / "memory.db") as con:
        for r in con.execute("SELECT metadata FROM atoms").fetchall():
            try:
                m = json.loads(r["metadata"]) or {}
            except Exception:
                continue
            mid = m.get("source_memory_id")
            if mid:
                alive.add(int(mid))
    return alive


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只展示）")
    args = ap.parse_args()

    alive = all_atom_mids()
    g = GraphStore(MEMORY_DIR / "graph.db")
    before = {k: g.stats()[k] for k in ("nodes", "edges", "entries")}

    # 按 mid 分组统计孤儿规模与归属会话
    orphan_mids: dict[int, int] = {}
    by_session: dict[int, str] = {}
    for e in g.entries.values():
        m = e.source_memory_id
        if not m or m in alive:
            continue
        orphan_mids[m] = orphan_mids.get(m, 0) + 1
        by_session.setdefault(m, e.session_id or "(空)")

    if not orphan_mids:
        print("没有需要清理的孤儿条目。")
        return 0

    print(f"找到 {len(orphan_mids)} 个无主 memory_id，共 {sum(orphan_mids.values())} 条图条目：")
    for m, n in sorted(orphan_mids.items(), key=lambda kv: -kv[1]):
        print(f"  mid={m:<12} 条目={n:<5} 会话={by_session[m]}")
    print(f"清理前: nodes={before['nodes']} edges={before['edges']} entries={before['entries']}")

    if not args.apply:
        print("\n--apply 未传，未做任何修改。")
        return 0

    dropped = sum(g.forget_memory(m) for m in sorted(orphan_mids))
    after = {k: g.stats()[k] for k in ("nodes", "edges", "entries")}
    print(f"已清理条目 {dropped} 条")
    print(f"清理后: nodes={after['nodes']} edges={after['edges']} entries={after['entries']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())