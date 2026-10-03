import hmac
import hashlib
import base64
import urllib.parse
import time
import json
import queue
import threading
import logging
import requests
from datetime import datetime

logger = logging.getLogger(__name__)


class DingTalkNotifier:
    """
    钉钉发送器。
    - 异步：send() 只入队，不阻塞任务
    - 限流：默认每次发送间隔 3 秒，避开钉钉 20 条/分钟限制
    - 兜底：网络异常不会影响任务执行
    """

    def __init__(self, webhook: str, secret: str = "", min_interval: float = 3.0):
        self.webhook = webhook
        self.secret = secret
        self.min_interval = min_interval
        self._queue = queue.Queue()
        self._stop = threading.Event()

        self._pending = 0
        self._pending_lock = threading.Lock()

        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    # ---------- 内部：签名与发送 ----------

    def _sign(self):
        timestamp = str(round(time.time() * 1000))
        secret_enc = self.secret.encode('utf-8')
        string_to_sign = f'{timestamp}\n{self.secret}'
        hmac_code = hmac.new(secret_enc, string_to_sign.encode('utf-8'),
                             digestmod=hashlib.sha256).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        return timestamp, sign

    def _send_raw(self, title: str, content: str) -> bool:
        url = self.webhook
        if self.secret:
            ts, sign = self._sign()
            url += f"&timestamp={ts}&sign={sign}"

        payload = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": content},
        }

        try:
            resp = requests.post(url, json=payload, timeout=15)
            ok = resp.json().get("errcode") == 0
            if not ok:
                logger.error(f"钉钉返回错误: {resp.text}")
            return ok
        except Exception as e:
            logger.error(f"钉钉请求异常: {e}")
            return False

        try:
            data = resp.json()
        except ValueError:
            logger.error(f"[钉钉] 返回非 JSON: {resp.status_code} {resp.text[:200]}")
            return False

        ok = data.get("errcode") == 0
        if ok:
            logger.info(f"[钉钉] 发送成功: {title}")
        else:
            logger.error(f"[钉钉] 返回错误: {data}")
        return ok
    # ---------- 内部：后台消费 ----------

    def _worker(self):
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=1)
            except queue.Empty:
                continue

            # 退出信号：让 flush 能感知（pending 减掉自己）
            if item is None:
                with self._pending_lock:
                    self._pending -= 1
                break

            title, content = item
            try:
                self._send_raw(title, content)
            finally:
                # ★ 无论成功失败都减计数
                with self._pending_lock:
                    self._pending -= 1

            # 限流：两次发送之间等
            if not self._stop.is_set():
                time.sleep(self.min_interval)


    # ---------- 对外：发送接口 ----------

    def send(self, title: str, content: str):
        """异步发送：入队即返回"""
        with self._pending_lock:
            self._pending += 1
        self._queue.put((title, content))

    def send_sync(self, title: str, content: str) -> bool:
        """同步发送：阻塞直到发送完成"""
        return self._send_raw(title, content)

    def flush(self, timeout: float = 120.0) -> bool:
        """
        ★ 修正：等到所有「已入队」的消息都发送完成。
        判断依据是 _pending == 0（不只是队列空），
        避免 worker 正在发送时误判"已完成"。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._pending_lock:
                if self._pending == 0:
                    return True
            time.sleep(0.2)

        with self._pending_lock:
            logger.warning(f"[钉钉] flush 超时，仍有 {self._pending} 条未发完")
        return False

    def shutdown(self, timeout: float = 120.0):
        """等队列发完，然后停止后台线程"""
        self.flush(timeout=timeout)
        self._stop.set()
        with self._pending_lock:
            self._pending += 1  # 给退出信号占位
        self._queue.put(None)
        self._thread.join(timeout=10)