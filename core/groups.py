# -*- coding: utf-8 -*-
"""人格组（记忆组）。

一个组 = 一个实例下的一组会话 + **一个人格** + 可选副人格。组内：

* **记忆共享**：记忆按组键存取，组内多个会话共用一份
  （不是复制到每个会话 —— 复制会各自衰减，很快就不一致）。
* **人格同步**：组内任一会话触发切换人格 / 增删副人格，其余会话一并生效。
* **不设组的会话**：记忆与人格各自独立（组键就是它自己的 UMO）。

数据（`groups.json`）
--------------------
    {
      "version": 2,
      "groups": {
        "组名": {
          "persona": "persona_a",
          "costars": ["strinova_kanami"],
          "members": ["BotA:GroupMessage:123", "BotA:FriendMessage:456"],
          "instance": "BotA",
          "updated_at": "2026-10-26T01:00:00"
        }
      }
    }

兼容旧格式 `{"groups": {"组名": ["umo", ...]}}`：载入时转成
`{persona: "", costars: [], members: [...]}`。

一个组只能属于一个实例（拆库后同一个组键只能落在一个 memory.db 里），
跨实例归组直接拒绝。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .paths import GROUPS_JSON

_VERSION = 2


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class MemoryGroups:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or GROUPS_JSON
        self._lock = threading.RLock()
        self._umo_to_group: dict[str, str] = {}
        self._groups: dict[str, dict] = {}
        self.load()

    # ------------------------------------------------------------ 读写
    @staticmethod
    def inst_of(umo: str) -> str:
        """`BotA:GroupMessage:1` -> `BotA`。"""
        return str(umo or "").split(":", 1)[0]

    def load(self) -> None:
        with self._lock:
            try:
                raw = self.path.read_text(encoding="utf-8").strip()
                data = json.loads(raw) if raw else {}
            except Exception:
                data = {}
            groups = data.get("groups") if isinstance(data, dict) else None
            if not isinstance(groups, dict):
                groups = {}
            out: dict[str, dict] = {}
            for name, val in groups.items():
                name = str(name)
                if isinstance(val, list):            # 旧格式：纯成员列表
                    members = [str(x) for x in val if str(x).strip()]
                    out[name] = {"persona": "", "costars": [],
                                 "members": members,
                                 "instance": self.inst_of(members[0]) if members else "",
                                 "updated_at": ""}
                elif isinstance(val, dict):
                    members = [str(x) for x in (val.get("members") or [])
                               if str(x).strip()]
                    costars = [str(x) for x in (val.get("costars") or [])
                               if str(x).strip()]
                    out[name] = {
                        "persona": str(val.get("persona") or "").strip(),
                        "costars": costars,
                        "members": members,
                        "instance": str(val.get("instance")
                                        or (self.inst_of(members[0]) if members else "")),
                        "updated_at": str(val.get("updated_at") or ""),
                    }
            self._groups = out
            self._reindex()

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"version": _VERSION,
                       "groups": {k: dict(v) for k, v in self._groups.items()}}
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self.path)

    def _reindex(self) -> None:
        idx: dict[str, str] = {}
        for gname, meta in self._groups.items():
            for u in meta.get("members") or []:
                idx[u] = gname
        self._umo_to_group = idx

    # ------------------------------------------------------------ 查询
    def group_of(self, umo: str) -> str:
        """记忆组键：有归属返回组名，否则返回 UMO 自己。"""
        with self._lock:
            return self._umo_to_group.get(umo, umo)

    def in_group(self, umo: str) -> bool:
        with self._lock:
            return umo in self._umo_to_group

    def names(self) -> list[str]:
        with self._lock:
            return list(self._groups.keys())

    def members(self, group: str) -> list[str]:
        with self._lock:
            return list((self._groups.get(group) or {}).get("members") or [])

    def meta(self, group: str) -> dict:
        with self._lock:
            m = self._groups.get(group)
            if not m:
                return {}
            return {**m, "members": list(m.get("members") or []),
                    "costars": list(m.get("costars") or [])}

    def all_groups(self) -> dict[str, dict]:
        with self._lock:
            return {k: self.meta(k) for k in self._groups}

    def persona_of(self, group: str) -> str:
        with self._lock:
            return str((self._groups.get(group) or {}).get("persona") or "")

    def costars_of(self, group: str) -> list[str]:
        with self._lock:
            return list((self._groups.get(group) or {}).get("costars") or [])

    def stats(self) -> dict:
        with self._lock:
            n = sum(len(m.get("members") or []) for m in self._groups.values())
            return {"groups": len(self._groups), "members": n,
                    "path": str(self.path)}

    # ------------------------------------------------------------ 校验
    def _assert_group_instance(self, group: str, inst: str) -> None:
        cur = str((self._groups.get(group) or {}).get("instance") or "")
        if cur and inst and cur != inst:
            raise ValueError(
                f"人格组「{group}」已属于实例 {cur}，不能把 {inst} 的会话并进去"
                f"（实例拆分后不支持跨实例共享记忆）")

    # ------------------------------------------------------------ 写
    def create(self, name: str, persona: str = "", costars: Any = None,
               instance: str = "") -> dict:
        name = str(name or "").strip()
        if not name:
            raise ValueError("组名不能为空")
        with self._lock:
            if name in self._groups:
                raise ValueError(f"人格组「{name}」已存在")
            self._groups[name] = {
                "persona": str(persona or "").strip(),
                "costars": [str(x).strip() for x in (costars or []) if str(x).strip()],
                "members": [],
                "instance": str(instance or "").strip(),
                "updated_at": _now(),
            }
            self._reindex()
            self.save()
            return self.meta(name)

    def delete(self, name: str) -> None:
        with self._lock:
            self._groups.pop(str(name), None)
            self._reindex()
            self.save()

    def rename(self, old: str, new: str) -> str:
        old, new = str(old or "").strip(), str(new or "").strip()
        with self._lock:
            if old not in self._groups:
                raise ValueError(f"找不到人格组「{old}」")
            if not new:
                raise ValueError("新组名不能为空")
            if new == old:
                return new
            if new in self._groups:                 # 合并
                tgt = self._groups[new]
                src = self._groups.pop(old)
                for u in src.get("members") or []:
                    if u not in tgt["members"]:
                        tgt["members"].append(u)
                if src.get("persona") and not tgt.get("persona"):
                    tgt["persona"] = src["persona"]
                tgt["updated_at"] = _now()
            else:
                self._groups[new] = self._groups.pop(old)
                self._groups[new]["updated_at"] = _now()
            self._reindex()
            self.save()
            return new

    def _ensure_group(self, name: str, inst: str) -> dict:
        meta = self._groups.get(name)
        if meta is None:
            meta = {"persona": "", "costars": [], "members": [],
                    "instance": inst, "updated_at": _now()}
            self._groups[name] = meta
        return meta

    def assign(self, umo: str, group: str) -> None:
        self.assign_many([umo], group)

    def assign_many(self, umos: Iterable[str], group: str) -> list[str]:
        """把一批会话加入组；返回实际加入的列表。跨实例/空组名抛错。"""
        group = str(group or "").strip()
        if not group:
            raise ValueError("组名不能为空")
        items = [str(u).strip() for u in (umos or []) if str(u).strip()]
        if not items:
            raise ValueError("没有选中任何会话")
        insts = {self.inst_of(u) for u in items}
        if len(insts) > 1:
            raise ValueError(f"一次只能加入同一实例的会话，收到：{', '.join(sorted(insts))}")
        inst = insts.pop()
        with self._lock:
            self._assert_group_instance(group, inst)
            meta = self._ensure_group(group, inst)
            meta["instance"] = meta.get("instance") or inst
            for u in items:
                # 先从别的组摘掉
                for other, m in self._groups.items():
                    if other != group and u in (m.get("members") or []):
                        m["members"].remove(u)
                        m["updated_at"] = _now()
                if u not in meta["members"]:
                    meta["members"].append(u)
            meta["updated_at"] = _now()
            self._reindex()
            self.save()
            return items

    def remove(self, umo: str) -> None:
        self.remove_many([umo])

    def remove_many(self, umos: Iterable[str]) -> list[str]:
        items = [str(u).strip() for u in (umos or []) if str(u).strip()]
        if not items:
            return []
        with self._lock:
            touched = False
            for u in items:
                for m in self._groups.values():
                    if u in (m.get("members") or []):
                        m["members"].remove(u)
                        m["updated_at"] = _now()
                        touched = True
            if touched:
                self._reindex()
                self.save()
            return items

    def set_persona(self, group: str, pid: str) -> dict:
        group = str(group or "").strip()
        with self._lock:
            meta = self._groups.get(group)
            if meta is None:
                raise ValueError(f"找不到人格组「{group}」")
            meta["persona"] = str(pid or "").strip()
            meta["updated_at"] = _now()
            self.save()
            return self.meta(group)

    def set_costars(self, group: str, pids: Iterable[str]) -> dict:
        group = str(group or "").strip()
        with self._lock:
            meta = self._groups.get(group)
            if meta is None:
                raise ValueError(f"找不到人格组「{group}」")
            clean: list[str] = []
            for p in (pids or []):
                s = str(p).strip()
                if s and s not in clean:
                    clean.append(s)
            meta["costars"] = clean
            meta["updated_at"] = _now()
            self.save()
            return self.meta(group)

    def drop_empty(self) -> int:
        """清掉没有成员的组（可选维护动作）。"""
        with self._lock:
            empty = [k for k, m in self._groups.items()
                     if not (m.get("members") or [])]
            for k in empty:
                self._groups.pop(k, None)
            if empty:
                self._reindex()
                self.save()
            return len(empty)
