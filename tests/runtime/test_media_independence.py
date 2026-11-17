"""AI 图片能力不得通过事件适配器加载旧框架。"""

from __future__ import annotations

import sys
import unittest
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fxbot.chat import media


class MediaIndependenceTests(unittest.TestCase):
    """视觉和图片持久化的公共能力必须独立可导入。"""

    def test_media_and_vision_do_not_import_event_adapter_or_sdk(self) -> None:
        root = Path(__file__).resolve().parents[2]
        script = """
import sys
import asyncio
import tempfile
import importlib.abc
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
class RejectLegacy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('fxbot.adapter', 'fxbot.chat.message_adapter')):
            raise AssertionError(fullname)
        if fullname.split('.')[0] in {'openai', 'anthropic', 'google'}:
            raise AssertionError(fullname)
sys.meta_path.insert(0, RejectLegacy())
with tempfile.TemporaryDirectory() as directory, patch('fxbot.utils.paths.project_root', return_value=Path(directory)):
    from PIL import Image
    from fxbot.chat.media import load_images
    import fxbot.chat.vision
    import fxbot.chat.amemorix.image_writeback
    output = BytesIO()
    Image.new('RGB', (4, 4), 'red').save(output, format='PNG')
    images = asyncio.run(load_images([output.getvalue()]))
    assert len(images) == 1
    assert images[0].startswith('data:image/png;base64,')
"""
        result = subprocess.run(
            [sys.executable, "-I", "-X", "utf8", "-c", script, str(root / "src")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unknown_image_format_is_not_reported_as_jpeg(self) -> None:
        with self.assertRaises(ValueError):
            media._image_mime(b"not an image")


class MediaFailureBoundaryTests(unittest.IsolatedAsyncioTestCase):
    """资源输入错误和内部错误不是同一种结果。"""

    async def test_unexpected_internal_error_is_not_silently_skipped(self) -> None:
        error = RuntimeError("implementation bug")
        with patch.object(media, "_load_image", new=AsyncMock(side_effect=error)):
            with self.assertRaises(RuntimeError) as caught:
                await media.load_images([b"image"])
        self.assertIs(caught.exception, error)
