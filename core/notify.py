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

    # ---------- 内部：后台消费 ----------

    def _worker(self):
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=1)
            except queue.Empty:
                continue
            if item is None:
                break
            title, content = item
            self._send_raw(title, content)
            time.sleep(self.min_interval)  # 限流

    # ---------- 对外：发送接口 ----------

    def send(self, title: str, content: str):
        """异步发送：入队即返回，不阻塞调用方"""
        self._queue.put((title, content))

    def send_sync(self, title: str, content: str) -> bool:
        """同步发送：阻塞直到发送完成，返回成功与否"""
        return self._send_raw(title, content)

    def flush(self, timeout: float = 30.0):
        """阻塞等待队列清空。程序退出前调用，避免丢消息"""
        deadline = time.time() + timeout
        while not self._queue.empty() and time.time() < deadline:
            time.sleep(0.5)

    def shutdown(self):
        """停止后台线程"""
        self._stop.set()
        self._queue.put(None)
        self._thread.join(timeout=5)