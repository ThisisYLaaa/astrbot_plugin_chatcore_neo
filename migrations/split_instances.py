# -*- coding: utf-8 -*-
"""一次性迁移：把单份配置/记忆拆成「每实例一份」。

    python migrations/split_instances.py            # 预演（只打印）
    python migrations/split_instances.py --apply    # 真正执行

做四件事
--------
1. 每个启用的平台实例建 `instances/<id>/config.json`（当前全局配置的快照；
   第一个实例保留全量，其余套 MINIMAL_PRESET）。
2. `instances/<id>/personas.json`：默认全部人格授权给所有实例；
   要按实例收窄就改脚本里的 `CUSTOMIZE`。
3. `memory.db` 拆库：复制一份，按 `umo` 前缀删掉别人的行。
4. `graph.db` 拆库：复制一份，按本库现存 `source_memory_id` 剪枝。

幂等：`instances/.migrated_v1` 存在就直接退出；已存在的实例配置不覆盖。
回滚：把 `_legacy/pre_split_*/` 里的东西拷回来，删掉 `instances/`。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent         # 插件目录
DATA = ROOT.parent.parent                              # .../data
PDATA = DATA / "plugin_data" / "chatcore_neo"          # 本插件数据根
PLUGIN_CFG = DATA / "config" / "astrbot_plugin_chatcore_neo_config.json"
MAIN_CFG = DATA / "cmd_config.json"
PERSONAS = PDATA / "personas"
STATE = PDATA / "state"
MEM = PDATA / "memory"
INST = PDATA / "instances"
SENTINEL = INST / ".migrated_v1"
LEGACY = PDATA / "_legacy" / f"pre_split_{time.strftime('%Y%m%d-%H%M%S')}"

MINIMAL_OFF = {
    "enable_attention": False, "enable_segmentation": False,
    "enable_markers": False, "enable_proactive": False,
    "enable_tts": False, "enable_streaming": False,
    "enable_no_reply_judge": False, "enable_intent": False,
    "enable_graph_retrieval": False, "costar_enabled": False,
    "char_mention_enabled": False, "enable_affection": False,
    "enable_lewdness": False, "enable_impression": False,
    "enable_nsfw_impression": False, "poke_enabled": False,
    "merge_pending_messages": False, "privilege_uid": "",
}


def load(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    if SENTINEL.exists():
        print(f"[skip] 已迁移过：{SENTINEL}")
        return 0

    cfg = load(PLUGIN_CFG)
    main_cfg = load(MAIN_CFG)
    plats = [p for p in (main_cfg.get("platform") or [])
             if isinstance(p, dict) and p.get("id")]
    enabled = [p["id"] for p in plats if p.get("enable", True)]
    if not enabled:
        print("[err] cmd_config.json 里没有启用的平台实例")
        return 2
    print(f"实例：{enabled}")

    old_uid = ""
    qids = cfg.get("privilege_qids") or []
    if isinstance(qids, list) and qids:
        old_uid = str(qids[0])
    print(f"旧全局特权：{old_uid or '（空）'}")

    priv = load(STATE / "_session_privileges.json")
    print(f"旧分会话特权：{priv}")

    personas = sorted(d.name for d in PERSONAS.iterdir() if d.is_dir()) \
        if PERSONAS.is_dir() else []
    print(f"人格 {len(personas)} 个")

    plan: dict[str, dict] = {}
    for idx, iid in enumerate(enabled):
        over: dict = {}
        # 第一个实例 = 全量；其余 = 最小可用
        if idx > 0:
            over.update(MINIMAL_OFF)
        uid = old_uid if idx == 0 else ""
        if iid in priv and isinstance(priv[iid], list) and priv[iid]:
            uid = str(priv[iid][0])
        elif idx > 0 and isinstance(priv.get(iid), str):
            uid = str(priv[iid])
        over["privilege_uid"] = uid
        plan[iid] = over

    # 每个实例可用的人格。默认：全部人格授权给所有实例。
    # 想按实例收窄就在下面写死，例如：
    #   CUSTOMIZE = {"BotB": ["persona_a"], "BotA": ["*"]}
    #   —— "*" 表示除被其它实例独占的之外，其余全给。
    CUSTOMIZE: dict[str, list[str]] = {}
    taken = {p for v in CUSTOMIZE.values() for p in v if p != "*"}
    grants: dict[str, list[str]] = {}
    for iid in enabled:
        spec = CUSTOMIZE.get(iid)
        if spec is None:
            grants[iid] = [p for p in personas if p not in taken]
        elif "*" in spec:
            grants[iid] = [p for p in personas if p not in taken] + \
                          [p for p in spec if p != "*"]
        else:
            grants[iid] = [p for p in personas if p in spec]

    print("\n计划：")
    for iid in enabled:
        print(f"  {iid}: 覆盖 {len(plan[iid])} 项, 特权={plan[iid]['privilege_uid']!r}, "
              f"人格 {len(grants[iid])} 个")

    if not a.apply:
        print("\n（预演结束，加 --apply 执行）")
        return 0

    # ---- 0. 备份 ----
    LEGACY.mkdir(parents=True, exist_ok=True)
    for src in (MEM, STATE, PERSONAS):
        if src.exists():
            shutil.copytree(src, LEGACY / src.name, dirs_exist_ok=True)
    shutil.copy2(PLUGIN_CFG, LEGACY / "plugin_config.json")
    print(f"[backup] {LEGACY}")

    INST.mkdir(parents=True, exist_ok=True)

    # ---- 1+2. 配置与授权 ----
    for iid in enabled:
        d = INST / iid
        (d / "memory").mkdir(parents=True, exist_ok=True)
        (d / "proactive").mkdir(parents=True, exist_ok=True)
        cpath = d / "config.json"
        if not cpath.exists():
            data = dict(cfg)
            data.update(plan[iid])
            cpath.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        ppath = d / "personas.json"
        if not ppath.exists():
            ppath.write_text(json.dumps(
                {"version": 1, "grants": grants[iid],
                 "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
                ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[cfg] {iid}: {cpath.name} + {ppath.name}")

    # ---- 3. 拆 memory.db ----
    src_db = MEM / "memory.db"
    if src_db.exists():
        for iid in enabled:
            dst = INST / iid / "memory" / "memory.db"
            for suffix in ("", "-wal", "-shm"):
                s = Path(str(src_db) + suffix)
                if s.exists():
                    shutil.copy2(s, Path(str(dst) + suffix))
            try:
                con = sqlite3.connect(str(dst))
                cur = con.execute("DELETE FROM atoms WHERE umo NOT LIKE ?",
                                  (f"{iid}:%",))
                con.commit()
                left = con.execute("SELECT COUNT(*) FROM atoms").fetchone()[0]
                con.close()
                print(f"[mem] {iid}: 保留 {left} 条")
            except Exception as e:
                print(f"[warn] {iid} memory 拆分失败：{e!r}")

    # ---- 4. 拆 graph.db ----
    src_g = MEM / "graph.db"
    if src_g.exists():
        for iid in enabled:
            dst = INST / iid / "memory" / "graph.db"
            for suffix in ("", "-wal", "-shm"):
                s = Path(str(src_g) + suffix)
                if s.exists():
                    shutil.copy2(s, Path(str(dst) + suffix))
            try:
                mem_db = INST / iid / "memory" / "memory.db"
                alive = set()
                mc = sqlite3.connect(str(mem_db))
                for (meta,) in mc.execute("SELECT metadata FROM atoms"):
                    try:
                        m = json.loads(meta or "{}")
                    except Exception:
                        continue
                    mid = m.get("source_memory_id")
                    if mid:
                        alive.add(int(mid))
                mc.close()
                gc = sqlite3.connect(str(dst))
                before = gc.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
                if alive:
                    qmarks = ",".join("?" * len(alive))
                    gc.execute(f"DELETE FROM entries WHERE source_memory_id "
                               f"NOT IN ({qmarks})", tuple(alive))
                    gc.execute(f"DELETE FROM edges WHERE source_memory_id "
                               f"NOT IN ({qmarks})", tuple(alive))
                else:
                    gc.execute("DELETE FROM entries")
                    gc.execute("DELETE FROM edges")
                gc.commit()
                after = gc.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
                gc.close()
                print(f"[graph] {iid}: entries {before} -> {after}")
            except Exception as e:
                print(f"[warn] {iid} graph 拆分失败：{e!r}")

    # ---- 5. 旧特权文件下架 ----
    sp = STATE / "_session_privileges.json"
    if sp.exists():
        sp.rename(sp.with_suffix(".json.migrated"))
        print("[state] _session_privileges.json -> .migrated")

    SENTINEL.write_text(json.dumps({
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "instances": enabled,
        "backup": str(LEGACY),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[done] {SENTINEL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
