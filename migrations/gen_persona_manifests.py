# -*- coding: utf-8 -*-
"""W1 人格迁移：旧 `data/skills/pfm_*` → `plugin_data/chatcore_neo/personas/<id>/`

W1 首轮迁 15 个（`PERSONAS_V1`），W1 补轮把旧树里剩余的全部人格也迁入
（`PERSONAS_V2`，21 个）。`PERSONAS` 是两批的并集；`pfm_diy_moss` 按决定舍弃。

设计原则
--------
1. **正文逐字节拷贝，不做任何改写。** `content/*.md` 就是原文件，
   无损性由"文件即原文件"直接保证，不依赖任何重组逻辑正确。
2. **只把真正结构化的东西写进 `persona.yaml`**：身份外键、别名、档位文件引用、
   注入策略覆盖。理由见方案 9.5 —— 把长文塞进 YAML 只会让 diff 变脏。
3. **字符串嗅探只在这一步发生一次**，推断结果固化成数据；
   之后插件代码再也不做 `split("_")` 之类的解析（硬性约束 7）。
4. **幂等**：已存在 `persona.yaml` 的目录默认跳过（`--force` 覆盖）。

产出的 `migration_report.json` 记录：推断结果、置信度、以及所有
"需要人工确认"的项（世界归属为推断、别名有冲突、正文缺失等）。
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

from core.persona.model import LoreData, PersonaDoc  # noqa: E402
from core.persona.outline import read_skill_file, split_frontmatter  # noqa: E402
from core.paths import PERSONAS_DIR  # noqa: E402
import os

# 旧环境技能树（只读）。可用 --old-skills 覆盖。
# 2026-09 起旧环境目录已更名为 `obslete-.astrbot`（`.astrbot` 不再存在）。
OLD_SKILLS = Path(os.environ.get("CHATCORE_OLD_SKILLS")
                 or "_请设置环境变量_CHATCORE_OLD_SKILLS_")
NEW_ROOT = PERSONAS_DIR.parent
REPORT = Path(__file__).resolve().parent / "migration_report.json"

# 首轮（W1）15 个
PERSONAS_V1 = [
    "pfm_arke_zhuangfangyi", "pfm_diy_banyan", "pfm_diy_deepseekv4",
    "pfm_diy_sui", "pfm_sr_sparxie", "pfm_strinova_celestia",
    "pfm_strinova_cielle", "pfm_strinova_kanami", "pfm_strinova_maddelenaleary",
    "pfm_strinova_qiandai", "pfm_strinova_yugiri", "pfm_strinova_yvette",
    "pfm_zzz_remielle", "pfm_zzz_vivian", "pfm_zzz_yeshunguang",
]

# 补轮：旧树里剩余、且新环境尚无的全部人格（`pfm_diy_moss` 已按决定舍弃）
PERSONAS_V2 = [
    "pfm_arke_arcane", "pfm_nte_lacrimosa",
    "pfm_strinova_aika", "pfm_strinova_audreygrove", "pfm_strinova_baimo",
    "pfm_strinova_flavia", "pfm_strinova_fragrans", "pfm_strinova_fuchsia",
    "pfm_strinova_galatealeary", "pfm_strinova_kokona", "pfm_strinova_lawine",
    "pfm_strinova_leona", "pfm_strinova_mara", "pfm_strinova_meredith",
    "pfm_strinova_ming", "pfm_strinova_nobunaga", "pfm_strinova_nora",
    "pfm_strinova_reiichi",
    "pfm_zzz_airui", "pfm_zzz_nangongyu", "pfm_zzz_qianxia",
]

PERSONAS = PERSONAS_V1 + PERSONAS_V2

# 旧代码里 _PERSONA_RENAME_MAP 的反向映射：新名 -> 旧名列表（供 supersedes）
SUPERSEDES = {
    "pfm_strinova_cielle": ["pfm_strinova_xi"],
    "pfm_strinova_celestia": ["pfm_strinova_xinghui"],
    "pfm_strinova_maddelenaleary": ["pfm_strinova_madeleina"],
    "pfm_strinova_yvette": ["pfm_strinova_yiweite"],
    "pfm_arke_arcane": ["pfm_arke_jue"],
    "pfm_nte_lacrimosa": ["pfm_nte_anhunqu"],
    "pfm_strinova_audreygrove": ["pfm_strinova_aodaili"],
    "pfm_strinova_fragrans": ["pfm_strinova_fagelansi"],
    "pfm_strinova_fuchsia": ["pfm_strinova_feisha"],
    "pfm_strinova_flavia": ["pfm_strinova_fulaweiya"],
    "pfm_strinova_galatealeary": ["pfm_strinova_jialadiya"],
    "pfm_strinova_lawine": ["pfm_strinova_lawei"],
    "pfm_strinova_leona": ["pfm_strinova_leiouna"],
    "pfm_strinova_reiichi": ["pfm_strinova_ling"],
    "pfm_strinova_mara": ["pfm_strinova_mala"],
    "pfm_strinova_meredith": ["pfm_strinova_meiruidisi"],
    "pfm_strinova_nobunaga": ["pfm_strinova_xin"],
    "pfm_strinova_kokona": ["pfm_strinova_xinxia"],
}

# 有世界观目录的系列。"diy" 不是世界（这正是旧实现 split("_")[1] 的 bug：
# pfm_diy_moss 被判成 world=diy，《流浪地球》资料因此无处安放）。
def world_dirs() -> set[str]:
    wd = OLD_SKILLS / "_worlds"
    return {d.name for d in wd.iterdir() if d.is_dir()} if wd.exists() else set()

PREFERRED_QIDS = [q for q in os.environ.get("CHATCORE_PREFERRED_QIDS", "").split(",")
                  if q.strip()]


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def copy_verbatim(src: Path, dst: Path) -> dict:
    """逐字节拷贝，返回校验信息。绝不改一个字节。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    sb, db = src.read_bytes(), dst.read_bytes()
    return {
        "src": str(src), "dst": str(dst),
        "bytes": len(db), "sha256": hashlib.sha256(db).hexdigest(),
        "identical": sb == db,
    }


