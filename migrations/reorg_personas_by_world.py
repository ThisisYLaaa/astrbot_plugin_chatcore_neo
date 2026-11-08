# -*- coding: utf-8 -*-
"""人格池重排：扁平 `personas/<id>/` → 两层 `personas/<游戏·世界观>/<角色人格>/`。

规则（2026-10-27 用户要求）
--------------------------
1. 第一层按 `persona.yaml` 的 `world` 字段建「游戏/世界观」目录，目录名用
   **英文全名**（空格→下划线），代码里的 world 代码不变（外键仍是 sr/zzz/...）。
2. 第二层文件夹名**去掉 `{world}_` 前缀**（`sr_robin_qingge` → `robin_qingge`）。
3. `persona.yaml` 的 `id` 一律**不动** —— groups.json / instances/personas.json /
   state / memory 都按 id 引用，改 id 等于把这些引用全打断。

幂等：已经是两层的目录会被识别并跳过，可反复执行。
用法：
    python migrations/reorg_personas_by_world.py [--dry-run]
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from core.paths import PERSONAS_DIR, world_dir_name  # noqa: E402

REPORT = PERSONAS_DIR.parent / "personas_layout_report.json"
SKIP = {"__pycache__", ".git"}


def _load_world(persona_dir: Path) -> str:
    raw = yaml.safe_load((persona_dir / "persona.yaml").read_text(
        encoding="utf-8-sig")) or {}
    return str(raw.get("world") or "").strip()


def _strip_prefix(name: str, world: str) -> str:
    low = name.lower()
    for pref in (world, "diy"):
        if pref and low.startswith(pref.lower() + "_"):
            return name[len(pref) + 1:]
    return name


def main(dry_run: bool = False) -> int:
    if not PERSONAS_DIR.is_dir():
        print(f"人格池不存在: {PERSONAS_DIR}")
        return 1

    moves: list[dict] = []
    skipped: list[dict] = []
    conflicts: list[dict] = []

    for d in sorted(PERSONAS_DIR.iterdir()):
        if not d.is_dir() or d.name in SKIP or d.name.startswith((".", "_")):
            continue
        if not (d / "persona.yaml").exists():
            # 已经是「世界观」层目录（或其它非人格目录）→ 不动
            skipped.append({"dir": d.name, "why": "no persona.yaml (已是分层目录?)"})
            continue

        world = _load_world(d)
        world_dir = world_dir_name(world)
        new_name = _strip_prefix(d.name, world)
        dst = PERSONAS_DIR / world_dir / new_name

        if dst.resolve() == d.resolve():
            skipped.append({"dir": d.name, "why": "目标与源相同"})
            continue
        if dst.exists():
            conflicts.append({"src": d.name, "dst": str(dst.relative_to(PERSONAS_DIR))})
            continue

        moves.append({
            "id_src": d.name,
            "world": world,
            "world_dir": world_dir,
            "new_name": new_name,
            "dst": str(dst.relative_to(PERSONAS_DIR)).replace("\\", "/"),
        })
        if not dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(d), str(dst))

    print(f"{'[dry-run] ' if dry_run else ''}移动 {len(moves)} 个，"
          f"跳过 {len(skipped)} 个，冲突 {len(conflicts)} 个")
    for m in moves:
        print(f"  {m['id_src']:<24} world={m['world'] or '(空)':<8} -> {m['dst']}")
    for c in conflicts:
        print(f"  ! 冲突 {c['src']} -> {c['dst']}")
    for s in skipped:
        print(f"  · 跳过 {s['dir']}：{s['why']}")

    if not dry_run:
        REPORT.write_text(json.dumps(
            {"moves": moves, "skipped": skipped, "conflicts": conflicts},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告: {REPORT}")
    return 0 if not conflicts else 2


if __name__ == "__main__":
    raise SystemExit(main(dry_run="--dry-run" in sys.argv))
