# -*- coding: utf-8 -*-
"""角色名识别与"角色卡"注入。

做什么
------
把用户消息里出现的**角色名**（本名 / 英文名 / 外号）用**纯字符串匹配**认出来，
命中后给出一张很短的角色卡（本名、英文名、外号、一句话身份，以及"与当前人格
的关系"）。命中结果由装配器走 `extra_user_content_parts` + `mark_as_temp()`
注入，因此**只会影响本轮**，不会落进聊天记录、不会污染 system_prompt 前缀。

为什么值得做
------------
角色扮演里最常见的翻车是"AI 说不认识某个剧情人物"：人格本身只认识自己
关系表里写过的人，而世界观资料（`_worlds/`）是检索式的，模型不一定会主动去查。
这里把"消息里明确提到了谁"变成一次确定性的查表，把答案直接递到模型手上。

数据在哪
--------
* `characters.json`（本插件数据根）：`{角色中文名: [外号, ...]}`
  —— 与旧 `astrbot_plugin_strinova_news` 的 `roles.json` **同构**，可直接搬过来。
  外号由用户维护（命令 / WebUI），首次加载按人格与默认名册补齐键位。
* 角色资料不另存一份：
  - 能在 `personas/<id>/` 里找到的（本名/英文名/来历），直接取现成的；
  - 找不到的（非人格角色，如 米雪儿·李），从 `_worlds/**/*.md` 的
    「关键人物」表/条目里捞一行身份描述。
  这样不存在"两份数据互相漂移"的老问题。

匹配规则（纯逻辑，无 LLM、无向量）
----------------------------------
* 大小写不敏感（英文名 `Yvette` / `yvette` 都算）。
* 关键词长度 ≥ `min_len`（默认 2）时按子串匹配。
* 长度 1 的名称（明/信/令/汐）默认**不参与匹配** —— 它们在汉语里太常见
  （"我明白了""相信"），否则每条消息都会误触发。若把 `min_len` 调成 1，
  单字名改用"左右不挨汉字"的边界规则匹配。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .paths import WORLDS_DIR

# ---------------------------------------------------------------- 默认名册

# 卡拉彼丘角色名（沿用旧 astrbot_plugin_strinova_news 的 DEFAULT_ROLES）。
# 只是"首次建库的键位"，不覆盖用户已填的外号。
DEFAULT_CHARACTERS: tuple[str, ...] = (
    "艾卡", "奥黛丽", "白墨", "星绘", "汐", "芙拉薇娅", "珐格兰丝", "绯莎",
    "加拉蒂亚", "香奈美", "心夏", "拉薇", "蕾欧娜", "玛德蕾娜", "玛拉",
    "梅瑞狄斯", "米雪儿·李", "明", "信", "诺诺", "千代", "令", "忧雾", "伊薇特",
)

# 别名里的目录/拼音 slug（pfm_strinova_yvette / strinova_yvette）不进匹配索引：
# 它们不可能出现在聊天里，留着只会把索引撑大、并在 WebUI 里刷屏。
_SLUG = re.compile(r"[_/\\]")
_TABLE_ROW = re.compile(r"^\|(.+)\|\s*$")
_BULLET_PERSON = re.compile(r"^[-*]\s*\*\*(.+?)\*\*\s*[：:]\s*(.+)$")
_SPLIT_NAMES = re.compile(r"[/／、,，]")
_CJK = "\u4e00-\u9fff"
# 单字名：左右都不挨汉字才算命中（"明 在吗"命中，"我明白了"不命中）
_ONE_CHAR_TMPL = r"(?<![{c}]){{kw}}(?![{c}])".format(c=_CJK)


def _one_char_in(kw: str, text: str) -> bool:
    """单字关键词的边界匹配（左右不能是汉字）。"""
    return re.search(_ONE_CHAR_TMPL.format(kw=re.escape(kw)), text) is not None


def _atomic_write(path: Path, data: Any) -> None:
    """原子写 JSON，避免写入中断把外号库写坏。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- 存储层


