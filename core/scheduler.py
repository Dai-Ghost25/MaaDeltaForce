# core/scheduler.py
import os
import time
import signal
import logging
import threading
from pathlib import Path

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core.config import PROJECT_ROOT
from core.task_runner import get_runner

logger = logging.getLogger(__name__)

SCHEDULES_FILE = PROJECT_ROOT / "schedules.yaml"


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

        runner = get_runner()
        try:
            for task_name in s["tasks"]:
                if stop_flag["stop"]:
                    logger.info(f"[{name}] stop_when 命中，跳过剩余任务")
                    break
                runner.run_task(task_name, stop_checker=stop_checker)
        finally:
            runner.flush_notifications()

        logger.info(f"===== 定时任务结束: {name} =====")

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

        # 只 flush 已经存在的 TaskRunner，不要主动触发初始化
        from core import task_runner
        if task_runner._runner is not None:
            try:
                task_runner._runner.tasker.post_stop()   # 停止正在跑的任务
            except Exception as e:
                logger.warning(f"停止 Tasker 失败: {e}")
            try:
                task_runner._runner.flush_notifications()
                task_runner._runner.notifier.shutdown()
            except Exception as e:
                logger.warning(f"关闭时 flush 通知失败: {e}")