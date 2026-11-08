# -*- coding: utf-8 -*-
"""W2：迁移 lili_state 状态数据到 chatcore_neo 状态根。

迁移动作
--------
1. 复制 `plugin_data/lili_state/**` → `plugin_data/chatcore_neo/state/**`
2. **重写人格 id**：旧目录名 `pfm_strinova_kanami` → 新 id `strinova_kanami`
   - 目录名 `{umo}/{persona}/{uid}.json`
   - `_session_persona.json` 的值
   - `_inject_overrides.json` 的键（`{umo}|{persona}|{uid}`）
3. **丢弃未迁移人格的数据**（23 个人格不搬入新环境），并记录丢弃清单，
   避免在新环境里留下指向不存在人格的孤儿文件
4. `_name_cache.json` 不迁：它是显示名缓存，会自行重建
   （旧实现缓存的是群名/昵称，与新环境的群列表无关）

为什么重写而不是保平
--------------------
新环境的 `world` / `id` 是显式外键，人格目录名不再是 `pfm_*`。
若原样搬运，`_session_persona` 里会全是解析不到的名字，
表现就是"迁移完了但所有人格的会话都退回默认人格"。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))
from core.paths import STATE_DIR  # noqa: E402
from core.persona import PersonaService  # noqa: E402
import os

OLD = Path(os.environ.get("CHATCORE_OLD_STATE")
           or "_请设置环境变量_CHATCORE_OLD_STATE_")
REPORT = Path(__file__).resolve().parent / "state_migration_report.json"

SPECIAL = ("_session_persona.json", "_session_initialized.json",
           "_inject_overrides.json", "_session_second_persona.json")
SKIP_FILES = ("_name_cache.json",)


def build_id_map(svc: PersonaService) -> dict[str, str]:
    """旧人格目录名 → 新 id。同时纳入 supersedes 里的历史名。"""
    m: dict[str, str] = {}
    for doc in svc.all():
        if doc.legacy_dir:
            m[doc.legacy_dir] = doc.id
        m[doc.id] = doc.id
        for old in doc.supersedes:
            m[old] = doc.id
    return m


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", default=str(OLD))
    ap.add_argument("--apply", action="store_true", help="真正执行（默认只预检）")
    a = ap.parse_args()
    old = Path(a.old)
    if not old.is_dir():
        print(f"旧状态目录不存在: {old}", file=sys.stderr)
        return 2

    svc = PersonaService()
    idmap = build_id_map(svc)

    report: dict = {
        "old": str(old), "new": str(STATE_DIR),
        "copied_files": 0, "rewritten_persona_dirs": 0,
        "dropped_unknown_personas": {}, "session_persona_rewritten": {},
        "inject_rewritten": 0, "inject_dropped": 0, "skipped": [],
        "applied": bool(a.apply),
    }

    # ---- 1. 私聊状态：{umo}/{persona}.json ----
    plan: list[tuple[Path, Path]] = []
    todo_drop: dict[str, list[str]] = {}
    for umo_dir in sorted(p for p in old.iterdir() if p.is_dir()
                          and not p.name.startswith("_")):
        dst_umo = STATE_DIR / umo_dir.name
        for f in sorted(umo_dir.iterdir()):
            if f.is_file() and f.suffix == ".json":
                old_pid = f.stem
                new_pid = idmap.get(old_pid)
                if new_pid is None:
                    todo_drop.setdefault(old_pid, []).append(
                        f"{umo_dir.name}/{f.name}")
                    continue
                plan.append((f, dst_umo / f"{new_pid}.json"))
                if new_pid != old_pid:
                    report["rewritten_persona_dirs"] += 1
            elif f.is_dir():
                # ---- 2. 群聊状态：{umo}/{persona}/{uid}.json ----
                old_pid = f.name
                new_pid = idmap.get(old_pid)
                if new_pid is None:
                    for m in f.glob("*.json"):
                        todo_drop.setdefault(old_pid, []).append(
                            f"{umo_dir.name}/{f.name}/{m.name}")
                    continue
                for m in sorted(f.glob("*.json")):
                    plan.append((m, dst_umo / new_pid / m.name))
                if new_pid != old_pid:
                    report["rewritten_persona_dirs"] += 1

    # ---- 3. _impressions ----
    imp = old / "_impressions"
    if imp.is_dir():
        for f in sorted(imp.glob("*.json")):
            plan.append((f, STATE_DIR / "_impressions" / f.name))

    # ---- 4. 特殊文件 ----
    session_persona = {}
    sp = old / "_session_persona.json"
    if sp.exists():
        try:
            raw = json.loads(sp.read_text(encoding="utf-8")) or {}
            for umo, pid in raw.items():
                new_pid = idmap.get(pid)
                if new_pid:
                    session_persona[umo] = new_pid
                    if new_pid != pid:
                        report["session_persona_rewritten"][pid] = new_pid
                else:
                    todo_drop.setdefault(pid, []).append(f"_session_persona:{umo}")
        except Exception as e:
            report["skipped"].append(f"_session_persona.json 解析失败: {e!r}")

    inject_new = {}
    ip = old / "_inject_overrides.json"
    if ip.exists():
        try:
            raw = json.loads(ip.read_text(encoding="utf-8")) or {}
            for key, val in raw.items():
                parts = key.split("|")
                if len(parts) == 3:
                    umo, pid, uid = parts
                    new_pid = idmap.get(pid)
                    if new_pid is None:
                        report["inject_dropped"] += 1
                        todo_drop.setdefault(pid, []).append(f"_inject:{key}")
                        continue
                    inject_new[f"{umo}|{new_pid}|{uid}"] = val
                    report["inject_rewritten"] += 1
                else:
                    inject_new[key] = val
        except Exception as e:
            report["skipped"].append(f"_inject_overrides.json 解析失败: {e!r}")

    init_list = []
    ipp = old / "_session_initialized.json"
    if ipp.exists():
        try:
            init_list = json.loads(ipp.read_text(encoding="utf-8")) or []
        except Exception as e:
            report["skipped"].append(f"_session_initialized.json 解析失败: {e!r}")

    only_dropped = {k: len(v) for k, v in sorted(todo_drop.items())}
    report["dropped_unknown_personas"] = only_dropped
    report["copied_files"] = len(plan)
    for f in SKIP_FILES:
        if (old / f).exists():
            report["skipped"].append(f"{f}（显示名缓存，不迁，会自行重建）")

    print(f"状态迁移计划：{len(plan)} 个文件 → {STATE_DIR}")
    print(f"  重写人格目录名: {report['rewritten_persona_dirs']} 处")
    print(f"  会话人格映射  : {len(session_persona)} 条"
          f"（重写 {len(report['session_persona_rewritten'])} 种旧名）")
    print(f"  注入覆盖      : 保留 {report['inject_rewritten']}，丢弃 {report['inject_dropped']}")
    print(f"  未迁移人格数据: {len(only_dropped)} 个人格，"
          f"共 {sum(only_dropped.values())} 处")
    for k, v in list(only_dropped.items())[:10]:
        print(f"      - {k}: {v} 处")
    if len(only_dropped) > 10:
        print(f"      … 另有 {len(only_dropped) - 10} 个")

    if not a.apply:
        print("\n预检完成（未改动任何文件）。加 --apply 执行。")
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        return 0

    # ---- 执行 ----
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    for src, dst in plan:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    (STATE_DIR / "_session_persona.json").write_text(
        json.dumps(session_persona, ensure_ascii=False, indent=2), encoding="utf-8")
    (STATE_DIR / "_inject_overrides.json").write_text(
        json.dumps(inject_new, ensure_ascii=False, indent=2), encoding="utf-8")
    (STATE_DIR / "_session_initialized.json").write_text(
        json.dumps(init_list, ensure_ascii=False, indent=2), encoding="utf-8")
    (STATE_DIR / "_session_second_persona.json").write_text(
        json.dumps({}, ensure_ascii=False, indent=2), encoding="utf-8")

    # 校验：新状态根里不应再有 pfm_ 前缀的人格路径
    leftover = [str(p.relative_to(STATE_DIR)).replace("\\", "/")
                for p in STATE_DIR.rglob("pfm_*")][:20]
    report["leftover_pfm_paths"] = leftover
    print(f"\n已写入 {len(plan)} 个文件")
    print(f"[核验] 残留 pfm_ 路径: {leftover or '无'}")

    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    print(f"报告: {REPORT}")
    return 1 if leftover else 0


if __name__ == "__main__":
    raise SystemExit(main())
