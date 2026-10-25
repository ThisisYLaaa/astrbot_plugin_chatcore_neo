# -*- coding: utf-8 -*-
"""人格服务：加载、索引、解析。

取代旧 `lili_state` 的三处反射式解析：
    `_resolve_persona_by_name` / `_build_persona_alias_list` / `_persona_alias_index_cache`
以及 `_PERSONA_RENAME_MAP`（旧名兼容）—— 后者现在是 `persona.yaml` 里的
`supersedes` 数据，代码不再维护映射表。

解析优先级（与旧行为一致，但数据来源是显式别名）：
    1. 精确匹配 id
    2. 精确匹配 legacy_dir（旧目录名，迁移期兼容）
    3. 精确匹配别名（含中文名/英文名/拼音/首字母）
    4. 唯一子串模糊匹配别名（多个命中视为歧义，返回 None）
"""
from __future__ import annotations

from pathlib import Path

from ..paths import PERSONAS_DIR
from .model import PersonaDoc, iter_persona_dirs


class PersonaService:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or PERSONAS_DIR
        self.docs: dict[str, PersonaDoc] = {}
        self._alias_index: dict[str, list[str]] = {}
        self._legacy_index: dict[str, str] = {}
        self._supersedes_index: dict[str, str] = {}
        self.errors: list[str] = []
        self.reload()

    # ------------------------------------------------------------ 加载
    def reload(self) -> None:
        self.docs = {}
        self.errors = []
        for d in iter_persona_dirs(self.root):
            try:
                doc = PersonaDoc.load(d)
            except Exception as e:
                self.errors.append(f"{d.name}: {e!r}")
                continue
            if doc.id in self.docs:
                self.errors.append(f"id 重复: {doc.id} ({d.name})")
                continue
            self.docs[doc.id] = doc
        self._rebuild_index()

    def _rebuild_index(self) -> None:
        self._alias_index = {}
        self._legacy_index = {}
        self._supersedes_index = {}
        for doc in self.docs.values():
            for a in (doc.aliases or []):
                key = a.strip().lower()
                if key:
                    self._alias_index.setdefault(key, []).append(doc.id)
            if doc.legacy_dir:
                self._legacy_index[doc.legacy_dir.lower()] = doc.id
            for old in doc.supersedes:
                self._supersedes_index[old.lower()] = doc.id

    # ------------------------------------------------------------ 查询
    def __len__(self) -> int:
        return len(self.docs)

    def __contains__(self, key: str) -> bool:
        return key in self.docs

    def get(self, persona_id: str) -> PersonaDoc | None:
        return self.docs.get(persona_id)

    def all(self) -> list[PersonaDoc]:
        return [self.docs[k] for k in sorted(self.docs)]

    def ids(self) -> list[str]:
        return sorted(self.docs)

    def by_world(self, world: str) -> list[PersonaDoc]:
        return [d for d in self.all() if d.world == world]

    def worlds(self) -> list[str]:
        return sorted({d.world for d in self.docs.values() if d.world})

    def resolve(self, name: str) -> PersonaDoc | None:
        """把用户输入的名字解析为一个人格（歧义/未命中返回 None）。"""
        name = (name or "").strip()
        if not name:
            return None
        # 1. 精确 id
        if name in self.docs:
            return self.docs[name]
        low = name.lower()
        # 2. 旧目录名 / supersedes
        for idx in (self._legacy_index, self._supersedes_index):
            hit = idx.get(low)
            if hit:
                return self.docs.get(hit)
        # 3. 精确别名
        hits = self._alias_index.get(low) or []
        if len(hits) == 1:
            return self.docs.get(hits[0])
        if len(hits) > 1:
            return None
        # 4. 唯一子串模糊匹配
        fuzzy = {pid for key, pids in self._alias_index.items() if low in key
                 for pid in pids}
        if len(fuzzy) == 1:
            return self.docs.get(next(iter(fuzzy)))
        return None

    def resolve_or_raise(self, name: str) -> PersonaDoc:
        doc = self.resolve(name)
        if doc is None:
            cands = self.suggest(name)
            raise KeyError(
                f"无法解析人格 {name!r}"
                + (f"；你是不是想找：{', '.join(cands)}" if cands else ""))
        return doc

    def suggest(self, name: str, limit: int = 5) -> list[str]:
        low = (name or "").strip().lower()
        if not low:
            return []
        scored: list[tuple[int, str]] = []
        for pid in self.docs:
            for a in self.docs[pid].aliases + [pid]:
                al = a.lower()
                score = 0
                if low in al or al in low:
                    score = 100 - abs(len(al) - len(low))
                else:
                    common = len(set(low) & set(al))
                    score = common
                scored.append((score, pid))
        best: dict[str, int] = {}
        for s, pid in scored:
            if s > best.get(pid, -1):
                best[pid] = s
        return [p for p, _ in sorted(best.items(), key=lambda kv: -kv[1])[:limit]]

    def alias_conflicts(self) -> dict[str, list[str]]:
        return {k: sorted(v) for k, v in self._alias_index.items() if len(v) > 1}

    def stats(self) -> dict:
        return {
            "personas": len(self.docs),
            "worlds": self.worlds(),
            "aliases": sum(len(d.aliases) for d in self.docs.values()),
            "load_errors": list(self.errors),
        }
