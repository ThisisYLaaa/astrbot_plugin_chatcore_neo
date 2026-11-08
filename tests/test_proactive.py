# -*- coding: utf-8 -*-
"""W4 主动消息调度测试：免打扰时段 / 冷却 / 空闲阈值 / 配额。"""

from __future__ import annotations

import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.proactive import (ProactiveScheduler, in_quiet_hours,  # noqa: E402
                           parse_quiet_hours)


# ---------------------------------------------------------------- 时段解析

def test_parse_quiet_hours_same_day():
    w = parse_quiet_hours("13:00-15:30")
    assert w == (dtime(13, 0), dtime(15, 30))


def test_parse_quiet_hours_cross_midnight():
    w = parse_quiet_hours("23:30-07:30")
    assert w == (dtime(23, 30), dtime(7, 30))


def test_parse_quiet_hours_invalid():
    for bad in ("", "  ", "abc", "25:00-07:00", "23:30", "23:30-"):
        assert parse_quiet_hours(bad) is None, bad


def test_in_quiet_hours_same_day():
    w = parse_quiet_hours("13:00-15:30")
    assert in_quiet_hours(datetime(2026, 9, 12, 14, 0), w) is True
    assert in_quiet_hours(datetime(2026, 9, 12, 12, 59), w) is False
    assert in_quiet_hours(datetime(2026, 9, 12, 15, 30), w) is False  # 左闭右开


def test_in_quiet_hours_cross_midnight():
    w = parse_quiet_hours("23:30-07:30")
    assert in_quiet_hours(datetime(2026, 9, 12, 23, 45), w) is True
    assert in_quiet_hours(datetime(2026, 9, 12, 2, 0), w) is True
    assert in_quiet_hours(datetime(2026, 9, 12, 12, 0), w) is False
    assert in_quiet_hours(datetime(2026, 9, 12, 23, 0), w) is False


def test_in_quiet_hours_none_window():
    assert in_quiet_hours(datetime.now(), None) is False


# ---------------------------------------------------------------- 决策

def _sched(**kw) -> ProactiveScheduler:
    kw.setdefault("enabled", True)
    kw.setdefault("min_interval_min", 45)
    kw.setdefault("idle_threshold_min", 90)
    kw.setdefault("quiet_hours", "")
    kw.setdefault("jitter", 0.0)
    # 默认**关掉**静默连发上限：大多数用例测的是别的判据（空闲/冷却/免打扰…），
    # 开着它会让"连发几条之后被挡"意外干扰断言。专测它的用例自己显式传值。
    kw.setdefault("silent_max", 0)
    # **必须用临时 state_path**：默认值指向真实 plugin_data，
    # 而调度状态现在是持久化的 —— 不隔离就会读到正在运行的 bot 的会话
    # （实测 `test_candidates_returns_only_ready` 因此多出 3 个真实群会话）。
    import tempfile
    kw.setdefault("state_path", Path(tempfile.mkdtemp()) / "state.json")
    return ProactiveScheduler(**kw)


def _on(s: ProactiveScheduler, *umos: str) -> ProactiveScheduler:
    """把这些会话显式「开启」。

    **主动消息是逐会话 opt-in 的**：全局开关只负责"放行"，没被显式开启过的
    会话一律不开口（`should_emit` 返回 `session-not-enabled`）。所以凡是要测
    "后面那些判据"（空闲/冷却/配额/免打扰…）的用例，都得先这样把会话开起来，
    否则测到的是开关本身而不是被测的那条判据。
    """
    for u in umos:
        s.set_override(u, {"enabled": True})
    return s


def test_disabled_never_emits():
    s = _sched(enabled=False)
    s.note_user_message("umo1")
    ok, why = s.should_emit("umo1")
    assert not ok and why == "disabled"


# ---------------------------------------------------------------- 逐会话 opt-in

