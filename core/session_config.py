# core/session_config.py
"""
会话配置（session.yaml）

与 schedules.yaml 的区别：
  - schedules.yaml  → apscheduler 定时任务，daemon 常驻，按 cron/interval 触发
  - session.yaml    → 会话档案，前台一次性运行，用「时间 + 次数」控制循环

一个档案描述一个「会话」：
    启动（一次） → body（循环 N 次 / 到点） → 关闭（一次）

字段（与 CLI 参数一一对应）
--------------------------
  startup       启动阶段，只跑一次，例如 [启动游戏, 切换到全面战场]
  body          循环阶段，例如 [蜂医挂机]
  shutdown      关闭阶段，只跑一次，例如 [关闭游戏]；为空表示不关闭
  body_repeat   body 跑几次；0 / -1 = 不限（由时间约束决定何时停）
  repeat        整个会话重复几轮；0 / -1 = 不限
  duration      总时长，例如 8h
  until         绝对截止时刻，例如 "12:00"
  active        允许运行的时间窗，例如 "07:30~23:00"（跨午夜写 23:00~07:30）
  quiet         安静时段，到点不再开始新的 body，例如 "23:00~07:30"
  wait_quiet    命中安静时段时是否等到时段结束再继续（默认 false = 直接结束）
  skip_if_missed 未进入时间窗时跳过而不是等待（默认 false = 等待）
  force_cut     到点是否立即中断当前任务（默认 false = 跑完当前一次）
  interval      body 之间的间隔秒数
  keep_open     结束时保持游戏运行（留给下一个会话），默认 false
  notify        覆盖任务自身的通知设置（none/simple/report）
  max_wait      等待下一次时间窗打开的上限秒数

兼容老写法：只写 tasks 时，整轮重复（启动/干活/关闭都在每一轮里）。
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path

import yaml

from core.config import PROJECT_ROOT
from core.config import CONFIG_DIR
from core.session import (
    TimeParseError,
    Session,
    Window,
    parse_active_window,
    parse_duration,
    parse_hhmm,
)

logger = logging.getLogger(__name__)

SESSION_FILE = CONFIG_DIR / "session.yaml"

# 转成 Session 时直接透传的字段
FIELDS = (
    "body_repeat", "repeat", "duration", "until", "active", "quiet",
    "wait_quiet", "force_cut", "interval", "notify", "max_wait",
    "keep_open", "skip_if_missed", "shutdown_after_interrupt",
)
LIST_FIELDS = ("startup", "body", "shutdown", "tasks")

META_FIELDS = ("name", "enabled", "description")


class SessionConfigError(ValueError):
    """会话配置有误"""


# ---------- 读取 ----------


def load_raw(path: Path = None) -> dict:
    path = Path(path or SESSION_FILE)
    if not path.is_file():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise SessionConfigError(f"{path} 的顶层结构应该是字典")
    return data


def load_profiles(path: Path = None, include_disabled: bool = True) -> list[dict]:
    """读取所有档案（未合并 defaults）。"""
    data = load_raw(path)
    items = data.get("sessiones") or data.get("boxes") or []
    if not isinstance(items, list):
        raise SessionConfigError("sessiones 应该是列表")
    profiles = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise SessionConfigError(f"第 {i + 1} 个会话不是字典")
        if not item.get("name"):
            raise SessionConfigError(f"第 {i + 1} 个会话缺少 name")
        if not include_disabled and not item.get("enabled", True):
            continue
        profiles.append(dict(item))
    return profiles


def get_defaults(path: Path = None) -> dict:
    data = load_raw(path)
    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise SessionConfigError("defaults 应该是字典")
    return dict(defaults)


def get_profile(name: str, path: Path = None) -> dict:
    for p in load_profiles(path):
        if p["name"] == name:
            merged = copy.deepcopy(get_defaults(path))
            merged.update(p)
            return merged
    raise KeyError(f"未找到会话档案: {name}")


def list_names(path: Path = None) -> list[str]:
    return [p["name"] for p in load_profiles(path)]


# ---------- 合并 CLI 覆盖 ----------


def merge(profile: dict = None, overrides: dict = None, defaults: dict = None) -> dict:
    """
    合并 defaults → profile → overrides（注意形参顺序，调用时建议写关键字）。

    None 视为「未指定」，不会覆盖上一层；空列表会覆盖（表示显式清空）；
    值为 None 的档案字段（例如注释掉的配置）也会被跳过。

    active / quiet 兼容档案里写 start + stop 两个键的形式。
    """
    merged = {}
    for layer in (defaults, profile, overrides):
        if not layer:
            continue
        for key, value in layer.items():
            if value is None:
                continue
            merged[key] = value

    # active / quiet 的两种写法归一
    for key in ("active", "quiet"):
        if key not in merged and (f"{key}_start" in merged or f"{key}_stop" in merged):
            start = merged.get(f"{key}_start")
            stop = merged.get(f"{key}_stop")
            if start:
                merged[key] = f"{start}~{stop}" if stop else f"{start}~24:00"
        merged.pop(f"{key}_start", None)
        merged.pop(f"{key}_stop", None)

    # steps 是「startup + body + shutdown」的简写
    if not merged.get("startup") and not merged.get("body") and merged.get("steps"):
        merged["body"] = list(merged.pop("steps"))

    merged.setdefault("body_repeat", -1)
    return merged


def build(name: str, profile: dict = None, overrides: dict = None,
          defaults: dict = None, base_tasks: list = None) -> Session:
    """把配置合成为一个 Session（会校验参数）。"""
    cfg = merge(profile=profile, overrides=overrides, defaults=defaults)
    cfg["name"] = name or cfg.get("name") or "session"

    if not cfg.get("startup") and not cfg.get("body") and not cfg.get("shutdown"):
        # 老写法：tasks 整轮重复
        cfg["body"] = list(cfg.get("tasks") or base_tasks or [])

    kwargs = {"name": cfg["name"], "tasks": list(cfg.get("tasks") or [])}
    for field_name in ("startup", "body", "shutdown"):
        if cfg.get(field_name) is not None:
            kwargs[field_name] = list(cfg[field_name])
    for field_name in FIELDS:
        if field_name in cfg:
            kwargs[field_name] = cfg[field_name]

    # 配置里写的是 active，Session 里叫 window
    if "active" in cfg:
        kwargs["window"] = cfg["active"]
        kwargs.pop("active", None)

    if kwargs.get("window") is not None:
        kwargs["window"] = _window(kwargs["window"], "active")
    if kwargs.get("quiet") is not None:
        kwargs["quiet"] = _window(kwargs["quiet"], "quiet")

    try:
        return Session(**kwargs)
    except (TimeParseError, ValueError) as e:
        raise SessionConfigError(f"会话「{kwargs['name']}」配置有误: {e}") from e


def _window(value, field_name: str):
    if isinstance(value, Window):
        return value
    return Window.from_window(value, field_name)


# ---------- 校验 ----------


def validate(cfg: dict, tasks_available: list = None) -> list[str]:
    """校验一个已合并的配置，返回错误信息列表（空表示通过）。"""
    errors = []
    cfg = dict(cfg or {})
    name = cfg.get("name", "<未命名>")

    all_tasks = []
    for key in ("startup", "body", "shutdown", "tasks"):
        all_tasks.extend(cfg.get(key) or [])
    if not all_tasks:
        errors.append(f"[{name}] 没有配置任何任务（startup / body / shutdown / tasks 都是空的）")

    if tasks_available is not None:
        for t in all_tasks:
            if t not in tasks_available:
                errors.append(f"[{name}] 未知任务: {t}")

    for key in ("body_repeat", "repeat"):
        value = cfg.get(key)
        if value is None:
            continue
        try:
            value = int(value)
            if value < -1:
                errors.append(f"[{name}] {key} 只能是正整数、0 或 -1（当前 {value}）")
        except (TypeError, ValueError):
            errors.append(f"[{name}] {key} 不是整数: {value!r}")

    if cfg.get("duration") is not None:
        try:
            parse_duration(cfg["duration"])
        except TimeParseError as e:
            errors.append(f"[{name}] duration 有误: {e}")

    if cfg.get("until") is not None:
        try:
            parse_hhmm(cfg["until"], "until")
        except TimeParseError as e:
            errors.append(f"[{name}] until 有误: {e}")

    for key in ("active", "quiet"):
        if cfg.get(key) is not None:
            try:
                parse_active_window(cfg[key], key)
            except TimeParseError as e:
                errors.append(f"[{name}] {key} 有误: {e}")

    for key in ("interval", "max_wait"):
        if cfg.get(key) is None:
            continue
        try:
            if float(cfg[key]) < 0:
                errors.append(f"[{name}] {key} 不能为负数")
        except (TypeError, ValueError):
            errors.append(f"[{name}] {key} 不是数字: {cfg[key]!r}")

    if cfg.get("notify") not in (None, "none", "simple", "report"):
        errors.append(f"[{name}] notify 只能是 none/simple/report")

    # 没有任何上限的会话会一直跑到 Ctrl+C，提醒一下（不算错误）
    return errors


def validate_all(tasks_available: list = None, path: Path = None) -> list[str]:
    errors = []
    try:
        defaults = get_defaults(path)
        profiles = load_profiles(path)
    except SessionConfigError as e:
        return [str(e)]

    names = [p["name"] for p in profiles]
    for p in profiles:
        merged = merge(profile=p, defaults=defaults)
        errors.extend(validate(merged, tasks_available))
        if names.count(p["name"]) > 1:
            errors.append(f"[{p['name']}] 档案名重复")
    return errors


def warnings(cfg: dict) -> list[str]:
    """非致命的提醒（例如「没有上限」）。"""
    out = []
    name = cfg.get("name", "<未命名>")
    has_time = any(cfg.get(k) is not None for k in ("duration", "until", "active", "quiet"))
    body_count = cfg.get("body_repeat")
    unlimited_body = body_count in (None, -1, 0)
    rounds = cfg.get("repeat")
    unlimited_rounds = rounds in (None, -1, 0)
    if not has_time and unlimited_body:
        out.append(f"[{name}] 没有时间上限、body 也不限次数 → 只能靠 Ctrl+C 结束")
    return out


# ---------- 展示 ----------


def describe(cfg: dict) -> list[str]:
    """把一个档案配置描述成人类可读的若干行。"""
    lines = []

    def count(value):
        if value in (None, -1, 0):
            return "不限"
        return str(value)

    startup = cfg.get("startup") or []
    body = cfg.get("body") or []
    shutdown = cfg.get("shutdown") or []
    tasks = cfg.get("tasks") or []

    if startup or shutdown:
        lines.append(f"启动（一次）: {' → '.join(startup) or '（无）'}")
        lines.append(f"body（{count(cfg.get('body_repeat'))} 次）: {' → '.join(body) or '（无）'}")
        close = "保持游戏运行（keep_open）" if cfg.get("keep_open") else (' → '.join(shutdown) or '（无）')
        lines.append(f"关闭（一次）: {close}")
    elif tasks:
        lines.append(f"整轮重复模式（老写法）: {' → '.join(tasks)}")
        lines.append(f"轮数: {count(cfg.get('repeat'))} / 每轮间隔: {cfg.get('interval', 0)} 秒")
        return lines
    else:
        lines.append("（没有配置任何任务）")
        return lines

    lines.append(f"会话轮数: {count(cfg.get('repeat'))}")

    if cfg.get("duration") is not None:
        try:
            lines.append(f"总时长: {cfg['duration']}（{parse_duration(cfg['duration'])} 秒）")
        except TimeParseError:
            lines.append(f"总时长: {cfg['duration']}（无法解析）")

    for key, label in (("until", "截止时刻"), ("active", "运行时间窗"), ("quiet", "安静时段")):
        if cfg.get(key) is not None:
            lines.append(f"{label}: {cfg[key]}")

    if cfg.get("skip_if_missed"):
        lines.append("时间窗未到: 跳过（不等待）")
    if cfg.get("wait_quiet"):
        lines.append("命中安静时段: 等到时段结束再继续")
    if cfg.get("force_cut"):
        lines.append("到达时间上限: 立即中断当前任务")
    if cfg.get("interval") is not None:
        lines.append(f"body 间隔: {cfg['interval']} 秒")
    if cfg.get("keep_open"):
        lines.append("结束时: 保持游戏运行")
    if cfg.get("notify") is not None:
        lines.append(f"通知: {cfg['notify']}")
    return lines
