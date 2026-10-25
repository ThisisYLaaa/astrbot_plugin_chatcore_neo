# -*- coding: utf-8 -*-
"""把 `content/lore_extracted.yaml`（抽取暂存）合并进 `persona.yaml`。

为什么不直接让抽取过程写 persona.yaml
--------------------------------------
抽取器是 LLM 子代理，让它并发写同一个 YAML 很容易产出半截文件或
键序错乱。改成"子代理只写独立暂存文件 → 本脚本统一校验 + 合并"，
好处是：
  1. 合并前可以做**强校验**（键齐、类型对、signature_lines 真在原文里）
  2. 合并是原子的，persona.yaml 永远不会处于半写状态
  3. 合并后删除暂存文件，doctor 的"暂存未合并"提示随之消失

校验项（任一不过就不合并该人格，并报告原因）：
  * 顶层键必须是规定的 8 个
  * `relations[].weight` 必须是 1~5 整数
  * `signature_lines` 必须能在 lore.md 原文里逐条找到（防止子代理编台词）
  * `extra_blocks` 每项必须有 id/title/reason
  * `discarded` 每项必须有 summary/reason
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))

import yaml  # noqa: E402

from core.paths import PERSONAS_DIR  # noqa: E402
from core.persona.model import LoreData, PersonaDoc  # noqa: E402

REPORT = Path(__file__).resolve().parent / "merge_lore_report.json"
STAGING = "content/lore_extracted.yaml"

REQUIRED_KEYS = ("origin", "motivation", "themes", "signature_lines",
                 "relations", "timeline", "extra_blocks", "discarded")


# 引号与省略号的等价写法：抄录时把 '' 写成「」是常见且无害的差异，
# 不该被误判成"编造"。这里统一折叠，保证校验只在**内容**上较真。
_QUOTE_MAP = str.maketrans({
    # 所有引号（含 ASCII 直引号）统一折叠成同一个字符：
    # lore.md 里写的是 '形象受损'（直单引号），抄录时常被写成「形象受损」，
    # 二者是同一条引文，不该判成编造。
    "「": '"', "」": '"', "『": '"', "』": '"',
    "“": '"', "”": '"', "‘": '"', "’": '"', "'": '"', "`": '"',
    "…": ".", "．": ".", "—": "-", "–": "-", "─": "-",
    "，": ",", "。": ".", "：": ":", "；": ";", "！": "!", "？": "?",
    "（": "(", "）": ")", "、": ",",
})


def norm(s: str) -> str:
    """比对用归一：去掉空白 + 折叠引号/省略号/全角标点的等价写法。"""
    return "".join(ch for ch in (s or "").translate(_QUOTE_MAP)
                   if not ch.isspace())


def validate(raw: dict, lore_text: str, doc=None) -> list[str]:
    errs: list[str] = []
    if not isinstance(raw, dict):
        return ["YAML 顶层不是映射"]
    missing = [k for k in REQUIRED_KEYS if k not in raw]
    if missing:
        errs.append(f"缺少顶层键: {missing}")
    unknown = [k for k in raw if k not in REQUIRED_KEYS]
    if unknown:
        errs.append(f"多余顶层键: {unknown}")

    for key in ("themes", "signature_lines", "timeline"):
        v = raw.get(key)
        if v is not None and not isinstance(v, list):
            errs.append(f"{key} 不是列表")

    for i, r in enumerate(raw.get("relations") or []):
        if not isinstance(r, dict):
            errs.append(f"relations[{i}] 不是映射")
            continue
        w = r.get("weight")
        if not isinstance(w, int) or not (1 <= w <= 5):
            errs.append(f"relations[{i}].weight 必须是 1~5 整数，实际 {w!r}")
        if not r.get("who"):
            errs.append(f"relations[{i}] 缺少 who")

    for i, b in enumerate(raw.get("extra_blocks") or []):
        if not isinstance(b, dict) or not b.get("id") or not b.get("reason"):
            errs.append(f"extra_blocks[{i}] 需要 id 与 reason")
            continue
        body = b.get("body")
        if body is not None and not isinstance(body, str):
            errs.append(f"extra_blocks[{i}].body 必须是字符串")
        if not (body or b.get("file")):
            errs.append(
                f"extra_blocks[{i}]（{b.get('id')}）既无 body 也无 file —— "
                f"内容只写在 reason 里等于丢失，请补上实际正文")

    for i, d in enumerate(raw.get("discarded") or []):
        if not isinstance(d, dict) or not d.get("summary") or not d.get("reason"):
            errs.append(f"discarded[{i}] 需要 summary 与 reason")

    # extra_blocks 不能和 SKILL.md 的章节重复。
    # SKILL.md 每轮全量注入（L1），把它的内容再放一份到 on_demand 块里，
    # 既省不下上下文，又制造了"两处不一致"的隐患。
    if doc is not None and doc.outline is not None:
        from core.persona.registry import normalize_title
        sec = {normalize_title(t) for _l, t in doc.outline.titles()}
        for i, b in enumerate(raw.get("extra_blocks") or []):
            if not isinstance(b, dict):
                continue
            for field in ("title", "id"):
                v = normalize_title(str(b.get(field) or ""))
                if v and v in sec:
                    errs.append(
                        f"extra_blocks[{i}]（{b.get('id')}）与 SKILL.md 章节"
                        f"「{b.get('title')}」重复 —— 该内容已由 L1 每轮注入，"
                        f"应删除此块而不是再搬一份")
                    break

    # 台词必须真在原文里（防编造）。
    # 合法来源不止 lore.md：SKILL.md / 台词样本 / 语料里的台词同样是"原文"，
    # 只要能在任一来源逐字命中即可；全部落空才判为编造。
    sources = {}
    if doc is not None:
        for key in ("skill", "lore", "dialog", "speech_raw"):
            txt = doc.read_content(key)
            if txt:
                sources[key] = norm(txt)
    else:
        sources["lore"] = norm(lore_text)
    if not sources:
        sources["lore"] = norm(lore_text)

    for line in raw.get("signature_lines") or []:
        if not isinstance(line, str):
            errs.append(f"signature_lines 含非字符串项: {line!r}")
            continue
        n = norm(line)
        if not n:
            continue
        hit = [k for k, v in sources.items() if n in v]
        if not hit:
            errs.append(
                f"标志性台词在 lore/SKILL/语料 中均找不到（疑似编造）: {line[:40]!r}")
    return errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-staging", action="store_true",
                    help="合并后保留暂存文件")
    ap.add_argument("--only", default="")
    a = ap.parse_args()

    targets = [x.strip() for x in a.only.split(",") if x.strip()]
    report: dict = {"merged": [], "skipped": [], "failed": {}}

    for d in sorted(PERSONAS_DIR.iterdir()):
        if not d.is_dir() or not (d / "persona.yaml").exists():
            continue
        if targets and d.name not in targets:
            continue
        stage = d / STAGING
        if not stage.exists():
            report["skipped"].append(f"{d.name}: 无暂存文件")
            continue
        try:
            raw = yaml.safe_load(stage.read_text(encoding="utf-8")) or {}
        except Exception as e:
            report["failed"][d.name] = [f"YAML 解析失败: {e!r}"]
            continue

        doc = PersonaDoc.load(d)
        lore_text = doc.read_content("lore")
        errs = validate(raw, lore_text, doc)
        if errs:
            report["failed"][d.name] = errs
            print(f"[FAIL] {d.name}: {len(errs)} 项校验不过")
            for e in errs[:6]:
                print(f"        - {e}")
            continue

        # extra_blocks 的正文落成 content/sections/<id>.md（方案 9.5 的布局），
        # persona.yaml 里只留文件引用 —— 长文不进 YAML，diff 才干净。
        blocks: list[dict] = []
        sec_dir = d / "content" / "sections"
        for b in (raw.get("extra_blocks") or []):
            bid = str(b.get("id") or "").strip()
            entry = {"id": bid, "title": b.get("title", "") or "",
                     "reason": b.get("reason", "") or "",
                     "inject": b.get("inject", "on_demand")}
            body = b.get("body")
            if body and bid:
                sec_dir.mkdir(parents=True, exist_ok=True)
                fname = f"{bid}.md"
                (sec_dir / fname).write_text(str(body).strip() + "\n",
                                             encoding="utf-8")
                entry["file"] = f"content/sections/{fname}"
            elif b.get("file"):
                entry["file"] = b["file"]
            blocks.append(entry)

        doc.lore = LoreData(
            origin=raw.get("origin", "") or "",
            motivation=raw.get("motivation", "") or "",
            themes=list(raw.get("themes") or []),
            signature_lines=list(raw.get("signature_lines") or []),
            relations=list(raw.get("relations") or []),
            timeline=list(raw.get("timeline") or []),
            extra_blocks=blocks,
            extracted=True,
        )
        doc.save(d)
        if not a.keep_staging:
            stage.unlink()

        # 抽取产物的体量：每轮实际注入的只有 brief()
        n_src = len(lore_text.encode("utf-8"))
        n_brief = len(doc.lore.brief().encode("utf-8"))
        n_blocks = sum(1 for b in blocks if b.get("file"))
        report["merged"].append({
            "id": d.name, "lore_src_bytes": n_src, "brief_bytes": n_brief,
            "signature_lines": len(doc.lore.signature_lines),
            "relations": len(doc.lore.relations),
            "extra_blocks": len(blocks),
            "extra_blocks_with_body": n_blocks,
            "discarded": len(raw.get("discarded") or []),
        })
        print(f"[OK  ] {d.name:26} lore {n_src:6}B -> brief {n_brief:5}B "
              f"台词{len(doc.lore.signature_lines)} 关系{len(doc.lore.relations)} "
              f"额外块{len(blocks)}(含正文{n_blocks})")

    print()
    print(f"已合并 {len(report['merged'])}，跳过 {len(report['skipped'])}，"
          f"失败 {len(report['failed'])}")
    if report["failed"]:
        print("失败清单:")
        for k, v in report["failed"].items():
            print(f"  {k}: {v[0] if v else ''}")

    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    print(f"报告: {REPORT}")
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
