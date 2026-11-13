"""Provider 配置重建、在途租约与关闭所有权测试。"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import Mock, AsyncMock, patch

from fxbot.config import ConfigManager
from fxbot.chat.providers.base import ChatProvider
from fxbot.chat.providers.manager import ProviderManager
from fxbot.chat.providers.entities import LLMResponse


class ProviderOwnershipTests(unittest.IsolatedAsyncioTestCase):
    """只使用可控实现，不创建 SDK 连接或真实文件资源。"""

    async def asyncSetUp(self) -> None:
        self.values = {
            "chat": {
                "provider": "main",
                "providers": {
                    "main": {"type": "test", "provider_type": "chat", "model": "old"},
                },
            }
        }
        self.configuration = Mock(spec=ConfigManager)
        self.configuration.get_system.side_effect = lambda: self.values
        self.manager = ProviderManager(self.configuration)
        self.expected_close_error = None

        class FakeProvider(ChatProvider):
            """实例和关闭计数属于当前测试。"""

            def __init__(self, provider_id, config):
                super().__init__(provider_id, config)
                self.close = AsyncMock()

            async def chat(self, *args, **kwargs):
                return LLMResponse(content="test")

        self.enterContext(patch("fxbot.chat.providers.manager.get_provider_class", return_value=FakeProvider))
        self.logger = self.enterContext(patch("fxbot.chat.providers.manager.logger"))

    async def asyncTearDown(self) -> None:
        if self.expected_close_error is None:
            await asyncio.wait_for(self.manager.aclose(), 1)
        else:
            with self.assertRaises(self.expected_close_error):
                await asyncio.wait_for(self.manager.aclose(), 1)

    async def test_hot_reload_keeps_old_instance_until_its_last_lease(self) -> None:
        async with self.manager.lease("main") as old:
            self.values["chat"]["providers"]["main"]["model"] = "new"
            async with self.manager.lease("main") as new:
                self.assertIsNot(old, new)
                self.assertEqual(old.config["model"], "old")
                self.assertEqual(new.config["model"], "new")
                old.close.assert_not_awaited()
            old.close.assert_not_awaited()
        await self.manager.aclose()
        old.close.assert_awaited_once()
        new.close.assert_awaited_once()

    async def test_reset_does_not_close_in_use_provider(self) -> None:
        async with self.manager.lease("main") as provider:
            await self.manager.reset()
            provider.close.assert_not_awaited()
        await self.manager.aclose()
        provider.close.assert_awaited_once()

    async def test_shared_manager_reopens_only_after_explicit_application_start(self) -> None:
        old = await self.manager.get_provider("main")
        await self.manager.aclose()
        with self.assertRaises(RuntimeError):
            await self.manager.get_provider("main")
        await self.manager.start()
        current = await self.manager.get_provider("main")
        self.assertIsNot(old, current)
        old.close.assert_awaited_once()
        current.close.assert_not_awaited()

    async def test_shutdown_waits_for_active_lease_and_rejects_new_requests(self) -> None:
        async with self.manager.lease("main") as provider:
            closing = asyncio.create_task(self.manager.aclose())
            await asyncio.sleep(0)
            self.assertFalse(closing.done())
            with self.assertRaises(RuntimeError):
                await self.manager.get_provider("main")
            provider.close.assert_not_awaited()
        await asyncio.wait_for(closing, 1)
        provider.close.assert_awaited_once()

    async def test_cancelled_waiter_does_not_cancel_sdk_close(self) -> None:
        provider = await self.manager.get_provider("main")
        entered, release = asyncio.Event(), asyncio.Event()

        async def close() -> None:
            entered.set()
            await release.wait()

        provider.close.side_effect = close
        first = asyncio.create_task(self.manager.aclose())
        await asyncio.wait_for(entered.wait(), 1)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        release.set()
        await asyncio.wait_for(self.manager.aclose(), 1)
        provider.close.assert_awaited_once()

    async def test_failed_close_is_reported_and_keeps_resource_ownership(self) -> None:
        provider = await self.manager.get_provider("main")
        provider.close.side_effect = OSError("close failed")
        self.expected_close_error = OSError
        with self.assertRaises(OSError):
            await self.manager.reset()
        self.assertIs(self.manager._leases[id(provider)].provider, provider)
        self.assertEqual(len(self.manager._closing), 1)
        self.logger.opt.return_value.error.assert_called_once()
        provider.close.assert_awaited_once()

    async def test_all_closes_finish_before_first_failure_is_reported(self) -> None:
        self.values["chat"]["providers"]["other"] = {"type": "test", "provider_type": "chat", "model": "other"}
        failed = await self.manager.get_provider("main")
        other = await self.manager.get_provider("other")
        failed.close.side_effect = OSError("close failed")
        self.expected_close_error = OSError
        entered, release = asyncio.Event(), asyncio.Event()

        async def close() -> None:
            entered.set()
            await release.wait()

        other.close.side_effect = close
        closing = asyncio.create_task(self.manager.aclose())
        await asyncio.wait_for(entered.wait(), 1)
        self.assertFalse(closing.done())
        release.set()
        with self.assertRaises(OSError):
            await closing
        self.assertNotIn(id(other), self.manager._leases)

    async def test_snapshot_instance_is_also_owned_until_scope_exits(self) -> None:
        snapshot = {"type": "test", "provider_type": "chat", "model": "snapshot"}
        async with self.manager.lease_snapshot("snapshot", snapshot) as provider:
            closing = asyncio.create_task(self.manager.aclose())
            await asyncio.sleep(0)
            self.assertFalse(closing.done())
        await asyncio.wait_for(closing, 1)
        provider.close.assert_awaited_once()

    async def test_explicit_missing_name_never_chooses_another_provider(self) -> None:
        with self.assertRaises(ValueError):
            await self.manager.get_provider("missing")
        self.assertEqual(self.manager.list_cached(), {})
