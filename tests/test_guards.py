# -*- coding: utf-8 -*-
"""静态守卫测试：把方案 11.4 的硬性约束变成会失败的测试。

这些约束如果只写在文档里，下一次改动就会悄悄违反。
这里用源码扫描把它们钉住 —— 违反时给出 文件:行号。
"""
from __future__ import annotations

import ast
import io
import re
import tokenize
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))

from run_tests import Skip  # noqa: E402

SKIP_DIRS = {"__pycache__", "tests", "templates", "migrations"}
# 允许出现 provider 解析的模块（都不做**对话生成**）：
#   core/proactive.py        —— 合成事件重入管道（唯一生成入口）
#   core/memory/embedder.py  —— 只做 embedding，不是生成；且在后台任务里用
#   core/judge.py            —— 分类判定（REPLY/NOP、意图分），不生成发给
#                               用户的文本、不碰主对话 req、temperature=0
#   core/impression.py       —— 印象摘要，同上（不写主对话 req、fail-open）
#   core/tts.py              —— 语音合成，读的是已生成的回复文本
#   core/segment_agent.py    —— 决定分段位置，只输出原文本+分隔符
#   core/memory/summarizer.py—— 把对话窗口总结成结构化记忆，不生成回复
ALLOW_PROVIDER_CALL_FILES = {"core/proactive.py", "core/memory/embedder.py",
                             "core/judge.py", "core/impression.py", "core/tts.py", "core/segment_agent.py", "core/memory/summarizer.py"}

# 真正的"对话生成"接口：这些出现在任何地方都违规（除非走管道）
CHAT_GENERATION_PATTERNS = (r"\.text_chat\(", r"\.text_chat_stream\(")


def _py_files() -> list[Path]:
    out = []
    for p in PLUGIN.rglob("*.py"):
        rel = p.relative_to(PLUGIN).as_posix()
        if any(part in SKIP_DIRS for part in p.relative_to(PLUGIN).parts[:-1]):
            continue
        if rel.startswith("migrations/"):
            continue
        out.append(p)
    return sorted(out)


def _code_lines(f: Path) -> list[str]:
    """返回"把注释与字符串字面量抹成空格"后的源码行（行号保持对齐）。

    守卫必须只看**代码**：注释和文档里出现 `star_map`、`extra_user_content_parts`
    这类词是正常的说明文字，不该让测试变红。
    """
    src = f.read_text(encoding="utf-8")
    lines = src.splitlines()
    blanked = [list(line) for line in lines]
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except Exception:
        return lines
    for tok in toks:
        if tok.type not in (tokenize.COMMENT, tokenize.STRING):
            continue
        (sr, sc), (er, ec) = tok.start, tok.end
        for r in range(sr, er + 1):
            if not (1 <= r <= len(blanked)):
                continue
            line = blanked[r - 1]
            c0 = sc if r == sr else 0
            c1 = ec if r == er else len(line)
            for c in range(c0, min(c1, len(line))):
                line[c] = " "
    return ["".join(x) for x in blanked]


def _hits(pattern: str, *, files: list[Path] | None = None,
          skip: set[str] | None = None, code_only: bool = True,
          raw: bool = False) -> list[str]:
    rx = re.compile(pattern)
    skip = skip or set()
    found = []
    for f in (files or _py_files()):
        rel = f.relative_to(PLUGIN).as_posix()
        if rel in skip:
            continue
        if raw:
            # 原样扫描（类名正是写在字符串里，抹掉字符串就抓不到了），
            # 但跳过"整行都是注释或文档字符串"的行 —— 说明文字里提到类名是正常的。
            src_lines = f.read_text(encoding="utf-8").splitlines()
            code = _code_lines(f)
            candidates = [
                (src_lines[i] if i < len(code) and code[i].strip() else "")
                for i in range(len(src_lines))
            ]
        elif code_only:
            candidates = _code_lines(f)
        else:
            candidates = f.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(candidates, 1):
            if rx.search(line):
                found.append(f"{rel}:{i}: {line.strip()[:110]}")
    return found

