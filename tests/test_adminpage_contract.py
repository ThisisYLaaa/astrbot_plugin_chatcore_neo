# -*- coding: utf-8 -*-
"""管理页与后端的接口契约：页面不能有"点了没反应"的死页签。"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PAGE = (Path(__file__).resolve().parent.parent
        / "pages" / "admin" / "index.html")


def _code() -> str:
    s = PAGE.read_text(encoding="utf-8")
    body = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", s,
                      re.DOTALL)[0]
    return body


def test_tab_list_matches_sections_and_loaders():
    """每个页签都必须：① 有对应的 <section> ② 有 LOADERS 实现。

    缺任何一个都会表现为"点了没反应" —— 这正是用户报过的问题。

    **页签 id 允许连字符**（`session-params`）。早期版本这里只认 `[a-z_]+`，
    于是 `session-params` 在两边都被静默跳过 —— 它明明没有 <section>，
    测试却全绿，线上表现就是点开一片空白。
    """
    code = _code()
    s = PAGE.read_text(encoding="utf-8")

    body = re.search(r"const TABS = \[(.*?)\n\];", code, re.S).group(1)
    tabs = re.findall(r'\[\s*"([a-z][a-z0-9_-]*)"\s*,\s*"([^"]+)"\s*,', body)
    assert tabs, "应能解析出页签定义"
    ids = [t[0] for t in tabs]

    # 允许"复用别的段"的子页（SUB_SECTION 里声明，如 config-template → config）
    alias_txt = re.search(r"const SUB_SECTION = \{(.*?)\};", code, re.S)
    alias = dict(re.findall(r'"([a-z][a-z0-9_-]*)"\s*:\s*"([a-z][a-z0-9_-]*)"',
                            alias_txt.group(1) if alias_txt else ""))

    # ① 有对应 section（别名字段复用目标段）
    for tid in ids:
        sec = alias.get(tid, tid)
        assert f'id="tab-{sec}"' in s, f"页签 {tid} 没有对应的 <section>（应为 tab-{sec}）"

    # ② 有 LOADERS 实现（点号与方括号两种写法都算）
    loaders = _loaders(code)
    for tid in ids:
        assert tid in loaders, \
            f"页签 {tid} 没有 LOADERS 实现（点了会没反应）"

    # 没有多余的 section / loader
    secs = re.findall(r'<section id="tab-([a-z][a-z0-9_-]*)"', s)
    want_secs = {alias.get(i, i) for i in ids}
    assert set(secs) == want_secs, f"section 与页签不一致: {set(secs) ^ want_secs}"
    assert loaders == set(ids), f"LOADER 与页签不一致: {loaders ^ set(ids)}"


def _loaders(code: str) -> set[str]:
    """收集 `LOADERS.x =` 与 `LOADERS["x"] =` 两种写法的 key。"""
    out: set[str] = set()
    for m in re.finditer(
            r'LOADERS(?:\.([A-Za-z0-9_]+)|\[\s*"([A-Za-z0-9_-]+)"\s*\])\s*=',
            code):
        out.add(m.group(1) or m.group(2))
    return out


def test_every_tab_has_a_tooltip_explaining_it():
    """每个页签都要有 title 说明，否则用户不知道它是干什么的。"""
    code = _code()
    body = re.search(r"const TABS = \[(.*?)\n\];", code, re.S).group(1)
    tabs = re.findall(r'\[\s*"([a-z][a-z0-9_-]*)"\s*,\s*"([^"]+)"\s*,\s*"([^"]+)"', body)
    assert tabs, "页签定义应带第三个参数（说明文字）"
    for tid, label, tip in tabs:
        assert len(tip.strip()) >= 4, f"页签 {label} 的说明太短: {tip!r}"


def _js_buttons(body: str):
    """切出 JS 模板里每个 `<button …>标签</button>` 的 (开标签, 可见文字)。"""
    for m in re.finditer(r"<button", body):
        seg = body[m.start():m.start() + 400]
        end = seg.find(">")
        if end <= 0:
            continue
        inner = ""
        close = seg.find("</button>")
        if close > end:
            inner = re.sub(r"<[^>]*>", "", seg[end + 1:close])
        yield seg[:end + 1], inner


def _label_is_clear(label: str) -> bool:
    """看得懂的文字标签：至少 2 个汉字（`×`/`⌄` 这类图标不算）。

    模板里混着拼接表达式（`' + esc(x) + '`），先去掉引号/加号/分号再数。
    """
    plain = re.sub(r"['\"+;]", "", label or "")
    return len(re.findall(r"[\u4e00-\u9fff]", plain)) >= 2


def test_buttons_have_titles_or_clear_labels():
    """按钮必须有 title（悬停说明）**或**足够清楚的中文标签。

    用户反馈过"意义不明"——所以要求每个按钮至少有一个 title 属性。
    """
    s = PAGE.read_text(encoding="utf-8")
    body = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", s,
                      re.DOTALL)[0]
    # 静态 HTML 里的按钮（**要排除 <script> 区域**，否则会把 JS 模板里的
    # 按钮当成静态按钮 —— 那些由下面的 JS 分支单独检查）
    html_only = re.sub(r"<script[^>]*>.*?</script>", "", s, flags=re.DOTALL)
    static = re.findall(r"<button[^>]*>", html_only)
    no_title = [b for b in static if "title=" not in b]
    assert not no_title, f"这些按钮没有 title 说明: {no_title}"

    # JS 里生成的按钮：`title=` **或**看得清的文字标签都算数（本用例的标题
    # 与文档字符串一直是这个口径）。
    # 2026-10-28：这里曾只认 `title=`，把「移出」「创建」「保存副人格」
    # 「加入所选」「移出所选」「全选/反选」这 6 个**标签本身就说明了一切**的
    # 按钮判成缺陷 —— 那是断言写漏了 OR 分支，不是页面缺 title：
    # ① 按钮有可见文字时，无障碍名字就来自文字，缺 title 不影响可读性；
    # ② 同页 51 个 JS 按钮里 45 个已有 title，缺的 6 个全是这类动作按钮。
    # 纯图标按钮（`×` 等）没有文字标签，仍然必须给 title。
    missing = [tag for tag, label in _js_buttons(body)
               if "title=" not in tag and not _label_is_clear(label)]
    assert not missing, f"JS 生成的按钮既没 title 也没有清楚的中文标签: {missing[:4]}"


def test_async_buttons_go_through_busy_helper():
    """会发请求的按钮必须走 busy()，否则重复点击会打出一堆请求且没有反馈。"""
    code = _code()
    assert "async function busy(" in code, "应有 busy() 助手"
    # 至少要有若干处使用
    assert code.count("busy(") >= 10, \
        f"busy() 用得很少（{code.count('busy(')} 处），按钮可能没有加载反馈"


def test_destructive_actions_are_marked_danger():
    """破坏性操作（清空/删除/遗忘）应标成 danger 且有确认。"""
    s = PAGE.read_text(encoding="utf-8")
    code = _code()
    for kw in ("清空该会话记忆", "清空状态日志"):
        assert kw in s, f"应有「{kw}」按钮"
    # 清空记忆必须有二次确认（插件页沙箱拦截原生 confirm，用页内 askConfirm）
    assert "askConfirm(" in code, "破坏性操作应有二次确认"

def test_config_page_has_floating_save_bar():
    """配置页：保存条固定在右下角，只在有改动时出现，且保存后不重建页面。

    用户 2026-10-30 反馈：以前保存按钮在卡片头，要保存得滚回顶部；
    而且点保存后页面被重建成初始状态。现在改成脏检查 + 浮动条。
    """
    s = PAGE.read_text(encoding="utf-8")
    code = _code()
    assert 'id="cfgSaveBar"' in code, "应有浮动保存条"
    assert "#cfgSaveBar{position:fixed" in s, "保存条应 position:fixed 固定在右下角"
    assert "#cfgSaveBar.on{display:flex}" in s, "保存条默认隐藏，有改动才显示"
    assert "#btnCfgReset" in code, "应有「撤销」按钮"
    # 保存回调里不能再调 LOADERS.config（那会重建整页、把页面甩回顶部）
    i = code.find('#btnCfgSave')
    assert i > 0, "应有保存按钮的事件绑定"
    seg = code[i:i + 3000]
    assert "keepScroll(el, function(){ return LOADERS.config(el); })" not in seg, \
        "保存后不该重建页面（旧写法会整页重建、滚动回顶）"
    assert "base = collectCfgValues()" in seg, "保存成功应重置脏基线"


def test_proactive_page_is_standalone_feature():
    """主动消息页：有功能默认参数卡，且不再出现实例级总闸文案。"""
    code = _code()
    for kw in ("功能默认参数", "btnPoDefSave", "proactive/defaults"):
        assert kw in code, f"主动消息页缺少 {kw}"
    for bad in ("启用主动消息", "全局开关"):
        assert bad not in code, f"主动消息页仍残留实例级总闸文案：{bad}"

def test_proactive_page_explains_scheduler_params():
    """主动消息页的参数必须自解释（用户 2026-10-30 反馈「间隔/空闲」看不懂）。

    要求两件事：
      * 标签本身说清"影响什么"（沉默多久才开口 / 两次主动最少隔 / 没人理时最多连发）；
      * 默认参数卡下面有一段总说明，解释四个参数各自的计时起点。
    """
    code = _code()
    for label in ("沉默多久才开口", "两次主动最少隔", "没人理时最多连发"):
        assert label in code, f"缺少自解释标签：{label}"
    assert "这四个参数分别影响什么" in code, "缺少四个参数的总说明"
    assert "以「用户最后一条消息」为起点" in code, "说明里应写清计时起点"
    for bad in (">间隔(分)</th>", ">空闲(分)</th>", "空闲阈值"):
        assert bad not in code, f"仍残留黑话标签/文案：{bad}"
    for kw in ("沉默触发(分)", "最短间隔(分)", "留空继承默认"):
        assert kw in code, f"缺少 {kw}"

def test_session_params_page_shows_inherited_default():
    """会话设置：继承项必须显示"继承成什么"，无覆盖时不显示「清除」。

    用户 2026-10-30 的疑问：为什么只有某个私聊看起来特殊 —— 其实是
    私聊默认（剧情向 + NSFW 开）与群聊默认（纯信息 + NSFW 关）不同。
    """
    code = _code()
    assert "default_label" in code, "继承标签应使用后端给出的默认值"
    assert "继承（全局：" in code, "私聊继承项要写出解析后的默认"
    assert "已覆盖" in code, "有显式覆盖的会话应有标记"
    assert "r.overridden" in code, "清除按钮应按是否覆盖决定显隐"

def test_groups_page_uses_resolved_persona_availability():
    """记忆组页不能拿 `grants` 当白名单：空授权表 = 不限制。

    用户 2026-10-30 报的现象：Banyan 的组人格下拉只有当前在用的「祀」。
    根因是页面用 `new Set(i.grants)` 过滤，而 Banyan 的 grants 是空的
    （= 全部可用）。可用性只在后端（PersonaPool）判断一次。
    """
    code = _code()
    assert "function availSet(" in code, "应有统一的可用人格判断"
    assert "m.unrestricted" in code, "要识别「空授权表 = 不限制」"
    assert "grants[inst]" not in code, "不能把 grants 当白名单直接用"
    assert "newGroupPersonaOpts" in code, "新建组的人格下拉也要按实例过滤"

def test_nav_groups_cover_every_tab_once():
    """6 个一级分组必须把每个段恰好覆盖一次，且子页 id 合法。

    导航重排（2026-10-30）：17 个平铺页 → 6 个一级 + 子页。
    这里钉住"不漏页、不重复页"，避免以后加页面忘了挂到某一组。
    """
    code = _code()
    body = re.search(r"const GROUPS = \[(.*?)\n\];", code, re.S).group(1)
    groups = re.findall(r'\{id:\s*"([a-z][a-z0-9_-]*)".*?subs:\s*\[(.*?)\]',
                        body, re.S)
    assert len(groups) == 6, f"应为 6 个一级分组，实际 {len(groups)}"
    subs: list[str] = []
    for gid, raw in groups:
        subs += re.findall(r'"([a-z][a-z0-9_-]*)"', raw)
    assert len(subs) == len(set(subs)), f"子页被重复挂载: {subs}"
    tabs_body = re.search(r"const TABS = \[(.*?)\n\];", code, re.S).group(1)
    tab_ids = re.findall(r'\[\s*"([a-z][a-z0-9_-]*)"\s*,', tabs_body)
    assert set(subs) == set(tab_ids), f"分组与段不一致: {set(subs) ^ set(tab_ids)}"
    alias_txt = re.search(r"const SUB_SECTION = \{(.*?)\};", code, re.S).group(1)
    alias = dict(re.findall(r'"([a-z][a-z0-9_-]*)"\s*:\s*"([a-z][a-z0-9_-]*)"',
                            alias_txt))
    assert alias.get("config-template") == "config", "模板子页应复用 config 段"


def test_scope_bar_and_deep_link_exist():
    """作用域栏 + 深链 + 记住上次位置：导航重排的三项配套能力。"""
    s = PAGE.read_text(encoding="utf-8")
    code = _code()
    for kw in ('id="scopeInst"', 'id="scopeBadge"', 'id="scopeBar"'):
        assert kw in s, f"缺少作用域栏元素 {kw}"
    for fn in ("function syncScope(", "function loadInstList(", "async function bootRoute("):
        assert fn in code, f"缺少 {fn}"
    assert "location.hash" in code and "ccn.sub" in code, "应有深链/记忆上次位置"

def test_select_options_are_sorted_by_pinyin():
    """所有下拉框（人格/会话/组名）都按首字母（中文拼音）排序。

    用户 2026-11-04 反馈：记忆组的「组人格」下拉不是按首字母。
    统一走 `sortedByName()`（Intl.Collator("zh-Hans-CN")），
    这样中英文混排时顺序也稳定（角色库/人格列表用的是同一口径）。
    """
    code = _code()
    assert "function sortedByName(" in code, "缺少统一下拉排序助手"
    assert 'Intl.Collator("zh-Hans-CN"' in code, "排序应使用中文拼音 collator"
    for kw in ("sortedByName(d.personas", "sortedByName(ps.personas",
               "sortedByName(SESSIONS.sessions", "sortedByName((SESSIONS"):
        assert kw in code, f"这个下拉没接入排序：{kw}"
    n = code.count("sortedByName(")
    assert n >= 8, f"排序接入点太少（{n} 处），可能有下拉漏了"

