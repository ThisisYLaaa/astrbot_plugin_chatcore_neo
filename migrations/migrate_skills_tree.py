# -*- coding: utf-8 -*-
"""迁移 AstrBot 技能树到新环境：`data/skills/`

搬什么 / 不搬什么
-----------------
搬：`_pfm_framework/`（人格框架规则，装配时要读）
    `_pfm_template/`（`/persona new` 的脚手架来源）
    `_worlds/`（世界观资料，自由层级原样平移）
    `func_*`（4 个真正的 AstrBot 功能技能）

不搬：`pfm_*`（15 个人格已迁到 `plugin_data/chatcore_neo/personas/`；
      其余 23 个不搬入新环境）—— 这正是"每轮省 14.8 KB 技能清单"的落地动作
      `_backup/`（旧的一次性备份，287 KB，无用途）
      `_privilege_config.json`（privilege_qids 改由插件配置持有；
      world_role 已固化进各自 persona.yaml）

`_index.md` 删除 6 个：文件树本身就是清单（方案 9.9.1）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))
from core.paths import SKILLS_DIR  # noqa: E402
import os

OLD_SKILLS = Path(os.environ.get("CHATCORE_OLD_SKILLS")
                 or "_请设置环境变量_CHATCORE_OLD_SKILLS_")
REPORT = Path(__file__).resolve().parent / "skills_tree_report.json"

COPY_DIRS = ("_pfm_framework", "_pfm_template", "_worlds")
COPY_FUNC_PREFIX = "func_"
SKIP_DIRS = ("_backup",)
SKIP_FILES = ("_privilege_config.json", "extract_onlymd.py", "slim_pfm_skills.py")


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def copy_tree(src: Path, dst: Path) -> list[dict]:
    out: list[dict] = []
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src)
        target = dst / rel
        if p.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, target)
        out.append({"rel": str(rel).replace("\\", "/"),
                    "bytes": target.stat().st_size,
                    "sha256": sha256_file(target),
                    "identical": sha256_file(p) == sha256_file(target)})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old-skills", default="")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    global OLD_SKILLS
    if a.old_skills:
        OLD_SKILLS = Path(a.old_skills)

    report: dict = {"old_skills": str(OLD_SKILLS), "new_skills": str(SKILLS_DIR),
                    "copied": {}, "deleted_index": [], "skipped": [], "failures": []}
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)

    # ---- 1. 目录平移 ----
    for name in COPY_DIRS:
        src = OLD_SKILLS / name
        if not src.is_dir():
            report["failures"].append(f"缺少源目录: {src}")
            continue
        files = copy_tree(src, SKILLS_DIR / name)
        report["copied"][name] = {"count": len(files),
                                  "bytes": sum(f["bytes"] for f in files)}
        for f in files:
            if not f["identical"]:
                report["failures"].append(f"{name}/{f['rel']} 逐字节不一致")

    # ---- 2. func_* 平移 ----
    for src in sorted(OLD_SKILLS.glob(f"{COPY_FUNC_PREFIX}*")):
        if not src.is_dir():
            continue
        files = copy_tree(src, SKILLS_DIR / src.name)
        report["copied"][src.name] = {"count": len(files),
                                      "bytes": sum(f["bytes"] for f in files)}
        for f in files:
            if not f["identical"]:
                report["failures"].append(f"{src.name}/{f['rel']} 逐字节不一致")

    # ---- 3. 删除 _worlds 下的 _index.md（文件树取代清单）----
    worlds = SKILLS_DIR / "_worlds"
    if worlds.exists():
        for idx in sorted(worlds.rglob("_index.md")):
            rel = idx.relative_to(SKILLS_DIR)
            idx.unlink()
            report["deleted_index"].append(str(rel).replace("\\", "/"))

    # ---- 4. 记录跳过项 ----
    for name in SKIP_DIRS:
        if (OLD_SKILLS / name).exists():
            report["skipped"].append(f"{name}/（旧备份，无用途）")
    for name in SKIP_FILES:
        if (OLD_SKILLS / name).exists():
            report["skipped"].append(name)
    pfm = [d.name for d in OLD_SKILLS.glob("pfm_*") if d.is_dir()]
    report["skipped"].append(
        f"pfm_* 共 {len(pfm)} 个人格目录（15 个已迁至 persona 数据根；其余不搬）")

    # ---- 汇总 ----
    total = sum(v["count"] for v in report["copied"].values())
    nbytes = sum(v["bytes"] for v in report["copied"].values())
    print(f"技能树迁移完成 → {SKILLS_DIR}")
    for k, v in report["copied"].items():
        print(f"  {k:22} {v['count']:3} 文件 / {v['bytes']:8} B")
    print(f"  合计 {total} 文件 / {nbytes} B")
    print(f"删除 _index.md: {len(report['deleted_index'])} 个 -> "
          f"{report['deleted_index']}")
    print(f"跳过: {'; '.join(report['skipped'])}")
    print(f"逐字节失败: {len(report['failures'])}")

    # 核验：新环境 skills 下不应再有 pfm_* 或 _index.md
    leftover_pfm = [p.name for p in SKILLS_DIR.glob("pfm_*")]
    leftover_idx = [str(p.relative_to(SKILLS_DIR)) for p in SKILLS_DIR.rglob("_index.md")]
    print(f"\n[核验] 新 skills 下残留 pfm_*: {leftover_pfm or '无'}")
    print(f"[核验] 新 skills 下残留 _index.md: {leftover_idx or '无'}")
    report["leftover_pfm"] = leftover_pfm
    report["leftover_index"] = leftover_idx

    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    print(f"报告: {REPORT}")
    return 1 if (report["failures"] or leftover_pfm or leftover_idx) else 0


if __name__ == "__main__":
    raise SystemExit(main())