# ---------------------------------------------------------------- 11.4-6 禁反射

def test_no_star_map_lookup():
    hits = _hits(r"star_map")
    assert not hits, "禁止使用 star_map 反射查找其他插件:\n" + "\n".join(hits)


def test_no_class_name_string_matching():
    """旧耦合的病根：按类名匹配别的插件。"""
    hits = _hits(r'cls_name|__class__\.__name__|type\(.*\)\.__name__\s*==')
    assert not hits, "禁止按类名字符串匹配插件/对象:\n" + "\n".join(hits)


def test_no_foreign_plugin_name_sniffing():
    hits = _hits(r"LiliStatePlugin|LivingMemory|ProactiveChat|chatcore_lite",
                 raw=True)
    # 允许出现在注释/docstring 里的历史说明，这里只禁代码里出现类名比较
    hits = [h for h in hits if "==" in h or "!=" in h or "in " in h]
    assert not hits, "禁止按其他插件类名做判定:\n" + "\n".join(hits)


# ---------------------------------------------------------------- 11.4-1 唯一生成入口

def test_no_direct_chat_generation_outside_pipeline():
    """硬性约束 1：除合成事件重入管道外，任何模块不得直接做对话生成。"""
    bad = []
    for pat in CHAT_GENERATION_PATTERNS:
        bad += _hits(pat, skip=ALLOW_PROVIDER_CALL_FILES)
    assert not bad, ("任何模块都不得直接调用对话生成接口"
                     "（必须经原生管道）:\n" + "\n".join(bad))


def test_provider_resolution_confined_to_allowlist():
    """provider 解析只允许出现在白名单模块里，避免悄悄长出新调用点。"""
    hits = _hits(r"get_provider_by_id|get_all_providers|get_using_provider",
                 skip=ALLOW_PROVIDER_CALL_FILES)
    assert not hits, ("provider 解析只允许出现在 "
                      f"{sorted(ALLOW_PROVIDER_CALL_FILES)}:\n" + "\n".join(hits))


# ---------------------------------------------------------------- 11.4-2 禁写 req.contexts

# 允许的"只减不增"操作：把上下文交给这些函数做剥离。
# 方案 3.5.1-③ 明确要求靠 strip 清掉被取代的工具结果，
# 所以 `req.contexts = strip_...(...)` 是**必须存在**的写法；
# 禁止的是往 contexts 里追加内容（那会落库并在历史里累积）。
_STRIP_ALLOWLIST = ("strip_tool_results", "strip_previous", "history_hygiene")


def _is_strip_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    fn = node.func
    name = fn.id if isinstance(fn, ast.Name) else (
        fn.attr if isinstance(fn, ast.Attribute) else "")
    return any(tok in name for tok in _STRIP_ALLOWLIST)


def test_no_injection_into_req_contexts():
    """L3/L4 动态内容只能走 extra_user_content_parts；写 contexts 会落库。"""
    bad = []
    for f in _py_files():
        rel = f.relative_to(PLUGIN).as_posix()
        src = f.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError as e:
            bad.append(f"{rel}: 语法错误 {e}")
            continue
        lines = src.splitlines()

        def flag(node):
            bad.append(f"{rel}:{node.lineno}: {lines[node.lineno-1].strip()[:110]}")

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if _is_req_contexts(t) and not _is_strip_call(node.value):
                        flag(node)
            if isinstance(node, ast.AugAssign) and _is_req_contexts(node.target):
                flag(node)
            # 只禁"添加型"方法调用；读取/list() 之类不算
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in ("append", "extend", "insert", "add") \
                        and _is_req_contexts(node.func.value):
                    flag(node)
    assert not bad, ("禁止往 req.contexts 注入内容（会落库并累积）；"
                     "只允许交给 strip 函数做剥离:\n" + "\n".join(bad))


