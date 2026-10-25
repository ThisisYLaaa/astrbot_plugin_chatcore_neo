# -*- coding: utf-8 -*-
"""JSON 落盘的小型映射表（会话→人格、全局开关等）。

刻意保持极简：只做"读一次进内存 + 改动后原子落盘"。
复杂状态（数值/印象/记忆）在各自模块里，不塞进这里。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Iterator


class JsonMap:
    """键值均为字符串的字典，落盘为 JSON。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        with self._lock:
            try:
                raw = self.path.read_text(encoding="utf-8").strip()
                self._data = json.loads(raw) if raw else {}
                if not isinstance(self._data, dict):
                    self._data = {}
            except FileNotFoundError:
                self._data = {}
            except Exception:
                # 坏文件不应让插件起不来：备份后从空开始
                try:
                    self.path.replace(self.path.with_suffix(".json.bad"))
                except Exception:
                    pass
                self._data = {}

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self.path)

    # ---- 字典接口 ----
    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any, *, persist: bool = True) -> None:
        with self._lock:
            self._data[key] = value
            if persist:
                self.save()

    def update_many(self, items: dict[str, Any]) -> int:
        """批量写入，**只落盘一次**。返回变更条数。

        逐个 `set()` 会在补群成员昵称（一次几百人）时反复重写整个文件，
        白跑一遍 O(n²) 的磁盘 IO。
        """
        with self._lock:
            n = 0
            for k, v in (items or {}).items():
                if self._data.get(k) != v:
                    self._data[k] = v
                    n += 1
            if n:
                self.save()
            return n

    def pop(self, key: str, *, persist: bool = True) -> Any:
        with self._lock:
            v = self._data.pop(key, None)
            if persist:
                self.save()
            return v

    def clear(self, *, persist: bool = True) -> None:
        with self._lock:
            self._data.clear()
            if persist:
                self.save()

    def __contains__(self, key: str) -> bool:
        with self._lock:
            return key in self._data

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def __iter__(self) -> Iterator[str]:
        with self._lock:
            return iter(list(self._data))

    def items(self) -> list[tuple[str, Any]]:
        with self._lock:
            return list(self._data.items())

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)


class JsonSet:
    """落盘为 JSON 数组的字符串集合。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._data: set[str] = set()
        self.load()

    def load(self) -> None:
        with self._lock:
            try:
                raw = self.path.read_text(encoding="utf-8").strip()
                v = json.loads(raw) if raw else []
                self._data = set(v) if isinstance(v, list) else set()
            except FileNotFoundError:
                self._data = set()
            except Exception:
                self._data = set()

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(sorted(self._data), ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self.path)

    def add(self, v: str, *, persist: bool = True) -> None:
        with self._lock:
            self._data.add(v)
            if persist:
                self.save()

    def discard(self, v: str, *, persist: bool = True) -> None:
        with self._lock:
            self._data.discard(v)
            if persist:
                self.save()

    def __contains__(self, v: str) -> bool:
        with self._lock:
            return v in self._data

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def as_set(self) -> set[str]:
        with self._lock:
            return set(self._data)
