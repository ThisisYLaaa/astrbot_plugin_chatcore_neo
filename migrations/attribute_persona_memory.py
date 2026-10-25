# -*- coding: utf-8 -*-
"""给「来源人格不明」的历史记忆补齐 persona（一次性）。

背景
----
`memory_persona_policy` 支持「本命优先 / 严格隔离」后，只能靠
`atoms.persona` / `entries.persona_id` 判断归属。历史数据里这两列
可能是空的（早期写入没记 persona），于是：
  * 罕见「本命优先」时它们不会被降权，也标不出「来源不明」；
  * 「严格隔离」时会被整批丢掉（空 persona 不等于本命）。

处理策略（与用户确认）
----------------------
对 `persona` 为空的行，在正文里**找人格的名字**：
  * 恰好命中**一个**人格 → 归属到它；
  * 命中 0 个或多个（无法判定）→ **删除**（归属不明的旧记忆不留）。
匹配用的名字：`display_name` / `display_name_en` / `id` / `aliases`
（长度 < 2 的别名忽略，避免误命中）。

安全边界
--------
* 默认 dry-run，只统计不写库；`--apply` 才真正写。
* `--apply` 前会先备份 `memory.db` 与 `graph.db`（含 `-wal` / `-shm`）
  到 `memory/_persona_migration_backup_<时间戳>/`。
* 已有 persona 的行**一律不动**。
* 图谱条目只做「归属」；删不掉的条目交给 `prune_leftover_graph.py` 收尾
  （本脚本对无法归属的图谱条目也只删条目行，不碰节点/边）。
* 跑之前请先停 AstrBot（或重启后再跑），否则内存态与库不一致。

用法
----
    python migrations/attribute_persona_memory.py            # 只看
    python migrations/attribute_persona_memory.py --apply    # 真写
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))

from core.db import connection, transaction  # noqa: E402
from core.paths import MEMORY_DIR, PERSONAS_DIR  # noqa: E402
from core.persona.service import PersonaService  # noqa: E402


def build_name_index() -> list[tuple[str, str]]:
    """(名字, persona_id) 列表，按名字长度降序（长名优先，减少子串误伤）。"""
    svc = PersonaService(PERSONAS_DIR)
    idx: list[tuple[str, str]] = []
    for pid, doc in svc.docs.items():
        names = [doc.display_name, doc.display_name_en, doc.id, *(doc.aliases or [])]
        for n in names:
            n = (n or "").strip()
            if len(n) >= 2:
                idx.append((n, pid))
    idx.sort(key=lambda kv: -len(kv[0]))
    return idx


def match_personas(text: str, index: list[tuple[str, str]]) -> set[str]:
    low = text or ""
    return {pid for name, pid in index if name in low}


def _backup_dbs() -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = MEMORY_DIR / f"_persona_migration_backup_{stamp}"
    dest.mkdir(parents=True, exist_ok=True)
    for base in ("memory.db", "graph.db"):
        for extra in ("", "-wal", "-shm"):
            src = Path(str(MEMORY_DIR / base) + extra)
            if src.exists():
                shutil.copy2(src, dest / src.name)
    return dest


def _migrate_atoms(index, apply: bool) -> tuple[int, int, dict]:
    attributed = deleted = 0
    by_persona: dict[str, int] = {}
    with connection(MEMORY_DIR / "memory.db") as con:
        rows = con.execute(
            "SELECT id, text FROM atoms WHERE persona IS NULL OR persona=''"
        ).fetchall()
    for r in rows:
        hits = match_personas(r["text"], index)
        if len(hits) == 1:
            pid = next(iter(hits))
            attributed += 1
            by_persona[pid] = by_persona.get(pid, 0) + 1
            if apply:
                with transaction(MEMORY_DIR / "memory.db") as con:
                    con.execute("UPDATE atoms SET persona=? WHERE id=?", (pid, r["id"]))
        else:
            deleted += 1
            if apply:
                with transaction(MEMORY_DIR / "memory.db") as con:
                    con.execute("DELETE FROM atoms WHERE id=?", (r["id"],))
    return attributed, deleted, by_persona


def _migrate_entries(index, apply: bool) -> tuple[int, int, dict]:
    attributed = deleted = 0
    by_persona: dict[str, int] = {}
    with connection(MEMORY_DIR / "graph.db") as con:
        rows = con.execute(
            "SELECT entry_key, content FROM entries"
            " WHERE persona_id IS NULL OR persona_id=''"
        ).fetchall()
    for r in rows:
        hits = match_personas(r["content"], index)
        if len(hits) == 1:
            pid = next(iter(hits))
            attributed += 1
            by_persona[pid] = by_persona.get(pid, 0) + 1
            if apply:
                with transaction(MEMORY_DIR / "graph.db") as con:
                    con.execute("UPDATE entries SET persona_id=? WHERE entry_key=?",
                                (pid, r["entry_key"]))
        else:
            deleted += 1
            if apply:
                with transaction(MEMORY_DIR / "graph.db") as con:
                    con.execute("DELETE FROM entries WHERE entry_key=?",
                                (r["entry_key"],))
    return attributed, deleted, by_persona


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只展示）")
    args = ap.parse_args()

    if not (MEMORY_DIR / "memory.db").exists():
        print(f"没有找到 memory.db：{MEMORY_DIR / 'memory.db'}")
        return 0

    index = build_name_index()
    print(f"已加载 {len({pid for _n, pid in index})} 个人格，{len(index)} 个可匹配名字。")

    a_attr, a_del, a_by = _migrate_atoms(index, args.apply)
    print(f"\n[原子记忆] 可归属 {a_attr} 条，无法归属（待删）{a_del} 条")
    for pid, n in sorted(a_by.items(), key=lambda kv: -kv[1]):
        print(f"    -> {pid:<24} {n} 条")

    e_attr = e_del = 0
    e_by: dict[str, int] = {}
    if (MEMORY_DIR / "graph.db").exists():
        e_attr, e_del, e_by = _migrate_entries(index, args.apply)
        print(f"\n[图谱条目] 可归属 {e_attr} 条，无法归属（待删）{e_del} 条")
        for pid, n in sorted(e_by.items(), key=lambda kv: -kv[1]):
            print(f"    -> {pid:<24} {n} 条")

    if a_attr == 0 and a_del == 0 and e_attr == 0 and e_del == 0:
        print("\n没有需要迁移的行（persona 都为非空）。")
        return 0

    if not args.apply:
        print("\n--apply 未传，未做任何修改。")
        return 0

    dest = _backup_dbs()
    print(f"\n已备份数据库到：{dest}")
    print("写入完成。建议随后跑一次 migrations/prune_leftover_graph.py 收尾。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
