# -*- coding: utf-8 -*-
"""记忆层：SQLite + 余弦 Top-K。

存储形态
--------
`memory/memory.db`（SQLite / WAL）里一张 `atoms` 表，向量存 float32 BLOB。
`umo` 是**一列**，不是一个文件 —— 所有会话同住一个库，
删/备份/查看都走 SQL。

为什么不再用 JSON
-----------------
旧实现是「一个会话一个 JSON，任何改动都整文件重写」。实测的代价：
  * 一条 4096 维记忆序列化后 107 KB，而正文只有几百字节（99.7% 是向量文本）
  * 462 条的会话文件 58 MB，一次 `save()` 全量重写要 1.91 s
  * `recall()` 会给命中的记忆 `hits += 1` → 置脏 → **每条消息都触发一次
    59 MB 重写**（而召回就在消息主链路上）
  * `list[float]` 里每个 float 是独立对象，462 条常驻 59 MB

SQLite 版把这三件事一起解决：向量 BLOB 化、写入按行增量、读回用 `array('f')`。

**检索算法本身不变**：仍是全量手写余弦（实测 480 条 ~0.1 s，够用）。
换 ANN 索引是另一件事，不是这次的目标。

不引入 faiss / networkx / jieba：向量就是 `array('f')`，相似度就是余弦，
中文不做分词，直接用整句 embedding。
"""
from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..db import (connection, open_ready, pack_vec, to_f32, transaction,
                  unpack_vec)
from ..paths import MEMORY_DIR

# `math.sumprod`（3.12+）在 C 层做点积，比手写 Python 循环快数倍；
# 拿不到就退回循环，行为完全一致。
try:
    from math import sumprod as _sumprod
except ImportError:                                     # pragma: no cover
    _sumprod = None

# numpy 只是召回的可选加速：`_batch_cosine` 一次算完整批余弦。
# AstrBot 后端自带 numpy；万一部署环境没有，就退回逐条循环，行为完全一致。
try:
    import numpy as _np
