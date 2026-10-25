# -*- coding: utf-8 -*-
"""状态持久化 + 特权分级 + 用户印象。

路径规则（沿用旧 `lili_state` 的三级目录，已在 W2 迁移时保平）：
    私聊（uid 为空）: {ROOT}/state/{umo}/{persona}.json
    群聊（uid 非空）: {ROOT}/state/{umo}/{persona}/{uid}.json
    {ROOT}/state/_session_persona.json
    {ROOT}/state/_inject_overrides.json
    {ROOT}/state/_impressions/{uid}.json
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from ..paths import STATE_DIR
from ..sessionmap import JsonMap, JsonSet
from .model import MemberState

TIER_INTIMATE = "intimate"
TIER_FRIEND = "friend"


def safe_segment(seg: str) -> str:
    """UMO / 人格名 / uid → 安全路径段（与旧实现同样的替换规则）。"""
    return (seg or "").replace(":", "_").replace("/", "_").replace("\\", "_")


class StateStore:
    """状态读写。所有落盘都是"写临时文件再替换"，避免半截 JSON。"""

    def __init__(self, root: Path | None = None, *,
                 default_affection: int = 65,
                 default_lewdness: int = 20,
                 default_depravity: int = 0) -> None:
        self.root = root or STATE_DIR
        self.default_affection = default_affection
        self.default_lewdness = default_lewdness
        self.default_depravity = default_depravity
        self._lock = threading.RLock()
        self._cache: dict[str, MemberState] = {}
        self.session_persona = JsonMap(self.root / "_session_persona.json")
        self.session_initialized = JsonSet(self.root / "_session_initialized.json")
        self.inject_overrides = JsonMap(self.root / "_inject_overrides.json")

    # ------------------------------------------------------------ 路径
    def path_for(self, umo: str, persona: str, uid: str = "") -> Path:
        d = self.root / safe_segment(umo)
        if not persona:
            return d / (f"{safe_segment(uid)}.json" if uid else "state.json")
        sp = safe_segment(persona)
        if uid:
            return d / sp / f"{safe_segment(uid)}.json"
        return d / f"{sp}.json"

    def inject_key(self, umo: str, persona: str, uid: str = "") -> str:
        return f"{safe_segment(umo)}|{persona}|{uid}"

    # ------------------------------------------------------------ 读写
    def load(self, umo: str, persona: str, uid: str = "",
             *, default_affection: int | None = None) -> MemberState:
        """读取状态。

        `default_affection` 用于**按场景给不同初值**：群聊普通成员的初值比
        私聊/特权低（`group_member_initial_affection`），避免普通群友天然亲近
        导致乱认主人/引航者。已有的状态文件不受影响。
        """
        p = self.path_for(umo, persona, uid)
        key = str(p)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
            data: dict[str, Any] = {}
            try:
                raw = p.read_text(encoding="utf-8").strip()
                if raw:
                    data = json.loads(raw)
            except FileNotFoundError:
                pass
            except Exception:
                # 坏文件不阻断运行：改名留证后从默认值开始
                try:
                    p.replace(p.with_suffix(".json.bad"))
                except Exception:
                    pass
            st = MemberState.from_dict(
                data,
                default_affection=(self.default_affection
                                   if default_affection is None
                                   else default_affection),
                default_lewdness=self.default_lewdness,
                default_depravity=self.default_depravity)
            st.inject = str(self.inject_overrides.get(
                self.inject_key(umo, persona, uid), "") or "")
            self._cache[key] = st
            return st

    def save(self, umo: str, persona: str, uid: str, st: MemberState) -> Path:
        p = self.path_for(umo, persona, uid)
        with self._lock:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(st.to_dict(), ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(p)
            self._cache[str(p)] = st
        return p

    def set_inject(self, umo: str, persona: str, uid: str, text: str) -> None:
        key = self.inject_key(umo, persona, uid)
        if text:
            self.inject_overrides.set(key, text)
        else:
            self.inject_overrides.pop(key)
        p = self.path_for(umo, persona, uid)
        with self._lock:
            st = self._cache.get(str(p))
            if st is not None:
                st.inject = text

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    # ------------------------------------------------------------ 印象
    def impression_path(self, uid: str) -> Path:
        return self.root / "_impressions" / f"{safe_segment(uid)}.json"

    def load_impression(self, uid: str) -> dict[str, Any]:
        p = self.impression_path(uid)
        try:
            raw = p.read_text(encoding="utf-8").strip()
            d = json.loads(raw) if raw else {}
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def save_impression(self, uid: str, data: dict[str, Any]) -> None:
        p = self.impression_path(uid)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(p)

    def impression_text(self, uid: str, *, nsfw: bool = False) -> str:
        """取用于注入的印象文本（自定义优先，其次自动摘要）。"""
        d = self.load_impression(uid)
        if nsfw:
            return (d.get("nsfw_custom") or d.get("nsfw") or "").strip()
        return (d.get("sfw_custom") or d.get("sfw") or "").strip()

    # ------------------------------------------------------------ 枚举
    def sessions(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir()
                      if p.is_dir() and not p.name.startswith("_"))

    def personas_in(self, umo: str) -> list[str]:
        d = self.root / safe_segment(umo)
        if not d.is_dir():
            return []
        out = []
        for p in sorted(d.iterdir()):
            if p.is_file() and p.suffix == ".json":
                out.append(p.stem)
            elif p.is_dir():
                out.append(p.name)
        return out

    def members_in(self, umo: str, persona: str) -> list[str]:
        d = self.root / safe_segment(umo) / safe_segment(persona)
        if not d.is_dir():
            return []
        return sorted(p.stem for p in d.iterdir()
                      if p.is_file() and p.suffix == ".json")

    def stats(self) -> dict[str, int]:
        files = list(self.root.rglob("*.json")) if self.root.is_dir() else []
        return {"sessions": len(self.sessions()),
                "files": len(files),
                "bytes": sum(f.stat().st_size for f in files)}


# ---------------------------------------------------------------- 特权分级

class Privilege:
    """用户分级：特权（T1/intimate）vs 普通（T2/friend）。

    实例化之后，**每个实例有且只有一名特权用户**（`config.json` 的
    `privilege_uid`），不再有「全局名单 + 分会话前缀覆盖」那一层：
    实例边界本身就是那个前缀。

    `uid_of(inst)` 是一个取值回调（插件侧从实例配置里读），
    这样本类不依赖配置对象，单测可直接传匿名函数。
    """

    def __init__(self, uid_of: Any = None, uid: str = "") -> None:
        self._uid_of = uid_of if callable(uid_of) else None
        self._uid = str(uid or "").strip()

    @staticmethod
    def inst_of(scope: str) -> str:
        """`BotA:GroupMessage:123` -> `BotA`。"""
        return str(scope or "").split(":", 1)[0]

    def uid_for(self, scope: str = "") -> str:
        if self._uid_of is not None:
            try:
                return str(self._uid_of(self.inst_of(scope)) or "").strip()
            except Exception:
                return ""
        return self._uid

    # 兼容旧调用点（webadmin / 口令）：返回 0 或 1 个元素的集合
    def effective(self, scope: str = "") -> set[str]:
        u = self.uid_for(scope)
        return {u} if u else set()

    def tier(self, uid: str, scope: str = "") -> str:
        target = self.uid_for(scope)
        return TIER_INTIMATE if target and str(uid).strip() == target \
            else TIER_FRIEND

    @staticmethod
    def is_private_umo(umo: str) -> bool:
        return "FriendMessage" in umo or "PrivateMessage" in umo
