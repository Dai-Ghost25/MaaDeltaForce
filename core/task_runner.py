# core/task_runner.py
import time
import logging
from datetime import datetime

from maa.tasker import Tasker, TaskerEventSink
from maa.context import Context, ContextEventSink
from maa.resource import Resource
from maa.controller import AdbController

from core.config import get_config
from core.device_config import get_active_device_name, get_device
from core.notify import DingTalkNotifier
from core import formatter

logger = logging.getLogger(__name__)


# ---------- 事件监听：任务级 ----------

class _TaskerSink(TaskerEventSink):
    def __init__(self, runner):
        super().__init__()
        self.runner = runner

    def _on_raw_notification(self, handle, msg, details):
        r = self.runner
        if msg == "Tasker.Task.Starting":
            r._start_time = time.time()
            logger.info(f"[任务开始] {details.get('entry')}")
        elif msg == "Tasker.Task.Succeeded":
            r._finish(success=True)
        elif msg == "Tasker.Task.Failed":
            r._finish(success=False)


# ---------- 事件监听：节点级 ----------

class _ContextSink(ContextEventSink):
    def __init__(self, runner):
        super().__init__()
        self.runner = runner
        # self._node_start = 0    # ← 新增

    def _on_raw_notification(self, handle, msg, details):
        r = self.runner
        if r._current is None:
            return

        # now = time.time()
        # node_name = details.get("name", "?")

        # # 记录每个节点的开始和结束时间
        # if msg.endswith(".Starting"):
        #     self._node_start = now
        #     logger.info(f"[节点开始] {node_name}")
        # elif msg.endswith(".Succeeded") or msg.endswith(".Failed"):
        #     elapsed = now - self._node_start if self._node_start else 0
        #     logger.info(f"[节点结束] {node_name} 耗时 {elapsed:.2f}s ({msg})")

        if msg == "Node.Recognition.Succeeded":
            self._handle_recognition(handle, details, r)
        elif msg == "Node.Action.Succeeded":
            self._handle_action(details, r)

    def _handle_recognition(self, handle, details, r):
        node_name = details.get("name")
        if not self._should_capture(node_name, r):
            return

        reco_id = details.get("reco_id")
        context = Context(handle)
        reco = context.tasker.get_recognition_detail(reco_id)
        if not reco or not reco.hit:
            return

        text = self._extract_text(reco)
        if not text:
            return

        # 记录"这个节点有识别结果"，供动作事件判断
        r._reco_captured_nodes.add(node_name)

        r._captured.append({
            "node": node_name,
            "text": text,
            "time": time.time(),
            "kind": "reco",
        })
        logger.info(f"[捕获-识别] {node_name}: {text}")

        if r._stop_checker is not None:
            try:
                if r._stop_checker(node_name, text):
                    r._should_stop = True
            except Exception as e:
                logger.warning(f"stop_checker 异常: {e}")

    def _handle_action(self, details, r):
        node_name = details.get("name")
        if not self._should_capture(node_name, r):
            return

        # 这个节点已经有识别捕获，跳过动作捕获，避免重复
        if node_name in r._reco_captured_nodes:
            return

        r._captured.append({
            "node": node_name,
            "text": node_name,
            "time": time.time(),
            "kind": "action",
        })
        logger.info(f"[捕获-动作] {node_name}")

    @staticmethod
    def _should_capture(node_name, r):
        watch = r._current.get("watch_nodes")
        if not watch:
            return True
        keys = watch.keys() if isinstance(watch, dict) else watch
        return node_name in keys

    @staticmethod
    def _extract_text(reco) -> str:
        best = reco.best_result
        if best is None:
            return ""
        text = getattr(best, "text", None)
        return str(text) if text else ""
    
# ---------- TaskRunner ----------