def test_global_switch_alone_opens_nothing():
    """全局开关只负责放行，**不开启任何会话**（opt-in）。

    全局打开 ≠ 所有会话都开口。否则"打开全局"就等于"给所有群都开"——
    既不是配置文件里那句"放行"的语义，也没人想要。
    """
    s = _sched(idle_threshold_min=1, jitter=0.0)
    now = time.time()
    s.note_user_message("umo1", "p")
    s.state.activity("umo1").last_user_ts = now - 3600   # 其它条件全都满足
    ok, why = s.should_emit("umo1", now)
    assert not ok and why == "session-not-enabled", why
    assert s.candidates(now) == []
    assert s.any_session_enabled() is False

    # 显式开启后才开口
    _on(s, "umo1")
    ok, why = s.should_emit("umo1", now)
    assert ok, why
    assert s.candidates(now) == ["umo1"]
    assert s.any_session_enabled() is True


def test_only_configured_sessions_emit():
    """同一个调度器里：开过的会话出话，没开过的不出话。"""
    s = _sched(idle_threshold_min=1, jitter=0.0)
    now = time.time()
    for u in ("open", "closed"):
        s.note_user_message(u, "p")
        s.state.activity(u).last_user_ts = now - 3600
    _on(s, "open")
    ok, why = s.should_emit("open", now)
    assert ok, why
    ok, why = s.should_emit("closed", now)
    assert not ok and why == "session-not-enabled", why
    assert s.candidates(now) == ["open"]


# ---------------------------------------------------------------- 分会话覆盖

def test_per_session_override():
    """分会话覆盖：开过的会话用覆盖参数，没开过的会话不开口；可持久化。"""
    s = _sched()
    now = time.time()
    s.note_user_message("umoA", "p1")
    s.note_user_message("umoB", "p1")
    _on(s, "umoA", "umoB")
    s.state.activity("umoA").last_user_ts = now - 95 * 60   # 全局空闲 90 分 ✓
    s.state.activity("umoB").last_user_ts = now - 95 * 60
    ok, why = s.should_emit("umoA", now)
    assert ok, why

    # A 覆盖：空闲阈值 200 分 → A 不满足；B 不受影响
    s.set_override("umoA", {"idle_threshold_min": 200})
    ok, why = s.should_emit("umoA", now)
    assert not ok and "idle" in why, why
    ok, why = s.should_emit("umoB", now)
    assert ok, why

    # 会话级禁用
    s.set_override("umoB", {"enabled": False})
    ok, why = s.should_emit("umoB", now)
    assert not ok and why == "disabled-by-session"

    # 覆盖参数归一化：非法免打扰时段被丢弃、负值被夹紧
    s.set_override("umoA", {"quiet_hours": "不是时间", "min_interval_min": -5,
                            "silent_max": 3})
    ov = s.override_for("umoA")
    assert "quiet_hours" not in ov
    assert ov["min_interval_min"] == 1
    assert ov["silent_max"] == 3

    # 持久化：重启后覆盖仍在
    s2 = _sched(state_path=s.state_path)
    assert s2.override_for("umoA")["idle_threshold_min"] == 200

    # 清除
    s.clear_override("umoA")
    assert "umoA" not in s.overrides()


def test_no_user_activity_never_emits():
    """从未说过话的会话不主动打扰（避免对着空会话自言自语）。"""
    s = _sched()
    _on(s, "umo_new")
    ok, why = s.should_emit("umo_new")
    assert not ok and why == "no-user-activity"


def test_idle_threshold_blocks_recent_activity():
    s = _sched(idle_threshold_min=90)
    now = time.time()
    s.note_user_message("umo1")
    _on(s, "umo1")
    ok, why = s.should_emit("umo1", now)
    assert not ok and why.startswith("idle")


def test_emits_after_idle():
    s = _sched(idle_threshold_min=90)
    now = time.time()
    s.note_user_message("umo1")
    _on(s, "umo1")
    s.state.activity("umo1").last_user_ts = now - 91 * 60
    ok, why = s.should_emit("umo1", now)
    assert ok, why


def test_cooldown_blocks_too_soon():
    s = _sched(idle_threshold_min=10, min_interval_min=45)
    now = time.time()
    a = s.state.activity("umo1")
    a.last_user_ts = now - 3600
    a.last_proactive_ts = now - 60        # 1 分钟前刚发过
    _on(s, "umo1")
    ok, why = s.should_emit("umo1", now)
    assert not ok and why.startswith("cooldown")


