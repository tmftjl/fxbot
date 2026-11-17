"""核心浏览器渲染服务的惰性启动、复用与回收。"""

from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock, patch

from fxbot.runtime import browser


class FakeElement:
    def __init__(self) -> None:
        self.closed = False

    async def screenshot(self, type: str = "png") -> bytes:
        return b"ELEMENT-PNG"


class FakePage:
    def __init__(self) -> None:
        self.closed = False
        self.content = ""

    async def set_content(self, html: str, wait_until: str) -> None:
        self.content = html

    async def query_selector(self, selector: str):
        return FakeElement() if selector else None

    async def screenshot(self, type: str = "png", full_page: bool = True) -> bytes:
        return b"PAGE-PNG"

    async def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self) -> None:
        self.pages: list[FakePage] = []
        self.closed = False
        self.connected = True

    def is_connected(self) -> bool:
        return self.connected

    async def new_page(self, device_scale_factor: float = 1.0) -> FakePage:
        page = FakePage()
        self.pages.append(page)
        return page

    async def close(self) -> None:
        self.closed = True
        self.connected = False


class FakeChromium:
    def __init__(self) -> None:
        self.launches = 0
        self.browsers: list[FakeBrowser] = []

    async def launch(self) -> FakeBrowser:
        self.launches += 1
        instance = FakeBrowser()
        self.browsers.append(instance)
        return instance


class FakePlaywright:
    def __init__(self, chromium: FakeChromium) -> None:
        self.chromium = chromium
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class FakeFactory:
    def __init__(self) -> None:
        self.chromium = FakeChromium()
        self.instances: list[FakePlaywright] = []

    async def start(self) -> FakePlaywright:
        instance = FakePlaywright(self.chromium)
        self.instances.append(instance)
        return instance


def fake_module() -> tuple[SimpleNamespace, FakeFactory]:
    factory = FakeFactory()
    module = SimpleNamespace(async_playwright=lambda: factory)
    return module, factory


class BrowserRendererTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_is_lazy_reused_and_closed_by_application(self) -> None:
        module, factory = fake_module()
        renderer = browser.BrowserRenderer()
        with patch.dict(sys.modules, {"playwright": module, "playwright.async_api": module}):
            first = await renderer.render("<html>one</html>")
            second = await renderer.render("<html>two</html>")
            await renderer.close()
        self.assertEqual(first, b"PAGE-PNG")
        self.assertEqual(second, b"PAGE-PNG")
        self.assertEqual(factory.chromium.launches, 1)
        instance = factory.chromium.browsers[0]
        self.assertTrue(instance.closed)
        self.assertTrue(all(page.closed for page in instance.pages))
        self.assertTrue(factory.instances[0].stopped)

    async def test_disconnected_browser_is_relaunched(self) -> None:
        module, factory = fake_module()
        renderer = browser.BrowserRenderer()
        with patch.dict(sys.modules, {"playwright": module, "playwright.async_api": module}):
            await renderer.render("<html>one</html>")
            factory.chromium.browsers[0].connected = False
            await renderer.render("<html>two</html>")
            await renderer.close()
        self.assertEqual(factory.chromium.launches, 2)

    async def test_selector_screenshot_uses_matching_element(self) -> None:
        module, _factory = fake_module()
        renderer = browser.BrowserRenderer()
        with patch.dict(sys.modules, {"playwright": module, "playwright.async_api": module}):
            data = await renderer.render("<html></html>", selector=".container")
            await renderer.close()
        self.assertEqual(data, b"ELEMENT-PNG")

    async def test_missing_playwright_raises_unavailable(self) -> None:
        renderer = browser.BrowserRenderer()
        with patch.dict(sys.modules, {"playwright": None, "playwright.async_api": None}):
            with self.assertRaises(browser.BrowserUnavailable):
                await renderer.render("<html></html>")


class ChromiumInstallTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_chromium_skips_install(self) -> None:
        with (
            patch.object(browser, "_chromium_installed", return_value=True),
            patch.object(browser.asyncio, "create_subprocess_exec", AsyncMock()) as spawn,
        ):
            self.assertTrue(await browser.ensure_chromium())
        spawn.assert_not_awaited()

    async def test_missing_chromium_runs_playwright_install(self) -> None:
        process = Mock(returncode=0)
        process.communicate = AsyncMock(return_value=(b"installed", None))
        with (
            patch.object(browser, "_chromium_installed", return_value=False),
            patch.object(
                browser.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
            ) as spawn,
        ):
            self.assertTrue(await browser.ensure_chromium())
        spawn.assert_awaited_once()
        args = spawn.await_args.args
        self.assertEqual(args[1:5], ("-m", "playwright", "install", "chromium"))

    async def test_failed_install_is_reported_without_raising(self) -> None:
        process = Mock(returncode=1)
        process.communicate = AsyncMock(return_value=(b"download failed", None))
        with (
            patch.object(browser, "_chromium_installed", return_value=False),
            patch.object(browser.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)),
        ):
            self.assertFalse(await browser.ensure_chromium())


if __name__ == "__main__":
    unittest.main()
