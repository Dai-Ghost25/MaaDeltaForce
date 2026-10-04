# core/scheduler.py
import os
import time
import signal
import logging
import threading
from pathlib import Path
from datetime import datetime

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core.config import SCHEDULES_FILE
from core.task_runner import get_runner

logger = logging.getLogger(__name__)

# SCHEDULES_FILE = PROJECT_ROOT / "schedules.yaml"


class ScheduleManager:
    def __init__(self):
        self.schedules = self._load()
        self.run_counts = {s["name"]: 0 for s in self.schedules}
        self._sched = BackgroundScheduler()
        self._lock = threading.Lock()

    # ---- 配置加载 ----

    def _load(self) -> list[dict]:
        if not SCHEDULES_FILE.is_file():
            logger.warning(f"未找到 {SCHEDULES_FILE}，无定时任务")
            return []
        with open(SCHEDULES_FILE, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("schedules", [])

    # ---- 启动 ----

    def start(self):
        for s in self.schedules:
            if not s.get("enabled", True):
                continue
            self._add_job(s)
        self._sched.start()
        logger.info(f"调度器启动，共 {len(self._sched.get_jobs())} 个定时任务")

    def _add_job(self, s: dict):
        trigger = self._build_trigger(s["trigger"])
        self._sched.add_job(
            func=self._run_schedule,
            args=[s["name"]],
            trigger=trigger,
            id=s["name"],
            name=s["name"],
            max_instances=1,        # 同一 schedule 不并发
            coalesce=True,          # 错过多次只补一次
            misfire_grace_time=300, # 错过 5 分钟内仍执行
        )

    def _build_trigger(self, t: dict):
        if t["type"] == "cron":
            return CronTrigger.from_crontab(t["expr"])
        if t["type"] == "interval":
            return IntervalTrigger(seconds=int(t["seconds"]))
        raise ValueError(f"未知 trigger 类型: {t['type']}")

    # ---- 执行 ----

    def _run_schedule(self, name: str):
        s = next((x for x in self.schedules if x["name"] == name), None)
        if s is None:
            return

        with self._lock:
            if s.get("max_runs") and self.run_counts[name] >= s["max_runs"]:
                logger.info(f"[{name}] 已达最大运行次数，禁用")
                self._sched.remove_job(name)
                return
            self.run_counts[name] += 1

        logger.info(f"===== 触发定时任务: {name} (第 {self.run_counts[name]} 次) =====")

        stop_when = s.get("stop_when") or []
        stop_flag = {"stop": False}

        def stop_checker(node: str, text: str) -> bool:
            for rule in stop_when:
                if rule["node"] != node:
                    continue
                if self._match(text, rule["match"], rule["value"]):
                    logger.info(f"[{name}] 命中停止条件: {node} = {text}")
                    stop_flag["stop"] = True
                    return True
            return False

        try:
            if "session" in s:
                # 引用 session.yaml 里的档案
                self._run_session_ref(s, stop_checker)
            elif self._is_lifecycle(s):
                # 内联会话语法（startup / body / shutdown）
                self._run_lifecycle(s, stop_checker)
            elif "task" in s:
                # 单任务 + params
                self._run_single_task(s["task"], s.get("params", {}), stop_checker)
            elif "tasks" in s:
                # 老语法：任务列表，线性执行一遍
                for task_name in s["tasks"]:
                    if stop_flag["stop"]:
                        logger.info(f"[{name}] stop_when 命中，跳过剩余任务")
                        break
                    self._run_single_task(task_name, {}, stop_checker)
            else:
                logger.error(f"[{name}] 未识别的 schedule 结构，跳过")
        except Exception as e:
            logger.exception(f"[{name}] 执行失败: {e}")
        finally:
            from core import task_runner
            for r in getattr(task_runner, "_runners", {}).values():
                try:
                    r.flush_notifications()
                except Exception as e:
                    logger.warning(f"flush 通知失败: {e}")

        logger.info(f"===== 定时任务结束: {name} =====")

# ---- 会话语法：startup / body / shutdown ----

    LIFECYCLE_KEYS = (
        "startup", "body", "shutdown", "body_repeat",
        "duration", "until", "active", "quiet", "wait_quiet",
        "skip_if_missed", "force_cut", "keep_open", "max_wait", "interval",
    )

    @classmethod
    def _is_lifecycle(cls, s: dict) -> bool:
        """是不是会话语法。"""
        if any(k in s for k in ("startup", "body", "shutdown", "body_repeat")):
            return True
        # 只写 tasks + 时间约束时，也用会话语义跑
        return "tasks" in s and any(k in s for k in cls.LIFECYCLE_KEYS)

    def _run_lifecycle(self, s: dict, stop_checker=None):
        """把 schedules.yaml 里的一条配置当成会话来跑。"""
        from core.session import run_session
        from core import session_config as tcc
        from core import task_runner

        cfg = dict(s)
        cfg.setdefault("name", s["name"])
        cfg.setdefault("body", list(s.get("tasks") or []))
        if s.get("task") and not cfg.get("body"):
            cfg["body"] = [s["task"]]

        box = tcc.build(s["name"], profile=cfg)
        box.start_time = datetime.now()

        runner = _ScheduleRunner(
            stop_checker=stop_checker,
            params=s.get("params") or {},
            runner_factory=task_runner.get_runner,
        )
        result = run_session(runner, box, verbose=False)
        logger.info(
            f"[{s['name']}] 会话结束: {result.reason_label}"
            f"（{result.rounds} 轮 / body {result.body_runs} 次 / "
            f"成功 {result.success} 失败 {result.failed}）"
        )
        return result

    # 调度元数据字段（不参与覆盖 session 档案）
    _SCHEDULE_META = {
        "name", "session", "trigger", "enabled",
        "max_runs", "stop_when", "params",
    }

    def _run_session_ref(self, s: dict, stop_checker=None):
        """引用 session.yaml 里的档案来跑。schedule 里的其他字段会覆盖档案同名字段。"""
        from core.session import run_session
        from core import session_config as tcc
        from core import task_runner

        session_name = s["session"]
        try:
            profile = tcc.get_profile(session_name)
        except KeyError:
            logger.error(f"[{s['name']}] 未找到会话档案: {session_name}")
            return None
        except Exception as e:
            logger.error(f"[{s['name']}] 加载档案 {session_name} 失败: {e}")
            return None

        # schedule 里除元数据外的字段作为覆盖项
        overrides = {
            k: v for k, v in s.items()
            if k not in self._SCHEDULE_META
        }

        try:
            cfg = tcc.merge(
                profile=profile,
                overrides=overrides,
                defaults=tcc.get_defaults(),
            )
            cfg["name"] = s["name"]
            box = tcc.build(s["name"], profile=cfg)
        except Exception as e:
            logger.error(f"[{s['name']}] 构造会话失败: {e}")
            return None

        box.start_time = datetime.now()

        runner = _ScheduleRunner(
            stop_checker=stop_checker,
            params=s.get("params") or {},
            runner_factory=task_runner.get_runner,
        )
        result = run_session(runner, box, verbose=False)
        logger.info(
            f"[{s['name']}] 会话结束: {result.reason_label}"
            f"（{result.rounds} 轮 / body {result.body_runs} 次 / "
            f"成功 {result.success} 失败 {result.failed}）"
        )
        return result

    @staticmethod
    def _match(text: str, mode: str, value: str) -> bool:
        if mode == "contains":
            return value in text
        if mode == "equals":
            return text == value
        if mode == "regex":
            import re
            return re.search(value, text) is not None
        return False

    def _run_single_task(self, task_name: str, params: dict, stop_checker=None):
        """
        执行单个任务。可能是：
        - 函数任务（如"交易行扫描"）
        - pipeline 任务（如"邮件检查"）
        """
        # ---- 函数任务分发 ----
        if task_name == "交易行扫描":
            from core.trade_flow import run_scan_all
            logger.info(f"执行函数任务: 交易行扫描, 参数={params}")
            run_scan_all(
                items=params.get("items"),
                list_name=params.get("list_name"),
            )
            return

        # ---- pipeline 任务 ----
        runner = get_runner()
        runner.run_task(task_name, stop_checker=stop_checker)

    # ---- 生命周期 ----

    def run_forever(self):
        from core.config import PROJECT_ROOT
        PID_FILE = PROJECT_ROOT / "mdf.pid"
        PID_FILE.write_text(str(os.getpid()))

        # 注册跨平台信号（Linux 才有 SIGHUP，Windows 跳过）
        try:
            signal.signal(signal.SIGHUP, self._on_sighup)
        except AttributeError:
            pass

        self.start()
        logger.info("按 Ctrl+C 退出")

        try:
            # 关键：带超时，每秒醒一次，让 KeyboardInterrupt 有机会抛出
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("收到 Ctrl+C，正在关闭...")
        finally:
            self.shutdown()
            if PID_FILE.is_file():
                PID_FILE.unlink()

    def shutdown(self):
        logger.info("调度器关闭中...")
        if self._sched.running:
            self._sched.shutdown(wait=False)

        from core import task_runner
        for r in getattr(task_runner, "_runners", {}).values():
            try:
                r.tasker.post_stop()
            except Exception as e:
                logger.warning(f"停止 Tasker 失败: {e}")
            try:
                r.flush_notifications()
                r.notifier.shutdown()
            except Exception as e:
                logger.warning(f"关闭时 flush 通知失败: {e}")

# ---------- 让会话可以直接用定时任务里的「函数任务」 ----------

class _ScheduleRunner:
    """
    把 TaskRunner 包一层，补上两个东西：
      1. 函数任务（交易行扫描）的分发
      2. 定时任务里的 stop_checker / params 传递
    """

    def __init__(self, stop_checker=None, params: dict = None, runner_factory=None):
        self.stop_checker = stop_checker
        self.params = params or {}
        self._factory = runner_factory or get_runner

    @property
    def runner(self):
        return self._factory()

    def run_task(self, name, stop_checker=None, pipeline_override=None, notify=None):
        if name == "交易行扫描":
            from core.trade_flow import run_scan_all
            logger.info(f"执行函数任务: 交易行扫描, 参数={self.params}")
            run_scan_all(
                items=self.params.get("items"),
                list_name=self.params.get("list_name"),
            )
            return True

        checker = stop_checker or self.stop_checker
        return self.runner.run_task(
            name,
            stop_checker=checker,
            pipeline_override=pipeline_override,
            notify=notify,
        )

    def flush_notifications(self):
        from core import task_runner
        for r in task_runner._runners.values():
            try:
                r.flush_notifications()
            except Exception as e:
                logger.warning(f"flush 失败: {e}")