class TaskRunner:
    def __init__(self, device_name: str | None = None):
        self.cfg = get_config()
        self.device_name = device_name or get_active_device_name()
        self.device = get_device(self.device_name)

        self.notifier = DingTalkNotifier(
            webhook=self.cfg.webhook,
            secret=self.cfg.secret,
        )

        self._current = None
        self._start_time = 0
        self._captured = []
        self._result = None
        self._stop_checker = None
        self._should_stop = False
        self._reco_captured_nodes = set()

        self._init_controller()
        self._init_resource()
        self._init_tasker()

    # ---- 初始化 ----

    def _init_controller(self):
            adb = self.cfg.adb_path or "adb"
            addr = self.device["adb_address"]
            logger.info(f"连接设备 [{self.device_name}] {addr} (adb={adb})")
            self.controller = AdbController(
                adb_path=adb,
                address=addr,
                screencap_methods=4 | 2 | 1,
            )
            job = self.controller.post_connection().wait()
            if not job.succeeded:
                raise RuntimeError(
                    f"ADB 连接失败: [{self.device_name}] {addr}"
                )
            self.controller.set_screenshot_target_short_side(720)
            logger.info(f"设备 [{self.device_name}] 连接成功")

    def _init_resource(self):
        self.resource = Resource()
        self.resource.post_bundle(str(self.cfg.resource_dir)).wait()
        if not self.resource.loaded:
            raise RuntimeError("资源加载失败")
        logger.info("资源加载完成")

    def _init_tasker(self):
        self.tasker = Tasker()
        self.tasker.bind(self.resource, self.controller)
        self.tasker.add_sink(_TaskerSink(self))
        self.tasker.add_context_sink(_ContextSink(self))

    # ---- 任务回调（由 sink 触发） ----

    def _finish(self, success: bool):
        self._result = "success" if success else "failed"

    # ---- 执行任务 ----

    def run_task(self, task_name: str, stop_checker=None, pipeline_override=None) -> bool:
        task = self.cfg.get_task(task_name)
        entry = task["entry"]
        notify = task.get("notify", "none")

        self._current = task
        self._captured = []
        self._result = None
        self._start_time = time.time()
        self._stop_checker = stop_checker
        self._should_stop = False
        self._reco_captured_nodes = set()

        logger.info(f"===== 执行任务: {task_name} (entry={entry}) =====")
        job = self.tasker.post_task(entry, pipeline_override=pipeline_override)
        job.wait()
        # 如果你之前是 post_task(entry).wait()，现在改成拿 job 引用，
        # 后面想硬停止时可以用 job.cancel()（如果框架支持）
        # 不想改的话，保持原样也行

        elapsed = time.time() - self._start_time
        success = (self._result == "success")
        logger.info(f"===== 任务结束: {task_name} 成功={success} 耗时={elapsed:.2f}s =====")

        # 通知部分不变
        if notify == "simple":
            self._notify_simple(task, success, elapsed)
        elif notify == "report":
            self._notify_report(task, success, elapsed)

        self._current = None
        self._stop_checker = None
        return success

    def run_raw(self, entry: str, pipeline_override=None, watch_nodes=None):
        """
        直接跑一个 Pipeline 入口，不查 config.TASKS。
        用于测试、临时任务。
        """
        self._current = {"watch_nodes": watch_nodes}
        self._captured = []
        self._result = None
        self._start_time = time.time()
        self._stop_checker = None
        self._should_stop = False
        self._reco_captured_nodes = set()

        t0 = time.time()

        job = self.tasker.post_task(entry, pipeline_override=pipeline_override)
        job.wait()

        elapsed = time.time() - t0
        logger.info(f"[run_raw] {entry} 耗时 {elapsed:.2f}s")

        self._current = None
        return self._captured

    # ---- 通知 ----

    def _notify_simple(self, task, success, elapsed):
        status = "成功" if success else "失败"
        title = f"{task['label']} - {status}"

        # 把捕获内容拼成正文，不再显示耗时
        if self._captured:
            # content = "\n".join(f"{r['node']}: {r['text']}" for r in self._captured)
            content = self._format_captured()
        else:
            content = "（无捕获内容）"

        self.notifier.send(title, formatter.simple(title, content))

    def _notify_report(self, task, success, elapsed):
        status = "成功" if success else "失败"
        title = f"{task['label']} - {status}"

        if self._captured:
            content = self._format_captured()
            # content = "\n".join(f"{r['node']}: {r['text']}" for r in self._captured)
        else:
            content = "（无捕获内容）"

        self.notifier.send(title, formatter.simple(title, content))

    @staticmethod
    def _parse_watch_entry(entry):
        """
        解析 watch_nodes 的单个条目，返回 (mode, label, strip_prefix)
        - entry 为 None    → ("auto", None, None)
        - entry 为字符串   → (entry, None, None)
        - entry 为字典     → (mode, label, strip_prefix)
        """
        if entry is None:
            return "auto", None, None
        if isinstance(entry, str):
            return entry, None, None
        if isinstance(entry, dict):
            mode = entry.get("mode", "text")
            label = entry.get("label")
            strip = entry.get("strip_prefix")
            return mode, label, strip
        return "auto", None, None

    def _format_captured(self) -> str:
        if not self._captured:
            return "（无捕获内容）"

        watch = self._current.get("watch_nodes") if self._current else None
        lines = []

        for r in self._captured:
            node, text = r["node"], r["text"]
            entry = watch.get(node) if isinstance(watch, dict) else None
            mode, label, strip = self._parse_watch_entry(entry)

            # 优先用配置的固定值，否则用识别文本
            if isinstance(entry, dict) and "value" in entry:
                display_text = entry["value"]
            else:
                display_text = text
                if strip and display_text.startswith(strip):
                    display_text = display_text[len(strip):].strip()

            if label:
                lines.append(f"- **{label}**: {display_text}")
            elif mode == "node":
                lines.append(f"- {node}")
            elif mode == "text":
                lines.append(f"- {display_text}")
            elif mode == "both":
                lines.append(f"- **{node}**: {display_text}")
            else:
                if node == display_text:
                    lines.append(f"- {display_text}")
                else:
                    lines.append(f"- **{node}**: {display_text}")

        return "\n".join(lines)

    # ---- 生命周期 ----

    def flush_notifications(self):
        """程序退出前调用，确保钉钉消息发完"""
        self.notifier.flush()


# ---------- 多设备单例 ----------

_runners: dict[str, TaskRunner] = {}


def get_runner(device_name: str | None = None) -> TaskRunner:
    """按设备名缓存 TaskRunner。不传则用当前 active device。"""
    key = device_name or get_active_device_name()
    if key not in _runners:
        _runners[key] = TaskRunner(device_name=key)
    return _runners[key]