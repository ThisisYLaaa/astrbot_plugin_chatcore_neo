# -*- coding: utf-8 -*-
"""离线记忆召回评测（hit@5 / MRR / precision@5），给「排序类改动」一个量化基线。

为什么需要
----------
分类衰减、取代降权、top-k 去冗余这三项改的都是**排序**。没有数字基线的话，
"改完感觉好了"和"悄悄改坏了"分不出来 —— 而召回坏掉是**没有报错**的，
用户只会看到"AI 突然不记得了"。

数据从哪来（全部只读，不联网、不改真实数据）
-------------------------------------------
  * query：真实会话消息，抄在 `tests/data/memory_eval.json` 里（取自
    `memory/_pending.json`，即真实用户消息）；
  * query 向量：存在 `tests/data/memory_eval_vecs.json`（从真实 `memory.db` 的
    `embed_cache` 导出的一次性快照）—— 这些消息到达时被真实 embedding 过，
    所以向量是真的，跑分不需要 provider；放进评测集里是为了**不会因为换
    embedding 模型、缓存被清而失效**。缺失时回退到线上缓存查询；
  * 候选记忆：真实 `memory.db` 里的 `atoms`；
  * 跑在**真实库的临时副本**上：生产代码路径（`MemoryStore.recall` /
    `decayed_score`）原样参与，真实文件一个字节都不动。

标注口径
--------
`expect_any`：这条消息「理想召回」的记忆正文里应当出现的任一子串（人工标注）。
检索**按记忆键隔离**（本插件的设计），所以只在同一 key 内比对。

指标
----
  * `hit@5`        top-5 里至少命中 1 条理想记忆的 case 占比（越高越好）
  * `mrr`          第一条理想记忆排名倒数的均值（越高越好）
  * `precision@5`  top-5 里命中理想记忆的平均占比（越高越好；被去冗余直接改善）

⚠️ 读数注意：真实库里同时躺着 4096 维（旧 embedding 模型）与 1024 维（现模型）
的向量，**维度不符的记忆根本进不了候选**（`cosine` 对长度不同的向量只能返回
0.0）。所以报告里每个 case 都带 `qdim / reach`（query 维度 / 该键内同维度的
记忆条数）—— 这两个数一小，hit@5 低就不是排序问题，而是向量没迁移。

用法::
    python tests/memory_eval.py                  # 用真实数据跑，打印报告
    python tests/memory_eval.py --json           # 机器可读（改前/改后 diff）
    python tests/memory_eval.py --db <path> --cases <path> --top-k 5
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from array import array
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = TESTS_DIR.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from core.db import unpack_vec                        # noqa: E402
from core.memory.embedder import Embedder             # noqa: E402
from core.memory.retrieval import (DualRouteRetriever, RouteHit,  # noqa: E402
                                   asks_about_past, text_similarity)
from core.memory.service import SUPERSEDED_FACTOR, recall_gate  # noqa: E402
from core.memory.store import MemoryStore             # noqa: E402

DEFAULT_CASES = TESTS_DIR / "data" / "memory_eval.json"


def default_db() -> Path:
    """真实记忆库路径（从插件位置推导，不写死绝对路径）。"""
    from core.paths import MEMORY_DIR
    return MEMORY_DIR / "memory.db"


def vectors_path(cases_path: Path) -> Path:
    """向量快照跟着评测集走：`xxx.json` → `xxx_vecs.json`。"""
    p = Path(cases_path)
    return p.with_name(f"{p.stem}_vecs.json")


# ---------------------------------------------------------------- 输入

def load_cases(path: Path) -> list[dict]:
    """读评测集：`{key, query, expect_any?}`，顺序原样保留。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    raw = data.get("cases") if isinstance(data, dict) else data
    out: list[dict] = []
    for item in raw or []:
        key = str(item.get("key") or "").strip()
        query = str(item.get("query") or "").strip()
        if not key or not query:
            continue
        out.append({"key": key, "query": query,
                    "expect_any": [str(w) for w in (item.get("expect_any") or [])]})
    return out


def _load_vectors(path: Path) -> dict[str, array]:
    """读向量快照（`{sha256(text)[:32]: [float, ...]}`）；没有就返回空表。"""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, array] = {}
    for k, v in (raw or {}).items():
        if isinstance(v, list) and v:
            out[str(k)] = array("f", (float(x) for x in v))
    return out


