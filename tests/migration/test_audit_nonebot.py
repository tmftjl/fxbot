"""依赖审计必须识别别名、动态线索和控制流，并保持纯静态。"""

from __future__ import annotations

import unittest

from scripts.audit_nonebot import inspect_source, is_nonebot_module


class AuditTests(unittest.TestCase):
    """验证语法候选提取，不依赖被审计模块的安装或初始化。"""

    def test_import_aliases_and_relative_imports(self) -> None:
        source = (
            "from nonebot import on_regex as register, logger\n"
            "from .nonebot_plugin_local import helper\n"
            "matcher = register('hello', priority=5, block=True)\n"
        )
        report = inspect_source(source, "example.py")
        self.assertEqual(len(report["imports"]), 2)
        self.assertFalse(report["logger_only_imports"])
        self.assertEqual(report["calls"][0]["operation"], "on_regex")
        self.assertEqual(report["calls"][0]["keywords"], {"priority": "5", "block": "True"})

    def test_logger_only_is_not_a_claim_of_transitive_independence(self) -> None:
        report = inspect_source("from nonebot.log import logger as log\nfrom . import service\n", "example.py")
        self.assertTrue(report["logger_only_imports"])

    def test_hook_decorators_and_function_registration(self) -> None:
        source = (
            "from nonebot.message import event_preprocessor as pre\n"
            "@pre\nasync def handler(): pass\n"
            "@get_driver().on_startup\nasync def startup(): pass\n"
            "get_driver().on_shutdown(handler)\n"
        )
        report = inspect_source(source, "example.py")
        self.assertEqual(
            [item["operation"] for item in report["hooks"]],
            ["event_preprocessor", "on_startup", "on_shutdown"],
        )

    def test_wrapper_dynamic_strings_and_control_candidates(self) -> None:
        source = (
            "matcher = P.on_notice(name='welcome', block=False)\n"
            "module = 'nonebot.adapters.wxauto:Bot'\n"
            "await matcher.finish()\n"
        )
        report = inspect_source(source, "example.py")
        self.assertEqual([item["kind"] for item in report["calls"]], ["registration", "control"])
        self.assertEqual(report["strings"][0]["value"], "nonebot.adapters.wxauto:Bot")

    def test_parsing_does_not_execute_source_and_does_not_hide_syntax_errors(self) -> None:
        self.assertEqual(inspect_source("raise RuntimeError('must not execute')", "example.py")["imports"], [])
        with self.assertRaises(SyntaxError):
            inspect_source("def broken(", "example.py")

    def test_module_boundaries(self) -> None:
        self.assertTrue(is_nonebot_module("nonebot_plugin_apscheduler"))
        self.assertFalse(is_nonebot_module("fxbot.fake"))
        self.assertFalse(is_nonebot_module("not_nonebot"))

    def test_business_hook_on_is_not_a_matcher_factory(self) -> None:
        report = inspect_source("extension.on('observer', callback)\n", "example.py")
        self.assertEqual(report["calls"], [])


if __name__ == "__main__":
    unittest.main()