def _is_req_contexts(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "contexts"
            and isinstance(node.value, ast.Name) and node.value.id in
            {"req", "request", "provider_request"})


# ---------------------------------------------------------------- 11.4-7 禁字符串嗅探

def test_no_series_sniffing_in_runtime_code():
    """系列/world 必须来自 persona.yaml 的外键，不再 split 目录名。"""
    bad = []
    for f in _py_files():
        rel = f.relative_to(PLUGIN).as_posix()
        if rel.startswith("core/persona/"):
            # registry/outline 里对**章节标题**做归一化是合法的解析，
            # 但也不该出现对人格目录名的 split("_")
            pass
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"split\(\s*['\"]_['\"]\s*\)", line):
                bad.append(f"{rel}:{i}: {line.strip()[:110]}")
    assert not bad, ("禁止用 split('_') 从目录名嗅探系列/世界"
                     "（迁移期一次性推断已固化为 persona.yaml 数据）:\n"
                     + "\n".join(bad))


def test_no_hardcoded_world_display_names():
    hits = _hits(r"_WORLD_DISPLAY_NAMES|_persona_series|_build_world_hint")
    assert not hits, "世界观显示名/系列解析已废弃，应改为数据驱动:\n" + "\n".join(hits)


# ---------------------------------------------------------------- 11.4-3/4/5 落库控制

def test_no_heavy_vector_dependencies_imported():
    hits = _hits(r"^\s*(import|from)\s+(faiss|networkx|jieba)\b")
    assert not hits, "记忆层不引入 faiss/networkx/jieba:\n" + "\n".join(hits)


def test_dynamic_injection_uses_mark_as_temp():
    """凡往 extra_user_content_parts 追加动态内容的模块，必须 mark_as_temp()。"""
    bad = []
    for f in _py_files():
        # 只看代码：注释/文档里提到 extra_user_content_parts 是说明文字，不算使用
        code = "\n".join(_code_lines(f))
        if "extra_user_content_parts" not in code:
            continue
        rel = f.relative_to(PLUGIN).as_posix()
        if "mark_as_temp" not in code:
            bad.append(f"{rel}: 使用了 extra_user_content_parts 但没有 mark_as_temp")
    assert not bad, ("动态内容必须 mark_as_temp() 以免落库:\n" + "\n".join(bad))


def test_world_path_traversal_guard_exists():
    """world_read 之类的路径参数必须做越界校验。"""
    p = PLUGIN / "core" / "world.py"
    if not p.exists():
        raise Skip("core/world.py 尚未实现（W2）")
    src = p.read_text(encoding="utf-8")
    assert ("resolve()" in src or ".." in src), \
        "世界观文件读取缺少路径越界校验"


# ---------------------------------------------------------------- 结构卫生

def test_no_syntax_errors_anywhere():
    bad = []
    for f in _py_files():
        try:
            ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError as e:
            bad.append(f"{f.relative_to(PLUGIN)}:{e.lineno}: {e.msg}")
    assert not bad, "存在语法错误:\n" + "\n".join(bad)


def test_plugin_entrypoint_does_not_use_deprecated_register():
    """`@register` 已废弃（框架 v3.5.19 后自动识别 `Star` 子类）。

    旧断言要求"必须有 @register"，本身已过期 —— 它会阻止我们
    删掉废弃 API（保留只会发 DeprecationWarning）。
    """
    src = (PLUGIN / "main.py").read_text(encoding="utf-8")
    assert "class ChatCoreNeoPlugin(Star)" in src, "入口类必须继承 Star"
    assert "@register(" not in src, (
        "@register 已废弃，框架会自动识别 Star 子类")
    star_import = src.split("from astrbot.api.star import")[1].split("\n")[0]
    assert "register" not in star_import, "不应再导入已废弃的 register"

