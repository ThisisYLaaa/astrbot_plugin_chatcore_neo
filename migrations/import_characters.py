# -*- coding: utf-8 -*-
"""把旧 `astrbot_plugin_strinova_news` 的角色外号库导入 chatcore_neo。

数据对应
--------
旧：`<旧根>/plugin_data/astrbot_plugin_strinova_news/roles.json`
     `{角色中文名: [外号, ...]}`
新：`<新根>/plugin_data/chatcore_neo/characters.json`
     **同构**（见 core/characters.py），所以只需并集合并，不需要格式转换。

合并语义（幂等，可反复跑）
--------------------------
* 旧库里有的角色、新库里没有 → 新建该键
* 旧库里的外号 → 并进该角色的外号列表（**只加不删**，不动用户后来自己加/留的）
* 过滤空项、以及"外号 == 角色名"这种无意义项（匹配时角色名本来就会命中）

注意事项
--------
* 旧库里有**同一个外号挂在多个角色上**的情况（如 奥黛丽/汐 都是「盾构」，
  香奈美/心夏 都是「大狙」）。匹配层对同一关键词会把命中的角色**全部**列出，
  再由 `char_mention_max_hits` 截断 —— 这是有意保留的旧语义。
* 旧外号里有不少日常词（香水 / 卡牌 / 颜料 / 蜗牛 / 大运 …），
  在普通聊天里会误命中。这是社区外号的固有代价，导入后在管理页「角色」
  按需删改即可。

用法
----
    python migrations/import_characters.py            # 预检（不写盘）
    python migrations/import_characters.py --apply    # 真正写入
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))
from core.characters import CharacterStore  # noqa: E402
from core.paths import CHARACTERS_JSON, LEGACY_DATA_DIR  # noqa: E402
import os

REPORT = Path(__file__).resolve().parent / "characters_import_report.json"

# 旧环境数据根的候选（目录被重命名过：.astrbot → obslete-.astrbot）
_OLD_CANDIDATES = (
    LEGACY_DATA_DIR,
    Path(os.environ.get("CHATCORE_OLD_DATA") or "_请设置环境变量_CHATCORE_OLD_DATA_"),
)
_PLUGIN_SUBPATH = Path("plugin_data") / "astrbot_plugin_strinova_news" / "roles.json"


def find_old_roles(explicit: str = "") -> Path:
    """定位旧 roles.json：显式路径优先，否则按候选目录探测。"""
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else p / _PLUGIN_SUBPATH
    for root in _OLD_CANDIDATES:
        p = Path(root) / _PLUGIN_SUBPATH
        if p.is_file():
            return p
    return Path(_OLD_CANDIDATES[0]) / _PLUGIN_SUBPATH


def load_old(path: Path) -> dict[str, list[str]]:
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise ValueError(f"roles.json 结构不对（应为对象）: {path}")
    out: dict[str, list[str]] = {}
    for k, v in raw.items():
        name = str(k).strip()
        if not name:
            continue
        out[name] = ([str(a).strip() for a in v if str(a).strip()]
                     if isinstance(v, list) else [])
    return out


def merge(old: dict[str, list[str]], cur: dict[str, list[str]]) -> dict:
    """并集合并，返回变更明细（不落盘）。"""
    new_chars: list[str] = []
    added: dict[str, list[str]] = {}
    already: dict[str, list[str]] = {}
    dropped: list[str] = []
    for name, aliases in old.items():
        if name not in cur:
            cur[name] = []
            new_chars.append(name)
        for a in aliases:
            if a == name:
                dropped.append(f"{name}:{a}（外号=角色名）")
                continue
            if a in cur[name]:
                already.setdefault(name, []).append(a)
                continue
            cur[name].append(a)
            added.setdefault(name, []).append(a)
    return {"new_characters": new_chars, "added_aliases": added,
            "already_present": already, "dropped": dropped}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", default="", help="旧 roles.json 路径（默认自动探测）")
    ap.add_argument("--new", default=str(CHARACTERS_JSON), help="目标 characters.json")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只预检）")
    a = ap.parse_args()

    old_path = find_old_roles(a.old)
    if not old_path.is_file():
        print(f"[ERR ] 找不到旧外号库: {old_path}")
        return 1
    old = load_old(old_path)

    store = CharacterStore(Path(a.new))
    cur = store.load()
    if not cur:
        print(f"[WARN] 目标库为空（{a.new}）—— 先启动一次插件补齐键位再跑本脚本更稳")
    before_chars, before_alias = len(cur), sum(len(v) for v in cur.values())

    diff = merge(old, cur)
    after_alias = sum(len(v) for v in cur.values())

    print(f"[读取] 旧库 {old_path}")
    print(f"       角色 {len(old)} 个 / 外号 {sum(len(v) for v in old.values())} 条")
    print(f"[目标] {a.new}")
    print(f"       角色 {before_chars} → {len(cur)}（+{len(diff['new_characters'])}）")
    print(f"       外号 {before_alias} → {after_alias}"
          f"（+{after_alias - before_alias}）")
    for name in diff["new_characters"]:
        print(f"  + 新角色 {name}")
    for name, al in diff["added_aliases"].items():
        print(f"  + {name}：{'、'.join(al)}")
    for name, al in diff["already_present"].items():
        print(f"  = {name}：{'、'.join(al)}（已存在）")
    for d in diff["dropped"]:
        print(f"  - 跳过 {d}")

    if not a.apply:
        print("\n[预检] 未写盘。加 --apply 真正写入。")
        return 0

    store.save(cur)
    REPORT.write_text(json.dumps({
        "source": str(old_path), "target": str(a.new),
        "old_characters": len(old),
        "old_aliases": sum(len(v) for v in old.values()),
        "new_characters": diff["new_characters"],
        "added_aliases": diff["added_aliases"],
        "already_present": diff["already_present"],
        "dropped": diff["dropped"],
        "total_characters_after": len(cur),
        "total_aliases_after": after_alias,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[写入] 完成，报告：{REPORT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
