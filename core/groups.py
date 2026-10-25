# -*- coding: utf-8 -*-
"""跨会话记忆组。

场景
----
同一个人在 Banyan 和 Sui 两个适配器下、或同一适配器的多个群之间，
bot 应该"记得是同一个人/同一件事"。`groups.json` 把若干 UMO 归到一组：

    {"groups": {"ADOFAI_Banyan": ["Banyan:GroupMessage:132292540",
                                  "Banyan:GroupMessage:239996243"]}}

实现方式很轻：**记忆按组键存取**，而不是按单个 UMO。
组键 = 该 UMO 所属的组名；没有归属时组键就是 UMO 自己。

为什么不做成"记忆复制到每个会话"
--------------------------------
复制会产生多份、且衰减/强化各自独立，很快就不一致。
按组键存一份、多会话共享，语义更准，也不会膨胀。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from .paths import GROUPS_JSON


class MemoryGroups:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or GROUPS_JSON
        self._lock = threading.RLock()
        self._umo_to_group: dict[str, str] = {}
        self._groups: dict[str, list[str]] = {}
        self.load()

    # ------------------------------------------------------------ 读写
    def load(self) -> None:
        with self._lock:
            try:
                raw = self.path.read_text(encoding="utf-8").strip()
                data = json.loads(raw) if raw else {}
            except FileNotFoundError:
                data = {}
            except Exception:
                data = {}
            groups = data.get("groups") if isinstance(data, dict) else None
            if not isinstance(groups, dict):
                groups = {}
            self._groups = {
                str(k): [str(x) for x in (v or []) if str(x).strip()]
                for k, v in groups.items()
            }
            self._reindex()

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"groups": self._groups},
                                      ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self.path)

    def _reindex(self) -> None:
        idx: dict[str, str] = {}
        for gname, umos in self._groups.items():
            for u in umos:
                idx[u] = gname
        self._umo_to_group = idx

    # ------------------------------------------------------------ 查询
    def group_of(self, umo: str) -> str:
        """返回该 UMO 的记忆组键（无归属时就是 UMO 自己）。"""
        with self._lock:
            return self._umo_to_group.get(umo, umo)

    def members(self, group: str) -> list[str]:
        with self._lock:
            return list(self._groups.get(group, []))

    def all_groups(self) -> dict[str, list[str]]:
        with self._lock:
            return {k: list(v) for k, v in self._groups.items()}

    # ------------------------------------------------------------ 写
    @staticmethod
    def inst_of(umo: str) -> str:
        """`Banyan:GroupMessage:1` -> `Banyan`。"""
        return str(umo or "").split(":", 1)[0]

    def _assert_same_instance(self, umo: str, group: str) -> None:
        """拆库后一个组键只落在一个实例的记忆库里。

        跨实例归组会让同一个组键在两个 memory.db 里各存一份、此后各自
        衰减固化（本文件开头就写明了"复制会产生多份，很快就不一致"）。
        所以直接拒绝，把问题暴露在配置时而不是运行时。
        """
        target = self.inst_of(umo)
        for u in self._groups.get(group, []):
            if u != umo and self.inst_of(u) != target:
                raise ValueError(
                    f"记忆组「{group}」已属于实例 {self.inst_of(u)}，"
                    f"不能把 {target} 的会话并进去"
                    f"（实例拆分后不支持跨实例共享记忆）")

    def assign(self, umo: str, group: str) -> None:
        with self._lock:
            group = str(group).strip()
            if not group:
                self.remove(umo)
                return
            self._assert_same_instance(umo, group)
            for g, umos in self._groups.items():
                if umo in umos:
                    umos.remove(umo)
            self._groups.setdefault(group, [])
            if umo not in self._groups[group]:
                self._groups[group].append(umo)
            self._groups = {k: v for k, v in self._groups.items() if v}
            self._reindex()
            self.save()

    def remove(self, umo: str) -> None:
        with self._lock:
            for g in list(self._groups):
                if umo in self._groups[g]:
                    self._groups[g].remove(umo)
                if not self._groups[g]:
                    self._groups.pop(g, None)
            self._reindex()
            self.save()

    def merge(self, umo_a: str, umo_b: str) -> str:
        """把两个会话并到同一组，返回组键。跨实例直接拒绝。"""
        if self.inst_of(umo_a) != self.inst_of(umo_b):
            raise ValueError(
                f"不能把 {self.inst_of(umo_a)} 和 {self.inst_of(umo_b)} 的会话"
                f"并成一组（实例拆分后不支持跨实例共享记忆）")
        ga, gb = self.group_of(umo_a), self.group_of(umo_b)
        target = ga if ga in self._groups else (gb if gb in self._groups
                                               else f"{ga}+{gb}")
        self._assert_same_instance(umo_a, target)
        self._assert_same_instance(umo_b, target)
        with self._lock:
            for u in (umo_a, umo_b):
                for g in list(self._groups):
                    if u in self._groups[g]:
                        self._groups[g].remove(u)
            members = set(self._groups.get(target, [])) | {umo_a, umo_b}
            self._groups[target] = sorted(members)
            self._groups = {k: v for k, v in self._groups.items() if v}
            self._reindex()
            self.save()
        return target

    # ------------------------------------------------------------ 视图
    def stats(self) -> dict:
        with self._lock:
            n = sum(len(v) for v in self._groups.values())
            return {"groups": len(self._groups), "members": n,
                    "path": str(self.path)}