except ImportError:                                     # pragma: no cover
    _np = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS atoms (
    id         TEXT PRIMARY KEY,
    umo        TEXT NOT NULL,
    text       TEXT NOT NULL,
    vec        BLOB,
    ts         REAL    NOT NULL DEFAULT 0,
    importance REAL    NOT NULL DEFAULT 0.5,
    hits       INTEGER NOT NULL DEFAULT 0,
    last_hit   REAL    NOT NULL DEFAULT 0,
    persona    TEXT    NOT NULL DEFAULT '',
    uid        TEXT    NOT NULL DEFAULT '',
    kind       TEXT    NOT NULL DEFAULT 'dialog',
    tags       TEXT    NOT NULL DEFAULT '[]',
    metadata   TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_atoms_umo ON atoms(umo);
"""

_UPDATE_SQL = (
    "UPDATE atoms SET text=?, vec=?, ts=?, importance=?, hits=?, last_hit=?,"
    " persona=?, uid=?, kind=?, tags=?, metadata=? WHERE id=?")
_INSERT_SQL = (
    "INSERT OR REPLACE INTO atoms"
    " (id, umo, text, vec, ts, importance, hits, last_hit,"
    "  persona, uid, kind, tags, metadata)"
    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)")


def _dumps(value: Any, default: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return json.dumps(default, ensure_ascii=False)


def _loads(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    try:
        got = json.loads(raw)
    except Exception:
        return default
    return got if isinstance(got, type(default)) else default


# ---------------------------------------------------------------- 数据

@dataclass
class MemoryAtom:
    id: str = ""
    text: str = ""
    vec: Any = field(default_factory=list)   # array('f')（来自库）或 list[float]
    ts: float = 0.0                 # 写入时间
    importance: float = 0.5         # 0~1，越高越不容易被衰减掉
    hits: int = 0                   # 被召回次数
    last_hit: float = 0.0
    umo: str = ""
    persona: str = ""
    uid: str = ""
    kind: str = "dialog"            # document / dialog / fact / preference
    tags: list[str] = field(default_factory=list)
    # 固化产出的结构化元数据（图谱抽取的输入）：
    #   summary / topics / key_facts / participants / importance / session_id
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            self.id = uuid.uuid4().hex[:16]
        if not self.ts:
            self.ts = time.time()


def _row_to_atom(row: Any) -> MemoryAtom:
    return MemoryAtom(
        id=row["id"], text=row["text"], vec=unpack_vec(row["vec"]),
        ts=float(row["ts"] or 0), importance=float(row["importance"] or 0),
        hits=int(row["hits"] or 0), last_hit=float(row["last_hit"] or 0),
        umo=row["umo"], persona=row["persona"] or "",
        uid=row["uid"] or "", kind=row["kind"] or "dialog",
        tags=_loads(row["tags"], []), metadata=_loads(row["metadata"], {}))


def _insert_params(a: MemoryAtom) -> tuple:
    return (a.id, a.umo, a.text, pack_vec(a.vec), float(a.ts),
            float(a.importance), int(a.hits), float(a.last_hit),
            a.persona or "", a.uid or "", a.kind or "dialog",
            _dumps(a.tags, []), _dumps(a.metadata, {}))


def _update_params(a: MemoryAtom) -> tuple:
    return (a.text, pack_vec(a.vec), float(a.ts), float(a.importance),
            int(a.hits), float(a.last_hit), a.persona or "", a.uid or "",
            a.kind or "dialog", _dumps(a.tags, []),
            _dumps(a.metadata, {}), a.id)


def cosine(a, b) -> float:
    if not len(a) or not len(b):
        return 0.0
    if len(a) != len(b):
        # 维度不一致**必须留痕**：换了 embedding 模型后所有旧向量都会失配，
        # 静默返回 0.0 会让用户只看到"记忆突然全忘了"而毫无线索。
        _warn_dim(a, b)
        return 0.0
    if _sumprod is not None:
        dot = _sumprod(a, b)
        na = _sumprod(a, a)
        nb = _sumprod(b, b)
    else:                                               # pragma: no cover
        dot = na = nb = 0.0
        for x, y in zip(a, b):
            dot += x * y
            na += x * x
            nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))

# 维度不一致只告警一次：换了 embedding 模型后旧向量会全部失配，
# 静默返回 0.0 会让用户以为"记忆突然全忘了"而毫无线索。
_DIM_WARNED: set = set()


def _warn_dim(a, b) -> None:
    key = (len(a), len(b))
    if key in _DIM_WARNED:
        return
    _DIM_WARNED.add(key)
    import logging
    logging.getLogger("astrbot").warning(
        f"[chatcore_neo] 向量维度不一致（{len(a)} vs {len(b)}），"
        f"该记忆不会参与召回 —— embedding 模型可能被换过，"
        f"建议清空记忆重建（/persona forget）")


def _batch_cosine(query_vec, atoms) -> list[float]:
    """一次算完一批向量与 query 的余弦（numpy 加速）。

    与逐条 `cosine` 行为一致：
      * 维度不一致 / 空向量的行得 0.0，维度不一致仍会告警；
      * 没有 numpy 时退回逐条 `cosine`，结果完全一样。
    区别只在求和顺序，浮点误差 ~1e-16，不影响排序。
    """
    if not atoms:
        return []
    if _np is None:                                     # pragma: no cover
        return [cosine(query_vec, a.vec) for a in atoms]
    d = len(query_vec)
    rows: list = []
    for a in atoms:
        v = a.vec
        if len(v) != d:
            # 维度不符的那一行不进矩阵（numpy 要求等长），按 0.0 处理
            if len(v):
                _warn_dim(query_vec, v)
            rows.append([0.0] * d)
        else:
            rows.append(v)
    M = _np.asarray(rows, dtype=_np.float64)
    q = _np.asarray(query_vec, dtype=_np.float64)
    norms = _np.linalg.norm(M, axis=1)
    qn = _np.linalg.norm(q)
    with _np.errstate(divide="ignore", invalid="ignore"):
        sims = (M / norms[:, None]) @ (q / qn) if qn > 0 \
            else _np.zeros(len(atoms))
    sims[norms == 0] = 0.0         # 空/零向量行：余弦本来就是 0
    sims[~_np.isfinite(sims)] = 0.0
    return [float(s) for s in sims]


def _atom_importance(atom: MemoryAtom) -> float:
    """优先用固化产出的 importance；没有就用 atom 自带的。"""
    try:
        v = atom.metadata.get("importance")
        if v is not None:
            return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError, AttributeError):
        pass
    return max(0.0, min(1.0, atom.importance))


# 分类差异化衰减：`kind` 决定半衰期倍率。
# 「喜好/事实」是长期有效的用户画像（"他讨厌早起"过半年依然成立），
# 而「闲聊」的价值本就随时间快速蒸发 —— 拿同一个半衰期对待这两种记忆，
# 要么让旧闲聊一直霸榜，要么把用户的稳定偏好提前忘掉。
KIND_HALF_LIFE = {
    "preference": 3.0,      # 喜好/厌恶/习惯
    "fact": 2.0,            # 稳定事实（住哪、做什么、叫什么）
    "document": 1.0,        # 事件记录/总结（默认）
    "dialog": 0.5,          # 一次性闲聊、当时的情绪
}


def kind_factor(kind: str) -> float:
    return KIND_HALF_LIFE.get(str(kind or "").strip().lower(), 1.0)


def is_superseded(atom: MemoryAtom) -> bool:
    """这条记忆是否被更新的记忆取代（取代只降权、不删除）。"""
    try:
        return bool((atom.metadata or {}).get("superseded_by"))
    except AttributeError:
        return False


def decayed_score(atom: MemoryAtom, sim: float, *, now: float,
                  half_life_days: float, kind_decay: bool = True) -> float:
    """最终排序分：相似度 × 重要度 × 时间衰减 + 命中强化。

    半衰期越长衰减越慢；命中过的记忆额外加权（强化），
    但权重有上限，避免"老记忆永久霸榜"。

    `kind_decay=True`（默认）时半衰期再乘两个因子：
      * 记忆类型（`KIND_HALF_LIFE`）—— 偏好/事实活得久，闲聊烂得快；
      * 重要度（1.0~1.25 倍，**只加长不缩短**）—— 重要的记忆衰减得慢一点。

    重要度的调制刻意做成"单向且轻微"：分数里已经有一个
    `0.4 + 0.6·importance`（2.5 倍跨度），半衰期这边再双向放大就会让
    importance 变成压倒性因子 —— 实测双向 0.7~1.3 时 precision@5 掉，
    改成单向 1.0~1.25 后与旧行为持平。
    """
    age_days = max(0.0, (now - atom.ts) / 86400.0)
    hl = max(0.1, half_life_days)
    imp = _atom_importance(atom)
    if kind_decay:
        hl *= kind_factor(atom.kind) * (1.0 + 0.25 * imp)
    decay = 0.5 ** (age_days / hl)
    importance = 0.4 + 0.6 * imp
    reinforce = 1.0 + min(0.5, 0.1 * atom.hits)
    return sim * importance * decay * reinforce


# ---------------------------------------------------------------- 存储

class MemoryStore:
    """`memory.db` 里所有会话的原子记忆。

    `umo` 是列而不是文件名，所以 `path_for()` 那种"一个会话一个文件"
    的概念已经不存在了；`root` 仍然保留（图谱/缓存的路径都由它推导）。
    """

    def __init__(self, root: Path | None = None,
                 db_path: Path | None = None) -> None:
        self.root = Path(root) if root else MEMORY_DIR
        self.db_path = Path(db_path) if db_path else self.root / "memory.db"
        self._cache: dict[str, list[MemoryAtom]] = {}
        self._touched: dict[str, set[str]] = {}   # umo -> 待回写的 atom id
        self._ready = open_ready(self.db_path, _SCHEMA)

    # ---- 读写 ----
    def load(self, umo: str) -> list[MemoryAtom]:
        if umo in self._cache:
            return self._cache[umo]
        atoms: list[MemoryAtom] = []
        if self._ready:
            try:
                with connection(self.db_path) as con:
                    rows = con.execute(
                        "SELECT * FROM atoms WHERE umo=? ORDER BY ts, rowid",
                        (umo,)).fetchall()
                atoms = [_row_to_atom(r) for r in rows]
            except Exception:
                atoms = []
        self._cache[umo] = atoms
        return atoms

    def touch(self, umo: str, atom: MemoryAtom) -> None:
        """标记某条原子需要回写。

        `add()` 已经即时插入，但调用方常常在插入之后才补齐字段
        （固化时的 `metadata`、`source_memory_id`），这时必须显式登记，
        否则那次修改只活在内存里。
        """
        self._touched.setdefault(umo, set()).add(atom.id)

    def save(self, umo: str) -> None:
        """回写被 `touch()` 标记过的行。

        只更新**被改过的那几条**，不是整个会话 —— 旧实现正是在这里
        每次召回都重写 59 MB。
        """
        ids = self._touched.pop(umo, None)
        if not ids or not self._ready:
            return
        atoms = self._cache.get(umo) or []
        todo = [a for a in atoms if a.id in ids]
        if not todo:
            return
        try:
            with transaction(self.db_path) as con:
                con.executemany(_UPDATE_SQL, [_update_params(a) for a in todo])
        except Exception:
            pass

    def save_all(self) -> None:
        for umo in list(self._touched):
            self.save(umo)

    # ---- 写 ----
    def add(self, umo: str, text: str, vec, *,
            persona: str = "", uid: str = "", kind: str = "dialog",
            importance: float = 0.5, tags: list[str] | None = None,
            metadata: dict[str, Any] | None = None,
            dedup_threshold: float = 0.95,
            atom_id: str = "") -> MemoryAtom:
        atoms = self.load(umo)
        text = (text or "").strip()
        # 近重复直接丢弃（同义反复不该占两条记忆）
        if atoms and len(vec or ()):
            for a in atoms[-50:]:
                if len(a.vec) and cosine(a.vec, vec) >= dedup_threshold:
                    a.hits += 1
                    a.last_hit = time.time()
                    self.touch(umo, a)
                    return a
        # `atom_id` 供迁移用：不传就现生成一个
        atom = MemoryAtom(id=atom_id, text=text, vec=to_f32(vec),
                          persona=persona, uid=uid, kind=kind,
                          importance=max(0.0, min(1.0, importance)),
                          tags=list(tags or []), metadata=dict(metadata or {}),
                          umo=umo)
        atoms.append(atom)
        self._insert(atom)
        return atom

    def forget(self, umo: str, atom_id: str) -> bool:
        atoms = self.load(umo)
        for i, a in enumerate(atoms):
            if a.id == atom_id:
                atoms.pop(i)
                self._touched.get(umo, set()).discard(atom_id)
                self._delete([atom_id])
                return True
        return False

    def clear(self, umo: str) -> int:
        n = len(self.load(umo))
        self._cache[umo] = []
        self._touched.pop(umo, None)
        if self._ready:
            try:
                with connection(self.db_path) as con:
                    con.execute("DELETE FROM atoms WHERE umo=?", (umo,))
            except Exception:
                pass
        return n

    # ---- 读 ----
    def recall(self, umo: str, query_vec, *, top_k: int = 5,
               min_score: float = 0.35, half_life_days: float = 30.0,
               persona: str | None = "", uid: str | None = "",
               prefer_persona: str = "", other_factor: float = 1.0,
               strict_persona: bool = False, kind_decay: bool = True,
               superseded_factor: float = 1.0
               ) -> list[tuple[MemoryAtom, float]]:
        """向量 Top-K 召回。

        `persona`：硬过滤（只丢弃「明确是别人格」的，空 persona 保留）。
        `prefer_persona` / `other_factor` / `strict_persona`：人格策略的可选增强，
        默认不改变旧行为（`prefer_persona=""`、`other_factor=1.0`、`strict=False`）：
          * `strict_persona=True`：只保留 `persona == prefer_persona` 的条目；
          * `other_factor`：别人格条目的排序分乘以此系数（在 top_k 截断**之前**），
            用于「本命优先」降权。
        `kind_decay`：按记忆类型差异化半衰期（见 `KIND_HALF_LIFE`）。
        `superseded_factor`：被更新的记忆取代过的旧条目，排序分乘以此系数
        （`1.0` = 不降权；问「过去」的查询就该传 1.0）。
        """
        atoms = self.load(umo)
        if not atoms or not len(query_vec or ()):
            return []
        now = time.time()
        # 先按 persona/uid 过滤（布尔掩码，不进矩阵），再整批算余弦
        keep: list[MemoryAtom] = []
        for a in atoms:
            if persona and a.persona and a.persona != persona:
                continue
            if uid and a.uid and a.uid != uid:
                continue
            if strict_persona and prefer_persona and a.persona != prefer_persona:
                continue
            keep.append(a)
        if not keep:
            return []
        sims = _batch_cosine(query_vec, keep)
        scored: list[tuple[MemoryAtom, float]] = []
        for a, sim in zip(keep, sims):
            if sim < min_score:
                continue
            score = decayed_score(a, sim, now=now,
                                  half_life_days=half_life_days,
                                  kind_decay=kind_decay)
            if (other_factor != 1.0 and prefer_persona
                    and a.persona and a.persona != prefer_persona):
                score *= other_factor
            if superseded_factor != 1.0 and is_superseded(a):
                score *= superseded_factor
            scored.append((a, score))
        scored.sort(key=lambda kv: -kv[1])
        picked = scored[:max(0, top_k)]
        for a, _s in picked:
            a.hits += 1
            a.last_hit = now
            self.touch(umo, a)
        return picked

    def prune(self, umo: str, *, keep: int = 5000,
              min_importance: float = 0.05) -> int:
        """裁剪：保留分数最高的 keep 条，并丢掉重要度极低的陈旧条目。"""
        atoms = self.load(umo)
        if len(atoms) <= keep:
            return 0
        now = time.time()
        atoms.sort(key=lambda a: -decayed_score(a, 1.0, now=now,
                                                half_life_days=30.0))
        kept = [a for a in atoms[:keep] if a.importance >= min_importance]
        kept_ids = {a.id for a in kept}
        gone = [a.id for a in atoms if a.id not in kept_ids]
        self._cache[umo] = kept
        self._touched.pop(umo, None)
        self._delete(gone)
        return len(gone)

    # ---- 落盘细节 ----
    def _insert(self, atom: MemoryAtom) -> None:
        if not self._ready:
            return
        try:
            with connection(self.db_path) as con:
                con.execute(_INSERT_SQL, _insert_params(atom))
        except Exception:
            pass

    def _delete(self, atom_ids: list[str]) -> None:
        if not atom_ids or not self._ready:
            return
        try:
            with transaction(self.db_path) as con:
                con.executemany("DELETE FROM atoms WHERE id=?",
                                [(i,) for i in atom_ids])
        except Exception:
            pass

    # ---- 维度迁移 ----
    def count_stale(self, dim: int) -> int:
        """向量维度与 `dim` 不符（含完全没向量）的原子条数。

        换 embedding 模型后旧向量会整体失配 —— `cosine()` 对长度不同的
        向量只能返回 0.0，这些记忆就**静默失效**了（用户只看到"记忆突然
        全忘了"）。这里只做一次廉价计数，用来决定要不要真去重编码。
        """
        if dim <= 0 or not self._ready:
            return 0
        try:
            with connection(self.db_path) as con:
                row = con.execute(
                    "SELECT count(*) AS n FROM atoms"
                    " WHERE vec IS NULL OR length(vec) != ?",
                    (int(dim) * 4,)).fetchone()      # float32 = 4 字节
            return int(row["n"]) if row else 0
        except Exception:
            return 0

    def stale_atoms(self, dim: int, *, limit: int = 0,
                    skip: set[str] | None = None
                    ) -> list[tuple[str, MemoryAtom]]:
        """挑出待重编码的原子（维度不符 / 没向量），返回 `(umo, atom)`。

        `skip` 里的 atom id 不再返回 —— 后台重编码逐批推进，已经处理过的
        条目不该在下一批里又出现（失败的条目也算处理过，否则死循环）。
        """
        if dim <= 0 or not self._ready:
            return []
        try:
            with connection(self.db_path) as con:
                rows = con.execute(
                    "SELECT umo, id FROM atoms"
                    " WHERE vec IS NULL OR length(vec) != ?"
                    " ORDER BY ts, rowid", (int(dim) * 4,)).fetchall()
        except Exception:
            return []
        by_umo: dict[str, set[str]] = {}
        for r in rows:
            by_umo.setdefault(r["umo"], set()).add(r["id"])
        out: list[tuple[str, MemoryAtom]] = []
        for umo, ids in by_umo.items():
            for a in self.load(umo):
                if a.id not in ids:
                    continue
                if skip and a.id in skip:
                    continue
                out.append((umo, a))
                if limit and len(out) >= limit:
                    return out
        return out

    def stats(self) -> dict[str, Any]:
        sessions = total = 0
        if self._ready:
            try:
                with connection(self.db_path) as con:
                    rows = con.execute(
                        "SELECT umo, count(*) AS n FROM atoms GROUP BY umo"
                    ).fetchall()
                sessions = len(rows)
                total = sum(int(r["n"]) for r in rows)
            except Exception:
                pass
        size = 0
        for extra in ("", "-wal"):
            try:
                size += Path(str(self.db_path) + extra).stat().st_size
            except OSError:
                pass
        return {"sessions": sessions, "atoms": total, "bytes": size}
