# -*- coding: utf-8 -*-
"""人格池与实例授权矩阵。

一个文件夹放全部人格（`personas/`），每个实例用一份 `personas.json`
声明「哪些人格在本实例可用」：

    instances/<实例 id>/personas.json
    {"version": 1, "grants": ["sr_robin_qingge", "diy_sui"]}

* 授权是**多对多**：同一个人格可以同时给多个实例。
* 文件不存在 = 该实例**没有任何**可用人格（新实例的默认状态），
  而不是"全部可用"——安全默认，避免新号一接进来就顶着别人的人设说话。
* 默认人格仍写在实例配置的 `persona_current`，但必须 ∈ grants，
  否则 `resolve_default()` 会回退到池内第一个。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

from astrbot.api import logger

_VERSION = 1


class PersonaPool:
    def __init__(self, instances_root: Path) -> None:
        self.root = Path(instances_root)
        self._cache: dict[str, list[str]] = {}

    # ------------------------------------------------------------ 文件
    def path(self, inst: str) -> Path:
        return self.root / inst / "personas.json"

    def _read(self, inst: str) -> list[str]:
        p = self.path(inst)
        try:
            raw = json.loads(p.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return []
        except Exception as e:
            logger.warning(f"[chatcore_neo] 读取 {p} 失败：{e!r}")
            return []
        if isinstance(raw, list):            # 容错：裸数组
            vals = raw
        elif isinstance(raw, dict):
            vals = raw.get("grants") or []
        else:
            vals = []
        out: list[str] = []
        for v in vals:
            s = str(v).strip()
            if s and s not in out:
                out.append(s)
        return out

    def grants(self, inst: str) -> list[str]:
        if inst not in self._cache:
            self._cache[inst] = self._read(inst)
        return list(self._cache[inst])

    def set_grants(self, inst: str, ids: Iterable[str]) -> list[str]:
        clean: list[str] = []
        for v in ids or []:
            s = str(v).strip()
            if s and s not in clean:
                clean.append(s)
        p = self.path(inst)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": _VERSION, "grants": clean,
                   "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        # 原子写：先落临时文件再替换，避免半截 JSON
        fd, tmp = tempfile.mkstemp(prefix=".personas-", dir=str(p.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        self._cache[inst] = clean
        return list(clean)

    # ------------------------------------------------------------ 增删
    def grant(self, inst: str, pid: str) -> list[str]:
        cur = self.grants(inst)
        if pid not in cur:
            cur.append(pid)
        return self.set_grants(inst, cur)

    def revoke(self, inst: str, pid: str) -> list[str]:
        return self.set_grants(inst, [x for x in self.grants(inst) if x != pid])

    # ------------------------------------------------------------ 查询
    def available_ids(self, inst: str) -> set[str]:
        return set(self.grants(inst))

    def available_docs(self, inst: str, docs: Iterable[Any]) -> list[Any]:
        ids = self.available_ids(inst)
        return [d for d in docs if getattr(d, "id", "") in ids]

    def is_available(self, inst: str, pid: str) -> bool:
        return str(pid) in self.available_ids(inst)

    def resolve_default(self, inst: str, docs: Iterable[Any],
                        wanted: str = "") -> Any:
        """按「配置里指定的 -> 池内第一个」顺序解析默认人格。

        指定的人格不在授权池内时**不静默采用**，回退并留日志。
        """
        docs = list(docs)
        avail = {getattr(d, "id", ""): d for d in docs
                 if self.is_available(inst, getattr(d, "id", ""))}
        w = str(wanted or "").strip()
        if w and w in avail:
            return avail[w]
        if w:
            logger.warning(f"[chatcore_neo] 实例 {inst} 的默认人格 {w!r} "
                           f"不在授权池内，已回退")
        for d in docs:
            if getattr(d, "id", "") in avail:
                return avail[getattr(d, "id", "")]
        return None

    def referenced_by(self, pid: str, instances: Iterable[str]) -> list[str]:
        return [i for i in instances if pid in self.available_ids(i)]

    def matrix(self, docs: Iterable[Any], instances: Iterable[dict]) -> dict:
        """给 WebUI 拖拽页的完整数据。"""
        docs = list(docs)
        inst_rows = []
        for meta in instances:
            iid = meta["id"]
            grants = self.grants(iid)
            inst_rows.append({
                "id": iid,
                "enabled": bool(meta.get("enabled", True)),
                "type": meta.get("type", ""),
                "grants": grants,
                "unknown": [g for g in grants if g not in {d.id for d in docs}],
            })
        personas = []
        for d in docs:
            personas.append({
                "id": d.id,
                "display_name": getattr(d, "display_name", d.id),
                "display_name_en": getattr(d, "display_name_en", ""),
                "world": getattr(d, "world", ""),
                "aliases": list(getattr(d, "aliases", []) or []),
                "granted": [r["id"] for r in inst_rows if d.id in r["grants"]],
            })
        personas.sort(key=lambda x: (x["world"] or "~", x["display_name"]))
        return {"instances": inst_rows, "personas": personas,
                "version": _VERSION}
