"""服务器运维控制台配置。"""

from __future__ import annotations

from typing import Any

DEFAULTS: dict[str, Any] = {
    "blocked_commands": [
        r"\brm\s+(?:--?[a-zA-Z][a-zA-Z-]*\s+)*(?:/|\*|/\*|~|\$HOME)(?:\s|$|[;&|])",
        r"--no-preserve-root",
        r"\b(?:mkfs(?:\.\w+)?|mkswap|wipefs|blkdiscard)\b",
        r"\b(?:fdisk|parted|sgdisk)\s+/dev/",
        r"\bdd\b[^\n]*\bof\s*=\s*/dev/(?:sd|hd|vd|nvme|mmcblk|disk)",
        r"(?:>|tee\s+)\s*/dev/(?:sd|hd|vd|nvme|mmcblk|disk)",
        r":\s*\(\s*\)\s*\{[^}]*:\s*\|\s*:",
    ],
    "permission_mode": "auto",
    "confirmed_commands": [],
    "cc_idle_seconds": 1800,
    "cc_keep_context": False,
    "cc_permission_mode": "ask",
    "cc_show_thinking": False,
    "cc_output_format": "image",
    "cc_render_scale": 2,
    "cc_tool_display": "off",
    "cc_approve_timeout": 60,
    "cc_busy_behavior": "queue",
    "allow_sharing": True,
    "allow_debounce": False,
    "allow_typing": False,
    "allow_split": False,
    "persona": "",
}


