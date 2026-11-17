"""洛克王国插件自有资源与运行时数据路径。"""

from __future__ import annotations

from ...utils.paths import package_root

PLUGIN_DIR = package_root() / "plugins" / "rocom"
RESOURCE_DIR = PLUGIN_DIR / "resources"
DATA_DIR = RESOURCE_DIR / "data"
FONT_DIR = RESOURCE_DIR / "fonts"
MERCHANT_DIR = RESOURCE_DIR / "merchant"
POKEDEX_DIR = RESOURCE_DIR / "pokedex"
