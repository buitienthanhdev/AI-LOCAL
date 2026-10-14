"""Transaction manager: one question-answer exchange at a time, recorded, never repeated.

The lock covers the whole exchange (new conversation -> submit -> wait -> read), not single browser
operations, so two requests can never interleave their prompts or read each other's answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator

from .contract import ErrorClass, TransactionResult, TransactionStatus

DEFAULT_HISTORY = 200


def prompt_digest(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


class TransactionManager:
    def __init__(self, history: int = DEFAULT_HISTORY) -> None:
        self._lock = asyncio.Lock()
        self._records: OrderedDict[str, tuple[str, TransactionResult]] = OrderedDict()
        self._history = history

    def new_id(self) -> str:
        return uuid.uuid4().hex

    def recorded(self, transaction_id: str, prompt: str) -> TransactionResult | None:
        """The earlier outcome of this transaction id, so a repeated call never sends a second prompt.

        A repeated id with a different prompt is a caller bug and is refused as DUPLICATE.
        """
        entry = self._records.get(transaction_id)
        if entry is None:
            return None
        digest, result = entry
        if digest != prompt_digest(prompt):
            return TransactionResult(
                transaction_id=transaction_id,
                status=TransactionStatus.NOT_SUBMITTED,
                error_class=ErrorClass.DUPLICATE,
                error_message="transaction id was already used with a different prompt",
            )
        return result

    def get(self, transaction_id: str) -> TransactionResult | None:
        entry = self._records.get(transaction_id)
        return entry[1] if entry else None

    def busy(self) -> bool:
        return self._lock.locked()

    @contextlib.asynccontextmanager
    async def exclusive(self, wait_s: float) -> AsyncIterator[bool]:
        """Yield True when the exchange lock was taken within `wait_s`, else False (caller reports BUSY)."""
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=wait_s)
        except TimeoutError:
            yield False
            return
        try:
            yield True
        finally:
            self._lock.release()

    def record(self, prompt: str, result: TransactionResult) -> None:
        """Remember the outcome. NOT_SUBMITTED results are not kept: the same id may legitimately retry."""
        if result.status is TransactionStatus.NOT_SUBMITTED:
            return
        self._records[result.transaction_id] = (prompt_digest(prompt), result)
        while len(self._records) > self._history:
            self._records.popitem(last=False)

    def update(self, result: TransactionResult) -> None:
        """Replace the stored outcome of a known transaction (its prompt digest is kept)."""
        entry = self._records.get(result.transaction_id)
        if entry is None:
            return
        if result.status is TransactionStatus.NOT_SUBMITTED:
            del self._records[result.transaction_id]
        else:
            self._records[result.transaction_id] = (entry[0], result)

    def forget(self, transaction_id: str) -> None:
        self._records.pop(transaction_id, None)

    @staticmethod
    def now() -> float:
        return time.time()