def _cache_vectors(db_path: Path, texts: list[str]) -> dict[str, array]:
    """从 embed_cache 里取这些文本的向量（key = sha256(text)[:32]）。"""
    want = {Embedder.key_of(t) for t in texts}
    got: dict[str, array] = {}
    con = sqlite3.connect(str(db_path))
    try:
        for k in want:
            row = con.execute("SELECT vec FROM embed_cache WHERE key=?",
                              (k,)).fetchone()
            if row and row[0]:
                got[k] = unpack_vec(row[0])
    finally:
        con.close()
    return got


def _query_vectors(db_path: Path, texts: list[str],
                   snapshot: dict[str, array] | None = None
                   ) -> dict[str, array]:
    """快照优先（评测集不会因为换 embedding 模型而失效），缺失回退线上缓存。"""
    got: dict[str, array] = {}
    want = {Embedder.key_of(t) for t in texts}
    for k in want:
        v = (snapshot or {}).get(k)
        if v is not None and len(v):
            got[k] = v
    missing = [t for t in texts if Embedder.key_of(t) not in got]
    if missing:
        got.update(_cache_vectors(db_path, missing))
    return got


def dump_vectors(db_path: Path, cases: list[dict], out_path: Path) -> dict:
    """从线上 embed_cache 导出 query 向量快照（换 embedding 模型后重跑用）。"""
    got = _cache_vectors(db_path, [c["query"] for c in cases])
    data = {k: [round(float(x), 6) for x in v] for k, v in got.items()}
    Path(out_path).write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return {"written": len(data), "of": len(cases), "path": str(out_path)}


