# -*- coding: utf-8 -*-
"""人格组·跨会话近况流水。

记录「谁在哪个会话说了什么、bot 回了什么」，供同组的**其他会话**在下一轮
提示词里看到这个人最近说过的话（默认 1 小时、最近 5 条）。

为什么单独一份
--------------
长期记忆是"固化 + 向量召回"，适合几周前的偏好；这里要的是**一小时内的
原话**，要逐字、要带 bot 的回复、而且不能进记忆库。所以独立成一份流水。

为什么用 JSONL
--------------
每轮都要追加一条；如果每次整文件重写，1200 条的 JSON 每条约 200KB，
每条消息都写一遍很浪费。改成 **一行一条 JSON 追加**，每轮只写几百字节；
条数到 `_COMPACT_AFTER` 才整体重写一次做压缩。

数据结构（每行）
----------------
    {"g": 组名, "u": umo, "uid": 用户, "t": 时间戳, "q": 用户原话, "a": bot 回复}
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

_MAX_ENTRIES = 1200
_KEEP_SEC = 24 * 3600
_COMPACT_AFTER = 200          # 追加这么多行后整体重写一次


class GroupJournal:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._entries: list[dict] = []
        self._appended = 0
        self.load()

    # ------------------------------------------------------------ 读写
    def load(self) -> None:
        with self._lock:
            out: list[dict] = []
            try:
                text = self.path.read_text(encoding="utf-8-sig")
            except FileNotFoundError:
                text = ""
            except Exception:
                text = ""
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if not isinstance(e, dict):
                    continue
                out.append({
                    "g": str(e.get("g") or ""),
                    "u": str(e.get("u") or ""),
                    "uid": str(e.get("uid") or ""),
                    "t": float(e.get("t") or 0),
                    "q": str(e.get("q") or ""),
                    "a": str(e.get("a") or ""),
                })
            self._entries = out[-_MAX_ENTRIES:]

    def _write_all(self) -> None:
        """整体重写（压缩/清理用）。"""
        self._prune()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for e in self._entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        tmp.replace(self.path)
        self._appended = 0

    def _prune(self) -> None:
        cutoff = time.time() - _KEEP_SEC
        self._entries = [e for e in self._entries if e["t"] >= cutoff]
        if len(self._entries) > _MAX_ENTRIES:
            self._entries = self._entries[-_MAX_ENTRIES:]

    def save(self, force: bool = False) -> None:
        with self._lock:
            if force or self._appended >= _COMPACT_AFTER:
                self._write_all()

    def flush(self) -> None:
        with self._lock:
            self._write_all()

    # ------------------------------------------------------------ 写
    def add(self, group: str, umo: str, uid: str,
            user: str, reply: str, ts: float | None = None) -> None:
        group = str(group or "").strip()
        if not group or group == umo:
            return
        user = str(user or "").strip()
        reply = str(reply or "").strip()
        if not user and not reply:
            return
        entry = {
            "g": group, "u": str(umo), "uid": str(uid),
            "t": float(ts if ts is not None else time.time()),
            "q": user[:600], "a": reply[:1200],
        }
        with self._lock:
            self._entries.append(entry)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                self._appended += 1
            except Exception:
                pass
            if self._appended >= _COMPACT_AFTER or len(self._entries) > _MAX_ENTRIES:
                try:
                    self._write_all()
                except Exception:
                    pass

    # ------------------------------------------------------------ 读
    def recent(self, group: str, uid: str, *, exclude_umo: str = "",
               window_sec: float = 3600.0, limit: int = 5) -> list[dict]:
        """同一组、同一用户、**其他会话**、窗口内最近 N 条（时间正序返回）。"""
        group = str(group or "").strip()
        uid = str(uid or "").strip()
        if not group or not uid:
            return []
        cutoff = time.time() - max(1.0, float(window_sec))
        with self._lock:
            hits = [e for e in self._entries
                    if e["g"] == group and e["uid"] == uid
                    and e["u"] != exclude_umo and e["t"] >= cutoff]
        hits.sort(key=lambda e: e["t"])
        return hits[-max(1, int(limit)):]

    def stats(self) -> dict:
        with self._lock:
            return {"entries": len(self._entries),
                    "groups": len({e["g"] for e in self._entries}),
                    "path": str(self.path)}
