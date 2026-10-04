# core/session.py
"""
时间盒 / 会话（Session）：用「一次启动 → 多次干活 → 一次关闭」的方式跑任务。

为什么要这样分
--------------
挂机场景里「启动游戏 → 切换模式 → 挂机×N → 关闭游戏」，
启动和关闭本身又慢又没必要每次重复，所以把它们从循环里拆出来：

    Session
      ├─ startup   只跑一次（启动游戏、切换模式…）
      ├─ body      循环跑 N 次 / 跑到时间上限（挂机、收菜…）
      └─ shutdown  只跑一次（关闭游戏…）

- 旧写法（tasks = [启动游戏, 收菜, 关闭游戏]）保持原样：整轮重复。
- 新写法（startup + body + shutdown）：startup/shutdown 各一次，body 重复。
- body_repeat 控制 body 内部重复几次；repeat 控制整个会话重复几轮。
- keep_open: true → 结束时不停游戏（留给下一个会话说「我已经开着了」）。

时间约束（每轮/每次 body 开始前判定，保证不会被切两半）
------------------------------------------------------
- duration  从开始运行起算的总时长
- until     绝对截止时刻（今天已过则视为已到）
- active    允许运行的时间窗（未到则等待到窗口打开，支持跨午夜）
- quiet     安静时段（到点不再开始新的 body）
- max_wait  等待窗口打开的上限，超过就结束

本模块不依赖 maa / 不依赖设备：任务对象通过参数注入，可以离线单测。
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

logger = logging.getLogger(__name__)

SECONDS_PER_DAY = 24 * 3600

# ---------- 结束原因 ----------

REASON_COMPLETED = "completed"     # 目标达成（轮数/次数跑满）
REASON_FORCE = "force"             # 用户强制中断（Ctrl+C）
REASON_DURATION = "duration"       # 到达配置的总时长
REASON_UNTIL = "until"             # 到达配置的截止时刻
REASON_BEFORE_END = "before_end"   # 到达配置的结束时刻
REASON_WAIT_DONE = "wait_done"     # 用户空输入结束等待
REASON_INTERRUPT = "interrupt"     # 等待期间被 Ctrl+C 打断
REASON_ERROR = "error"             # 出现异常
REASON_SKIPPED = "skipped"         # 未进入时间窗，跳过（不等待时）

# 未就绪的原因
NOT_READY_ACTIVE = "active"        # 还没进入允许运行的时间窗
NOT_READY_QUIET = "quiet"          # 命中安静时段

REASON_LABEL = {
    REASON_COMPLETED: "已完成",
    REASON_FORCE: "收到中断信号",
    REASON_DURATION: "到达指定运行时长",
    REASON_UNTIL: "到达指定截止时刻",
    REASON_BEFORE_END: "到达指定结束时刻",
    REASON_WAIT_DONE: "等待被手动结束",
    REASON_INTERRUPT: "等待期间被中断",
    REASON_ERROR: "执行出错",
    REASON_SKIPPED: "未进入允许运行的时间，跳过",
    NOT_READY_ACTIVE: "未到允许运行的时间",
    NOT_READY_QUIET: "处于安静时段",
}


# ---------- 时间解析 ----------


class TimeParseError(ValueError):
    """时间/时长字符串无法解析"""


_DURATION_TOKEN = re.compile(r"(\d+(?:\.\d+)?)\s*([a-zA-Z\u4e00-\u9fff]*)")

_DURATION_UNITS = {
    "": 1, "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "秒": 1, "秒钟": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "分": 60, "分钟": 60, "h": 3600, "hr": 3600, "hrs": 3600,
    "hour": 3600, "hours": 3600, "时": 3600, "小时": 3600,
    "d": 86400, "day": 86400, "days": 86400, "天": 86400, "日": 86400,
}


def parse_duration(text) -> int:
    """
    解析时长，返回秒数。支持：
      8h / 8小时 / 1h30m / 90m / 1.5h / 45 / 45s / 2天 / 1:30
    """
    if isinstance(text, bool):
        raise TimeParseError(f"无法解析时长: {text!r}")
    if isinstance(text, (int, float)):
        seconds = int(float(text))
        if seconds < 0:
            raise TimeParseError(f"时长不能为负数: {text!r}")
        return seconds

    raw = str(text or "").strip()
    if not raw:
        raise TimeParseError("时长为空")

    if re.fullmatch(r"\d+(?:\.\d+)?", raw):
        return int(float(raw))

    m = re.fullmatch(r"(\d+):(\d{1,2})", raw)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60

    total = 0.0
    pos = 0
    for m in _DURATION_TOKEN.finditer(raw):
        if m.start() != pos:
            raise TimeParseError(f"无法解析时长: {raw!r}")
        pos = m.end()
        unit = m.group(2).lower()
        if unit not in _DURATION_UNITS:
            raise TimeParseError(f"未知时间单位 {unit!r}（在 {raw!r} 中）")
        total += float(m.group(1)) * _DURATION_UNITS[unit]
    if pos != len(raw) or total <= 0:
        raise TimeParseError(f"无法解析时长: {raw!r}")
    return int(round(total))


def parse_hhmm(text, field_name: str = "时间") -> int:
    """解析 HH:MM / HH:MM:SS，返回「当天 0 点起的秒数」。24:00 = 当天结束。"""
    raw = str(text or "").strip()
    m = re.fullmatch(r"(\d{1,2})[:：](\d{1,2})(?:[:：](\d{1,2}))?", raw)
    if not m:
        raise TimeParseError(f"无法解析{field_name}: {raw!r}（应形如 12:00 或 07:30:00）")

    hour, minute = int(m.group(1)), int(m.group(2))
    second = int(m.group(3) or 0)
    if not (0 <= minute < 60 and 0 <= second < 60):
        raise TimeParseError(f"无法解析{field_name}: {raw!r}（分/秒应在 0-59）")
    if hour == 24 and minute == 0 and second == 0:
        return SECONDS_PER_DAY
    if not (0 <= hour < 24):
        raise TimeParseError(f"无法解析{field_name}: {raw!r}（小时应在 0-23）")
    return hour * 3600 + minute * 60 + second


def parse_active_window(text, field_name: str = "时间窗") -> tuple[int, int]:
    """
    解析「开始~结束」的时间窗，返回 (start_seconds, end_seconds)。
    结束小于开始表示跨午夜（23:00~07:00）。start == end 视为全天。
    """
    raw = str(text or "").strip()
    if not raw:
        raise TimeParseError(f"{field_name}为空")
    parts = [p for p in re.split(r"\s*(?:~|-{1,2}|–|—|至|到)\s*", raw) if p]
    if len(parts) != 2:
        raise TimeParseError(
            f"无法解析{field_name}: {raw!r}（应形如 08:00~23:00 或 23:00~07:30）"
        )
    start = parse_hhmm(parts[0], field_name)
    end = parse_hhmm(parts[1], field_name)
    if start == end:
        return 0, SECONDS_PER_DAY
    return start, end


def _fmt_hhmm(seconds: int) -> str:
    seconds = int(seconds) % SECONDS_PER_DAY
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}"


def fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}小时{minutes}分"
    if minutes:
        return f"{minutes}分{secs}秒"
    return f"{secs}秒"


_fmt_duration = fmt_duration      # 兼容旧名字


# ---------- 时刻点 ----------


@dataclass(frozen=True)
class Clock:
    """
    一个「每天都会到」的时刻。

    seconds     : 当天 0 点起的秒数（86400 表示 24:00）
    wait_passed : 当天这个时刻已经过去了怎么办
                  True  → 顺延到明天（--until 07:30 --wait-until）
                  False → 直接当作已到（默认）
    """

    seconds: int
    wait_passed: bool = False

    def at(self, day: datetime) -> datetime:
        return day.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
            seconds=self.seconds
        )

    def resolve(self, now: datetime, wait_passed: bool = None) -> tuple[datetime, bool]:
        """返回 (目标时刻, 是否顺延到了明天)。目标时刻严格大于 now。"""
        wait = self.wait_passed if wait_passed is None else wait_passed
        when = self.at(now)
        if when - now < timedelta(seconds=1):
            if not wait:
                return when, False
            return self.at(now + timedelta(days=1)), True
        return when, False

    def describe(self) -> str:
        return _fmt_hhmm(self.seconds)


def _clock(value, field_name: str = "时刻", wait_passed: bool = False) -> Optional[Clock]:
    if value is None:
        return None
    if isinstance(value, Clock):
        return value
    return Clock(parse_hhmm(value, field_name), wait_passed)


# ---------- 时间窗 ----------


@dataclass(frozen=True)
class Window:
    """
    每天重复的时间窗，支持跨午夜（23:00~07:00）。

    active_from : 窗口开始，None = 不限制
    active_to   : 窗口结束，None = 不限制
    open_end    : active_to 为 None 但语义是「到当天结束」（22:00~24:00）
    """

    active_from: Optional[Clock] = None
    active_to: Optional[Clock] = None
    open_end: bool = False

    @staticmethod
    def from_window(text, field_name: str = "时间窗") -> "Window":
        start, end = parse_active_window(text, field_name)
        return Window(
            active_from=Clock(start) if start else None,
            active_to=None if end >= SECONDS_PER_DAY else Clock(end),
            open_end=end >= SECONDS_PER_DAY,
        )

    @staticmethod
    def from_antimeridian(text, field_name: str = "安静时段") -> "Window":
        """安静时段：起点必须存在，终点 24:00 视为「到当天结束」。"""
        start, end = parse_active_window(text, field_name)
        return Window(
            active_from=Clock(start),
            active_to=None if end >= SECONDS_PER_DAY else Clock(end),
            open_end=end >= SECONDS_PER_DAY,
        )

    @property
    def bounded(self) -> bool:
        return self.active_from is not None or self.active_to is not None or self.open_end

    def _bounds(self, now: datetime) -> tuple[Optional[datetime], Optional[datetime], bool]:
        """把每天重复的窗口展开成 now 所在的这一段时间。"""
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = self.active_to.at(now) if self.active_to else (
            midnight + timedelta(days=1) if self.open_end else None
        )
        start = self.active_from.at(now) if self.active_from else None

        if start is None or end is None:
            return start, end, False
        if start < end:
            return start, end, False
        # 跨午夜：23:00~07:00；now 落在 0:00~07:00 时窗口从前一天开始
        if now < end:
            return start - timedelta(days=1), end, True
        return start, end + timedelta(days=1), False

    def contains(self, now: datetime) -> bool:
        if not self.bounded:
            return True
        start, end, _ = self._bounds(now)
        if start is not None and now < start:
            return False
        if end is not None and now >= end:
            return False
        return True

    def next_start(self, now: datetime) -> Optional[datetime]:
        """下一次窗口打开的时刻（保证晚于 now）；None 表示没有开始限制。"""
        if self.active_from is None:
            return None
        start, _, _ = self._bounds(now)
        # _bounds 可能给出「今天已经过去的那个开门时间」，往后顺延到下一次
        while start is not None and start <= now:
            start = self.active_from.at(start + timedelta(days=1))
        return start

    def end(self, now: datetime) -> Optional[datetime]:
        """当前（或今晚）这个窗口的结束时刻；None 表示没有结束限制。"""
        if self.active_to is None and not self.open_end:
            return None
        _, end, _ = self._bounds(now)
        return end

    def wait_for_start(self, now: datetime) -> Optional[float]:
        """还需要等待多少秒才进入窗口；已进入返回 None。"""
        if self.active_from is None or self.contains(now):
            return None
        start = self.next_start(now)
        return max(0.0, (start - now).total_seconds())

    def period_end_after(self, moment: datetime) -> Optional[datetime]:
        """
        moment 所在「周期」的结束时刻：从某一次开门，到下一次开门之前。
          例如 07:30~08:00：today 07:30 ~ tomorrow 07:30
        """
        if self.active_from is None:
            return None
        midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
        start = self.active_from.at(midnight)
        if moment < start:
            start = self.active_from.at(midnight - timedelta(days=1))
        return start + timedelta(days=1)

    def missed_by(self, session_start: datetime) -> bool:
        """
        会话启动时这个窗口是否「已经错过」。

        规则很简单：看下一次开门离现在有多远。
          - 超过一个完整周期（一天）→ 说明最近的那场已经关门了，这一轮不必干等
          - 在一个周期之内        → 只是还没到点，等它就是

        例（周期都是一天）：
          窗口 02:00~08:00，20:34 启动 → 下一场明天 02:00（5.4h）→ 等
          窗口 07:30~08:00，09:00 启动 → 下一场明天 07:30（22.5h）→ 等
          窗口 07:30~23:00，23:30 启动 → 下一场明天 07:30（8h）  → 等
          窗口 23:00~07:00，12:00 启动 → 下一场今晚 23:00（11h）  → 等
        「已过结束时刻」只在窗口已经彻底没有下一次开门时才成立。
        """
        if not self.bounded:
            return False
        if self.contains(session_start):
            return False
        start = self.next_start(session_start)
        if start is None:
            return True
        return (start - session_start) > timedelta(days=1)

    def quiet_until(self, now: datetime) -> Optional[float]:
        """安静时段还剩多少秒；当前不在安静时段返回 None。"""
        if self.active_from is None or not self.bounded or not self.contains(now):
            return None
        _, end, _ = self._bounds(now)
        if end is None:
            return None
        return max(0.0, (end - now).total_seconds())

    def describe(self) -> str:
        if not self.bounded:
            return "不限"
        start = self.active_from.describe() if self.active_from else "00:00"
        end = self.active_to.describe() if self.active_to else "24:00"
        return f"{start}~{end}"


def _window(value, field_name: str = "时间窗", antimeridian: bool = False) -> Optional[Window]:
    if value is None:
        return None
    if isinstance(value, Window):
        return value
    builder = Window.from_antimeridian if antimeridian else Window.from_window
    return builder(value, field_name)


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


# ---------- 会话（时间盒） ----------


@dataclass
class Session:
    """
    一个会话。

    任务分三段（生命周期）：
      startup   只跑一次，例如 [启动游戏, 切换到全面战场]
      body      循环跑，例如 [蜂医挂机]
      shutdown  只跑一次，例如 [关闭游戏]

    兼容老写法：只给 tasks（或 body）时，整轮重复（启动/干活/关闭都在里面）。

    轮数语义：
      repeat       整个会话重复几轮（-1 = 不限）
      body_repeat  每轮里 body 重复几次（-1 = 不限，由时间约束决定何时停）
    """

    name: str = "session"
    startup: list = None
    body: list = field(default_factory=list)
    shutdown: list = None
    tasks: list = field(default_factory=list)          # 兼容老配置

    repeat: Optional[int] = None                       # 会话轮数
    body_repeat: Optional[int] = None                  # body 次数
    duration: object = None
    until: object = None
    window: object = None
    quiet: object = None
    wait_quiet: bool = False
    force_cut: bool = False
    interval: float = 0.0
    start_time: Optional[datetime] = None
    max_wait: Optional[float] = None
    notify: Optional[str] = None
    keep_open: bool = False
    skip_if_missed: bool = False                       # 未进入时间窗时：跳过而不是等待
    skip_if_until_passed: bool = False                 # until 今天已过：跳过而不是立即结束
    shutdown_after_interrupt: bool = True              # Ctrl+C 后是否补一次关闭
    _legacy: bool = field(default=False, repr=False)    # 老写法：整轮重复

    # ---- 规范化 ----

    def __post_init__(self):
        # 先记住「配置里到底有没有写 startup / shutdown」——显式空列表也算写了
        lifecycle_given = self.startup is not None or self.shutdown is not None
        self.startup = _as_list(self.startup)
        self.body = _as_list(self.body)
        self.shutdown = _as_list(self.shutdown)
        self.tasks = _as_list(self.tasks)

        # 老写法：只给 tasks（没写 startup / shutdown）→ 整轮重复
        if not self.body and self.tasks:
            self.body = list(self.tasks)
            if not lifecycle_given:
                self._legacy = True
                self.startup, self.shutdown = [], []

        self.repeat = self._norm_repeat(self.repeat, default=None, field_name="repeat")
        self.body_repeat = self._norm_repeat(self.body_repeat, default=None, field_name="body_repeat")

        if self.duration is not None:
            self.duration = parse_duration(self.duration)

        self.until = _clock(self.until, "截止时刻",
                            wait_passed=getattr(self.until, "wait_passed", False))
        self.window = _window(self.window, "允许运行的时间窗")
        self.quiet = _window(self.quiet, "安静时段", antimeridian=True) if self.quiet is not None else None

        self.interval = max(0.0, float(self.interval or 0))
        if self.start_time is None:
            self.start_time = datetime.now()

    @staticmethod
    def _norm_repeat(value, default, field_name: str):
        if value is None:
            return default
        value = int(value)
        if value == 0 or value == -1:
            return None          # 0 / -1 = 不限
        if value < -1:
            raise ValueError(f"{field_name} 只能是正整数、0 或 -1（不限）")
        return value

    # ---- 结构 ----

    @property
    def flat(self) -> bool:
        """
        老写法：配置里只给了 tasks（没写 startup / shutdown），整轮重复。
        显式的空列表（startup: []）仍然算新写法——那样 body 是唯一的循环体。
        """
        return self._legacy

    @property
    def all_tasks(self) -> list:
        seen = []
        for name in list(self.startup) + list(self.body) + list(self.shutdown):
            if name not in seen:
                seen.append(name)
        return seen

    def body_times(self) -> Optional[int]:
        """body 一共要跑几次；None = 不限。"""
        if self.body_repeat is not None:
            return self.body_repeat
        if self.flat:
            return self.repeat
        return None

    def session_rounds(self) -> Optional[int]:
        """会话一共重复几轮；None = 不限。"""
        if self.flat:
            return 1
        return self.repeat

    # ---- 时间派生 ----

    @property
    def started_at(self) -> datetime:
        return self.start_time

    @property
    def ends_at(self) -> Optional[datetime]:
        if not self.duration:
            return None
        return self.start_time + timedelta(seconds=self.duration)

    @property
    def has_limit(self) -> bool:
        if self.duration or self.until:
            return True
        if self.window and (self.window.active_to or self.window.open_end):
            return True
        if self.quiet and not self.wait_quiet and (self.quiet.active_to or self.quiet.open_end):
            return True
        return False

    @property
    def deadline(self) -> Optional[datetime]:
        """所有时间上限中最早的那个。"""
        candidates = []
        if self.ends_at:
            candidates.append(self.ends_at)
        if self.until:
            candidates.append(self.until.resolve(self.start_time)[0])
        for win in (self.window, self.quiet):
            if win is None:
                continue
            end = win.end(self.start_time)
            if end is None:
                continue
            candidates.append(end + timedelta(days=1) if end <= self.start_time else end)
        return min(candidates) if candidates else None

    @property
    def infinite(self) -> bool:
        return self.body_times() is None and self.session_rounds() is None and not self.has_limit

    def describe_lines(self) -> list[str]:
        lines = [f"开始时间 {self.started_at:%Y-%m-%d %H:%M:%S}"]

        def times(count):
            return "不限" if count in (None, -1) else f"{count} 次"

        if self.flat:
            lines.append(f"模式 整轮重复（{times(self.session_rounds())}）")
            lines.append(f"轮内任务 {' → '.join(self.body) or '（无）'}")
            lines.append(f"每轮 body 次数 {times(self.repeat)}")
        else:
            lines.append(f"模式 一次启动 → 多次 body → 一次关闭")
            lines.append(f"会话轮数 {times(self.session_rounds())}")
            lines.append(f"启动（一次）{' → '.join(self.startup) or '（无）'}")
            lines.append(f"body {times(self.body_times())} {' → '.join(self.body) or '（无）'}")
            close = "保持游戏运行（keep_open）" if self.keep_open else (
                " → ".join(self.shutdown) or "（无）"
            )
            lines.append(f"关闭（一次）{close}")

        if self.duration:
            lines.append(f"总时长 {fmt_duration(self.duration)}（至 {self.ends_at:%H:%M:%S}）")
        if self.until:
            when, pushed = self.until.resolve(self.started_at)
            lines.append(
                f"截止时刻 {self.until.describe()}"
                + ("（已过今日，顺延到明天）" if pushed else "")
                + f" → {when:%m-%d %H:%M}"
            )
        if self.window and self.window.bounded:
            wait = "未到则跳过" if self.skip_if_missed else "未到则等待"
            lines.append(f"运行时间窗 {self.window.describe()}（{wait}）")
        if self.quiet:
            state = "等到时段结束再继续" if self.wait_quiet else "到点即结束"
            lines.append(f"安静时段 {self.quiet.describe()}（{state}）")
        lines.append(f"body 间隔 {fmt_duration(self.interval)}")
        lines.append("到点立即中断当前任务" if self.force_cut else "到点跑完当前一次再退出")
        return lines

    def describe(self) -> str:
        return "、".join(self.describe_lines())

    # ---- 判定 ----

    def check(self, now: Optional[datetime] = None, done_body: int = 0,
              done_rounds: int = 0, session_start: Optional[datetime] = None,
              check_body: bool = True) -> "CheckResult":
        """
        判定能不能开始下一段任务。

        参数
        ----
        done_body     : 已经跑过的 body 次数（整场累计）
        done_rounds   : 已经跑完的会话轮数
        session_start : 本次会话开始时的时刻，用来判断「时间窗是不是已经过去了」
                        （默认用 self.started_at）
        check_body    : 是否考虑「body 次数跑满」（会话内的内层循环用 False，
                        因为 body 跑满只代表这一轮结束，不代表整场结束）

        返回 CheckResult：
          not_ready=True  → 还要等（reason = active / quiet，wait_seconds = 等待秒数）
          finished=True   → 该结束了（reason 见 REASON_*）
        """
        now = now or datetime.now()
        session_start = session_start or self.started_at

        # 1) 会话轮数跑满
        rounds = self.session_rounds()
        if rounds not in (None, -1) and done_rounds >= rounds:
            return CheckResult(finished=True, reason=REASON_COMPLETED)

        # 2) 总时长
        if self.ends_at and now >= self.ends_at:
            return CheckResult(finished=True, reason=REASON_DURATION)

        # 3) 绝对截止时刻
        if self.until:
            when, passed = self.until.resolve(self.started_at)
            if now >= when:
                if self.skip_if_until_passed and self.until.wait_passed:
                    # chain 里 wait:false：这个点今天已经过了 → 跳过这一步
                    return CheckResult(finished=True, reason=REASON_SKIPPED,
                                       detail=f"截止时刻 {self.until.describe()} 今天已经过了")
                return CheckResult(finished=True, reason=REASON_UNTIL)

        # 4) 允许运行的时间窗
        #
        # 未到窗口通常要等待——等到明天凌晨 02:00 是完全正常的场景；
        # 但如果会话启动时「这个窗口今天已经关门，而且下一个周期的开门时间
        # 还在本次会话开始之前」（例如窗口 07:30~08:00，9:00 才启动），
        # 说明这一步已经错过了，不再干等下一个周期。
        if self.window and not self.window.contains(now):
            if self.window.contains(session_start):
                return CheckResult(
                    finished=True,
                    reason=REASON_BEFORE_END,
                    detail="已过允许运行的结束时刻",
                )

            if self.window.missed_by(session_start):
                return CheckResult(finished=True, reason=REASON_BEFORE_END,
                                   detail="已过允许运行的结束时刻")

            start = self.window.next_start(now)
            if start is None:
                return CheckResult(finished=True, reason=REASON_BEFORE_END,
                                   detail="没有可用的运行时间窗")
            wait = max(0.0, (start - now).total_seconds())
            if self.skip_if_missed:
                return CheckResult(finished=True, reason=REASON_SKIPPED,
                                   detail=f"距离开启还有 {fmt_duration(wait)}")
            if self.max_wait is not None and wait > self.max_wait:
                return CheckResult(
                    finished=True, reason=REASON_SKIPPED,
                    detail=f"距离开启还有 {fmt_duration(wait)}，超过上限 {fmt_duration(self.max_wait)}",
                )
            return CheckResult(not_ready=True, reason=NOT_READY_ACTIVE, wait_seconds=wait)

        # 5) 安静时段
        if self.quiet:
            wait = self.quiet.quiet_until(now)
            if wait is not None:
                detail = f"进入安静时段 {self.quiet.describe()}"
                if not self.wait_quiet:
                    return CheckResult(finished=True, reason=REASON_BEFORE_END, detail=detail)
                return CheckResult(not_ready=True, reason=NOT_READY_QUIET, wait_seconds=wait)

        # 6) body 次数：
        #    内层循环用 check_body=False 跳过这一条（body 跑满只代表这一轮结束）；
        #    顶层用它判断「整场的 body 预算是否已经用完」。
        if check_body:
            budget = self.body_times()
            if budget not in (None, -1) and done_body >= budget * max(1, self.session_rounds() or 1):
                return CheckResult(finished=True, reason=REASON_COMPLETED)

        return CheckResult()

    def current_period_end(self, session_start: Optional[datetime] = None) -> Optional[datetime]:
        """本次会话所落入的那个时间窗周期何时结束（用于中途判定）。"""
        if self.window is None:
            return None
        anchor = session_start or self.started_at
        end = self.window.end(anchor)
        if end is None:
            return None
        # 跨午夜窗口：会话在窗口中间（例如 03:00）时，end 可能是「今天 07:00」，
        # 但也可能是「昨天 07:00」之前的周期，这里只关心不早于会话开始的那个。
        if end <= anchor and self.window.contains(anchor):
            end = self.window.end(anchor + timedelta(days=1))
        return end

    def should_cut(self, now: Optional[datetime] = None,
                   session_start: Optional[datetime] = None) -> Optional[str]:
        """
        任务执行过程中的中断判定。只有 force_cut 打开、或「截止时间已到」才中途打断；
        时间窗结束会打断（跑完当前任务就停），安静时段不打断。
        """
        now = now or datetime.now()
        if self.force_cut:
            if self.ends_at and now >= self.ends_at:
                return REASON_DURATION
            if self.until:
                when, _ = self.until.resolve(self.started_at)
                if now >= when:
                    return REASON_UNTIL
        end = self.current_period_end(session_start)
        if end is not None and now >= end:
            return REASON_BEFORE_END
        return None

    def delay_until_next(self, now: Optional[datetime] = None) -> float:
        """这一次 body 结束后距离下一次开始的等待秒数（不会睡过截止时刻）。"""
        if self.interval <= 0:
            return 0.0
        now = now or datetime.now()
        wake = now + timedelta(seconds=self.interval)
        deadline = self.deadline
        if deadline and wake > deadline:
            wake = deadline
        return max(0.0, (wake - now).total_seconds())


@dataclass
class CheckResult:
    finished: bool = False
    reason: str = ""
    detail: str = ""
    not_ready: bool = False
    wait_seconds: Optional[float] = None

    @property
    def reason_label(self) -> str:
        return REASON_LABEL.get(self.reason, self.reason)


# ---------- 等待 ----------


def sleep_until(
    target_dt: datetime,
    *,
    now_fn: Callable[[], datetime] = datetime.now,
    sleep_fn: Callable[[float], None] = time.sleep,
    poll: float = 1.0,
    label: str = "",
    interrupt_after: int = 1,
    verbose: bool = True,
):
    """
    睡到 target_dt。Ctrl+C 不直接抛出：第一次提示，再按一次结束等待。
    返回 (实际到达的时间, 原因)。
    """
    presses = 0
    announced = False
    while True:
        now = now_fn()
        remaining = (target_dt - now).total_seconds()
        if remaining <= 0:
            return now, REASON_COMPLETED
        try:
            sleep_fn(min(poll, remaining))
        except KeyboardInterrupt:
            presses += 1
            if presses >= max(1, interrupt_after):
                return now_fn(), REASON_INTERRUPT
            if not announced and verbose:
                print(f"\n⚠️ 正在等待{label or '时间窗'}，再按一次 Ctrl+C 可结束等待")
                announced = True
            continue

        # 空输入可以提前结束等待（管道里没有输入时会报错，忽略即可）
        try:
            import select
            import sys

            if sys.stdin and sys.stdin.isatty():
                if select.select([sys.stdin], [], [], 0)[0]:
                    line = sys.stdin.readline().strip()
                    if not line:
                        return now_fn(), REASON_WAIT_DONE
        except Exception:
            pass


def sleep_for(seconds: float, *, sleep_fn: Callable[[float], None] = time.sleep,
              verbose: bool = True, label: str = ""):
    """确定性的秒级等待：返回 completed / interrupt。"""
    if seconds <= 0:
        return REASON_COMPLETED
    if verbose:
        print(f"\n等待 {fmt_duration(seconds)}...（{label}）")
    try:
        sleep_fn(seconds)
        return REASON_COMPLETED
    except KeyboardInterrupt:
        if verbose:
            print("\n⚠️ 中断等待")
        return REASON_INTERRUPT


# ---------- 执行结果 ----------


@dataclass
class StepResult:
    """一次任务执行的结果。"""
    name: str
    ok: bool
    duration: float = 0.0
    error: str = ""
    phase: str = ""        # startup / body / shutdown


@dataclass
class SessionResult:
    name: str = ""
    rounds: int = 0            # 会话轮数
    body_runs: int = 0         # body 执行次数
    executed: int = 0
    success: int = 0
    failed: int = 0
    skipped: int = 0
    duration: float = 0.0
    stop_reason: str = REASON_COMPLETED
    detail: str = ""
    interrupted: bool = False
    errors: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    game_open: bool = False    # 结束时游戏是否还开着
    waited: float = 0.0

    @property
    def reason_label(self) -> str:
        return REASON_LABEL.get(self.stop_reason, self.stop_reason)

    @property
    def ok(self) -> bool:
        return self.failed == 0 and not self.errors


# ---------- 主流程 ----------


def run_session(
    runner,
    box: Session,
    *,
    notify: Optional[str] = None,
    now_fn: Callable[[], datetime] = datetime.now,
    sleep_fn: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
    verbose: bool = True,
    on_finish=None,
) -> SessionResult:
    """
    执行一个会话。

    runner 需要提供：
        run_task(name, stop_checker=None, notify=None, pipeline_override=None) -> bool
        flush_notifications()

    返回 SessionResult（含停止原因、统计、结束时游戏是否开着）。
    """
    if not box.all_tasks:
        raise ValueError("时间盒没有配置任何任务")

    notify = notify if notify is not None else box.notify
    result = SessionResult(name=box.name)
    t0 = now_fn()
    started = False            # startup 是否已经跑完
    stopped = False
    shutdown_done = False
    body_per_round = box.body_times()        # 每轮会话内 body 跑几次
    session_total = box.session_rounds()     # 会话跑几轮（老写法为 1）
    if body_per_round in (None, -1):
        body_budget = None                   # 不限次数：由时间约束决定何时停
    elif session_total in (None, -1):
        body_budget = None
    else:
        body_budget = body_per_round * session_total   # 整场 body 总预算
    body_done = 0                            # 已经跑过的 body 次数（整场累计）
    round_no = 0                             # 会话轮数
    session_start = box.started_at           # 本次会话开始的时刻（判断时间窗用）

    log(f"\n{'=' * 52}")
    log(f"  会话: {box.name}")
    for line in box.describe_lines():
        log(f"  {line}")
    log(f"{'=' * 52}")

    try:
        while True:
            now = now_fn()
            check = box.check(now, done_body=body_done, done_rounds=round_no,
                              session_start=session_start)

            # ---- 未就绪：等待 ----
            if check.not_ready:
                wait = check.wait_seconds or 0.0
                label = "时间窗" if check.reason == NOT_READY_ACTIVE else "安静时段"
                desc = box.window.describe() if check.reason == NOT_READY_ACTIVE else box.quiet.describe()
                target = now + timedelta(seconds=wait)
                log(f"\n⏸️ {check.reason_label}（{desc}），等待到 {target:%m-%d %H:%M:%S}"
                    f"（约 {fmt_duration(wait)}）")
                _, why = sleep_until(target, now_fn=now_fn, sleep_fn=sleep_fn,
                                     label=label, verbose=verbose)
                result.waited += max(0.0, (now_fn() - now).total_seconds())
                if why != REASON_COMPLETED:
                    stopped = True
                    result.interrupted = why == REASON_INTERRUPT
                    result.stop_reason = why
                    result.detail = f"{label}等待未完成"
                    break
                continue

            # ---- 该结束了 ----
            if check.finished:
                result.stop_reason = check.reason or REASON_COMPLETED
                result.detail = check.detail
                break

            round_no += 1
            # 老写法里「一轮 = 一次 body」，新写法里「一轮 = 一次 startup→body×N→shutdown」
            result.rounds = body_done + 1 if box.flat else round_no
            if not box.flat:
                session_total = box.session_rounds()
                log(f"\n{'=' * 44}")
                log(f"  第 {round_no} 轮会话" + ("" if session_total in (None, -1)
                                                else f"/{session_total}"))
                log(f"{'=' * 44}")

            # ---- 启动：只在最开始跑一次 ----
            if not started:
                if not box.flat and box.startup:
                    log(f"\n{'-' * 40}")
                    log(f"  启动（只跑一次）")
                    log(f"{'-' * 40}")
                    if not _run_steps(runner, box, box.startup, "startup", now_fn, log, notify, result):
                        if verbose:
                            log("  ⚠️ 启动阶段有任务失败，继续尝试 body")
                started = True

            # ---- body ----
            done_this_round = 0
            while True:
                # 每次开始 body 之前都判定一次时间约束：
                # check_body=False 表示「body 次数跑满」交给下面的计数器处理，
                # 这里只关心窗口/安静时段/截止时间。
                now = now_fn()
                inner = box.check(now, done_body=body_done, done_rounds=round_no - 1,
                                  session_start=session_start, check_body=False)
                if inner.not_ready:
                    break
                if inner.finished:
                    result.stop_reason = inner.reason or REASON_COMPLETED
                    result.detail = inner.detail
                    stopped = True
                    break

                # 整场 body 预算用完
                if body_budget is not None and body_done >= body_budget:
                    break
                # 这一轮的 body 次数跑满
                if body_per_round not in (None, -1) and done_this_round >= body_per_round:
                    break

                body_label = f"body 第 {body_done + 1} 次" + (
                    "" if body_per_round is None else f"/{body_per_round}"
                )
                if not box.flat:
                    body_label += f"（会话第 {round_no} 轮）"
                log(f"\n{'-' * 40}")
                log(f"  {body_label}")
                log(f"{'-' * 40}")

                _run_steps(runner, box, box.body, "body", now_fn, log, notify, result)
                body_done += 1
                done_this_round += 1
                result.body_runs += 1
                if box.flat:
                    result.rounds = body_done        # 老写法：一轮 = 一次 body

                # 已经是最后一次 body（整场预算用完 / 这一轮跑满）→ 结束，不再间隔
                if body_budget is not None and body_done >= body_budget:
                    break
                if body_per_round not in (None, -1) and done_this_round >= body_per_round:
                    break

                # 还有下一次 body：先看时间是否还允许（到点就直接收尾，不会白等一轮）
                nxt = box.check(now_fn(), done_body=body_done, done_rounds=round_no - 1,
                                session_start=session_start, check_body=False)
                if nxt.not_ready or nxt.finished:
                    # 交给外层循环处理（not_ready 会等待，finished 会结束整场）
                    if nxt.finished:
                        result.stop_reason = nxt.reason or REASON_COMPLETED
                        result.detail = nxt.detail
                    break

                delay = box.delay_until_next(now_fn())
                if delay > 0:
                    why = sleep_for(delay, sleep_fn=sleep_fn, verbose=verbose, label="body 间隔")
                    if why == REASON_INTERRUPT:
                        result.stop_reason = REASON_INTERRUPT
                        result.interrupted = True
                        stopped = True
                        break

            if stopped:
                break

            # 内层循环是因为「时间到了」退出的（而不是这一轮跑满）→ 整场结束
            if body_budget is not None and body_done < body_budget and \
                    (body_per_round in (None, -1) or done_this_round < body_per_round):
                tail = box.check(now_fn(), done_body=body_done, done_rounds=round_no - 1,
                                 session_start=session_start, check_body=False)
                if tail.finished:
                    result.stop_reason = tail.reason or REASON_COMPLETED
                    result.detail = tail.detail
                    break

            # 一轮的统计收口：老写法的一轮就是一次 body
            result.rounds = body_done if box.flat else round_no

            # 这一轮结束后，如果还没定下结束原因，就用最新判定补上
            if result.stop_reason == REASON_COMPLETED and not result.detail:
                tail = box.check(now_fn(), done_body=body_done, done_rounds=round_no,
                                 session_start=session_start)
                if tail.finished:
                    result.stop_reason = tail.reason or REASON_COMPLETED
                    result.detail = tail.detail

            # ---- 要不要再开一轮 ----
            # 新写法：一轮 = 一次 startup→body×N→shutdown，由 repeat 决定几轮
            if box.flat:
                # 老写法：整轮重复，body 预算已经管住次数了
                result.stop_reason = result.stop_reason or REASON_COMPLETED
                break

            # 整场 body 预算用完 → 不会再开新一轮
            if body_budget is not None and body_done >= body_budget:
                result.stop_reason = result.stop_reason or REASON_COMPLETED
                break

            session_total = box.session_rounds()
            if session_total not in (None, -1) and round_no >= session_total:
                result.stop_reason = result.stop_reason or REASON_COMPLETED
                break

            # 时间约束已经到点 → 也不会再开新一轮（省掉最后一次无意义的间隔）
            nxt = box.check(now_fn(), done_body=body_done, done_rounds=round_no,
                            session_start=session_start)
            if nxt.finished:
                result.stop_reason = nxt.reason or REASON_COMPLETED
                result.detail = nxt.detail
                break

            # 进入下一轮会话：body 次数继续累计（预算不变）
            delay = box.delay_until_next(now_fn())
            if delay > 0:
                why = sleep_for(delay, sleep_fn=sleep_fn, verbose=verbose, label="轮间隔")
                if why == REASON_INTERRUPT:
                    result.stop_reason = REASON_INTERRUPT
                    result.interrupted = True
                    break

    except KeyboardInterrupt:
        if verbose:
            print("\n\n⚠️ 收到 Ctrl+C，正在收尾")
        result.interrupted = True
        result.stop_reason = REASON_FORCE
    except Exception as e:
        logger.exception("会话执行异常")
        result.errors.append(str(e))
        result.stop_reason = REASON_ERROR
        result.detail = str(e)

    # ---- 收尾：一次关闭 ----
    need_close = bool(box.shutdown) and not box.keep_open
    if need_close and started:
        if result.interrupted and not box.shutdown_after_interrupt:
            log("\n（配置为中断时不关闭，跳过关闭阶段）")
        else:
            log(f"\n{'-' * 40}")
            log(f"  关闭（只跑一次）")
            log(f"{'-' * 40}")
            try:
                _run_steps(runner, box, box.shutdown, "shutdown", now_fn, log, notify, result)
                shutdown_done = True
            except KeyboardInterrupt:
                log("\n⚠️ 关闭阶段被中断")

    # 结束时游戏是否还开着
    if not started:
        result.game_open = False                      # 根本没启动
    elif need_close:
        result.game_open = not shutdown_done          # 关闭失败就当作还开着
    else:
        result.game_open = True                       # keep_open 或没有关闭阶段

    try:
        runner.flush_notifications()
    except Exception as e:
        logger.warning(f"flush 通知失败: {e}")

    result.duration = max(0.0, (now_fn() - t0).total_seconds())
    _log_summary(result, box, log)

    if on_finish is not None:
        try:
            on_finish(result)
        except Exception as e:
            logger.warning(f"on_finish 回调失败: {e}")

    return result




def _run_steps(runner, box: Session, names: list, phase: str, now_fn, log, notify,
               result: SessionResult) -> bool:
    """按顺序跑一段任务；返回是否全部成功。"""
    all_ok = True
    for name in names:
        cut = box.should_cut(now_fn())
        if cut:
            result.skipped += 1
            log(f"  ⏭️ 跳过 {name}（{REASON_LABEL.get(cut, cut)}）")
            all_ok = False
            continue

        step = StepResult(name=name, ok=False, phase=phase)
        t0 = now_fn()

        def stop_checker(node_name=None, text=None):
            return box.should_cut(now_fn()) is not None

        try:
            kwargs = {"stop_checker": stop_checker}
            if _accepts(runner, "notify"):
                kwargs["notify"] = notify
            ok = bool(runner.run_task(name, **kwargs))
            step.ok = ok
        except KeyboardInterrupt:
            raise
        except Exception as e:
            logger.error(f"[{name}] 异常: {e}")
            step.error = str(e)
            result.errors.append(f"{name}: {e}")
            log(f"  ✗ {name}（异常: {e}）")
            ok = False

        step.duration = max(0.0, (now_fn() - t0).total_seconds())
        result.steps.append(step)
        result.executed += 1
        if step.ok:
            result.success += 1
            log(f"  ✓ {name}")
        else:
            result.failed += 1
            all_ok = False
            if not step.error:
                log(f"  ✗ {name}")
    return all_ok


def _accepts(runner, param: str) -> bool:
    """任务对象的方法是否支持某个参数（兼容老代码）。"""
    import inspect

    try:
        sig = inspect.signature(runner.run_task)
    except (TypeError, ValueError):
        return False
    return param in sig.parameters


def _log_summary(result: SessionResult, box: Session, log):
    log(f"\n{'=' * 52}")
    log(f"  会话结束: {box.name}")
    log(f"  原因: {result.reason_label}" + (f"（{result.detail}）" if result.detail else ""))
    log(f"  统计: {result.rounds} 轮 / body {result.body_runs} 次 / "
        f"{result.executed} 次执行（成功 {result.success}，失败 {result.failed}"
        + (f"，跳过 {result.skipped}" if result.skipped else "") + "）")
    log(f"  运行时长: {fmt_duration(result.duration)}"
        + (f"（其中等待 {fmt_duration(result.waited)}）" if result.waited >= 1 else ""))
    end = box.deadline
    if end:
        log(f"  计划结束: {end:%Y-%m-%d %H:%M:%S}")
    log(f"  游戏状态: {'保持运行' if result.game_open else '已关闭'}")
    if result.errors:
        log(f"  异常: {len(result.errors)} 项")
        for e in result.errors[:10]:
            log(f"    - {e}")
    log(f"{'=' * 52}")


def summarize(result: SessionResult) -> str:
    """给通知用的摘要。"""
    lines = [
        f"原因: {result.reason_label}",
        f"轮数: {result.rounds} / body {result.body_runs} 次",
        f"执行: {result.executed}（成功 {result.success} / 失败 {result.failed}）",
        f"时长: {fmt_duration(result.duration)}",
    ]
    if result.skipped:
        lines.append(f"跳过: {result.skipped}")
    if result.errors:
        lines.append(f"异常: {result.errors[0]}")
    return "\n".join(lines)
