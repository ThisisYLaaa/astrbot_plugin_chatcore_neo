# -*- coding: utf-8 -*-
"""SQLite 底座：短连接、事务复用、BLOB 向量编解码。

为什么不留常驻连接
------------------
SQLite 连接持有 db 文件句柄。Windows 上实测：连接不关时
`tempfile.TemporaryDirectory.cleanup()` 直接抛 `PermissionError`
（WinError 32「另一个程序正在使用此文件」），测试里遍地都是临时目录。
所以这里不养连接 —— 每次操作开一个、用完就关；
需要连写多行时用 `transaction()` 复用同一个连接。

为什么向量存 BLOB
-----------------
4096 维向量写成十进制文本约 90 KB/条（实测 462 条 = 58 MB，其中 99.7%
是向量的文本形式）。打包成 float32 只要 16 KB。读回来用 `array('f')` 承载，
Python 常驻内存也从"每个 float 一个独立对象"的 ~131 KB/条 降到 16 KB/条。
"""
from __future__ import annotations

import contextlib
import sqlite3
import threading
import time
from array import array
from pathlib import Path

_local = threading.local()


# ---------------------------------------------------------------- 向量编解码

def pack_vec(vec) -> bytes | None:
    """list / array → float32 BLOB。空向量存 NULL（省 16 KB）。"""
    if vec is None:
        return None
    if isinstance(vec, array):
        if not vec:
            return None
        return vec.tobytes() if vec.typecode == "f" else array("f", vec).tobytes()
    if not len(vec):
        return None
    return array("f", (float(x) for x in vec)).tobytes()


def unpack_vec(blob) -> array:
    """BLOB → `array('f')`。坏数据返回空数组，不让一条烂向量崩掉召回。"""
    out = array("f")
    if not blob:
        return out
    try:
        out.frombytes(blob)
    except Exception:
        return array("f")
    return out


def to_f32(vec) -> array:
    """任意向量序列 → `array('f')`（内存里也用紧凑表示）。"""
    if isinstance(vec, array):
        return vec if vec.typecode == "f" else array("f", vec)
    if not vec:
        return array("f")
    return array("f", (float(x) for x in vec))


# ---------------------------------------------------------------- 连接

def _stack() -> list:
    s = getattr(_local, "stack", None)
    if s is None:
        s = _local.stack = []
    return s


def _open(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), timeout=5.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    return con


def _close(con: sqlite3.Connection) -> None:
    with contextlib.suppress(Exception):
        con.close()


@contextlib.contextmanager
def transaction(db_path):
    """把一段连续写操作并进同一个连接与事务。

    嵌套调用复用外层连接（只有最外层提交/回滚），
    这样"批量写"不需要调用方层层传 connection。
    """
    db_path = Path(db_path)
    for path, con in _stack():
        if path == db_path:
            yield con
            return
    con = _open(db_path)
    _stack().append((db_path, con))
    try:
        yield con
        con.commit()
    except BaseException:
        with contextlib.suppress(Exception):
            con.rollback()
        raise
    finally:
        _stack().pop()
        _close(con)


@contextlib.contextmanager
def connection(db_path):
    """单次操作：复用在用的事务连接，否则开一个短连接并自动提交。"""
    db_path = Path(db_path)
    for path, con in _stack():
        if path == db_path:
            yield con
            return
    con = _open(db_path)
    try:
        yield con
        con.commit()
    except BaseException:
        with contextlib.suppress(Exception):
            con.rollback()
        raise
    finally:
        _close(con)


# ---------------------------------------------------------------- 建库

def ensure_schema(db_path: Path, ddl: str) -> None:
    """建表（幂等），并把库切成 WAL。

    WAL 是**写进文件头**的持久属性，设一次就够，对已有库重复执行是廉价的。
    `synchronous=NORMAL` 在 WAL 下仍然崩溃安全，但少一次 fsync。
    最后主动读一次 `sqlite_master`：**文件损坏只有真读一次才会暴露**，
    否则问题会推迟到第一次业务查询时才炸出来。
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = _open(db_path)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        con.executescript(ddl)
        con.commit()
    finally:
        _close(con)


def quarantine(db_path: Path) -> Path | None:
    """把损坏的库改名留证（连带 -wal / -shm），返回新路径；失败返回 None。

    沿用旧 JSON 实现的策略：坏文件不阻断运行，**改名而不是删除**，
    这样用户还能自己捞数据。
    """
    db_path = Path(db_path)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = db_path.with_name(f"{db_path.name}.bad-{stamp}")
    moved = False
    for extra in ("", "-wal", "-shm"):
        src = Path(str(db_path) + extra)
        if not src.exists():
            continue
        try:
            src.replace(Path(str(target) + extra))
            moved = True
        except Exception:
            pass
    return target if moved else None


def open_ready(db_path: Path, ddl: str) -> bool:
    """建好库并返回是否可用；库损坏则改名重建。

    **被占用（OperationalError）不算损坏** —— 那种情况绝不能动文件，
    否则多进程同时启动时会把一份好库改成 .bad。
    """
    try:
        ensure_schema(db_path, ddl)
        return True
    except sqlite3.OperationalError:
        return False
    except Exception:
        pass
    if quarantine(db_path) is None:
        return False
    try:
        ensure_schema(db_path, ddl)
        return True
    except Exception:
        return False