def test_all_core_modules_import_cleanly():
    """逐个 import，确保没有循环依赖或缺失依赖。"""
    import importlib
    mods = [
        "core.paths", "core.sessionmap",
        "core.persona.registry", "core.persona.outline", "core.persona.model",
        "core.persona.render", "core.persona.doctor", "core.persona.authoring",
        "core.persona.service", "core.persona",
    ]
    for m in mods:
        importlib.import_module(m)


# ---------------------------------------------------------------- 配置卫生

def test_every_schema_key_is_used_by_code():
    """**每个配置项都必须被代码真的读过。**

    理由：我犯过这个错 —— 声明了 `reply_mode: takeover`、`group_card_template`
    等 11 个配置项，却没有任何代码读它们。用户会在 WebUI 里看到一堆
    改了没反应的开关，这比没有这个开关更糟（是幻觉能力）。
    """
    import json
    import re

    schema_path = PLUGIN / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    src: dict[str, str] = {}
    for f in PLUGIN.rglob("*.py"):
        rel = f.relative_to(PLUGIN)
        if "__pycache__" in str(f) or rel.parts[0] in ("tests", "migrations"):
            continue
        src[rel.as_posix()] = f.read_text(encoding="utf-8")

    phantom = []
    for key in schema:
        pat = re.compile(r'["\']' + re.escape(key) + r'["\']')
        if not any(pat.search(s) for s in src.values()):
            phantom.append(key)
    assert not phantom, (
        "以下配置项声明了但没有任何代码读取（幽灵开关，要么接线要么删掉）:\n"
        + "\n".join(f"  - {k}: {schema[k].get('description', '')[:50]}"
                    for k in phantom))


def test_schema_is_valid_json_and_has_defaults():
    import json
    schema = json.loads((PLUGIN / "_conf_schema.json").read_text(encoding="utf-8"))
    assert schema, "配置 schema 不应为空"
    for k, v in schema.items():
        assert "default" in v, f"{k} 缺少 default"
        assert v.get("type") in ("bool", "int", "float", "string", "list", "text"), \
            f"{k} 的类型 {v.get('type')!r} 不受支持"


def test_schema_default_matches_declared_type():
    """**default 的类型必须和 type 一致。**

    我犯过这个错：用 PowerShell 助手函数批量加配置时参数少传了一个，
    结果 range 哈希表落到了 default 上 ——
    `graph_max_topics: {"type":"int","default":{"min":1,"max":20}}`。
    WebUI 会显示一个字典，`int(cfg)` 直接抛异常。
    """
    import json
    schema = json.loads((PLUGIN / "_conf_schema.json").read_text(encoding="utf-8"))
    ok = {"bool": bool, "int": int, "float": (int, float),
          "string": str, "text": str, "list": list}
    bad = []
    for k, v in schema.items():
        t, d = v.get("type"), v.get("default")
        want = ok.get(t)
        if want is None:
            continue
        # bool 是 int 的子类，要单独判，否则 True 会被当成合法 int
        if t == "bool" and not isinstance(d, bool):
            bad.append((k, t, d))
        elif t in ("int", "float") and isinstance(d, bool):
            bad.append((k, t, d))
        elif not isinstance(d, want):
            bad.append((k, t, d))
    assert not bad, "以下配置的 default 与 type 不符:\n" + "\n".join(
        f"  {k}: type={t} default={d!r}" for k, t, d in bad)


def test_schema_options_and_labels_line_up():
    """`options` 与 `labels` 必须等长（否则下拉框显示错位）。"""
    import json
    schema = json.loads((PLUGIN / "_conf_schema.json").read_text(encoding="utf-8"))
    bad = []
    for k, v in schema.items():
        opts, labels = v.get("options"), v.get("labels")
        if opts and labels and len(opts) != len(labels):
            bad.append((k, len(opts), len(labels)))
    assert not bad, f"options/labels 长度不一致: {bad}"
    # labels 存在时必须有 options，反之亦然
    for k, v in schema.items():
        if v.get("labels") and not v.get("options"):
            raise AssertionError(f"{k} 有 labels 却没有 options")