def test_candidates_returns_only_ready():
    s = _sched(idle_threshold_min=10)
    now = time.time()
    s.note_user_message("ready")
    s.note_user_message("notyet")
    _on(s, "ready", "notyet")
    s.state.activity("ready").last_user_ts = now - 3600
    s.state.activity("notyet").last_user_ts = now
    cands = s.candidates(now)
    assert cands == ["ready"], cands


def test_jitter_can_suppress():
    """抖动必须能实际压掉一部分触发（否则到点就发，像机器）。"""
    s = _sched(idle_threshold_min=1, jitter=1.0)   # 100% 抖动 → 必被压掉
    now = time.time()
    s.note_user_message("umo1")
    _on(s, "umo1")
    s.state.activity("umo1").last_user_ts = now - 3600
    ok, why = s.should_emit("umo1", now)
    assert not ok and why == "jitter"


# ---------------------------------------------------------------- 静默连发上限

def test_silent_cap_pauses_after_n_in_dead_group():
    """群里一直没人说话时，连发 N 条就暂停（默认 2）。

    线上症状（2026-09-16）：死群里 bot 按最小间隔一条接一条地自言自语，
    实测间隔 [60, 60, 60, 60] 分钟，一路发到撞闸才停 —— 因为群里没人时
    空闲判据**永远**满足（`last_user_ts` 不动），只剩 `min_interval` 在拦。
    """
    s = _sched(min_interval_min=1, idle_threshold_min=1, silent_max=2,
               jitter=0.0)
    now = time.time()
    umo = "G:GroupMessage:1"
    s.note_user_message(umo, "p")
    _on(s, umo)
    s.state.activity(umo).last_user_ts = now - 3600   # 群里很久没说话了

    for i in (1, 2):
        # 只测静默那道闸：把冷却清掉，免得 `min_interval` 先把它挡住
        s.state.activity(umo).last_proactive_ts = 0.0
        ok, why = s.should_emit(umo, now)
        assert ok, f"第 {i} 条不该被挡: {why}"
        s.note_proactive(umo)
    assert s.state.activity(umo).silent_streak == 2

    # 第 3 条：暂停（静默闸在冷却之前判定，所以这里直接就能看到 silent-cap）
    s.state.activity(umo).last_proactive_ts = 0.0
    ok, why = s.should_emit(umo, now)
    assert not ok and why == "silent-cap(2)", why
    assert s.candidates(now) == [], "打满静默上限后仍进了候选"


def test_silent_streak_resets_when_group_comes_alive():
    """群里有人说话 → 计数归零、恢复正常（「暂停」不是「永久停」）。"""
    s = _sched(min_interval_min=1, idle_threshold_min=1, silent_max=1,
               jitter=0.0)
    now = time.time()
    umo = "G:GroupMessage:1"
    s.note_user_message(umo, "p")
    _on(s, umo)
    s.state.activity(umo).last_user_ts = now - 3600
    s.note_proactive(umo)
    ok, why = s.should_emit(umo, now)
    assert not ok and why == "silent-cap(1)", why

    # 有人说话了 → 静默期结束
    s.note_user_message(umo, "p")
    assert s.state.activity(umo).silent_streak == 0
    a = s.state.activity(umo)
    a.last_user_ts = now - 3600          # 让它重新满足空闲
    a.last_proactive_ts = 0.0            # 跳过冷却，专测静默那道闸
    ok, why = s.should_emit(umo, now)
    assert ok, f"群里说话后仍开不了口: {why}"


def test_silent_cap_zero_means_unlimited():
    """填 0 = 关掉这条闸门（沿用旧行为）。"""
    s = _sched(min_interval_min=1, idle_threshold_min=1, silent_max=0,
               jitter=0.0)
    now = time.time()
    umo = "G:GroupMessage:1"
    s.note_user_message(umo, "p")
    _on(s, umo)
    for _ in range(5):
        s.note_proactive(umo)
    a = s.state.activity(umo)
    a.last_user_ts = now - 3600
    a.last_proactive_ts = 0.0
    ok, why = s.should_emit(umo, now)
    assert ok, why