class CharacterStore:
    """角色外号库：`{角色中文名: [外号, ...]}`。

    接口刻意与旧 `astrbot_plugin_strinova_news.storage.Storage` 的角色部分保持一致
    （`add_alias` / `delete_alias` / `set_aliases` / `parse_aliases`），
    这样旧 `roles.json` 可以直接改名搬过来，行为完全一样。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # ---------- 读写 ----------
    def load(self) -> dict[str, list[str]]:
        data: dict[str, list[str]] = {}
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        key = str(k).strip()
                        if not key:
                            continue
                        data[key] = ([str(a).strip() for a in v if str(a).strip()]
                                     if isinstance(v, list) else [])
        except Exception:
            return {}
        return data

    def save(self, roles: dict[str, list[str]]) -> None:
        _atomic_write(self.path, roles)

    # ---------- 初始化 ----------
    def ensure(self, names: Iterable[str]) -> int:
        """补齐缺失的角色键（**不覆盖**已有外号），返回新增键数。"""
        roles = self.load()
        added = 0
        for name in names:
            key = str(name or "").strip()
            if key and key not in roles:
                roles[key] = []
                added += 1
        if added:
            self.save(roles)
        return added

    # ---------- 编辑 ----------
    @staticmethod
    def parse_aliases(raw: str) -> list[str]:
        """逗号/顿号/分号/竖线/换行分隔 → 去重列表（与旧实现同规则）。"""
        if not raw:
            return []
        out: list[str] = []
        for part in re.split(r"[,，、;；|\n]+", str(raw)):
            v = str(part).strip()
            if v and v not in out:
                out.append(v)
        return out

    def add_character(self, name: str) -> bool:
        name = str(name or "").strip()
        if not name:
            return False
        roles = self.load()
        if name in roles:
            return False
        roles[name] = []
        self.save(roles)
        return True

    def delete_character(self, name: str) -> bool:
        name = str(name or "").strip()
        roles = self.load()
        if not name or name not in roles:
            return False
        del roles[name]
        self.save(roles)
        return True

    def set_aliases(self, name: str, raw: str) -> bool:
        """整体覆盖某角色的外号列表（过滤空项与"外号=角色名"）。"""
        name = str(name or "").strip()
        if not name:
            return False
        roles = self.load()
        if name not in roles:
            return False
        roles[name] = [a for a in self.parse_aliases(raw) if a != name]
        self.save(roles)
        return True

    def add_alias(self, name: str, alias: str) -> bool:
        name = str(name or "").strip()
        alias = str(alias or "").strip()
        roles = self.load()
        if not name or not alias or name not in roles:
            return False
        if alias == name or alias in roles[name]:
            return False
        roles[name].append(alias)
        self.save(roles)
        return True

    def delete_alias(self, name: str, alias: str) -> bool:
        name = str(name or "").strip()
        alias = str(alias or "").strip()
        roles = self.load()
        if not name or name not in roles or alias not in roles[name]:
            return False
        roles[name].remove(alias)
        self.save(roles)
        return True


# ---------------------------------------------------------------- 命中结果


@dataclass
class CharacterHit:
    """一个被提及的角色。字段都是"够用就好"的短信息。"""

    name: str
    en: str = ""
    aliases: list[str] = field(default_factory=list)
    brief: str = ""
    relation: str = ""
    world: str = ""
    persona_id: str = ""

    def line(self, brief_chars: int = 60) -> str:
        """渲染成一行角色卡。"""
        head = self.name
        if self.en and self.en != self.name:
            head += f"（{self.en}）"
        parts: list[str] = []
        brief = _cut(self.brief, brief_chars)
        if brief:
            parts.append(brief)
        if self.aliases:
            parts.append("别名：" + "、".join(self.aliases))
        text = f"- {head}：" + "；".join(parts) if parts else f"- {head}"
        if self.relation:
            text += "；与当前人格的关系：" + _cut(self.relation, brief_chars)
        return text


def _cut(text: str, limit: int) -> str:
    """按字符数截断（带省略号），limit<=0 表示不截断。"""
    s = (text or "").strip()
    if limit and limit > 0 and len(s) > limit:
        return s[:limit].rstrip() + "…"
    return s


# ---------------------------------------------------------------- 匹配与渲染


class CharacterMatcher:
    """把"消息 → 命中角色 → 角色卡"串起来的纯逻辑组件。

    `personas` 传一个 PersonaDoc 的可迭代对象（人或服务都行，鸭子类型）：
    只用到 `.id/.display_name/.display_name_en/.aliases/.world/.lore`。
    """

    def __init__(self, store: CharacterStore, personas: Iterable[Any] | None = None,
                 worlds_dir: Path | None = None) -> None:
        self.store = store
        self.personas = list(personas or [])
        self.worlds_dir = Path(worlds_dir) if worlds_dir else WORLDS_DIR
        self._roles: dict[str, list[str]] = {}
        self._by_name: dict[str, Any] = {}          # 中文名 -> PersonaDoc
        self._world_briefs: dict[str, str] = {}
        self.reload()

    # ---------- 索引 ----------
    def reload(self, personas: Iterable[Any] | None = None) -> None:
        if personas is not None:
            self.personas = list(personas)
        self._roles = self.store.load()
        self._by_name = {}
        for doc in self.personas:
            for key in (getattr(doc, "display_name", ""), ):
                if key:
                    self._by_name.setdefault(str(key).strip(), doc)
        self._world_briefs = _scan_world_briefs(self.worlds_dir)

    def _persona_of(self, name: str) -> Any | None:
        return self._by_name.get(name)

    def keywords(self) -> list[tuple[str, str]]:
        """(关键词, 角色中文名) 列表，长关键词在前（更具体的先命中）。"""
        pairs: dict[tuple[str, str], None] = {}
        for name, aliases in self._roles.items():
            if name:
                pairs[(name, name)] = None
            for a in aliases:
                if a:
                    pairs[(a, name)] = None
        for doc in self.personas:
            disp = str(getattr(doc, "display_name", "") or "").strip()
            if not disp:
                continue
            pairs[(disp, disp)] = None
            en = str(getattr(doc, "display_name_en", "") or "").strip()
            if en:
                pairs[(en, disp)] = None
            for a in (getattr(doc, "aliases", None) or []):
                a = str(a or "").strip()
                if a and not _SLUG.search(a):
                    pairs[(a, disp)] = None
        out = [(kw, nm) for (kw, nm) in pairs]
        out.sort(key=lambda p: -len(p[0]))
        return out

    # ---------- 匹配 ----------
    def match(self, text: str, *, min_len: int = 2,
              skip_names: Iterable[str] = ()) -> list[str]:
        """返回命中的角色中文名（去重，按关键词"具体程度"排序）。"""
        raw = str(text or "")
        if not raw.strip():
            return []
        low = raw.lower()
        skip = {str(s).strip() for s in skip_names if str(s).strip()}
        hits: list[str] = []
        for kw, name in self.keywords():
            if name in skip or name in hits:
                continue
            klen = len(kw)
            if klen < max(1, int(min_len or 1)):
                continue
            k = kw.lower()
            if klen >= 2:
                if k in low:
                    hits.append(name)
            else:
                # 单字名：左右不挨汉字才算命中
                if _one_char_in(k, low):
                    hits.append(name)
        return hits

    # ---------- 卡片 ----------
    def hit(self, name: str, doc: Any | None = None) -> CharacterHit:
        name = str(name or "").strip()
        roles = self._roles
        persona = self._persona_of(name)
        en = str(getattr(persona, "display_name_en", "") or "") if persona else ""
        world = str(getattr(persona, "world", "") or "") if persona else ""
        brief = self._persona_brief(persona) or self._world_briefs.get(name, "")
        return CharacterHit(
            name=name,
            en=en,
            aliases=list(roles.get(name) or []),
            brief=brief,
            relation=self._relation_with(doc, name, persona),
            world=world,
            persona_id=str(getattr(persona, "id", "") or "") if persona else "",
        )

    @staticmethod
    def _persona_brief(persona: Any | None) -> str:
        """人格自带的来历（lore.origin）优先 —— 它是为扮演写过的，最贴切。"""
        if persona is None:
            return ""
        lore = getattr(persona, "lore", None)
        if lore is None:
            return ""
        for attr in ("origin", "motivation"):
            v = str(getattr(lore, attr, "") or "").strip()
            if v:
                return v
        return ""

    @staticmethod
    def _relation_with(doc: Any | None, name: str, persona: Any | None) -> str:
        """当前人格与这个角色的关系（取自 lore.relations）。

        只在"确实写了"的时候返回；没有就留空，绝不编。
        """
        if doc is None:
            return ""
        lore = getattr(doc, "lore", None)
        rels = list(getattr(lore, "relations", None) or [])
        pname = str(getattr(persona, "display_name", "") or "") if persona else ""
        for r in rels:
            who = str((r or {}).get("who") or "").strip()
            if not who:
                continue
            # 双向包含："明（明姐）" 命中 "明"；"伊薇特" 命中 "伊薇特（玩家）"
            if who == name or name in who or (pname and pname in who) or who in name:
                rel = str((r or {}).get("rel") or "").strip()
                if rel:
                    return rel
        return ""

    def entries(self) -> list[CharacterHit]:
        """全部角色（WebUI / 命令展示用）。"""
        return [self.hit(name) for name in sorted(self._roles)]

    # ---------- 每轮注入块 ----------
    def render_block(self, text: str, *, doc: Any | None = None,
                     min_len: int = 2, max_hits: int = 3,
                     brief_chars: int = 60, max_chars: int = 240) -> str:
        """渲染本轮要注入的**正文**（标题由装配器加，与其它动态分区一致）。

        没有任何命中、或全部命中都指向当前人格自己时，返回空串 ——
        不注入才是默认状态。
        """
        skip: set[str] = set()
        if doc is not None:
            # 当前人格自己不必自我介绍
            for key in (str(getattr(doc, "display_name", "") or "").strip(),
                        str(getattr(doc, "id", "") or "").strip()):
                if key:
                    skip.add(key)
        names = self.match(text, min_len=min_len, skip_names=skip)
        if not names:
            return ""
        hits = [self.hit(n, doc) for n in names[:max(1, int(max_hits or 1))]]
        lines = [h.line(brief_chars) for h in hits]
        lines = [ln for ln in lines if ln.strip()]
        if not lines:
            return ""
        body = "\n".join(lines)
        limit = int(max_chars or 0)
        if limit > 0 and len(body) > limit:
            body = body[:limit].rstrip() + "…"
        return body


# ---------------------------------------------------------------- 世界观扫读


def _scan_world_briefs(worlds_dir: Path) -> dict[str, str]:
    """从 `_worlds/**/*.md` 里扫出「名字 -> 一句话身份」。

    只认两种写法（都是 `_worlds/` 里实际存在的）：
      * 表格行 `| 姓名 | 身份职责 |`
      * 条目行 `- **姓名**：说明`（`A／B` 会拆成多个名字共用同一说明）

    纯字典查表，不调模型；文件不存在就当没有。
    """
    out: dict[str, str] = {}
    root = Path(worlds_dir)
    if not root.is_dir():
        return out
    for path in sorted(root.rglob("*.md")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        for line in text.splitlines():
            s = line.strip()
            if not s:
                continue
            m = _BULLET_PERSON.match(s)
            if m:
                desc = m.group(2).strip()
                for raw_name in _SPLIT_NAMES.split(m.group(1)):
                    name = raw_name.strip().strip("*")
                    if name and desc:
                        out.setdefault(name, desc)
                continue
            m = _TABLE_ROW.match(s)
            if m:
                cells = [c.strip() for c in m.group(1).split("|")]
                if len(cells) >= 2:
                    name = cells[0].strip().strip("*")
                    desc = cells[1].strip()
                    # 跳过表头与分隔行
                    if (name and desc and not set(name) <= set("-: ")
                            and name not in ("姓名", "人物", "角色", "职务")
                            and not set(desc) <= set("-: ")):
                        out.setdefault(name, desc)
    return out


__all__ = [
    "DEFAULT_CHARACTERS", "CharacterStore", "CharacterHit", "CharacterMatcher",
]
