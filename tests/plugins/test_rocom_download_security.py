"""资源索引只在临时资源根目录内落盘，不访问真实资源站。"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from fxbot.plugins.rocom import resource_downloader as downloader


class ResourceDownloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "resources"

    async def download(self, handler, *, follow_redirects=False):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=follow_redirects
        ) as client:
            return await downloader._download_endpoint(client, "https://resources.test", "icons", self.root, 2)

    async def test_rejects_encoded_traversal_and_absolute_links_before_downloading_files(self):
        for href in (
            "../outside.png",
            "%2e%2e%2foutside.png",
            "%2e%2e/",
            "..%5coutside.png",
            "/absolute.png",
            "https://evil.test/a.png",
            "//evil.test/a.png",
            "C%3A%5Coutside.png",
            "nested/child.png",
            "NUL.png",
            "name.png.",
        ):
            calls = []

            def handler(request):
                calls.append(request)
                return httpx.Response(200, text=f'<a href="safe.png">safe</a><a href="{href}">bad</a>')

            with self.subTest(href=href), self.assertRaises(ValueError):
                await self.download(handler)
            self.assertEqual(len(calls), 1)
            self.assertFalse(any(self.root.rglob("*.png")))
            self.assertFalse((self.root.parent / "outside.png").exists())

    async def test_valid_direct_children_are_downloaded_atomically_with_one_shared_boundary(self):
        calls = []

        def handler(request):
            calls.append(request)
            if request.url.path.endswith("/icons/"):
                return httpx.Response(
                    200,
                    text=(
                        '<a href="../">parent</a><a href="sub/">dir</a>'
                        '<a href="image%20one.png">file</a><a href="image%20one.png">duplicate</a>'
                    ),
                )
            if request.url.path.endswith("/sub/"):
                return httpx.Response(200, text='<a href="../">parent</a><a href="two.png">file</a>')
            return httpx.Response(200, content=b"image")

        self.assertEqual(await self.download(handler), 2)
        self.assertEqual((self.root / "image one.png").read_bytes(), b"image")
        self.assertEqual((self.root / "sub/two.png").read_bytes(), b"image")
        self.assertFalse(list(self.root.rglob(".download-*")))
        self.assertEqual(len(calls), 4)

    async def test_redirects_cannot_leave_the_selected_origin(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(302, headers={"location": "https://evil.test/index"})

        with self.assertRaises(httpx.HTTPStatusError):
            await self.download(handler, follow_redirects=True)
        self.assertEqual(len(calls), 1)

    async def test_failure_cancels_and_awaits_other_downloads(self):
        entered, cleaned = asyncio.Event(), asyncio.Event()

        async def handler(request):
            if request.url.path.endswith("/icons/"):
                return httpx.Response(200, text='<a href="fail.png">fail</a><a href="wait.png">wait</a>')
            if request.url.path.endswith("fail.png"):
                await entered.wait()
                raise httpx.ReadError("failed", request=request)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        with self.assertRaises(httpx.ReadError):
            await self.download(handler)
        self.assertTrue(cleaned.is_set())
        self.assertFalse(list(self.root.rglob(".download-*")))

    async def test_entry_budget_fails_before_creating_file_download_tasks(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, text='<a href="one.png">one</a><a href="two.png">two</a>')

        with patch.object(downloader, "_MAX_ENTRIES", 1), self.assertRaises(ValueError):
            await self.download(handler)
        self.assertEqual(len(calls), 1)

    async def test_symlink_child_cannot_redirect_resource_writes(self):
        outside = self.root.parent / "outside"
        outside.mkdir()
        self.root.mkdir()
        try:
            (self.root / "sub").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("当前环境不能创建目录符号链接")
        with self.assertRaises(ValueError):
            await self.download(lambda request: httpx.Response(200, text='<a href="sub/">dir</a>'))
        self.assertFalse(list(outside.iterdir()))

    async def test_mirror_selection_awaits_losing_probes(self):
        entered, cleaned = asyncio.Event(), asyncio.Event()

        async def probe(client, tag, url):
            if tag == "fast":
                await entered.wait()
                return tag, url
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        with (
            patch.object(downloader, "_probe", probe),
            patch.object(downloader, "DEFAULT_URLS", [("fast", "https://fast.test"), ("slow", "https://slow.test")]),
            patch.object(downloader, "get_shared_async_client", AsyncMock()),
        ):
            self.assertEqual(await downloader._choose_base_url(""), ("fast", "https://fast.test"))
        self.assertTrue(cleaned.is_set())
