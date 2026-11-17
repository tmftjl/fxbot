"""核心共享的 Playwright 浏览器渲染接口，插件不再各自启动 Chromium。"""

from __future__ import annotations

import sys
import asyncio
import subprocess

from loguru import logger

from ..core.application import Application

_INSTALL_TIMEOUT_SECONDS = 900
_install_lock = asyncio.Lock()


class BrowserRenderError(RuntimeError):
    """页面渲染失败；调用方可据此回退到 PIL。"""


class BrowserUnavailable(BrowserRenderError):
    """Playwright 或 Chromium 不可用。"""


class BrowserRenderer:
    """惰性启动单个 Chromium 实例，按页面并发并统一回收。"""

    def __init__(self, concurrency: int = 2) -> None:
        self._semaphore = asyncio.Semaphore(max(int(concurrency), 1))
        self._lock = asyncio.Lock()
        self._playwright = None
        self._browser = None

    async def _close_locked(self) -> None:
        browser, self._browser = self._browser, None
        playwright, self._playwright = self._playwright, None
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                logger.opt(exception=True).debug("[Browser] 关闭浏览器失败")
        if playwright is not None:
            try:
                await playwright.stop()
            except Exception:
                logger.opt(exception=True).debug("[Browser] 关闭 Playwright 失败")

    async def _ensure_browser(self):
        async with self._lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            await self._close_locked()
            try:
                from playwright.async_api import async_playwright
            except Exception as exc:
                raise BrowserUnavailable("Playwright 未安装") from exc
            try:
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch()
            except Exception as exc:
                await self._close_locked()
                raise BrowserUnavailable("Chromium 启动失败，请执行 playwright install chromium") from exc
            return self._browser

    async def close(self) -> None:
        """应用关闭时回收浏览器和 Playwright。"""
        async with self._lock:
            await self._close_locked()

    async def render(
        self,
        html: str,
        *,
        selector: str | None = None,
        scale: float = 1.0,
        load_timeout: float = 15.0,
        screenshot_timeout: float = 15.0,
        full_page: bool = True,
    ) -> bytes:
        """渲染 HTML 并返回 PNG；selector 命中时只截该元素。"""
        async with self._semaphore:
            browser = await self._ensure_browser()
            try:
                page = await browser.new_page(device_scale_factor=max(float(scale or 1.0), 0.1))
            except Exception as exc:
                raise BrowserRenderError("创建页面失败") from exc
            try:
                await asyncio.wait_for(page.set_content(html, wait_until="load"), timeout=load_timeout)
                if selector:
                    element = await page.query_selector(selector)
                    if element is not None:
                        return await asyncio.wait_for(
                            element.screenshot(type="png"), timeout=screenshot_timeout
                        )
                return await asyncio.wait_for(
                    page.screenshot(type="png", full_page=full_page), timeout=screenshot_timeout
                )
            except BrowserRenderError:
                raise
            except Exception as exc:
                if self._browser is not None and not self._browser.is_connected():
                    async with self._lock:
                        await self._close_locked()
                raise BrowserRenderError("HTML 渲染失败") from exc
            finally:
                try:
                    await page.close()
                except Exception:
                    logger.opt(exception=True).debug("[Browser] 关闭页面失败")


browser_renderer = BrowserRenderer()


def _chromium_installed() -> bool:
    """通过 Playwright 的安装清单判断 Chromium 是否已就绪。"""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "--list"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    output = result.stdout
    return "chromium-" in output or "chromium_headless_shell-" in output


async def ensure_chromium() -> bool:
    """缺失时自动安装 Chromium；并发调用只执行一次。"""
    async with _install_lock:
        if await asyncio.to_thread(_chromium_installed):
            return True
        logger.info("[Browser] 未检测到 Chromium，开始自动安装")
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "playwright",
                "install",
                "chromium",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError:
            logger.opt(exception=True).warning("[Browser] 无法启动 Chromium 安装进程")
            return False
        try:
            output, _ = await asyncio.wait_for(
                process.communicate(), timeout=_INSTALL_TIMEOUT_SECONDS
            )
        except TimeoutError:
            process.kill()
            await process.communicate()
            logger.warning("[Browser] Chromium 自动安装超时")
            return False
        if process.returncode != 0:
            detail = (output or b"").decode("utf-8", errors="replace").strip()[-800:]
            logger.warning(f"[Browser] Chromium 自动安装失败: {detail}")
            return False
        logger.info("[Browser] Chromium 自动安装完成")
        return True


async def _setup_browser() -> None:
    """启动后后台检测并安装浏览器，不阻塞服务就绪。"""
    try:
        await ensure_chromium()
    except Exception:
        logger.opt(exception=True).warning("[Browser] 自动安装浏览器任务失败")


async def render_html(html: str, **options) -> bytes:
    """插件使用的统一渲染入口。"""
    return await browser_renderer.render(html, **options)


async def _close_browser() -> None:
    await browser_renderer.close()


def install(application: Application) -> None:
    """启动后台安装检查，并把关闭逻辑交给应用资源栈。"""

    async def start() -> None:
        application.spawn(_setup_browser(), name="browser-setup")

    application.resource(start, _close_browser)
