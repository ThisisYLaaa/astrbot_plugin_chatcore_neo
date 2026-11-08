# -*- coding: utf-8 -*-
"""人格结构体检：`python migrations/check_persona_structure.py [--world sr] [--id gi_hutao] [--strict]`

只读，不改任何文件。检查「结构对不对」，不判断文笔好坏。

判定项（每条都能落到具体文件/字段，便于队友修）
------------------------------------------------
A 布局：`personas/<World>/<角色人格>/`，目录名不得带 `{world}_` 前缀
B persona.yaml：schema/id/display_name/aliases/world/world_role/
  skill.frontmatter/content 引用/relationship.tiers/lore 抽取产物
C 文件：content/{skill,lore,dialog,speech_patterns,style_summary,name.txt,
  tier.friend,tier.intimate}.md 都在，且体量过线（不是空壳）
D 大纲：skill.md 必须能解析出框架要求的章节 id
E 一致性：id 前缀 = world 代码；folder 名 ∈ id 去前缀；别名里含 id；
  name.txt 有中英文两行
F 全局：id 不重复（跨世界观也不能撞）

退出码：0 全过；1 有 error；2 有 warn 且 --strict。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from core.paths import PERSONAS_DIR, world_for_dir  # noqa: E402
from core.persona.model import PersonaDoc, iter_persona_dirs  # noqa: E402

# 每个游戏都从这几章起步（章节 id 由 core/persona/registry.py 解析）
REQUIRED_SECTIONS = (
    "core_setting", "identity", "personality", "catchphrases", "extra_style",
    "initial_state", "emotion_map", "time_map", "notice", "role_rules",
    "behavior_tendency",
)
REQUIRED_CONTENT = ("skill", "lore", "dialog", "speech_raw", "style_summary",
                    "name_txt", "appearance")
REQUIRED_TIERS = ("intimate", "friend")

# 外貌文件固定五字段（frameworks/测试按机器可校验的方式检查，别写自由散文）
APPEARANCE_FIELDS = ("发色：", "发型：", "瞳色：", "服饰：", "体征：")

# 人格文件里出现即算错误：旧模板样板 + 把场景钉死在"联网刷群"的措辞
# （tests/test_framework_fidelity.py 在锁这两类）
BANNED_PHRASES = ("qq消息", "QQ消息", "刷QQ", "正在QQ群", "QQ群聊中和群友",
                  "read_persona_skill", "框架引用")

# content.<key> 指向的文件名必须含这个片段（防复制粘贴时把 style_summary 指到 skill.md）
EXPECTED_BASENAME = {
    "skill": "skill",
    "lore": "lore",
    "dialog": "dialog",
    "speech_raw": "speech",
    "style_summary": "style",
    "name_txt": "name",
    "appearance": "appearance",
}

# 体量下限（字节）——低于这个数基本是空壳/占位
MIN_BYTES = {
    "content/skill.md": 4000,
    "content/lore.md": 1500,
    "content/dialog.md": 1000,
    "content/speech_patterns.txt": 800,
    "content/style_summary.md": 300,
    "content/appearance.md": 120,
}
MIN_TIER_BYTES = 400
PREFIXES = ("sr_", "zzz_", "gi_", "arke_", "nte_", "strinova_", "diy_")


@dataclass
class Row:
    rel: str
    pid: str = ""
    world: str = ""
    aliases: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warns: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _check_one(pdir: Path) -> Row:
    rel = pdir.relative_to(PERSONAS_DIR).as_posix()
    row = Row(rel=rel)
    yml = pdir / "persona.yaml"
    try:
        raw = yaml.safe_load(yml.read_text(encoding="utf-8-sig")) or {}
    except Exception as e:
        row.errors.append(f"persona.yaml 解析失败: {e!r}")
        return row

    row.pid = str(raw.get("id") or "")
    row.world = str(raw.get("world") or "")
    row.aliases = [str(a).strip() for a in (raw.get("aliases") or [])
                   if str(a).strip()]
    folder = pdir.name

    # ---- A 布局 ----
    if pdir.parent == PERSONAS_DIR:
        row.errors.append("仍是扁平布局（应放在 personas/<World>/ 下）")
    for pref in PREFIXES:
        if folder.lower().startswith(pref):
            row.errors.append(f"目录名仍带前缀 {pref!r}（应去掉）")
            break
    wdir = pdir.parent.name
    expect_world = world_for_dir(wdir)
    if row.world and expect_world != row.world:
        row.warns.append(f"目录 {wdir} 与 world='{row.world}' 不对应"
                         f"（应放 {world_for_dir(row.world)}/）")

    # ---- B persona.yaml ----
    if raw.get("schema") != 1:
        row.errors.append(f"schema 应为 1，实际 {raw.get('schema')!r}")
    if not row.pid:
        row.errors.append("缺 id")
    elif row.world and not row.pid.lower().startswith(row.world.lower() + "_"):
        row.errors.append(f"id='{row.pid}' 未带 world 前缀 '{row.world}_'")
    if not str(raw.get("display_name") or "").strip():
        row.errors.append("缺 display_name")
    aliases = [str(a).strip() for a in (raw.get("aliases") or []) if str(a).strip()]
    if len(aliases) < 4:
        row.warns.append(f"aliases 只有 {len(aliases)} 条（建议 >=6：中/英/拼音/外号/id）")
    if row.pid and row.pid not in aliases:
        row.errors.append("aliases 里没有 id 本身（解析不到就白写）")
    disp = str(raw.get("display_name") or "").strip()
    if disp and disp not in aliases:
        row.errors.append("aliases 里没有 display_name（按名字叫不出来）")
    dup_alias = {a for a in aliases if [x.lower() for x in aliases].count(a.lower()) > 1}
    if dup_alias:
        row.errors.append("aliases 内部重复: " + "、".join(sorted(dup_alias)))
    if not row.world:
        row.warns.append("world 为空（不会注入世界观提示）")
    if not str(raw.get("world_role") or "").strip():
        row.warns.append("缺 world_role（该世界观对玩家的称谓）")

    skill_fm = (raw.get("skill") or {}).get("frontmatter") or {}
    if not str(skill_fm.get("name") or "").strip():
        row.errors.append("skill.frontmatter.name 为空")
    elif not str(skill_fm["name"]).endswith("_persona"):
        row.warns.append(f"skill.frontmatter.name 建议以 _persona 结尾（现 {skill_fm['name']!r}）")
    if len(str(skill_fm.get("description") or "")) < 20:
        row.warns.append("skill.frontmatter.description 太短")

    content = raw.get("content") or {}
    for key in REQUIRED_CONTENT:
        # speech_raw 在框架模型里是可选键，缺了只算警告；
        # 其余正文/来源文件缺失一律算错误。
        bucket = row.warns if key == "speech_raw" else row.errors
        if not str(content.get(key) or "").strip():
            bucket.append(f"content.{key} 未声明")
        elif not (pdir / content[key]).exists():
            bucket.append(f"content.{key} 指向的文件不存在: {content[key]}")
    tiers = (raw.get("relationship") or {}).get("tiers") or {}
    for t in REQUIRED_TIERS:
        if not str(tiers.get(t) or "").strip():
            row.errors.append(f"relationship.tiers.{t} 未声明")
        elif not (pdir / tiers[t]).exists():
            row.errors.append(f"档位文件不存在: {tiers[t]}")

    lore = raw.get("lore") or {}
    if not lore.get("extracted"):
        row.warns.append("lore.extracted 不是 true")
    for k in ("origin", "motivation"):
        if len(str(lore.get(k) or "")) < 20:
            row.warns.append(f"lore.{k} 太短（<=20 字）")
    if len(lore.get("signature_lines") or []) < 3:
        row.warns.append("lore.signature_lines 少于 3 条（官方台词示例）")
    if len(lore.get("relations") or []) < 3:
        row.warns.append("lore.relations 少于 3 条")
    if len(lore.get("timeline") or []) < 2:
        row.warns.append("lore.timeline 少于 2 条")
    if len(lore.get("themes") or []) < 2:
        row.warns.append("lore.themes 少于 2 条")

    # ---- C 文件体量 ----
    for rel_file, minimum in MIN_BYTES.items():
        p = pdir / rel_file
        if not p.exists():
            (row.warns if "speech" in rel_file else row.errors).append(
                f"缺文件 {rel_file}")
        elif p.stat().st_size < minimum:
            row.warns.append(f"{rel_file} 只有 {p.stat().st_size} B（建议 >= {minimum}）")
    for t in REQUIRED_TIERS:
        rel_t = tiers.get(t)
        if rel_t and (pdir / rel_t).exists():
            if (pdir / rel_t).stat().st_size < MIN_TIER_BYTES:
                row.warns.append(f"{rel_t} 太短（建议 >= {MIN_TIER_BYTES} B）")
    name_txt = pdir / str(content.get("name_txt") or "content/name.txt")
    if name_txt.exists():
        lines = [l.strip() for l in name_txt.read_text(
            encoding="utf-8-sig").splitlines() if l.strip()]
        if len(lines) < 2:
            row.warns.append("name.txt 建议两行：中文名 / 英文名")

    # ---- D 大纲（框架注册表口径）----
    try:
        doc = PersonaDoc.load(pdir)
        nodes = doc.outline.all_nodes() if doc.outline else []
        ids = [n.section_id for n in nodes]
        missing = [s for s in REQUIRED_SECTIONS if s not in set(ids)]
        if missing:
            row.errors.append("skill.md 缺章节: " + ", ".join(missing))
        dup_ids = {i for i in ids if ids.count(i) > 1}
        if dup_ids:
            row.errors.append("章节 id 重复（同一标题写了两遍）: "
                              + "、".join(sorted(dup_ids)))
        unknown = [n.title for n in nodes if n.spec is None and n.level >= 2]
        if unknown:
            row.errors.append(
                "有未注册章节（自定义内容请写 content/sections/*.md 再挂 extra_blocks）: "
                + "、".join(unknown))
        if doc.loaded_errors:
            row.errors.extend(doc.loaded_errors)
    except Exception as e:
        row.errors.append(f"PersonaDoc.load 失败: {e!r}")

    # ---- E 外貌五字段 ----
    app_path = pdir / str(content.get("appearance") or "content/appearance.md")
    if app_path.exists():
        txt = app_path.read_text(encoding="utf-8-sig", errors="replace")
        lack = [f for f in APPEARANCE_FIELDS if f not in txt]
        if lack:
            row.errors.append("appearance.md 缺字段: " + "、".join(lack))

    # ---- F 禁用措辞（数据侧锁死；.md/.txt/.yaml 全扫，校验器旧版只扫 .md）----
    for p in pdir.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in (".md", ".txt", ".yaml", ".yml"):
            continue
        t = p.read_text(encoding="utf-8-sig", errors="replace")
        hits = [b for b in BANNED_PHRASES if b in t]
        if hits:
            row.errors.append(f"{p.relative_to(pdir).as_posix()} 含禁用措辞: "
                              + "、".join(hits))

    # ---- G 引用「指向对不对」（旧版只验存在，查不出 style_summary→skill.md）----
    for key, rel in content.items():
        rel_s = str(rel or "").strip()
        if not rel_s:
            continue
        base = Path(rel_s).name.lower()
        want = EXPECTED_BASENAME.get(key)
        if want and want not in base:
            row.errors.append(
                f"content.{key} 指向了不像它的文件: {rel_s}（期望文件名含 {want}）")

    # ---- H 档位方向（friend=等级2/普通，intimate=等级1/特权）不能写反 ----
    for tier, hints in (("friend", ("等级 2", "等级2", "普通")),
                        ("intimate", ("等级 1", "等级1", "特权"))):
        rel_t = str(tiers.get(tier) or "").strip()
        if not rel_t or not (pdir / rel_t).exists():
            continue
        t = (pdir / rel_t).read_text(encoding="utf-8-sig", errors="replace")[:800]
        if not any(h in t for h in hints):
            row.warns.append(f"{rel_t} 里找不到「{'/'.join(hints)}」字样"
                             f"——档位可能写反或没按框架口径写")
    return row


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    want_world = ""
    want_id = ""
    strict = "--strict" in argv
    for i, a in enumerate(argv):
        if a == "--world" and i + 1 < len(argv):
            want_world = argv[i + 1]
        if a == "--id" and i + 1 < len(argv):
            want_id = argv[i + 1]

    dirs = iter_persona_dirs(PERSONAS_DIR)
    all_rows: list[Row] = []
    transient = 0
    for d in dirs:
        # 多人在同一棵树上写时，别人正写到一半的文件会短暂 PermissionError /
        # 半截 YAML。这类不算错误，直接跳过（下次再跑就有了），否则一个人
        # 的写入会连累所有人的自检。
        try:
            all_rows.append(_check_one(d))
        except PermissionError:
            transient += 1
        except Exception as e:
            row = Row(rel=d.relative_to(PERSONAS_DIR).as_posix())
            row.errors.append(f"检查失败: {e!r}")
            all_rows.append(row)

    # ---- 全局：id 重复 + 别名跨人格冲突（按全池算，--id 也查得到）----
    by_alias: dict[str, set[str]] = {}
    for r in all_rows:
        if r.pid:
            for a in r.aliases:
                by_alias.setdefault(a.lower(), set()).add(r.pid)
    id_owner: dict[str, str] = {}
    for r in all_rows:
        if not r.pid:
            continue
        if r.pid in id_owner:
            r.errors.append(f"id 重复：{id_owner[r.pid]} 与 {r.rel}")
        else:
            id_owner[r.pid] = r.rel
        clash = sorted(a for a in {x.lower() for x in r.aliases}
                       if len(by_alias.get(a, ())) > 1)
        if clash:
            others = sorted({p for a in clash for p in by_alias[a]} - {r.pid})
            r.errors.append("别名与其它人格冲突: " + "、".join(clash)
                            + "（对方: " + "、".join(others) + "）")

    rows = [r for r in all_rows
            if (not want_world or r.world == want_world
                or world_for_dir(PERSONAS_DIR.joinpath(*r.rel.split("/")).parent.name) == want_world)
            and (not want_id or r.pid == want_id)]

    n_err = sum(1 for r in rows if r.errors)
    n_warn = sum(1 for r in rows if r.warns)
    for r in rows:
        flag = "[ERR] " if r.errors else ("[WARN]" if r.warns else "[OK]  ")
        line = f"{flag} {r.rel}  [{r.pid} / world={r.world or '-'}]"
        if r.errors:
            line += "\n     E " + "\n     E ".join(r.errors)
        if r.warns:
            line += "\n     W " + "\n     W ".join(r.warns)
        print(line)
    extra = f"（跳过 {transient} 个正在写入的）" if transient else ""
    print(f"\n共 {len(rows)} 个：错误 {n_err} 个，仅警告 {n_warn} 个{extra}")
    if n_err:
        return 1
    return 2 if (strict and n_warn) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