def test_silent_cap_per_session_override():
    """分会话覆盖优先：管理页「静默上限」可按会话单独设。"""
    s = _sched(min_interval_min=1, idle_threshold_min=1, silent_max=5,
               jitter=0.0)
    now = time.time()
    umo = "G:GroupMessage:1"
    s.note_user_message(umo, "p")
    _on(s, umo)
    s.set_override(umo, {"silent_max": 1})
    assert s.override_for(umo)["silent_max"] == 1
    assert s.override_for(umo)["enabled"] is True, "合并覆盖时不该丢掉开关"
    s.note_proactive(umo)
    ok, why = s.should_emit(umo, now)
    assert not ok and why == "silent-cap(1)", why


def test_silent_streak_persists_across_restart():
    """连发计数要落盘：否则重启一次就能再连发一轮。"""
    s = _sched(min_interval_min=1, idle_threshold_min=1, silent_max=2,
               jitter=0.0)
    umo = "G:GroupMessage:1"
    s.note_user_message(umo, "p")
    s.note_proactive(umo)
    s.note_proactive(umo)
    s2 = _sched(state_path=s.state_path)
    assert s2.state.activity(umo).silent_streak == 2, \
        "重启后连发计数丢了（死群保护会失效）"


def test_report_shape():
    s = _sched()
    r = s.report()
    for k in ("enabled", "running", "sessions_on", "sessions_off", "emitted",
              "ticks", "min_interval_min", "idle_threshold_min", "quiet_hours",
              "tracked_sessions", "candidates", "errors"):
        assert k in r, k


def test_global_off_blocks_everything():
    """全局不放行时，会话开得再多也不开口。

    两级开关职责不同：全局 = 放行（关掉即整个功能停），会话级 = 该会话要不要。
    反过来（会话「开启」能把全局关闭顶开）会让配置文件里写着关闭、它却照发，
    「配置说了什么」与「实际做了什么」变成两套事实。
    """
    s = _sched(enabled=False)
    s.note_user_message("umoA", "p")
    ok, why = s.should_emit("umoA")
    assert not ok and why == "disabled", why

    # 会话「开启」→ 仍然被全局挡住，且原因不能降级成别的判据
    s.set_override("umoA", {"enabled": True})
    ok, why = s.should_emit("umoA")
    assert not ok and why == "disabled", why
    assert s.any_session_enabled(), "开启状态本身要原样存着（只是不生效）"

    # 没配置的会话同样被挡住
    s.note_user_message("umoB", "p")
    ok, why = s.should_emit("umoB")
    assert not ok and why == "disabled", why

    # 放行之后：开过的会话进入后续空闲判据，没开过的仍然不开口
    s.enabled = True
    ok, why = s.should_emit("umoA")
    assert not ok and why.startswith("idle"), why
    ok, why = s.should_emit("umoB")
    assert not ok and why == "session-not-enabled", why

    # 会话级「关闭」仍然有效：放行了也能把单个会话压掉
    s.set_override("umoA", {"enabled": False})
    ok, why = s.should_emit("umoA")
    assert not ok and why == "disabled-by-session", why


def test_start_does_not_run_when_global_off():
    """全局不放行 + 有会话开着 → 循环也**不能**起来。"""
    import asyncio
    s = _sched(enabled=False)
    s.set_override("umoA", {"enabled": True})
    assert s.any_session_enabled(), "开启状态存着，但不代表功能被放行"

    async def _run():
        s.start(lambda umo: None)
        assert s._task is None, "没放行时 start() 不许启动循环"
        # 放行要当场生效（apply_enabled），否则"改了不生效"
        assert s.apply_enabled(True) and s.running
        assert s.apply_enabled(False) is False and not s.running, \
            "撤销放行必须真的把循环停掉"
        await s.stop()

    asyncio.run(_run())


# ---------------------------------------------------------------- 真伪会话