def get_ui_schema() -> dict[str, Any]:
    """返回服务器运维配置界面 schema。"""
    return {
        "key": "server_admin",
        "title": "服务器运维",
        "cards": [
            {
                "key": "server_admin",
                "title": "服务器运维",
                "schemas": [
                    {
                        "field": "permission_mode",
                        "label": "权限模式",
                        "component": "Select",
                        "default": DEFAULTS["permission_mode"],
                        "helpMessage": (
                            "auto：黑名单仍然拒绝；已永久允许的直接执行；其余命令由分类器判断是否与你的要求一致，"
                            "一致且低风险直接执行，不一致、拿不准或分类器不可用就确认。"
                            "confirm：除黑名单和已永久允许的命令外全部确认。"
                            "bypass：除黑名单外全部直接执行。"
                        ),
                        "componentProps": {
                            "options": [
                                {"label": "跟随请求自动判断（auto）", "value": "auto"},
                                {"label": "逐条确认（confirm）", "value": "confirm"},
                                {"label": "全部放行（bypass）", "value": "bypass"},
                            ]
                        },
                    },
                    {
                        "field": "blocked_commands",
                        "label": "禁止提交的命令规则",
                        "component": "GTags",
                        "default": DEFAULTS["blocked_commands"],
                        "helpMessage": "正则匹配，不区分大小写；命中即拒绝，不进入确认。只防误操作，不是安全边界。",
                    },
                    {
                        "field": "allow_sharing",
                        "label": "共享学习内容",
                        "component": "Switch",
                        "default": DEFAULTS["allow_sharing"],
                        "helpMessage": "按「学习 → 群表达 / 黑话 → 共享组」共用词条；关闭后只用本会话学到的。",
                    },
                    {
                        "field": "allow_debounce",
                        "label": "消息防抖",
                        "component": "Switch",
                        "default": DEFAULTS["allow_debounce"],
                        "helpMessage": "连续消息合并后再统一回复。",
                    },
                    {
                        "field": "allow_typing",
                        "label": "正在输入状态",
                        "component": "Switch",
                        "default": DEFAULTS["allow_typing"],
                        "helpMessage": "回复前发送正在输入提示。",
                    },
                    {
                        "field": "allow_split",
                        "label": "分段回复",
                        "component": "Switch",
                        "default": DEFAULTS["allow_split"],
                        "helpMessage": "长回复按句拆成多条发送，更像真人聊天。",
                    },
                    {
                        "field": "cc_idle_seconds",
                        "label": "Claude Code 空闲回收",
                        "component": "InputNumber",
                        "default": DEFAULTS["cc_idle_seconds"],
                        "helpMessage": "空闲超过该秒数关闭子进程，下次 cc 自动重开。",
                        "componentProps": {"min": 60, "max": 86400},
                    },
                    {
                        "field": "cc_keep_context",
                        "label": "空闲回收保留上下文",
                        "component": "Switch",
                        "default": DEFAULTS["cc_keep_context"],
                        "helpMessage": (
                            "关：回收时连上下文一起清，下次 cc 是全新会话；"
                            "开：只关子进程、保留 ACP 会话，下次自动 resume（冷启动慢几秒）。"
                        ),
                    },
                    {
                        "field": "cc_permission_mode",
                        "label": "Claude Code 工具权限",
                        "component": "Select",
                        "default": DEFAULTS["cc_permission_mode"],
                        "helpMessage": (
                            "ask：危险工具调用发到聊天里，等「cc 允许 / cc 允许 永久 / cc 拒绝」；allow：自动允许。"
                        ),
                        "componentProps": {
                            "options": [
                                {"label": "发到聊天确认（ask）", "value": "ask"},
                                {"label": "自动允许（allow）", "value": "allow"},
                            ]
                        },
                    },
                    {
                        "field": "cc_output_format",
                        "label": "Claude Code 输出格式",
                        "component": "Select",
                        "default": DEFAULTS["cc_output_format"],
                        "helpMessage": "image：正文渲染成图片；text：纯文本；auto：超过 600 字符才出图。",
                        "componentProps": {
                            "options": [
                                {"label": "图片（image）", "value": "image"},
                                {"label": "纯文本（text）", "value": "text"},
                                {"label": "自动（auto）", "value": "auto"},
                            ]
                        },
                    },
                    {
                        "field": "cc_render_scale",
                        "label": "渲染清晰度",
                        "component": "InputNumber",
                        "default": DEFAULTS["cc_render_scale"],
                        "helpMessage": "1 省流量，2 高清（默认），3 更清晰但体积大。",
                        "componentProps": {"min": 1, "max": 3},
                    },
                    {
                        "field": "cc_tool_display",
                        "label": "工具调用显示",
                        "component": "Select",
                        "default": DEFAULTS["cc_tool_display"],
                        "helpMessage": "off 不显示；brief 只显示工具标题；full 额外加简要说明。",
                        "componentProps": {
                            "options": [
                                {"label": "不显示（off）", "value": "off"},
                                {"label": "标题（brief）", "value": "brief"},
                                {"label": "标题+说明（full）", "value": "full"},
                            ]
                        },
                    },
                    {
                        "field": "cc_approve_timeout",
                        "label": "Claude Code 审批超时",
                        "component": "InputNumber",
                        "default": DEFAULTS["cc_approve_timeout"],
                        "helpMessage": "权限请求等待人工回复的秒数，超时按拒绝处理。",
                        "componentProps": {"min": 10, "max": 3600},
                    },
                    {
                        "field": "cc_busy_behavior",
                        "label": "Claude Code 忙时行为",
                        "component": "Select",
                        "default": DEFAULTS["cc_busy_behavior"],
                        "helpMessage": "queue：新消息排队串行执行；reject：直接拒绝。",
                        "componentProps": {
                            "options": [
                                {"label": "排队（queue）", "value": "queue"},
                                {"label": "拒绝（reject）", "value": "reject"},
                            ]
                        },
                    },
                    {
                        "field": "cc_show_thinking",
                        "label": "Claude Code 回发思考",
                        "component": "Switch",
                        "default": DEFAULTS["cc_show_thinking"],
                        "helpMessage": "开启后把 agent 的思考片段也发到聊天里。",
                    },
                    {
                        "field": "persona",
                        "label": "人格内容",
                        "component": "Textarea",
                        "default": DEFAULTS["persona"],
                        "helpMessage": "留空使用插件内置人格；填写后每次进入服务器模式都会写入「运维」人格。",
                        "componentProps": {"rows": 10},
                    },
                ],
            }
        ],
    }
