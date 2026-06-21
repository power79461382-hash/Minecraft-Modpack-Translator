import threading
import time
from typing import Optional


class AdaptiveConcurrency:
    """根據近期限流狀態動態調整有效並發數。

    這個類別不直接管理 ThreadPoolExecutor，而是提供目前建議的 workers
    數量；呼叫端可在批次排程時依此限制同時送出的任務。
    """

    def __init__(self, initial: int, minimum: int = 1,
                 maximum: Optional[int] = None) -> None:
        initial = max(1, int(initial or 1))
        self._minimum = max(1, int(minimum or 1))
        self._maximum = max(self._minimum, int(maximum or initial))
        self._current = max(self._minimum, min(initial, self._maximum))
        self._success_streak = 0
        self._cooldown_until = 0.0
        self._lock = threading.Lock()

    def effective_workers(self) -> int:
        """回傳目前建議的有效並發數。"""
        with self._lock:
            return max(self._minimum, min(self._current, self._maximum))

    def on_success(self) -> int:
        """記錄一次成功；連續成功後逐步回升並發。"""
        with self._lock:
            self._success_streak += 1
            if (time.time() >= self._cooldown_until
                    and self._success_streak >= max(4, self._current * 2)
                    and self._current < self._maximum):
                self._current += 1
                self._success_streak = 0
            return self._current

    def on_rate_limit(self, retry_after: float = 30.0) -> int:
        """記錄一次 429/限流；立即降低並發並設定冷卻期。"""
        with self._lock:
            self._success_streak = 0
            self._current = max(self._minimum, max(1, self._current // 2))
            self._cooldown_until = max(
                self._cooldown_until,
                time.time() + max(1.0, float(retry_after or 30.0)),
            )
            return self._current

    def on_error(self) -> int:
        """記錄一般錯誤；保守地清掉成功累積但不立即降速。"""
        with self._lock:
            self._success_streak = 0
            return self._current
