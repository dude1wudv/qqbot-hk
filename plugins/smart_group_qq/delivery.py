"""Local command receipts share the final-reply delivery lifecycle."""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import re

from .formatter import format_for_qq, split_message
from .response import ReplyRequest


def send_failure_state(result):
    """Hermes retryable includes network errors; it does not prove non-delivery."""
    explicit = getattr(result, "delivery_status", None)
    if explicit in {"failed", "unknown", "cancelled"}:
        return explicit
    error = str(getattr(result, "error", "") or "")
    if error == "Not connected":
        return "failed"
    rejected = re.match(r"QQ Bot API error \[(4\d\d)\]", error)
    if rejected and int(rejected.group(1)) not in {408, 409}:
        return "failed" if getattr(result, "retryable", False) else "cancelled"
    return "unknown"


class LocalReplies:
    def __init__(self, store, registry):
        self.store, self.registry = store, registry
        self.tasks = {}

    def cancel_scope(self, scope):
        for (owner, _), task in list(self.tasks.items()):
            if owner == scope and not task.done():
                task.cancel()

    def replay(self, scope, message_id, adapter, transport_chat_id):
        receipt = self.store.get_command_receipt(scope, message_id)
        if not receipt or receipt["epoch"] != self.store.memory_epoch(scope):
            return False
        if receipt["delivery_status"] not in {"pending", "failed"}:
            return False
        return self.schedule(scope, message_id, adapter, transport_chat_id,
                             text=receipt["result"], prepared=receipt.get("prepared", False))

    def schedule(self, scope, message_id, adapter, transport_chat_id, *, text=None, producer=None, claim=None, prepared=False):
        key = (scope, message_id)
        if key in self.tasks and not self.tasks[key].done():
            if inspect.iscoroutine(producer):
                producer.close()
            return True
        epoch = self.store.memory_epoch(scope)
        record = ReplyRequest(
            request_ref=hashlib.sha256((scope + "\0command\0" + message_id).encode()).hexdigest()[:32],
            group_id=scope, member_ref="", message_id=message_id, epoch=epoch,
            source_kind="command", merged_ids=(message_id,), question="", direct=True,
            transport_chat_id=transport_chat_id, record_on_success=False,
            progress_eligible=producer is not None,
        )

        def save_delivery(current, state):
            self.store.update_command_receipt(
                scope, message_id, delivery_status=state,
                sent_chunks=list(range(len(current.sent_chunks))), expected_epoch=epoch,
            )

        record.delivery_callback = save_delivery
        if not self.registry.register(record, official=True):
            if inspect.iscoroutine(producer):
                producer.close()
            return False

        def prepare(body):
            body = str(body) if prepared else format_for_qq(str(body))
            receipt = self.store.save_command_receipt(scope, message_id, body, epoch=epoch, prepared=True)
            if receipt is None or receipt["delivery_status"] not in {"pending", "failed"}:
                return False
            if receipt["delivery_status"] == "pending" and not receipt["sent_chunks"]:
                self.store.replace_pending_command_result(scope, message_id, body, expected_epoch=epoch, prepared=True)
                receipt = self.store.get_command_receipt(scope, message_id)
            body = receipt["result"]
            chunks = split_message(body)
            delivered = receipt["sent_chunks"]
            if delivered != list(range(len(delivered))) or len(delivered) > len(chunks):
                self.registry.note_delivery(record, "unknown")
                return False
            record.sent_chunks = chunks[:len(delivered)]
            record.pending_message = body
            self.registry.finish_progress(record)
            if claim:
                self.store.finish_claim(*claim, success=True)
            self.store.finish_claim("qqbot", message_id, "interaction:dispatch", success=True)
            return True

        if producer is None and not prepare(text):
            return False

        async def run():
            self.registry.begin_execution(record)
            try:
                if producer is not None:
                    self.registry.start_progress(
                        record,
                        lambda: adapter.send(transport_chat_id, "还在处理，完成后会直接回复。", reply_to=message_id),
                    )
                    try:
                        body = await producer
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        body = "处理失败，请稍后重试。"
                    if not self.registry.send_allowed(record) or not prepare(body):
                        return
                await adapter.send(transport_chat_id, record.pending_message, reply_to=message_id)
            finally:
                self.registry.finish_execution(record)
                if self.tasks.get(key) is asyncio.current_task():
                    self.tasks.pop(key, None)

        task = asyncio.create_task(run(), name="qq-local-reply")
        task.add_done_callback(lambda done: done.cancelled() or done.exception())
        self.tasks[key] = task
        return True
