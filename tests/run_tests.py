# -*- coding: utf-8 -*-
"""极简测试运行器（无需 pytest）。

为什么不装 pytest
-----------------
本项目要在用户的 AstrBot 解释器里直接跑，装依赖会污染那个环境。
这个运行器只用标准库：自动发现同目录下的 `test_*.py`，
收集 `test_*` 函数，逐个执行并汇总。

为什么默认多进程
----------------
每个用例几乎都要新建一套插件对象（读 131 个人格、建临时目录），
固定开销约 2.5s/用例 —— 全量 780+ 用例串行就要 5 分钟以上。
模块之间互不依赖，所以默认按**模块**分发到多个进程并行跑，
墙钟时间约等于「最慢的那个模块」+ 一次框架导入。

用法：
    python tests/run_tests.py            # 全部（默认 -j 8）
    python tests/run_tests.py persona    # 只跑名字含 persona 的模块
    python tests/run_tests.py -v         # 打印每个用例
    python tests/run_tests.py -j 1       # 强制串行（排障用）
    python tests/run_tests.py -j 16      # 更多进程
    python tests/run_tests.py --timing   # 末尾列出最慢的 10 个用例
"""
from __future__ import annotations

import importlib.util
import inspect
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = TESTS_DIR.parent


def _prepare_env() -> None:
    """把插件目录加进 sys.path，并把 cwd 挪到临时目录。

    挪 cwd **必须在导入任何被测代码之前**：AstrBot 框架会把 `data/`
    （t2i 模板、默认 cmd_config.json、temp/）按**相对路径**解析到当前
    工作目录，于是"在插件目录里跑测试"会在插件里长出一个 `data/`。
    本套测试全部走绝对路径（`Path(__file__).resolve().parent` /
    `tempfile.mkdtemp()`），挪 cwd 不影响它们。
    """
    if str(PLUGIN_DIR) not in sys.path:
        sys.path.insert(0, str(PLUGIN_DIR))
    os.chdir(tempfile.mkdtemp(prefix="ccn-tests-"))


class Skip(Exception):
    """用例跳过（环境不满足），不算失败。"""


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"_t_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _collect(path: Path):
    mod = _load_module(path)
    cases = [(n, o) for n, o in vars(mod).items()
             if n.startswith("test_") and callable(o)]
    cases.sort(key=lambda kv: inspect.getsourcelines(kv[1])[1])
    return cases


def _run_case(fn):
    """跑一个用例，返回 ("pass"|"skip"|"fail", 详情, 说明)。"""
    try:
        fn()
        return "pass", "", ""
    except Exception as e:  # noqa: BLE001 —— 用例里的任何异常都要被隔离
        # 用类名判定，避免 __main__ 与 run_tests 模块产生两个 Skip 类
        if type(e).__name__ == "Skip":
            return "skip", "", str(e)
        return "fail", traceback.format_exc(), str(e)
    except BaseException:  # 包括 SystemExit/KeyboardInterrupt —— 不能吞掉整个分片
        raise


_WORKER_CWD: str | None = None


def _enter_worker_cwd() -> None:
    """每个子进程用**自己**的临时 cwd（每个进程只切一次）。

    框架在**首次 import** 时会往 `data/cmd_config.json` 写配置快照，
    路径相对当前工作目录。并行跑时如果所有子进程共用父进程的临时目录，
    就会有 8 个进程同时 `os.replace` 同一个文件 —— 表现为偶发的
    `PermissionError: [WinError 5] ... cmd_config.json`，
    并且整个模块会"导入失败"，一次少跑几十个用例。
    """
    global _WORKER_CWD
    if _WORKER_CWD is None:
        _WORKER_CWD = tempfile.mkdtemp(prefix="ccn-tests-w-")
        os.chdir(_WORKER_CWD)


