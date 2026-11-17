"""运行时外部命令的进程树终止；不重试业务操作。"""

from __future__ import annotations

import os
import sys
import signal

import psutil


def terminate_process(process) -> None:
    """POSIX 调用者须创建独立会话；Windows 终止已派生子进程及父进程。"""
    if process.returncode is not None:
        return
    if sys.platform != "win32":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return
    try:
        children = psutil.Process(process.pid).children(recursive=True)
    except psutil.NoSuchProcess:
        children = []
    for child in reversed(children):
        try:
            child.kill()
        except psutil.NoSuchProcess:
            pass
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
