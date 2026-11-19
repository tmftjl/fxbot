"""QQ 消息审核结果的进程内等待与投递。"""

import asyncio


class AuditResultStore:
    def __init__(self):
        self._futures = {}

    def add_result(self, event):
        audit_id = event.metadata["audit_id"]
        if not audit_id:
            raise ValueError("audit_id cannot be empty")
        future = self._futures.get(audit_id)
        if future is not None and not future.done():
            future.set_result(event)

    async def fetch(self, audit_id, timeout=None):
        future = asyncio.get_running_loop().create_future()
        self._futures[audit_id] = future
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            del self._futures[audit_id]


audit_result = AuditResultStore()
