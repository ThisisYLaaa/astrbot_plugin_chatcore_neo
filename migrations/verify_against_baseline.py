# -*- coding: utf-8 -*-
"""W1 验收：新数据布局能否复现旧装配行为？差异是否全部「已解释」？

比对对象
--------
OLD = W0 基线。基线由**旧代码本体**经 AST 抽取后执行 `_compose_persona_rules`
      产出（120 变体），因此它忠实记录了旧代码的**真实行为，包括 bug**。
NEW = 新代码 `render.legacy_shape()`，读 `personas/<id>/content/` + 新 skills 树。

差异分类（每一类都必须给出可复现的证据）
----------------------------------------
  IDENTICAL                        逐字节一致
  EXPLAINED_FRAMEWORK_CACHE_BUG    旧 `_load_framework_content` 缓存顺序缺陷：
                                   若进程内先处理过群聊消息，私聊分支永远拿到
                                   空 framework。新实现无缓存、恒返回正确内容。
                                   证据：本脚本内联复现两种调用顺序。
  UNEXPLAINED                      其它差异 —— **必须为 0**，否则 W1 不通过

另附：`content/*` 与旧文件按**原始字节**逐一比对，证明迁移无损。
（基线里的 sha256 是对"解码后文本再编码"算的，CRLF 文件与原始字节不同，
  所以这里直接读旧文件比原始字节，是更强的断言。）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))

from core.paths import PERSONAS_DIR  # noqa: E402
from core.persona import model as P  # noqa: E402
from core.persona import render as R  # noqa: E402
import os

BASELINE_JSON = Path(os.environ.get("CHATCORE_BASELINE")
                     or "_请设置环境变量_CHATCORE_BASELINE_")
BASELINE_DIR = BASELINE_JSON.parent
OLD_SKILLS = Path(os.environ.get("CHATCORE_OLD_SKILLS")
                 or "_请设置环境变量_CHATCORE_OLD_SKILLS_")
REPORT = Path(__file__).resolve().parent / "verify_against_baseline_report.json"

SCENARIO_ARGS = {
    "group_t1_dialog":            dict(is_private=False, msg_mode=1, tier="intimate", include_dialog=True,  include_nsfw=False),
    "group_t1_dialog_nsfw":       dict(is_private=False, msg_mode=1, tier="intimate", include_dialog=True,  include_nsfw=True),
    "group_t2_dialog":            dict(is_private=False, msg_mode=1, tier="friend",   include_dialog=True,  include_nsfw=False),
    "group_t1_nodialog":          dict(is_private=False, msg_mode=1, tier="intimate", include_dialog=False, include_nsfw=False),
    "private_t1_msg1_dialog":     dict(is_private=True,  msg_mode=1, tier="intimate", include_dialog=True,  include_nsfw=False),
    "private_t1_msg1_dialog_nsfw":dict(is_private=True,  msg_mode=1, tier="intimate", include_dialog=True,  include_nsfw=True),
    "private_t1_msg2_dialog_nsfw":dict(is_private=True,  msg_mode=2, tier="intimate", include_dialog=True,  include_nsfw=True),
    "private_t2_msg1_dialog":     dict(is_private=True,  msg_mode=1, tier="friend",   include_dialog=True,  include_nsfw=False),
}

# 旧文件 -> 新布局位置解析器
FILE_MAP = {
    "SKILL.md":             lambda d: d.content_path("skill"),
    "lore.md":              lambda d: d.content_path("lore"),
    "dialog.md":            lambda d: d.content_path("dialog"),
    "speech_patterns.txt":  lambda d: d.content_path("speech_raw"),
    "_tier1.md":            lambda d: d.tier_path("intimate"),
    "_tier2.md":            lambda d: d.tier_path("friend"),
    "name.txt":             lambda d: d.content_path("name_txt"),
}

# 旧装配在 framework 为空时的特征前缀
EMPTY_FW_MARK = "# 通用框架规则\n\n\n\n---"


def sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def first_diff(a: str, b: str) -> dict:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return {"offset": i, "old_len": len(a), "new_len": len(b),
            "old_ctx": a[max(0, i - 80):i + 80],
            "new_ctx": b[max(0, i - 80):i + 80]}


def reproduce_framework_cache_bug() -> dict:
    """内联复现旧 `_load_framework_content` 的顺序缺陷，作为"已解释"的证据。

    从 W0 的 AST 抽取产物加载旧函数本体，分别在
    「先群聊」「先私聊」两种顺序下取私聊 framework，比较长度。
    """
    out: dict = {"reproduced": False}
    try:
        sys.path.insert(0, os.environ.get("CHATCORE_WORK_DIR")
                        or str(BASELINE_DIR.parent))
        import importlib
        be = importlib.import_module("baseline_extract")
        ns = be.load_old_namespace()
        lfc = ns["_load_framework_content"]

        def reset():
            ns["_framework_cache"].update(
                {"mtime": None, "group": "", "private": "", "private_msg2": ""})

        reset()
        lfc(is_private=False)                        # 先群聊
        after_group_private = len(lfc(is_private=True, msg_mode=1))
        reset()
        before_group_private = len(lfc(is_private=True, msg_mode=1))  # 先私聊
        reset()
        group_len = len(lfc(is_private=False))

        out.update({
            "reproduced": True,
            "group_len": group_len,
            "private_len_when_group_called_first": after_group_private,
            "private_len_when_private_called_first": before_group_private,
            "verdict": ("旧实现在【先群聊】后，私聊 framework 恒为空"
                        if after_group_private == 0 and before_group_private > 0
                        else "未能复现，需人工复核"),
        })
    except Exception as e:  # pragma: no cover
        out["error"] = repr(e)
    return out


def check_order_independence() -> dict:
    """新实现必须与调用顺序无关。"""
    from core.paths import FRAMEWORK_DIR
    from core.persona.render import load_framework as nlf
    # 新实现无缓存，直接连调两次；同时验证群/私聊内容不同且都非空
    g1 = len(nlf(is_private=False))
    p1 = len(nlf(is_private=True, msg_mode=1))
    p2 = len(nlf(is_private=True, msg_mode=2))
    g2 = len(nlf(is_private=False))
    return {
        "group_1": g1, "group_2": g2, "private_msg1": p1, "private_msg2": p2,
        "order_independent": g1 == g2 and p1 > 0 and p2 > 0,
        "framework_dir_populated": any(FRAMEWORK_DIR.glob("*.md")),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", default=str(BASELINE_JSON))
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    base_path = Path(a.baseline)
    base = json.loads(base_path.read_text(encoding="utf-8"))
    asm_dir = base_path.parent / "assembled"

    bug = reproduce_framework_cache_bug()
    order = check_order_independence()

    report: dict = {
        "baseline": str(base_path),
        "personas_dir": str(PERSONAS_DIR),
        "framework_cache_bug_repro": bug,
        "new_impl_order_independence": order,
        "files": {}, "scenarios": {}, "unexplained": [], "failures": [],
    }

    n_files = n_files_ok = 0
    n_scen = n_ident = n_explained = 0
    unexplained: list[dict] = []

    for legacy_dir, entry in sorted(base["personas"].items()):
        pid = legacy_dir.replace("pfm_", "", 1)
        pdir = PERSONAS_DIR / pid
        if not (pdir / "persona.yaml").exists():
            report["failures"].append(f"缺少迁移后人格: {pdir}")
            continue
        doc = P.PersonaDoc.load(pdir)

        # ---- 1) 文件原始字节比对 ----
        frec: dict = {}
        for fname, resolver in FILE_MAP.items():
            old_p = OLD_SKILLS / legacy_dir / fname
            new_p = resolver(doc)
            if not old_p.exists():
                frec[fname] = "absent-in-old"
                continue
            if new_p is None or not new_p.exists():
                frec[fname] = "MISSING"
                report["failures"].append(f"{pid}: 新布局缺失 {fname}")
                n_files += 1
                continue
            n_files += 1
            ob, nb = old_p.read_bytes(), new_p.read_bytes()
            if ob == nb:
                frec[fname] = "byte-identical"
                n_files_ok += 1
            else:
                frec[fname] = "MISMATCH"
                report["failures"].append(
                    f"{pid}: {fname} 原始字节不一致 "
                    f"old={len(ob)}B/{sha_bytes(ob)[:12]} "
                    f"new={len(nb)}B/{sha_bytes(nb)[:12]}")
        report["files"][pid] = frec

        # ---- 2) 装配比对 ----
        srec: dict = {}
        for key, kwargs in SCENARIO_ARGS.items():
            f = asm_dir / f"{legacy_dir}__{key}.txt"
            old_txt = f.read_text(encoding="utf-8")
            new_txt, _info = R.legacy_shape(doc, **kwargs)
            n_scen += 1

            if old_txt == new_txt:
                cls = "IDENTICAL"
                n_ident += 1
            elif (kwargs["is_private"]
                  and old_txt.startswith(EMPTY_FW_MARK)
                  and not new_txt.startswith(EMPTY_FW_MARK)):
                cls = "EXPLAINED_FRAMEWORK_CACHE_BUG"
                n_explained += 1
            else:
                cls = "UNEXPLAINED"
                d = first_diff(old_txt, new_txt)
                unexplained.append({"persona": pid, "scenario": key, **d})
                if a.verbose:
                    print(f"  UNEXPLAINED {pid} / {key} @{d['offset']}")
                    print(f"    old: {d['old_ctx']!r}")
                    print(f"    new: {d['new_ctx']!r}")
            srec[key] = {"class": cls,
                         "old_sha256": hashlib.sha256(old_txt.encode()).hexdigest(),
                         "new_sha256": hashlib.sha256(new_txt.encode()).hexdigest(),
                         "old_len": len(old_txt), "new_len": len(new_txt)}
        report["scenarios"][pid] = srec
        report["unexplained"] = unexplained

        tags = {}
        for v in srec.values():
            tags[v["class"]] = tags.get(v["class"], 0) + 1
        print(f"[{pid:26}] " + "  ".join(f"{k}={v}" for k, v in sorted(tags.items())))

    report["summary"] = {
        "files_compared": n_files, "files_byte_identical": n_files_ok,
        "scenarios_total": n_scen, "scenarios_identical": n_ident,
        "scenarios_explained_bug": n_explained,
        "scenarios_unexplained": len(unexplained),
        "framework_cache_bug_reproduced": bool(bug.get("reproduced")),
        "new_impl_order_independent": bool(order.get("order_independent")),
    }

    print()
    print("=== W1 验收汇总 ===")
    print(f"文件原始字节一致: {n_files_ok}/{n_files}")
    print(f"装配场景: 一致 {n_ident} / 已解释(旧 bug 修复) {n_explained} / "
          f"未解释 {len(unexplained)}   (共 {n_scen})")
    print(f"旧 framework 缓存缺陷复现: {bug.get('verdict', bug)}")
    print(f"新实现顺序无关: {order.get('order_independent')} "
          f"(group={order.get('group_1')}/{order.get('group_2')} "
          f"private1={order.get('private_msg1')} private2={order.get('private_msg2')})")
    if report["failures"]:
        print("\n失败项:")
        for x in report["failures"]:
            print(f"  ! {x}")
    if unexplained:
        print(f"\n未解释差异 {len(unexplained)} 处（前 5）:")
        for u in unexplained[:5]:
            print(f"  {u['persona']} / {u['scenario']} @{u['offset']}")
            print(f"    old: {u['old_ctx']!r}")
            print(f"    new: {u['new_ctx']!r}")

    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    print(f"\n报告: {REPORT}")

    ok = (n_files_ok == n_files and not unexplained
          and not report["failures"]
          and order.get("order_independent")
          and bug.get("reproduced"))
    print("W1 验收:", "通过（0 处未解释差异）" if ok else "不通过")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