def _run_module(path_str: str) -> dict:
    """子进程入口：跑完一个模块，返回结构化结果（不打印，避免输出交错）。"""
    _enter_worker_cwd()
    path = Path(path_str)
    out = {"module": path.stem, "cases": 0, "passed": 0, "skipped": 0,
           "fails": [], "skips": [], "slow": [], "import_error": "", "seconds": 0.0}
    t0 = time.perf_counter()
    try:
        cases = _collect(path)
    except Exception:
        out["import_error"] = traceback.format_exc()
        out["seconds"] = time.perf_counter() - t0
        return out
    out["cases"] = len(cases)
    for name, fn in cases:
        t = time.perf_counter()
        status, detail, note = _run_case(fn)
        dur = time.perf_counter() - t
        if dur >= 1.0:
            out["slow"].append((dur, f"{path.stem}::{name}"))
        if status == "pass":
            out["passed"] += 1
        elif status == "skip":
            out["skipped"] += 1
            out["skips"].append((name, note))
        else:
            out["fails"].append((f"{path.stem}::{name}", note, detail))
    out["seconds"] = time.perf_counter() - t0
    return out


def _run_module_inprocess(path: Path, verbose: bool) -> dict:
    """串行模式（-j 1）：日志直接打到终端，便于排障。"""
    out = {"module": path.stem, "cases": 0, "passed": 0, "skipped": 0,
           "fails": [], "skips": [], "slow": [], "import_error": "", "seconds": 0.0}
    t0 = time.perf_counter()
    try:
        cases = _collect(path)
    except Exception:
        out["import_error"] = traceback.format_exc()
        out["seconds"] = time.perf_counter() - t0
        return out
    out["cases"] = len(cases)
    print(f"\n[{path.stem}] {len(cases)} 个用例")
    for name, fn in cases:
        t = time.perf_counter()
        status, detail, note = _run_case(fn)
        dur = time.perf_counter() - t
        if dur >= 1.0:
            out["slow"].append((dur, f"{path.stem}::{name}"))
        if status == "pass":
            out["passed"] += 1
            if verbose:
                print(f"  ✓ {name}")
        elif status == "skip":
            out["skipped"] += 1
            out["skips"].append((name, note))
            print(f"  ~ SKIP {name}" + (f"  ({note})" if note else ""))
        else:
            out["fails"].append((f"{path.stem}::{name}", note, detail))
            print(f"  ✗ FAIL {name}: {note}")
            if verbose:
                print(detail)
    out["seconds"] = time.perf_counter() - t0
    return out


MAX_SAFE_JOBS = 8


