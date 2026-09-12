from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

from incidentzero.model.errors import TransientModelError

T = TypeVar("T")


class RetryPolicy:
    def __init__(self, max_attempts: int = 3, sleeper: Callable[[float], None] = time.sleep) -> None:
    def __init__(
        self,
        max_attempts: int = 3,
        sleeper: Callable[[float], None] = time.sleep,
        base_delay: float = 0.5,
        max_delay: float = 4.0,
    ) -> None:
        self.max_attempts = max_attempts
        self.sleeper = sleeper
        self.base_delay = base_delay
        self.max_delay = max_delay

    def call_model(self, fn: Callable[[], T]) -> T:
        # TODO(A1): bounded retry with backoff for TransientModelError only.
        # Do not retry PermanentModelError, schema mistakes, or an unsafe tool action.
        return fn()
        """Execute a model call with bounded retries and exponential backoff for transient errors.

        Only TransientModelError (e.g., HTTP 429 or provider timeouts) is retried.
        PermanentModelError, JSON schema errors, or tool execution issues are immediately raised.
        """
        attempts = 0
        while True:
            attempts += 1
            try:
                return fn()
            except TransientModelError:
                if attempts >= self.max_attempts:
                    raise
                delay = min(self.base_delay * (2 ** (attempts - 1)), self.max_delay)
                self.sleeper(delay)