def test_schema_option_defaults_are_in_options():
    """枚举项的 default 必须在自己的 options 里，否则 WebUI 显示空白。"""
    import json
    schema = json.loads((PLUGIN / "_conf_schema.json").read_text(encoding="utf-8"))
    bad = []
    for k, v in schema.items():
        opts = v.get("options")
        if opts and v.get("default") not in opts:
            bad.append((k, v.get("default"), opts))
    assert not bad, f"default 不在 options 中: {bad}"


def test_schema_conditions_point_at_real_keys():
    """`condition` 引用的 key 必须存在，否则该项永远不显示。"""
    import json
    schema = json.loads((PLUGIN / "_conf_schema.json").read_text(encoding="utf-8"))
    missing = []
    for k, v in schema.items():
        cond = v.get("condition") or {}
        for ref in cond:
            if ref.split(".")[0] not in schema:
                missing.append((k, ref))
    assert not missing, f"condition 引用了不存在的配置项: {missing}"


def test_condition_cycles_would_hide_settings():
    """父开关之间不能互相依赖成环 —— 那会让整组设置都点不出来。"""
    import json
    schema = json.loads((PLUGIN / "_conf_schema.json").read_text(encoding="utf-8"))
    for k, v in schema.items():
        for ref in (v.get("condition") or {}):
            parent = schema.get(ref.split(".")[0]) or {}
            for back in (parent.get("condition") or {}):
                assert back.split(".")[0] != k, \
                    f"{k} 与 {ref} 的 condition 互相依赖，会都显示不出来"


def test_no_phantom_or_dead_config_documented():
    """确认已删掉那个空壳开关（`reply_mode`）。"""
    import json
    schema = json.loads((PLUGIN / "_conf_schema.json").read_text(encoding="utf-8"))
    assert "reply_mode" not in schema, \
        "reply_mode(takeover) 是空壳开关，已删除，不应再次出现"


def test_no_config_read_that_schema_does_not_declare():
    """**反向检查**：代码里读的配置 key，必须在 schema 里声明过。

    原守卫只管"声明了但没读"（幽灵开关）。
    反过来同样有害：`_cfg("foo")` 读了 foo 而 schema 没声明，
    用户在 WebUI 里既看不到也改不了，插件却按硬编码默认值在跑。

    只认**函数名含 cfg 的调用 + 字符串字面量第一参数**：
    这样可以精确定位配置读取，不会把满地的 `dict.get("x")` 算进来。
    """
    import ast
    import json

    schema = json.loads((PLUGIN / "_conf_schema.json").read_text(encoding="utf-8"))
    declared = set(schema)

    # 这些不是插件配置项，是运行时数据的键（按函数名区分不了，列白名单）
    ALLOW = set()

    problems: list[str] = []
    for f in PLUGIN.rglob("*.py"):
        rel = f.relative_to(PLUGIN)
        if "__pycache__" in str(f) or rel.parts[0] in ("tests", "migrations"):
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else (
                fn.id if isinstance(fn, ast.Name) else "")
            if "cfg" not in name.lower():
                continue
            a0 = node.args[0]
            if not isinstance(a0, ast.Constant) or not isinstance(a0.value, str):
                continue      # 变量/三元表达式传 key 的，静态看不见，跳过
            key = a0.value
            if key not in declared and key not in ALLOW:
                problems.append(f"{rel.as_posix()}:{node.lineno} 读了未声明的配置 {key!r}")

    assert not problems, (
        "以下配置项被代码读取，但 _conf_schema.json 没有声明 —— "
        "用户在 WebUI 里改不了它们：\n  " + "\n  ".join(problems))
