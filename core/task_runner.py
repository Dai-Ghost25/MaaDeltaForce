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
    """任务级事件：Tasker.Task.Starting / Succeeded / Failed"""

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
            logger.info(f"[任务完成] {details.get('entry')}")
        elif msg == "Tasker.Task.Failed":
            r._finish(success=False)
            logger.warning(f"[任务失败] {details.get('entry')}")


# ---------- 事件监听：节点级 ----------

class _ContextSink(ContextEventSink):
    """节点级事件：Node.Recognition.* / Node.Action.*"""

    def __init__(self, runner):
        super().__init__()
        self.runner = runner

    def _on_raw_notification(self, handle, msg, details):
        r = self.runner
        if r._current is None:
            return

        if msg == "Node.Recognition.Succeeded":
            self._log_reco(handle, details)
            self._handle_recognition(handle, details, r)
        elif msg == "Node.Recognition.Failed":
            logger.debug(f"[识别失败] {details.get('name', '?')}")
        elif msg == "Node.Action.Succeeded":
            self._log_action(details, r)
            self._handle_action(details, r)

    def _log_reco(self, handle, details):
        node_name = details.get("name", "?")
        reco_id = details.get("reco_id")
        context = Context(handle)
        reco = context.tasker.get_recognition_detail(reco_id)
        if not reco or not reco.hit:
            return

        box = getattr(reco, "box", None)
        if box is None:
            best = getattr(reco, "best_result", None)
            box = getattr(best, "box", None) if best else None

        best = getattr(reco, "best_result", None)
        text = getattr(best, "text", "") if best else ""
        score = getattr(best, "score", None) if best else None
        algo = getattr(reco, "algorithm", None)

        if box:
            cx, cy = box[0] + box[2] // 2, box[1] + box[3] // 2
            logger.debug(
                f"[识别] {node_name}: algo={algo} text={text!r} "
                f"box={list(box)} center=({cx},{cy}) score={score}"
            )
        else:
            logger.debug(f"[识别] {node_name}: algo={algo} text={text!r} box=None score={score}")

    def _log_action(self, details, r):
        node_name = details.get("name", "?")
        action_details = details.get("action_details") or {}
        detail = action_details.get("detail") or {}
        point = detail.get("point") if isinstance(detail, dict) else None
        box = action_details.get("box")

        if point:
            logger.debug(f"[动作] {node_name}: point={point} box={box}")
        else:
            logger.debug(f"[动作] {node_name}: action={action_details.get('action')} box={box}")

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

        r._reco_captured_nodes.add(node_name)
        r._captured.append({
            "node": node_name,
            "text": text,
            "time": time.time(),
            "kind": "reco",
        })

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

        if node_name in r._reco_captured_nodes:
            return

        r._captured.append({
            "node": node_name,
            "text": node_name,
            "time": time.time(),
            "kind": "action",
        })

    @staticmethod
    def _should_capture(node_name, r):
        watch = r._current.get("watch_nodes") if r._current else None
        if not watch:
            return True
        keys = watch.keys() if isinstance(watch, dict) else watch
        return node_name in keys

    @staticmethod
    def _extract_text(reco) -> str:
        best = getattr(reco, "best_result", None)
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
        self._init_coords()

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

    def _init_coords(self):
        from core.coords import get_coord_store
        from core.template_resizer import resize_templates

        self._coord_store = get_coord_store(self.device_name)
        # 计算模板缩放比
        TEMPLATE_NATIVE_W = 1152   # 现有模板都是从 S9 的 1152 空间裁的
        gx_b, gy_b, gw_b, gh_b = self._coord_store._game_rect_in_base()
        img_w = round(gw_b)
        scale = img_w / TEMPLATE_NATIVE_W

        if abs(scale - 1.0) < 0.01:
            logger.info(f"[templates] scale={scale:.4f}，使用原模板")
        else:
            src_dir = self.cfg.resource_dir / "image"
            dst_dir = self.cfg.project_root / "debug" / "templates" / f"{self.device_name}_w{img_w}"
            self._coord_store.template_map = resize_templates(src_dir, dst_dir, scale)

        self._coord_override = self._coord_store.build_override(self.cfg.pipeline_dir)
        logger.info(
            f"坐标 override 构建完成: {len(self._coord_override)} 个节点 "
            f"(profile={self._coord_store.profile.name}, img_w={img_w}, "
            f"templates={len(self._coord_store.template_map)})"
        )
    # ----  坐标边距计算 ----

    def _merge_override(self, user_override: dict | None) -> dict:
        merged: dict[str, dict] = {}
        for node, fields in self._coord_override.items():
            merged[node] = dict(fields)
        if user_override:
            for node, fields in user_override.items():
                merged.setdefault(node, {}).update(fields)
        return merged

    # ---- 任务回调（由 sink 触发） ----

    def _finish(self, success: bool):
        self._result = "success" if success else "failed"

    # ---- 执行任务 ----

    def run_task(
        self,
        task_name: str,
        stop_checker=None,
        pipeline_override=None,
        notify: str | None = None,
    ) -> bool:
        task = self.cfg.get_task(task_name)
        entry = task["entry"]
        if notify is None:
            notify = task.get("notify", "none")

        self._current = task
        self._captured = []
        self._result = None
        self._start_time = time.time()
        self._stop_checker = stop_checker
        self._should_stop = False
        self._reco_captured_nodes = set()

        logger.info(f"===== 执行任务: {task_name} (entry={entry}) =====")
        merged = self._merge_override(pipeline_override)
        job = self.tasker.post_task(entry, pipeline_override=merged)
        job.wait()

        elapsed = time.time() - self._start_time

        # 优先用 job.succeeded，不依赖 sink 事件
        success = bool(job.succeeded)
        if not success and self._result == "success":
            # 兜底：如果 job 说失败但 sink 说成功，以 job 为准
            logger.warning(f"[任务] job.succeeded=False 但 _result=success，以 job 为准")

        logger.info(f"===== 任务结束: {task_name} 成功={success} 耗时={elapsed:.2f}s =====")

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

        merged = self._merge_override(pipeline_override)
        job = self.tasker.post_task(entry, pipeline_override=merged)
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
