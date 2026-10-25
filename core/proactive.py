# -*- coding: utf-8 -*-
"""主动消息调度：合成事件重入原生管道（方案 3.4 / 3.6）。

这是整个整合方案里**唯一**允许触发生成的模块，而它触发生成的方式
不是调用 provider，而是把合成事件塞回事件队列 —— 于是主动消息和
用户消息走**完全同一条**管道，所有钩子（人格注入、状态更新、记忆写入）
自动生效。旧架构里那批"如果类名是 X 就补一次注入"的补偿代码随之消失。

本模块可独立测试的部分
----------------------
* 免打扰时段判定
* 最小间隔 / 空闲阈值判定
* 候选会话挑选与冷却

需要真实环境验证的部分
----------------------
* `synthesize_event()` 依赖平台适配器**自己**造事件
  （`Platform.create_event(AstrBotMessage)`）。
  拿不到适配器或模板时返回 None 并记日志，**不会**抛异常影响主流程。
"""
from __future__ import annotations

import asyncio
import json
import random
import time
import logging
from dataclasses import dataclass, field
from datetime import datetime, time as dtime

# 用 stdlib logger 而不是 `astrbot.api.logger`：
# core/ 下的模块要能在没有框架的环境里被导入（测试就是这么跑的），
# AstrBot 自己的 logging handler 会接住 "astrbot" 这个 logger。
logger = logging.getLogger("astrbot")
from typing import Any, Callable

from . import compat
from .paths import PROACTIVE_DIR


# ---------------------------------------------------------------- 配置

def parse_quiet_hours(spec: str) -> tuple[dtime, dtime] | None:
    """解析 `23:30-07:30`。跨零点也支持；解析失败返回 None。"""
    spec = (spec or "").strip()
    if not spec or "-" not in spec:
        return None
    a, _, b = spec.partition("-")
    try:
        h1, m1 = (int(x) for x in a.strip().split(":"))
        h2, m2 = (int(x) for x in b.strip().split(":"))
        return dtime(h1, m1), dtime(h2, m2)
    except Exception:
        return None


def in_quiet_hours(now: datetime, window: tuple[dtime, dtime] | None) -> bool:
    if window is None:
        return False
    start, end = window
    t = now.time()
    if start <= end:
        return start <= t < end
    # 跨零点：23:30-07:30 → t >= 23:30 或 t < 07:30
    return t >= start or t < end


# ---------------------------------------------------------------- 会话账本

@dataclass
class SessionActivity:
    umo: str
    last_user_ts: float = 0.0
    last_proactive_ts: float = 0.0
    persona: str = ""
    failures: int = 0
    # 自**最近一条用户消息**以来，bot 连续发了多少条主动消息。
    # 群里没人说话时它会一直涨；一旦有人说话就归零（见 `note_user_message`）。
    # 这是"静默期连发上限"的计数依据 —— 死群不该被 bot 无限自言自语。
    silent_streak: int = 0