def pinyin_aliases(name: str) -> list[str]:
    """中文名 → 全拼 + 首字母。库缺失时返回空（不阻断迁移）。

    **单字首字母（如 信→`x`、明→`m`）一律丢弃**：一个孤立字母无法唯一
    指向某个人格，反而必然跨人格撞车 —— 实测 `信` 与 `汐` 都想占 `x`，
    doctor 直接判 `alias.conflict`（resolve 随之彻底失效）。
    单字中文名（`信`/`令`/`明`）本身已是别名，够用了。
    """
    try:
        from pypinyin import Style, lazy_pinyin
    except Exception:
        return []
    try:
        full = "".join(lazy_pinyin(name))
        initials = "".join(lazy_pinyin(name, style=Style.FIRST_LETTER))
    except Exception:
        return []
    out = []
    for v in (full, initials):
        if len(v) >= 2 and v not in out:
            out.append(v)
    return out


def read_aliases_txt(src: Path) -> tuple[list[str], list[str]]:
    """读旧人格的 `aliases.txt`（跨语言别名，# 开头为注释）。

    旧目录里只有 `pfm_nte_lacrimosa` 有这份文件（`Lacrimosa`），
    它的语义就是"补充别名"，因此直接并入 persona.yaml 的 aliases，
    不落成 content/ 文件 —— 否则会成为 yaml 未引用的孤儿文件。
    """
    f = src / "aliases.txt"
    if not f.exists():
        return [], []
    out: list[str] = []
    for raw in f.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out, []


def build_aliases(dir_name: str, name_txt: str,
                  extra_aliases: list[str] | None = None) -> tuple[list[str], list[str]]:
    """返回 (aliases, warnings)。

    去重必须**不区分大小写**。`name.txt` 第二行通常是英文名（如 `Kanami`），
    而目录名推导出的短名是小写（`kanami`）。若按大小写敏感去重，两者都会
    留下，别名索引里就出现"同一人格占用同一别名两次"，
    于是 `resolve("kanami")` 被判为歧义而**彻底失效** —— doctor 会报
    `alias.conflict`，但根因在迁移脚本这一行。
    """
    warn: list[str] = []
    aliases: list[str] = []
    seen: set[str] = set()
    lines = [x.strip() for x in (name_txt or "").splitlines() if x.strip()]
    cn = lines[0] if lines else ""
    en = lines[1] if len(lines) > 1 else ""

    def add(v: str) -> None:
        v = (v or "").strip()
        key = v.lower()
        if v and key not in seen:
            seen.add(key)
            aliases.append(v)

    add(cn)
    add(en)
    add(dir_name)
    add(dir_name.replace("pfm_", "", 1))
    # 目录名去掉系列前缀后的短名（如 strinova_kanami -> kanami）
    parts = dir_name.split("_")
    if len(parts) >= 3:
        add("_".join(parts[2:]))
    if not cn:
        warn.append("name.txt 为空，中文显示名回退为目录名")
    for a in (extra_aliases or []):
        add(a)
    for a in pinyin_aliases(cn):
        add(a)
    return aliases, warn