def _default_jobs() -> int:
    """默认并发数。

    实测：每个 worker 都要完整加载框架 + 131 人格（约 200-250 MB），
    这台机器 16 核 32 GB 但常驻占用高（AstrBot + 4 个 QQ + sd-webui，
    可用内存经常只有 7-9 GB）。`-j 16` 会直接触发原生崩溃
    （0xC0000005 访问冲突），所以上限压在 8，且取核数的一半。
    """
    cores = os.cpu_count() or 4
    return max(1, min(MAX_SAFE_JOBS, max(2, cores // 2)))


def _positive_int(text: str) -> int:
    try:
        n = int(str(text).strip())
    except ValueError:
        return 0
    return n if n > 0 else 0


def main() -> int:
    argv = list(sys.argv[1:])
    verbose = "-v" in argv
    timing = "--timing" in argv
    jobs = 0
    filters = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-j", "--jobs"):
            # `-j 8` 这种空格写法很常见，必须吃下后面的数字，
            # 否则 8 会被当成模块过滤词（曾经整个套件"没找到测试模块"）
            if i + 1 < len(argv) and _positive_int(argv[i + 1]):
                jobs = _positive_int(argv[i + 1])
                i += 2
                continue
            i += 1
            continue
        if a.startswith("--jobs=") or a.startswith("-j="):
            jobs = _positive_int(a.split("=", 1)[1]) or jobs
            i += 1
            continue
        if a.startswith("-j") and len(a) > 2:
            jobs = _positive_int(a[2:]) or jobs
            i += 1
            continue
        if a.startswith("-"):
            i += 1
            continue
        filters.append(a)
        i += 1
    if jobs <= 0:
        jobs = _default_jobs()

    _prepare_env()

    files = sorted(TESTS_DIR.glob("test_*.py"))
    if filters:
        files = [f for f in files if any(x in f.stem for x in filters)]
    if not files:
        print("没有找到测试模块")
        return 2

    t0 = time.perf_counter()
    results: list[dict] = []
    if jobs <= 1 or len(files) == 1:
        for f in files:
            results.append(_run_module_inprocess(f, verbose))
    else:
        import multiprocessing as mp
        if jobs > MAX_SAFE_JOBS:
            print(f"提示：-j {jobs} 超过实测安全上限 {MAX_SAFE_JOBS}"
                  f"（每个 worker 约 200-250 MB，过高会触发原生崩溃），已按上限执行")
            jobs = MAX_SAFE_JOBS
        ctx = mp.get_context("spawn")
        done: set[str] = set()
        pool = ctx.Pool(processes=min(jobs, len(files)))
        try:
            it = pool.imap_unordered(_run_module, [str(f) for f in files], chunksize=1)
            while True:
                try:
                    r = next(it)
                except StopIteration:
                    break
                done.add(r["module"])
                results.append(r)
        except Exception as e:  # noqa: BLE001 —— 子进程崩溃/管道断开都在这里
            # 原生崩溃（如 0xC0000005）会让整个 Pool 变得不可用。已经跑完的
            # 模块结果保留，剩下的改为串行，保证"要么慢、要么正确"，
            # 绝不因为并行而漏跑用例。
            print(f"\n!! 并行执行中断（{type(e).__name__}: {e}）——"
                  f"已完成的 {len(done)} 个模块保留，剩余模块改为串行")
        finally:
            try:
                pool.terminate()
                pool.join()
            except Exception:  # noqa: BLE001
                pass
        rest = [f for f in files if f.stem not in done]
        if rest:
            print(f"   串行补跑 {len(rest)} 个模块…")
            for f in rest:
                results.append(_run_module_inprocess(f, verbose))

        # 稳定输出：按模块名排序
        results.sort(key=lambda r: r["module"])
        for r in results:
            flag = "导入失败" if r["import_error"] else f"{r['cases']} 个用例"
            print(f"[{r['module']}] {flag}  通过 {r['passed']} 失败 {len(r['fails'])} "
                  f"跳过 {r['skipped']}  ({r['seconds']:.1f}s)")
            if verbose:
                for nm, _n in r["skips"]:
                    print(f"  ~ SKIP {nm}")
        if len(results) != len(files):
            print(f"!! 注意：结果数 {len(results)} 与模块数 {len(files)} 不一致")

    wall = time.perf_counter() - t0
    passed = sum(r["passed"] for r in results)
    skipped = sum(r["skipped"] for r in results)
    failures = []
    slow = []
    for r in results:
        if r["import_error"]:
            failures.append((f"{r['module']} <import>", r["import_error"]))
        for label, note, detail in r["fails"]:
            failures.append((label, detail if detail else note))
        slow.extend(r["slow"])
    failed = len(failures)

    print("\n" + "=" * 60)
    print(f"通过 {passed}   失败 {failed}   跳过 {skipped}"
          f"   模块 {len(results)}   进程 {jobs}   耗时 {wall:.1f}s")
    if timing and slow:
        print("\n最慢的 10 个用例:")
        for d, label in sorted(slow, reverse=True)[:10]:
            print(f"  {d:6.2f}s  {label}")
    if failures:
        print("\n失败详情:")
        for label, tb in failures:
            print(f"\n--- {label} ---")
            print(tb)
    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