@dataclass
class ProactiveState:
    sessions: dict[str, SessionActivity] = field(default_factory=dict)
    # 分会话覆盖：umo → {enabled/min_interval_min/idle_threshold_min/
    #                     quiet_hours/silent_max}（任选键，缺省继承全局）。
    overrides: dict[str, dict] = field(default_factory=dict)

    def activity(self, umo: str) -> SessionActivity:
        a = self.sessions.get(umo)
        if a is None:
            a = SessionActivity(umo=umo)
            self.sessions[umo] = a
        return a

    def to_dict(self) -> dict:
        return {
            "sessions": {
                u: {"umo": a.umo, "last_user_ts": a.last_user_ts,
                    "last_proactive_ts": a.last_proactive_ts,
                    "persona": a.persona, "failures": a.failures,
                    "silent_streak": a.silent_streak}
                for u, a in self.sessions.items()},
            "overrides": {str(k): dict(v)
                          for k, v in self.overrides.items()},
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "ProactiveState":
        st = cls()
        for u, a in ((d or {}).get("sessions") or {}).items():
            if not isinstance(a, dict):
                continue
            try:
                st.sessions[str(u)] = SessionActivity(
                    umo=str(a.get("umo") or u),
                    last_user_ts=float(a.get("last_user_ts") or 0.0),
                    last_proactive_ts=float(a.get("last_proactive_ts") or 0.0),
                    persona=str(a.get("persona") or ""),
                    failures=int(a.get("failures") or 0),
                    silent_streak=max(0, int(a.get("silent_streak") or 0)))
            except (TypeError, ValueError):
                continue
        raw_ov = (d or {}).get("overrides")
        if isinstance(raw_ov, dict):
            for u, ov in raw_ov.items():
                if isinstance(ov, dict) and str(u):
                    st.overrides[str(u)] = _norm_override(ov)
        return st


# 分会话覆盖的合法键与归一化
OVERRIDE_KEYS = ("enabled", "min_interval_min", "idle_threshold_min",
                 "quiet_hours", "silent_max")


def _norm_override(values: dict) -> dict:
    """把调用方给的覆盖参数归一化：只保留合法键、转成正确类型。"""
    out: dict = {}
    if not isinstance(values, dict):
        return out
    if values.get("enabled") is not None:
        out["enabled"] = bool(values["enabled"])
    for k in ("min_interval_min", "idle_threshold_min"):
        v = values.get(k)
        if v not in (None, ""):
            try:
                out[k] = max(1, int(float(v)))
            except (TypeError, ValueError):
                pass
    # 静默期连发上限：0 = 不限制（沿用旧行为）；>=1 = 群里持续没消息时
    # 最多连发几条就暂停（有人说话即重新计数）。
    v = values.get("silent_max")
    if v not in (None, ""):
        try:
            out["silent_max"] = max(0, int(float(v)))
        except (TypeError, ValueError):
            pass
    q = str(values.get("quiet_hours") or "").strip()
    if q and parse_quiet_hours(q) is not None:
        out["quiet_hours"] = q
    return out


# ---------------------------------------------------------------- 调度器

class ProactiveScheduler:
    """周期性判断"该不该主动开口"，并把决定交给 `emit` 回调。"""

    def __init__(self, *, enabled: bool = True, min_interval_min: int = 45,
                 idle_threshold_min: int = 90, quiet_hours: str = "",
                 tick_seconds: int = 60, jitter: float = 0.25,
                 silent_max: int = 2,
                 state_path=None,
                 known: Callable[[str], bool] | None = None) -> None:
        # `known(umo)` 由宿主注入："这个会话现在还算一个真会话吗"。
        # 不注入（None）时一律视为已知 —— core/ 要能脱离框架单测。
        self._known = known
        # 每次 tick 出话前 await 的钩子（可选）：宿主用来刷新外部判据
        # （如 AstrBot 会话存活集），保证 `candidates()` 用的是最新状态。
        self._pre_tick: Callable[[], Any] | None = None
        self.enabled = enabled
        self.min_interval = max(1, int(min_interval_min)) * 60
        self.idle_threshold = max(1, int(idle_threshold_min)) * 60
        self.quiet = parse_quiet_hours(quiet_hours)
        self.tick_seconds = max(5, int(tick_seconds))
        self.jitter = max(0.0, min(1.0, jitter))
        # 静默期连发上限（0 = 不限制）。这是**死群保护**，也是唯一的总量闸门：
        # 群里一直没人说话时空闲判据永远满足、只剩 `min_interval` 在拦，于是
        # bot 会按最小间隔一条接一条地自言自语；有了它，连发 N 条就停，
        # 等群里有人说话（`note_user_message`）才重新计数。
        #
        # 这里**没有**"每日上限"了：它是按自然日切的，与"群里有没有人说话"
        # 无关，既会在死群里放到第 N 条才停，又带来跨天重置的一堆边界问题；
        # 静默连发上限把同一件事表达得更准，于是整块删除。
        self.silent_max = max(0, int(silent_max))
        self.state = ProactiveState()
        self._task: asyncio.Task | None = None
        self._emit: Callable[[str], Any] | None = None
        self._rng = random.Random(20260912)
        self.stats = {"ticks": 0, "emitted": 0, "skipped_quiet": 0,
                      "skipped_interval": 0, "skipped_idle": 0,
                      "errors": 0}
        self.state_path = state_path or (PROACTIVE_DIR / "state.json")
        # 状态必须落盘：否则每次重启 last_user_ts 归零 → should_emit 恒返
        # "no-user-activity"（主动消息彻底不发），连发计数也会被清空。
        self._dirty = False
        self._last_save = 0.0
        self.load()

    # ------------------------------------------------------------ 持久化
    def load(self) -> None:
        try:
            raw = self.state_path.read_text(encoding="utf-8").strip()
            d = json.loads(raw) if raw else {}
        except FileNotFoundError:
            return
        except Exception as e:
            logger.warning(f"[chatcore_neo] 主动消息状态文件损坏，按空处理: {e!r}")
            return
        if not isinstance(d, dict):
            return
        st = ProactiveState.from_dict(d)
        # 丢弃**非会话键**：真正的 UMO 至少是 `实例:类型:id`，
        # 不含冒号的键（如旧版测试写进来的 `umo1` / `ready`）必定是脏数据。
        # 在加载时清掉，防止它们随保存继续残留；等下次落盘时一并抹除。
        bad = [u for u in st.sessions if ":" not in str(u)]
        if bad:
            logger.info(f"[chatcore_neo] 主动消息状态含非会话键，已忽略: {bad}")
            for u in bad:
                st.sessions.pop(u, None)
            self._dirty = True
        self.state = st
        # 老状态文件里的 `day` / `day_counts`（每日上限那套）不再读取；
        # 下次落盘时它们会自然消失。

    def save(self, *, force: bool = False) -> None:
        now = time.time()
        if not force and not self._dirty:
            return
        if not force and now - self._last_save < 5.0:
            return                      # 简单节流：最多 5 秒写一次
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"version": 1, **self.state.to_dict()}
            tmp = self.state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(self.state_path)
            self._dirty = False
            self._last_save = now
        except Exception as e:
            logger.warning(f"[chatcore_neo] 主动消息状态落盘失败: {e!r}")

    # ------------------------------------------------------------ 观察
    def note_user_message(self, umo: str, persona: str = "") -> None:
        a = self.state.activity(umo)
        a.last_user_ts = time.time()
        if persona:
            a.persona = persona
        # 群里有人说话了 → 静默期结束，连发计数归零。
        # 这是"静默连发上限"唯一的复位点：没有它，死群连发 N 条之后就永远
        # 不再开口；有了它，群一活过来 bot 就恢复正常的主动行为。
        a.silent_streak = 0
        self._dirty = True
        self.save()

    def note_proactive(self, umo: str) -> None:
        a = self.state.activity(umo)
        a.last_proactive_ts = time.time()
        a.silent_streak += 1          # 又自言自语了一条
        self.stats["emitted"] += 1
        self._dirty = True
        self.save(force=True)

    # ------------------------------------------------------------ 分会话覆盖
    def set_override(self, umo: str, values: dict) -> dict:
        """为指定会话设置主动消息参数覆盖（会合并进已有覆盖并立即落盘）。"""
        umo = str(umo)
        ov = _norm_override(values)
        cur = dict(self.state.overrides.get(umo, {}))
        cur.update(ov)
        if cur:
            self.state.overrides[umo] = cur
        else:
            self.state.overrides.pop(umo, None)
        self._dirty = True
        self.save(force=True)
        return dict(self.state.overrides)

    def clear_override(self, umo: str) -> dict:
        """清除某会话的全部主动消息覆盖。"""
        self.state.overrides.pop(str(umo), None)
        self._dirty = True
        self.save(force=True)
        return dict(self.state.overrides)

    def overrides(self) -> dict[str, dict]:
        return {str(k): dict(v) for k, v in self.state.overrides.items()}

    def override_for(self, umo: str) -> dict:
        return dict(self.state.overrides.get(str(umo), {}) or {})

    # ------------------------------------------------------------ 真伪会话
    def set_known(self, fn: Callable[[str], bool] | None) -> None:
        """注入/替换「真伪会话」判定（也可以构造时用 `known=` 传）。"""
        self._known = fn

    def set_pre_tick(self, fn: Callable[[], Any] | None) -> None:
        """注入每次 tick 出话前 await 的钩子（宿主刷新外部判据用）。"""
        self._pre_tick = fn

    def is_known(self, umo: str) -> bool:
        """该 UMO 现在是否还是一个真实会话（由宿主注入的 `known` 判定）。

        为什么需要这道闸门：账本是**只增不减**的 —— 每条用户消息都会写下
        一条 `note_user_message`，但**没有任何地方会删**。于是测试脚本
        （`webchat:FriendMessage:webchat!astrbot!newapi-balance-e2e-*`）、
        被删掉的会话都会永远留在账本里，既污染管理页的「各会话活跃度」，
        又会被当成候选真的去开口。

        **失败时 fail-open**（返回 True）：判定函数异常不能把主动消息整体
        掐死，宁可多发一条也不要静默全停。
        """
        fn = self._known
        if fn is None:
            return True
        try:
            return bool(fn(str(umo)))
        except Exception:
            return True

    def residue(self) -> list[str]:
        """账本里已不是真实会话的键（供管理页列入残留、一键清理）。"""
        return sorted(u for u in self.state.sessions if not self.is_known(u))

    def forget(self, umo: str) -> bool:
        """从账本里删除一个会话（活跃度与连发计数一并清掉）。返回是否真删了。"""
        umo = str(umo)
        hit = self.state.sessions.pop(umo, None) is not None
        if hit:
            self._dirty = True
            self.save(force=True)
        return hit

    # ------------------------------------------------------------ 判定
    def should_emit(self, umo: str, now: float | None = None) -> tuple[bool, str]:
        """返回 (是否该发, 原因)。原因便于 /persona proactive 排障。

        **两级开关，职责完全不同**：

          ① 全局 `enabled`（配置里的 `enable_proactive`）= **放行**。
             它只回答"这个功能允不允许被使用"，**不开启任何会话** ——
             打开它之后一个会话都不会开口，只是让下面的会话级开关能生效。
             关掉它 = 整个功能停，会话级配置一律不作数。
          ② 会话级 `enabled`（管理页「分会话配置」的「开关」列）= **该会话要不要**。
             **opt-in**：只有显式选了「开启」的会话才会开口；没配置过的会话
             一律不开口。否则"全局打开"就等于"给所有群都开"，
             那既不是配置文件的语义，也不是任何人想要的行为。

        其余参数（空闲阈值/间隔/免打扰/静默连发上限）仍然是**会话级覆盖优先**，
        缺省继承全局 —— 这一层才是"按群留白"真正要调的东西。
        """
        ov = self.state.overrides.get(umo, {}) or {}
        if not self.enabled:
            return False, "disabled"
        if ov.get("enabled") is not True:
            # 显式「禁用」和「没配置过」都挡掉，但原因要分开：
            # 前者是用户的决定，后者是"还没开启"，排障时区别很大。
            return False, ("disabled-by-session" if ov.get("enabled") is False
                           else "session-not-enabled")
        # 账本只增不减，里面混着测试脚本造的假会话与被删掉的旧会话。
        # 不对它们开口 —— 那才是真正的"对着空会话自言自语"。
        if not self.is_known(umo):
            return False, "unknown-session"
        now = now if now is not None else time.time()
        a = self.state.activity(umo)
        if a.last_user_ts <= 0:
            return False, "no-user-activity"     # 从未说过话的会话不主动打扰
        # ── 静默期连发上限（死群保护）──
        # 群里一直没人说话时，下面的空闲判据**永远**满足（`last_user_ts` 不动），
        # 于是唯一还在拦的就是 `min_interval` —— bot 会按最小间隔一条接一条地
        # 自言自语。这里加一道闸：自最近一条用户消息以来连发满 N 条就停，
        # 等群里有人说话再恢复。
        # 会话级覆盖优先（管理页「分会话配置 → 静默上限」）。
        silent_max = int(ov.get("silent_max", self.silent_max))
        if silent_max > 0 and a.silent_streak >= silent_max:
            return False, f"silent-cap({silent_max})"
        # 会话级覆盖优先
        idle_s = int(ov.get("idle_threshold_min", self.idle_threshold // 60)) * 60
        min_s = int(ov.get("min_interval_min", self.min_interval // 60)) * 60
        if "quiet_hours" in ov:
            quiet = parse_quiet_hours(str(ov.get("quiet_hours") or ""))
        else:
            quiet = self.quiet
        if in_quiet_hours(datetime.now(), quiet):
            return False, "quiet-hours"
        since_user = now - a.last_user_ts
        if since_user < idle_s:
            return False, f"idle({int(since_user)}s<{idle_s}s)"
        since_pro = now - a.last_proactive_ts
        if a.last_proactive_ts > 0 and since_pro < min_s:
            return False, f"cooldown({int(since_pro)}s<{min_s}s)"
        # 抖动：避免每次都在同一秒开口（像机器）
        if self.jitter and self._rng.random() < self.jitter:
            return False, "jitter"
        return True, "ok"

    def candidates(self, now: float | None = None) -> list[str]:
        out = []
        for umo in sorted(self.state.sessions):
            ok, _why = self.should_emit(umo, now)
            if ok:
                out.append(umo)
        return out

    def any_session_enabled(self) -> bool:
        """是否存在至少一个会话被配置成「开启」。

        这是 opt-in 的判据：没有它（且全局放行）时一个会话都不会开口 ——
        管理页据此提示"全局开了但还没开任何会话"。
        它**不**参与调度循环的启停（那只看全局放行，见 `ensure_started`）。
        """
        return any(ov.get("enabled") is True
                   for ov in self.state.overrides.values())

    # ------------------------------------------------------------ 循环
    async def run(self, emit: Callable[[str], Any]) -> None:
        """主循环。`emit(umo)` 由插件提供，负责真正合成并投递事件。"""
        while True:
            try:
                await asyncio.sleep(self.tick_seconds)
                self.stats["ticks"] += 1
                # tick 出话前先让宿主刷新外部判据（如 AstrBot 会话存活集），
                # 否则 candidates() 一直拿上一轮的过期判定。
                if self._pre_tick is not None:
                    try:
                        r = self._pre_tick()
                        if asyncio.iscoroutine(r):
                            await r
                    except Exception:
                        pass
                for umo in self.candidates():
                    try:
                        r = emit(umo)
                        if asyncio.iscoroutine(r):
                            await r
                    except Exception:
                        self.stats["errors"] += 1
                        self.state.activity(umo).failures += 1
            except asyncio.CancelledError:
                break
            except Exception:
                self.stats["errors"] += 1
                await asyncio.sleep(self.tick_seconds)

    def start(self, emit: Callable[[str], Any]) -> None:
        """记下 `emit` 回调并尝试启动循环（可重复调用，幂等）。"""
        self._emit = emit
        self.ensure_started()

    def ensure_started(self) -> bool:
        """确保循环在跑。返回当前是否真的在跑。

        为什么单独抽出来：`start()` 只在插件 `initialize()` 时调一次，而
        全局开关是**运行期**可以被改的（管理页保存配置 → `sync_config()`
        写 `scheduler.enabled`）。没有这个方法，把全局开关重新打开之后循环
        永远不会起来，主动消息看着就是"设置好了但不生效"。

        全局 `enabled` 只负责**放行**（详见 `should_emit`）：放行期间循环就该
        跑着，具体哪个会话开口由会话级 opt-in 决定。没放行则一律不启动。
        """
        if self._task is not None and not self._task.done():
            return True
        if self._emit is None:
            return False
        if not self.enabled:
            return False
        try:
            self._task = asyncio.create_task(self.run(self._emit))
        except RuntimeError:
            self._task = None
            return False
        return True

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def apply_enabled(self, enabled: bool) -> bool:
        """写全局**放行**开关并当场生效：放行 → 起循环，不放行 → 停循环。

        关闭时必须真的把循环停掉，不能只改标志位 —— 否则配置里写着关闭、
        循环还在跑，正是"配置文件与实现不符"的那种症状。
        `sync_config()` 是同步函数，不能 await，所以这里直接 cancel 任务
        （`stop()` 里的 await 只是为了收尾干净，取消本身是同步的）。
        """
        self.enabled = bool(enabled)
        if not self.enabled:
            self._cancel_task()
            return False
        return self.ensure_started()

    def _cancel_task(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            # cancel() 后 await 必然抛 CancelledError（py3.8+ 是 BaseException，
            # 旧的 `except Exception` 接不住）—— 属正常停服，吞掉即可。
            pass
        except Exception:
            pass
        self._task = None

    def report(self) -> dict:
        # `tracked_sessions` 只数**真会话**：账本里混着的测试脚本假会话与
        # 已删除的旧会话单列 `residue_sessions`，免得页面上"跟踪了 30 个
        # 会话"里有一半根本不是会话。
        res = self.residue()
        ovs = self.state.overrides
        return {**self.stats,
                # `enabled` = 全局总闸（配置里的 `enable_proactive`），
                # `running` = 循环真的在跑。两者必须分开报，页面才能既说清
                # "开关是什么"又说清"到底有没有在工作"。
                "enabled": self.enabled,
                "running": self.running,
                "sessions_on": sum(1 for ov in ovs.values()
                                   if ov.get("enabled") is True),
                "sessions_off": sum(1 for ov in ovs.values()
                                    if ov.get("enabled") is False),
                "min_interval_min": self.min_interval // 60,
                "idle_threshold_min": self.idle_threshold // 60,
                "silent_max": self.silent_max,
                "quiet_hours": (f"{self.quiet[0]}-{self.quiet[1]}" if self.quiet else ""),
                "tracked_sessions": len(self.state.sessions) - len(res),
                "residue_sessions": len(res),
                "candidates": len(self.candidates())}


# ---------------------------------------------------------------- 合成事件

# 主动消息的提示文本：**必须用消息级 `_no_save`**（硬性约束 3），
# 否则每发一条主动消息，历史里就多一条人造用户发言。
SIMULATED_USER_TEXT = (
    "[系统] 现在轮到你主动开口了。请自然地起个话头，"
    "不要提及这条系统提示，也不要复述它。"
)


def _find_adapter(context: Any, platform_name: str) -> Any:
    """按平台名找到适配器实例。

    UMO 的第一段是 `PlatformMetadata.**id**`（例如 "BotA" / "BotB"），
    而 `name` 是平台**类型**（多个适配器都叫 "aiocqhttp"），
    所以必须按 id 匹配。
    """
    try:
        mgr = getattr(context, "platform_manager", None)
        insts = list(getattr(mgr, "platform_insts", None) or [])
    except Exception:
        return None
    if not insts:
        return None

    def meta_of(inst):
        try:
            m = inst.meta()
        except Exception:
            return None
        return m if m is not None else None

    if platform_name:
        # **先按 id 匹配**：UMO 的第一段是 `platform_meta.id`（本机
        # id=BotA/BotB，而两个适配器的 name 都是 aiocqhttp）。
        # 若先按 name 匹配，BotB 的会话会选中 BotA 适配器 → 以错误的
        # umo 甚至错误的账号发出主动消息。
        for inst in insts:
            m = meta_of(inst)
            if m is not None and str(getattr(m, "id", "")) == platform_name:
                return inst
        for inst in insts:                      # 兜底：按 name
            m = meta_of(inst)
            if m is not None and str(getattr(m, "name", "")) == platform_name:
                return inst
    return insts[0] if len(insts) == 1 else None


def build_message(template: compat.EventTemplate, text: str,
                  sender: dict | None = None) -> Any:
    """造一个**真实的** `AstrBotMessage`（不是鸭子类型假对象）。

    适配器的 `create_event()` 会读它的 `message_str` / `session_id` /
    `type` / `sender` / `group`，所以这些必须是框架认识的类型。

    `sender` 用于覆盖骨架里的发送者：事件模板只写一次、此后不再更新，
    直接复用会把"谁在说话"永久钉在该会话第一个说话的人身上（戳一戳认错人）。
    """
    from astrbot.core.message.components import Plain
    from astrbot.core.platform.astrbot_message import (
        AstrBotMessage, Group, MessageMember)
    from astrbot.core.platform.message_type import MessageType

    is_group = bool(template.group_id)
    abm = AstrBotMessage()
    abm.type = MessageType.GROUP_MESSAGE if is_group else MessageType.FRIEND_MESSAGE
    abm.self_id = str(template.self_id or "")
    abm.session_id = str(
        template.session_id
        or (template.group_id if is_group else "")
        or abm.self_id)
    abm.message_id = f"proactive-{int(time.time() * 1000)}"
    abm.group = Group(group_id=str(template.group_id)) if is_group else None
    d = sender or template.sender or {}
    abm.sender = MessageMember(
        user_id=str(d.get("user_id") or template.self_id or "0"),
        nickname=str(d.get("nickname") or "系统"))
    abm.message = [Plain(text)]
    abm.message_str = text
    abm.raw_message = {}
    abm.timestamp = int(time.time())
    return abm


def synthesize_event(template: compat.EventTemplate, text: str,
                     context: Any, umo: str = "", *,
                     sender: dict | None = None,
                     source: str = "proactive") -> Any | None:
    """按骨架造一个 AstrMessageEvent。

    **走适配器的 `create_event()`**：事件基类的 `__init__` 会初始化
    `_extras` / `role` / `_force_stopped` / `trace` 等一整套字段，
    自己 `__new__` + setattr 是补不全的（补不全就会在 WakingCheckStage 崩）。

    `sender` 用来覆盖骨架里的发送者（戳一戳必须带上**发起人**的真实身份，
    否则会认成模板里那个人）；`source` 记来源，供装配层区分"主动开口"
    与"戳一戳回应"（前者没有真实的本轮说话人）。

    拿不到适配器或模板时返回 None（调用方据此跳过，不抛异常）。
    """
    if template is None or not text:
        return None
    try:
        platform_name = template.platform or (umo.split(":", 1)[0] if umo else "")
        adapter = _find_adapter(context, platform_name)
        if adapter is None or not hasattr(adapter, "create_event"):
            logger.warning(
                f"[chatcore_neo] 主动消息：找不到平台适配器 "
                f"(platform={platform_name!r})，无法合成事件")
            return None
        abm = build_message(template, text, sender=sender)
        ev = adapter.create_event(abm)
        if ev is None:
            return None
        # 合成事件是"用户开口了"的替身：标记成已唤醒，并打上模拟标记，
        # 让需要跳过的副作用（记忆写入等）能识别出来
        for name, value in (("is_wake", True), ("is_at_or_wake_command", True),
                            ("role", "member")):
            try:
                setattr(ev, name, value)
            except Exception:
                pass
        compat.mark_simulated(ev, source=source)
        return ev
    except Exception as e:
        logger.warning(f"[chatcore_neo] 主动消息：合成事件失败 {e!r}")
        return None