def migrate_one(dir_name: str, force: bool = False,
                refresh_aliases: bool = False) -> dict:
    src = OLD_SKILLS / dir_name
    pid = dir_name.replace("pfm_", "", 1)
    dst = PERSONAS_DIR / pid

    rec: dict = {"id": pid, "legacy_dir": dir_name, "src": str(src),
                 "dst": str(dst), "warnings": [], "copies": {}, "skipped": False}

    if not src.is_dir():
        rec["warnings"].append(f"源目录不存在: {src}")
        return rec
    if (dst / "persona.yaml").exists() and not force:
        # 已迁人格重算别名：只碰 aliases 一个字段，其余（含 W1.5 抽取出的
        # lore）原样保留 —— 用 --force 重建会把 lore.extracted 打回 false。
        if refresh_aliases:
            doc = PersonaDoc.load(dst)
            nt = src / "name.txt"
            name_txt = nt.read_text(encoding="utf-8") if nt.exists() else ""
            aliases, warn2 = build_aliases(dir_name, name_txt,
                                           read_aliases_txt(src)[0])
            rec["aliases_before"] = list(doc.aliases)
            rec["aliases"] = aliases
            rec["warnings"].extend(warn2)
            doc.aliases = aliases
            doc.save(dst)
            rec["refreshed_aliases"] = True
            return rec
        rec["skipped"] = True
        rec["warnings"].append("已存在 persona.yaml，跳过（--force 可覆盖）")
        return rec

    dst.mkdir(parents=True, exist_ok=True)
    content = dst / "content"

    # ---------- 1. 逐字节拷贝正文 ----------
    skill_md = src / "SKILL.md"
    if not skill_md.exists():
        rec["warnings"].append("缺少 SKILL.md，无法迁移")
        return rec
    rec["copies"]["skill"] = copy_verbatim(skill_md, content / "skill.md")

    optional = {
        "lore": ("lore.md", True),
        "dialog": ("dialog.md", True),
        "speech_raw": ("speech_patterns.txt", False),
    }
    refs: dict[str, str] = {"skill": "content/skill.md"}
    for key, (fname, required) in optional.items():
        f = src / fname
        if f.exists():
            rec["copies"][key] = copy_verbatim(f, content / fname)
            refs[key] = f"content/{fname}"
        elif required:
            rec["warnings"].append(f"缺少 {fname}（旧实现允许缺失，装配时跳过）")

    # ---------- 2. 档位文件（含命名分层）----------
    # 风格摘要：规范路径固定，即使尚未生成也先登记，
    # 这样 doctor 的 content.missing 能准确报出"还缺风格摘要"。
    refs["style_summary"] = "content/style_summary.md"

    tiers: dict[str, str] = {}
    for tier, old_name in (("intimate", "_tier1.md"), ("friend", "_tier2.md")):
        f = src / old_name
        if f.exists():
            new_name = f"tier.{tier}.md"
            rec["copies"][f"tier_{tier}"] = copy_verbatim(f, content / new_name)
            tiers[tier] = f"content/{new_name}"
        else:
            rec["warnings"].append(f"缺少 {old_name}（档位 {tier} 将不注入行为文案）")

    # ---------- 3. 推断身份外键（只此一次字符串嗅探）----------
    parts = dir_name.split("_")
    series = parts[1] if len(parts) >= 2 and parts[0] == "pfm" else ""
    if series in world_dirs():
        world = series
    else:
        world = ""
        if series:
            rec["warnings"].append(
                f"系列前缀 '{series}' 不是世界观目录 → world 置空"
                f"（旧代码会误判为 world={series}，导致世界观错配）")
    rec["inferred_series"] = series
    rec["world"] = world
    rec["world_confidence"] = "high" if world else "n/a"

    # world_role：读旧 _privilege_config.json 的 relation_labels
    role = ""
    pc = OLD_SKILLS / "_privilege_config.json"
    if pc.exists():
        try:
            cfg = json.loads(pc.read_text(encoding="utf-8"))
            role = (cfg.get("relation_labels") or {}).get(series, "") if series else ""
        except Exception as e:
            rec["warnings"].append(f"_privilege_config.json 解析失败: {e}")
    if not role:
        rec["warnings"].append("未取得 world_role（称谓），请人工确认")

    # ---------- 4. 元数据 ----------
    name_txt = ""
    nt = src / "name.txt"
    if nt.exists():
        name_txt = nt.read_text(encoding="utf-8")
        rec["copies"]["name_txt"] = copy_verbatim(nt, content / "name.txt")
        refs["name_txt"] = "content/name.txt"
    lines = [x.strip() for x in name_txt.splitlines() if x.strip()]
    display_name = lines[0] if lines else pid
    display_name_en = lines[1] if len(lines) > 1 else ""

    aliases, warn2 = build_aliases(dir_name, name_txt,
                                   read_aliases_txt(src)[0])
    rec["warnings"].extend(warn2)
    rec["aliases"] = aliases

    fm, body, outline = read_skill_file(skill_md)
    rec["frontmatter"] = fm
    rec["sections"] = outline.ids()
    rec["section_count"] = len(outline.all_nodes())
    rec["skill_body_bytes"] = len(body.encode("utf-8"))

    doc = PersonaDoc(
        id=pid,
        display_name=display_name,
        display_name_en=display_name_en,
        aliases=aliases,
        world=world,
        world_role=role,
        supersedes=SUPERSEDES.get(dir_name, []),
        legacy_dir=dir_name,
        skill_frontmatter=fm,
        default_inject="always",
        inject_overrides={},
        content=refs,
        tiers=tiers,
        lore=LoreData(),          # 抽取产物由 W1.5 单独写入
    )
    doc.save(dst)
    rec["persona_yaml"] = str(dst / "persona.yaml")
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="覆盖已存在的 persona.yaml")
    ap.add_argument("--only", default="", help="只迁移指定目录名（逗号分隔）")
    ap.add_argument("--refresh-aliases", action="store_true",
                    help="已迁人格只重算 aliases（保留 W1.5 抽取结果，不重拷正文）")
    ap.add_argument("--old-skills", default="", help="旧环境技能树路径")
    a = ap.parse_args()

    if a.old_skills:
        global OLD_SKILLS
        OLD_SKILLS = Path(a.old_skills)

    targets = [x.strip() for x in a.only.split(",") if x.strip()] or PERSONAS
    PERSONAS_DIR.mkdir(parents=True, exist_ok=True)

    report = {"old_skills": str(OLD_SKILLS), "new_root": str(NEW_ROOT),
              "personas": {}, "verbatim_failures": [], "warnings": []}
    for d in targets:
        rec = migrate_one(d, force=a.force, refresh_aliases=a.refresh_aliases)
        report["personas"][rec["id"]] = rec
        for k, c in rec["copies"].items():
            if not c.get("identical"):
                report["verbatim_failures"].append(f"{rec['id']}:{k}")
        for w in rec["warnings"]:
            report["warnings"].append(f"{rec['id']}: {w}")
        if rec.get("refreshed_aliases"):
            flag = "ALIA"
            extra = f"aliases {rec.get('aliases_before')} -> {rec.get('aliases')}"
        else:
            flag = "SKIP" if rec["skipped"] else "OK  "
            extra = (f"world={rec.get('world',''):9} "
                     f"sections={rec.get('section_count','-')} "
                     f"copies={len(rec['copies'])}")
        print(f"[{flag}] {rec['id']:26} {extra}")
        for w in rec["warnings"]:
            print(f"        ! {w}")

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                      encoding="utf-8")

    print(f"\n迁移 {len(report['personas'])} 个人格 → {PERSONAS_DIR}")
    print(f"逐字节校验失败: {len(report['verbatim_failures'])}")
    print(f"待人工确认项: {len(report['warnings'])}")
    print(f"报告: {REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