def test_unknown_session_is_not_candidate_nor_emitted():
    """账本里已不是真会话的键：不出话、不进候选、只算残留。

    线上症状：管理页「各会话活跃度」冒出一堆
    `webchat:FriendMessage:webchat!astrbot!newapi-balance-e2e-*` ——
    测试脚本打进来的假会话语法上是合法 UMO，账本只增不减，于是永远留着。
    """
    dead = "webchat:FriendMessage:webchat!astrbot!newapi-balance-e2e-1"
    live = "Banyan:FriendMessage:100000001"
    s = _sched(idle_threshold_min=1, jitter=0.0)
    now = time.time()
    for u in (dead, live):
        s.note_user_message(u, "p")
        s.state.activity(u).last_user_ts = now - 3600
    _on(s, dead, live)          # 两个都开启，才能测到"真伪会话"这条判据
    s.set_known(lambda u: u == live)

    ok, why = s.should_emit(dead, now)
    assert not ok and why == "unknown-session", why
    assert s.candidates(now) == [live], s.candidates(now)
    assert s.residue() == [dead]
    r = s.report()
    assert r["residue_sessions"] == 1, r
    assert r["tracked_sessions"] == 1, r


def test_known_predicate_failure_fails_open():
    """判定函数抛错时必须放行（fail-open），否则一次目录读取失败就静默全停。"""
    def boom(_u):
        raise OSError("目录读不了")

    s = _sched(idle_threshold_min=1, jitter=0.0)
    now = time.time()
    s.note_user_message("umo1")
    _on(s, "umo1")
    s.state.activity("umo1").last_user_ts = now - 3600
    s.set_known(boom)
    ok, why = s.should_emit("umo1", now)
    assert ok, why


def test_forget_clears_activity_and_streak():
    """forget 要把活跃度与连发计数一起清掉，否则会话回来后会被幽灵状态挡住。"""
    s = _sched(idle_threshold_min=1, min_interval_min=1, silent_max=1)
    now = time.time()
    s.note_user_message("umo1")
    _on(s, "umo1")
    s.state.activity("umo1").last_user_ts = now - 3600
    s.note_proactive("umo1")            # 连发打满静默上限
    ok, why = s.should_emit("umo1", now)
    assert not ok and why == "silent-cap(1)", why

    assert s.forget("umo1") is True
    assert "umo1" not in s.state.sessions
    assert s.forget("umo1") is False
    # 清干净后同一会话可以立刻重新出话（forget 不动"已开启"这个开关）
    assert s.override_for("umo1").get("enabled") is True, "forget 不该把开启状态也删了"
    s.note_user_message("umo1")
    s.state.activity("umo1").last_user_ts = now - 3600
    ok, why = s.should_emit("umo1", now)
    assert ok, why


def test_ensure_started_after_global_switch_turned_on():
    """加载后用户把全局开关放行 → 循环必须能补起来。

    `start()` 只在插件 `initialize()` 时调一次；当时没放行就直接跳过了，
    之后即使打开它，循环也不会自己起来 —— 正是"设置好了不生效"。
    补起循环的入口是 `apply_enabled()`（`sync_config()` 保存配置后调用）。

    注意这里补的是**全局放行**。没放行时，给某个会话开启是补不出循环的
    （见 `test_start_does_not_run_when_global_off`）。
    """
    import asyncio
    s = _sched(enabled=False)
    s.start(lambda umo: None)
    assert s._task is None, "没放行时不该启动"

    s.set_override("umoA", {"enabled": True})

    async def _run():
        assert s.ensure_started() is False, "没放行，会话开启补不出循环"
        assert s.apply_enabled(True) is True, "放行后应补起循环"
        assert s.running
        await s.stop()
        assert not s.running

    asyncio.run(_run())

# ──────────────────── 独立功能化（2026-10-30）：功能默认 / 每实例隔离 / 作用域

def test_feature_settings_roundtrip():
    """功能级默认参数能存能读，且非法值被归一化。"""
    import json
    import tempfile
    from pathlib import Path as _P
    from core import proactive as pr

    old = pr.SETTINGS_PATH
    tmp = _P(tempfile.mkdtemp()) / "settings.json"
    pr.SETTINGS_PATH = tmp
    try:
        assert pr.load_settings() == pr.DEFAULT_SETTINGS, "没文件时应回落内置默认"
        saved = pr.save_settings({"min_interval": "30", "idle_minutes": 12,
                                  "silent_max": "0", "quiet_hours": "23:30-07:30"})
        assert saved["min_interval"] == 30, saved
        assert saved["idle_minutes"] == 12, saved
        assert saved["silent_max"] == 0 and saved["quiet_hours"] == "23:30-07:30"
        assert pr.load_settings() == saved
        # 非法值：耗时/时段都不会把文件写坏
        bad = pr.save_settings({"min_interval": "abc", "quiet_hours": "25:00-26:00"})
        assert bad["min_interval"] == pr.DEFAULT_SETTINGS["min_interval"]
        assert bad["quiet_hours"] == ""
        json.loads(tmp.read_text(encoding="utf-8"))
    finally:
        pr.SETTINGS_PATH = old


