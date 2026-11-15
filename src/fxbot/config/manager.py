"""配置管理器编排。"""

from __future__ import annotations

from typing import Any
from hashlib import sha1
from collections.abc import Callable, Iterable

from loguru import logger

from .proxy import Validator, ConfigProxy
from ..utils.paths import config_dir
from .system_defaults import SYSTEM_DEFAULTS, validate_system_config


class ConfigConflictError(ValueError):
    """控制台提交的配置修订号已过期。"""


class ConfigManager:
    def __init__(self) -> None:
        self._proxies: dict[tuple[str, str], ConfigProxy] = {}
        self._bootstrapped = False
        self._subscribers: list[Callable[[], None]] = []

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        """显式订阅成功保存，不导入任何业务模块。"""
        self._subscribers.append(callback)
        return lambda: self._subscribers.remove(callback)

    def register(
        self,
        namespace: str,
        defaults: dict[str, Any] | None = None,
        *,
        filename: str = "config.json",
        clean_extra: bool = True,
        validator: Validator | None = None,
    ) -> ConfigProxy:
        if namespace == "system" and filename == "config.json":
            defaults = SYSTEM_DEFAULTS if defaults is None else defaults
            validator = validate_system_config
        key = (namespace, filename)
        proxy = self._proxies.get(key)
        if proxy is None:
            proxy = ConfigProxy(
                namespace=namespace,
                filename=filename,
                defaults=defaults or {},
                clean_extra=clean_extra,
                validator=validator,
            )
            self._proxies[key] = proxy
        elif validator is not None:
            proxy.validator = validator
        return proxy

    def bootstrap(self) -> None:
        if self._bootstrapped:
            return
        config_dir()
        self.register("system", SYSTEM_DEFAULTS, clean_extra=True, validator=validate_system_config)
        for proxy in list(self._proxies.values()):
            proxy.merge_and_save()
        self._bootstrapped = True
        logger.info("[FxBot] Config initialized")

    def get_system(self) -> dict[str, Any]:
        return self.register("system", SYSTEM_DEFAULTS, validator=validate_system_config).load()

    def get_all(self) -> dict[str, Any]:
        return {f"{namespace}/{filename}": proxy.load() for (namespace, filename), proxy in self._proxies.items()}

    def get_console_configs(self) -> dict[str, Any]:
        """返回控制台使用的配置数据。"""
        configs: dict[str, Any] = {"system": self.get_system()}
        for (namespace, filename), proxy in self._proxies.items():
            if namespace == "system" or filename != "config.json":
                continue
            configs[namespace] = proxy.load()
        configs["revisions"] = self.revisions(configs)
        return configs

    def revisions(self, namespaces: Iterable[str]) -> dict[str, str]:
        """返回控制台可写命名空间的文件内容修订号。"""
        revisions = {}
        for (namespace, filename), proxy in self._proxies.items():
            if filename != "config.json" or namespace not in namespaces:
                continue
            try:
                revisions[namespace] = sha1(proxy.path.read_bytes()).hexdigest()
            except FileNotFoundError:
                revisions[namespace] = ""
        return revisions

    def save_console_configs(self, payload: dict[str, Any], *, base_revisions: dict[str, str] | None = None) -> None:
        """保存控制台提交的配置数据。"""
        self.register("system", SYSTEM_DEFAULTS, validator=validate_system_config)
        if base_revisions is not None:
            conflicts = [
                namespace
                for namespace, revision in self.revisions(payload).items()
                if base_revisions.get(namespace) != revision
            ]
            if conflicts:
                raise ConfigConflictError(f"配置已被修改: {', '.join(conflicts)}")
        pending: list[tuple[ConfigProxy, dict[str, Any]]] = []
        for (namespace, filename), proxy in list(self._proxies.items()):
            if filename != "config.json" or namespace not in payload:
                continue
            value = payload[namespace]
            if not isinstance(value, dict):
                raise ValueError(f"{namespace} 配置必须是对象")
            proxy.validate(value)
            pending.append((proxy, value))
        snapshots = [proxy.snapshot() for proxy, _ in pending]
        attempted = []
        try:
            for (proxy, value), snapshot in zip(pending, snapshots, strict=True):
                attempted.append((proxy, snapshot))
                proxy.save(value)
        except Exception as error:
            failures = []
            for proxy, snapshot in reversed(attempted):
                try:
                    proxy.restore(snapshot)
                except Exception as restore_error:
                    failures.append(restore_error)
            if failures:
                raise ExceptionGroup("配置保存及回滚失败", [error, *failures]) from None
            raise
        for callback in tuple(self._subscribers):
            callback()

    def _registered_plugin_names(self) -> set[str]:
        """返回已注册插件配置命名空间。"""
        return {
            namespace for (namespace, filename) in self._proxies if namespace != "system" and filename == "config.json"
        }

    def reload_all(self) -> tuple[bool, dict[str, Any]]:
        result: dict[str, Any] = {}
        ok_all = True
        for (namespace, filename), proxy in list(self._proxies.items()):
            ok, data, error = proxy.reload_and_validate()
            ok_all = ok_all and ok
            result[f"{namespace}/{filename}"] = {
                "ok": ok,
                "error": error,
                "data": data,
            }
        return ok_all, result


_manager = ConfigManager()


def get_manager() -> ConfigManager:
    return _manager
