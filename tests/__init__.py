"""FxBot 标准库测试包。"""

from __future__ import annotations

import sys
from pathlib import Path

# 源码根目录本身就是 fxbot 包，测试入口需能从其父目录发现它。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