def _db_stats(db_path: Path) -> dict:
    """向量维度分布 —— 用来判断"召回差"到底是不是维度没迁移造成的。"""
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        by_dim: dict[int, int] = {}
        for r in con.execute("SELECT length(vec) b, count(*) n FROM atoms"
                             " GROUP BY b"):
            b = r["b"] or 0
            by_dim[b // 4] = by_dim.get(b // 4, 0) + int(r["n"])
        newest = con.execute(
            "SELECT length(vec) b FROM atoms ORDER BY ts DESC LIMIT 1").fetchone()
    finally:
        con.close()
    cur = (newest["b"] // 4) if newest and newest["b"] else 0
    total = sum(by_dim.values())
    return {"atoms": total, "by_dim": by_dim, "current_dim": cur,
            "unreachable": total - by_dim.get(cur, 0)}


# ---------------------------------------------------------------- 评测

def _redundancy(texts: list[str]) -> float:
    """top-k 两两正文相似度的均值（越低越不重复）—— 直接反映去冗余的效果。"""
    if len(texts) < 2:
        return 0.0
    vals = [text_similarity(texts[i], texts[j])
            for i in range(len(texts)) for j in range(i + 1, len(texts))]
    return round(sum(vals) / len(vals), 4)


def _grade(texts: list[str], expect: list[str]) -> tuple[bool, float, float]:
    """(是否命中, 排名倒数, 命中占比)。"""
    marks = [any(w in t for w in expect) for t in texts]
    ranks = [i + 1 for i, m in enumerate(marks) if m]
    return (bool(ranks),
            round(1.0 / ranks[0], 4) if ranks else 0.0,
            round(sum(marks) / len(marks), 4) if marks else 0.0)


def evaluate(db_path: Path, cases: list[dict], *, top_k: int = 5,
             min_score: float = 0.35, half_life_days: float = 30.0,
             kind_decay: bool = True, supersede: bool = True,
             mmr: bool = True, snapshot: dict[str, array] | None = None) -> dict:
    """在真实库的**临时副本**上跑一遍召回，返回逐条结果 + 汇总。

    参数默认值与 `_conf_schema.json` 里的出厂默认保持一致
    （memory_top_k=5 / memory_min_score=0.35 / memory_decay_days=30 /
    memory_kind_decay / memory_supersede / memory_mmr 都开）。

    每条 case 同时给出两份结果，方便隔离变量：
      * `raw`   —— **不去冗余**（纯按分数取 top-k），用来单独看打分项的效果；
      * `final` —— 去冗余后的结果，即线上默认行为。
    """
    cases = list(cases)
    workdir = Path(tempfile.mkdtemp(prefix="ccn-eval-"))
    # 连 -wal / -shm 一起复制：库是 WAL 模式，已经提交但还没 checkpoint 的
    # 数据只活在 -wal 里，只拷主文件会读到旧快照。
    src = Path(db_path)
    for suffix in ("", "-wal", "-shm"):
        extra = Path(str(src) + suffix)
        if extra.exists():
            shutil.copy2(extra, Path(str(workdir / "memory.db") + suffix))
    db = workdir / "memory.db"
    store = MemoryStore(workdir, db_path=db)
    vecs = _query_vectors(db, [c["query"] for c in cases], snapshot)
    retr = DualRouteRetriever({})

    rows: list[dict] = []
    for c in cases:
        key, query = c["key"], c["query"]
        pool = store.load(key)
        row = {"key": key, "query": query, "expect_any": list(c["expect_any"]),
               "gated": recall_gate(query), "pool": len(pool)}
        qv = vecs.get(Embedder.key_of(query))
        if qv is None:
            row["status"] = "no_query_vector"
            rows.append(row)
            continue
        row["query_dim"] = len(qv)
        row["reach"] = sum(1 for a in pool if len(a.vec) == len(qv))
        if not pool:
            row["status"] = "empty_pool"
            rows.append(row)
            continue
        if not row["reach"]:
            row["status"] = "no_reachable_memory"
            rows.append(row)
            continue
        sup = (1.0 if (not supersede or asks_about_past(query))
               else SUPERSEDED_FACTOR)
        got = store.recall(key, qv, top_k=max(top_k * 2, top_k),
                           min_score=min_score, half_life_days=half_life_days,
                           kind_decay=kind_decay, superseded_factor=sup)
        routes = [RouteHit(key=a.id, score=s, content=a.text)
                  for a, s in got]
        texts_raw = [h.content for h in retr.fuse(query, routes, [], k=top_k,
                                                  diversify=False)]
        texts = [h.content for h in retr.fuse(query, routes, [], k=top_k,
                                              diversify=mmr)]
        row["returned"] = len(texts)
        row["top1"] = texts[0][:60] if texts else ""
        row["redundancy_raw"] = _redundancy(texts_raw)
        row["redundancy"] = _redundancy(texts)
        if row["gated"]:
            row["status"] = "gated"          # 生产里根本不会查 → 不计入指标
        elif not row["expect_any"]:
            row["status"] = "unlabeled"
        else:
            row["status"] = "ok"
            hit, rr, prec = _grade(texts, row["expect_any"])
            hit_r, rr_r, prec_r = _grade(texts_raw, row["expect_any"])
            row.update(hit=hit, rr=rr, precision=prec,
                       hit_raw=hit_r, rr_raw=rr_r, precision_raw=prec_r)
        rows.append(row)

    graded = [r for r in rows if r["status"] == "ok"]

    def _avg(key: str) -> float:
        return (round(sum(r[key] for r in graded) / len(graded), 4)
                if graded else 0.0)

    summary = {
        "cases": len(rows),
        "graded": len(graded),
        "skipped": len(rows) - len(graded),
        "vectors": ("snapshot" if snapshot else "live_cache"),
        "raw": {"hit@5": _avg("hit_raw"), "mrr": _avg("rr_raw"),
                "precision@5": _avg("precision_raw"),
                "redundancy": _avg("redundancy_raw")},
        "final": {"hit@5": _avg("hit"), "mrr": _avg("rr"),
                  "precision@5": _avg("precision"),
                  "redundancy": _avg("redundancy")},
        "params": {"top_k": top_k, "min_score": min_score,
                   "half_life_days": half_life_days,
                   "kind_decay": kind_decay, "supersede": supersede,
                   "mmr": mmr},
    }
    return {"rows": rows, "summary": summary, "db": _db_stats(db)}


def format_report(result: dict, *, verbose: bool = False) -> str:
    s, db = result["summary"], result["db"]
    dims = ", ".join(f"{d}维×{n}" for d, n in sorted(db["by_dim"].items())
                     if d)
    p = s["params"]
    lines = [
        "=" * 78,
        f"记忆召回评测　库：{db['atoms']} 条记忆（{dims}）",
        f"　　当前 embedding 维度 = {db['current_dim']}；"
        f"**不可达（维度不符）= {db['unreachable']} 条**",
        "=" * 78,
        f"{'key':30} {'qdim':>4} {'pool':>5} {'reach':>5} {'回':>3} "
        f"{'hit':>4} {'rr':>6} {'prec':>6} {'冗余':>6}  query",
    ]
    for r in result["rows"]:
        mark = r.get("hit")
        lines.append(
            f"{r['key'][:30]:30} {r.get('query_dim', 0):>4} {r['pool']:>5} "
            f"{r.get('reach', 0):>5} {r.get('returned', 0):>3} "
            f"{('✓' if mark else ('✗' if r['status'] == 'ok' else '·')):>4} "
            f"{r.get('rr', 0):>6.3f} {r.get('precision', 0):>6.3f} "
            f"{r.get('redundancy', 0):>6.3f}  {r['query'][:30]!r}")
        if verbose and r["status"] not in ("ok", "gated"):
            lines.append(f"    ↳ status={r['status']}")
        if verbose and r.get("top1"):
            lines.append(f"    ↳ top1={r['top1']!r}")
    raw, fin = s["raw"], s["final"]
    lines += [
        "-" * 78,
        f"计入指标 {s['graded']}/{s['cases']} 条"
        f"（跳过 {s['skipped']}：无缓存向量 / 无同维度记忆 / 无标注 / 被闸门拦下）"
        f"　query 向量来源={s['vectors']}",
        f"不去冗余(raw)　hit@5={raw['hit@5']:.4f}  MRR={raw['mrr']:.4f}  "
        f"precision@5={raw['precision@5']:.4f}  冗余={raw['redundancy']:.4f}",
        f"线上默认(final) hit@5={fin['hit@5']:.4f}  MRR={fin['mrr']:.4f}  "
        f"precision@5={fin['precision@5']:.4f}  冗余={fin['redundancy']:.4f}",
        f"参数：top_k={p['top_k']} min_score={p['min_score']} "
        f"half_life_days={p['half_life_days']} kind_decay={p['kind_decay']} "
        f"supersede={p['supersede']} mmr={p['mmr']}",
        "=" * 78,
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="离线记忆召回评测")
    ap.add_argument("--db", default=None, help="真实 memory.db（只读，会复制副本）")
    ap.add_argument("--cases", default=None, help="评测集 JSON")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--min-score", type=float, default=0.35)
    ap.add_argument("--half-life", type=float, default=30.0)
    ap.add_argument("--no-kind-decay", action="store_true",
                    help="关掉分类差异化衰减（对比用）")
    ap.add_argument("--no-supersede", action="store_true",
                    help="关掉取代降权（对比用）")
    ap.add_argument("--no-mmr", action="store_true", help="关掉去冗余（对比用）")
    ap.add_argument("--dump-vectors", action="store_true",
                    help="从线上 embed_cache 重新导出 query 向量快照")
    ap.add_argument("--json", action="store_true", help="输出机器可读结果")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    db = Path(args.db) if args.db else default_db()
    cases_path = Path(args.cases) if args.cases else DEFAULT_CASES
    if not db.exists():
        print(f"记忆库不存在：{db}", file=sys.stderr)
        return 2
    cases = load_cases(cases_path)
    if args.dump_vectors:
        got = dump_vectors(db, cases, vectors_path(cases_path))
        print(f"已导出 {got['written']}/{got['of']} 条向量 → {got['path']}")
        if got["written"] < got["of"]:
            print("⚠️ 有 case 在缓存里找不到：换 embedding 模型后旧缓存会被清，"
                  "这些 query 需要重新挑（从 memory/_pending.json 里选）。")
        return 0
    result = evaluate(db, cases, top_k=args.top_k,
                      min_score=args.min_score, half_life_days=args.half_life,
                      kind_decay=not args.no_kind_decay,
                      supersede=not args.no_supersede,
                      mmr=not args.no_mmr,
                      snapshot=_load_vectors(vectors_path(cases_path)))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_report(result, verbose=args.verbose))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
