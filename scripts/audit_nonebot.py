"""静态盘点框架依赖、动态导入线索和分发行为，不执行被审计模块。"""

from __future__ import annotations

import ast
import sys
import json
import argparse
import subprocess
from pathlib import Path
from collections import Counter

FACTORIES = frozenset(
    {
        "on",
        "on_message",
        "on_notice",
        "on_request",
        "on_command",
        "on_regex",
        "on_fullmatch",
        "on_startswith",
        "on_endswith",
        "on_keyword",
        "on_type",
        "on_shell_command",
    }
)
HOOKS = frozenset(
    {
        "on_startup",
        "on_shutdown",
        "on_bot_connect",
        "on_bot_disconnect",
        "event_preprocessor",
        "event_postprocessor",
        "run_preprocessor",
        "run_postprocessor",
        "on_called_api",
        "on_calling_api",
    }
)
CONTROLS = frozenset({"finish", "reject", "pause", "got", "receive", "stop_propagation"})


def is_nonebot_module(name: str) -> bool:
    """识别框架、适配器和插件模块，排除本项目包名。"""
    if name == "fxbot" or name.startswith("fxbot."):
        return False
    return name == "nonebot" or name.startswith(("nonebot.", "nonebot_plugin_"))


def dotted_name(node: ast.AST) -> str:
    """提取静态调用名，不执行表达式。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Call):
        return dotted_name(node.func) + "()"
    return ""


def inspect_source(source: str, path: str) -> dict:
    """提取单文件的语法线索，调用候选不等同于运行时注册数量。"""
    tree = ast.parse(source, filename=path)
    imports = []
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and not node.level and is_nonebot_module(node.module or ""):
            for item in node.names:
                imports.append({"line": node.lineno, "module": node.module, "name": item.name})
                aliases[item.asname or item.name] = item.name
        elif isinstance(node, ast.Import):
            for item in node.names:
                if is_nonebot_module(item.name):
                    imports.append({"line": node.lineno, "module": item.name, "name": "*module*"})
                    aliases[item.asname or item.name.split(".")[0]] = item.name

    calls = []
    hooks = set()
    strings = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and is_nonebot_module(node.value):
            strings.append({"line": node.lineno, "value": node.value})
        if isinstance(node, (ast.Name, ast.Attribute)):
            name = dotted_name(node)
            tail = aliases.get(name, name.rsplit(".", 1)[-1])
            if tail in HOOKS:
                hooks.add((node.lineno, name, tail))
        if not isinstance(node, ast.Call):
            continue
        name = dotted_name(node.func)
        tail = aliases.get(name, name.rsplit(".", 1)[-1])
        if tail not in FACTORIES | CONTROLS | {"add_job", "scheduled_job"}:
            continue
        if tail == "on" and name not in {"on", "P.on"} and aliases.get(name) != "on":
            if aliases.get(name.split(".")[0]) != "nonebot":
                continue
        kind = "registration" if tail in FACTORIES else "control" if tail in CONTROLS else "job"
        calls.append(
            {
                "line": node.lineno,
                "call": name,
                "kind": kind,
                "operation": tail,
                "args": [ast.unparse(arg) for arg in node.args] if kind == "registration" else [],
                "keywords": {item.arg: ast.unparse(item.value) for item in node.keywords if item.arg is not None},
            }
        )
    return {
        "path": path,
        "imports": sorted(imports, key=lambda item: (item["line"], item["name"])),
        "logger_only_imports": bool(imports) and all(item["name"] == "logger" for item in imports),
        "strings": sorted(strings, key=lambda item: item["line"]),
        "calls": sorted(calls, key=lambda item: item["line"]),
        "hooks": [{"line": line, "name": name, "operation": tail} for line, name, tail in sorted(hooks)],
    }


def audit_repository(root: Path) -> dict:
    """扫描 Git 已跟踪及未忽略的 Python 文件，并输出分组计数。"""
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    paths = sorted(
        {name for name in result.stdout.decode("utf-8").split("\0") if name.endswith(".py") and (root / name).is_file()}
    )
    files = [inspect_source((root / name).read_text(encoding="utf-8-sig"), name) for name in paths]
    groups = {}
    for group, selected in (
        ("runtime", [item for item in files if not item["path"].startswith(("tests/", "scripts/"))]),
        ("tests", [item for item in files if item["path"].startswith("tests/")]),
        ("scripts", [item for item in files if item["path"].startswith("scripts/")]),
    ):
        coupled = [item for item in selected if item["imports"]]
        groups[group] = {
            "python_files": len(selected),
            "direct_import_files": len(coupled),
            "logger_only_import_files": sum(item["logger_only_imports"] for item in coupled),
            "import_symbols": sum(len(item["imports"]) for item in coupled),
            "coupled_by_directory": dict(
                sorted(Counter(item["path"].removeprefix("src/fxbot/").split("/")[0] for item in coupled).items())
            ),
            "registration_candidates": dict(
                sorted(
                    Counter(
                        call["operation"]
                        for item in selected
                        for call in item["calls"]
                        if call["kind"] == "registration"
                    ).items()
                )
            ),
            "control_candidates": dict(
                sorted(
                    Counter(
                        call["operation"] for item in selected for call in item["calls"] if call["kind"] == "control"
                    ).items()
                )
            ),
        }
    return {"summary": groups, "files": files}


def main() -> None:
    """输出审计摘要，按需包含每个语法位置的完整清单。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--details", action="store_true", help="包含逐文件依赖及调用候选")
    args = parser.parse_args()
    report = audit_repository(Path(__file__).resolve().parents[1])
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(report if args.details else report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