def test_state_is_per_instance_not_shared():
    """每个实例一份 state.json：一个实例的会话覆盖不能出现在另一个实例里。

    旧实现四个实例共用一个全局 state.json —— Banyan 里配的会话，
    Sui 的调度器也能读到，页面之间互相矛盾（用户 2026-10-30 报的问题）。
    """
    import tempfile
    from pathlib import Path as _P

    d = _P(tempfile.mkdtemp())
    a = ProactiveScheduler(state_path=d / "A" / "state.json")
    b = ProactiveScheduler(state_path=d / "B" / "state.json")
    a.set_override("Banyan:GroupMessage:1", {"enabled": True})
    assert "Banyan:GroupMessage:1" in a.overrides()
    assert b.overrides() == {}, "另一个实例不该看到这条覆盖"
    # 重新加载也不串（确认是落盘隔离，不是内存隔离假象）
    a2 = ProactiveScheduler(state_path=d / "A" / "state.json")
    b2 = ProactiveScheduler(state_path=d / "B" / "state.json")
    assert "Banyan:GroupMessage:1" in a2.overrides()
    assert b2.overrides() == {}


def test_scope_limits_candidates_to_own_instance():
    """scope 让每条循环只认自己实例的会话（人格/记忆组随之按实例走）。"""
    s = _sched(idle_threshold_min=1, jitter=0.0)
    s.set_scope(lambda umo: umo.startswith("Banyan:"))
    now = time.time()
    for umo in ("Banyan:GroupMessage:1", "Sui:GroupMessage:2"):
        s.set_override(umo, {"enabled": True})
        s.note_user_message(umo)
        s.state.activity(umo).last_user_ts = now - 3600
    ok_b, _ = s.should_emit("Banyan:GroupMessage:1", now)
    ok_s, _ = s.should_emit("Sui:GroupMessage:2", now)
    assert ok_b and ok_s, "should_emit 只回答单会话，不受 scope 影响"
    assert s.candidates(now) == ["Banyan:GroupMessage:1"], "候选只应包含本实例的会话"
    assert s.report()["sessions_on"] == 1, "统计口径也要按实例"


def test_schema_has_no_instance_level_proactive_keys():
    """配置 schema 里不许再有实例级主动消息键（任务 4：清残留）。"""
    import json
    from pathlib import Path as _P
    schema = json.loads((_P(__file__).resolve().parent.parent / "_conf_schema.json")
                        .read_text(encoding="utf-8"))
    for k in ("enable_proactive", "proactive_min_interval", "proactive_idle_minutes",
              "proactive_quiet_hours", "proactive_silent_max"):
        assert k not in schema, f"schema 仍残留实例级主动消息键：{k}"

def test_proactive_emit_is_bound_to_session_instance():
    """主动消息重入时必须切到该会话所属实例，并写回该实例的调度器。

    回归 2026-10-30：`_emit_proactive` 以前不切上下文，`note_proactive`
    也总记在 `self.scheduler`（默认实例）上 —— Sui 的主动消息会读到
    Banyan 的配置、状态也写进 Banyan 那份 state.json。
    """
    src = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
    assert 'async def _emit_proactive(self, umo: str, inst: str = "")' in src
    assert "self.use_inst(inst)" in src, "合成事件必须在目标实例上下文里跑"
    assert "self.end_inst(tok)" in src, "上下文要复位"
    assert "sched.note_proactive(umo)" in src, "状态要写回发起实例的调度器"
    assert "lambda umo, _i=iid: self._emit_proactive(umo, inst=_i)" in src, \
        "每条实例循环必须把自己的 id 绑进回调"

