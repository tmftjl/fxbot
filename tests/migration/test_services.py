"""服务作用域按对象身份缓存，不以工厂名称或相等判断猜测归属。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from functools import partial

from fxbot.core.services import Service, ServiceScope
from fxbot.core.application import Application


class ServiceIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = Application()
        await self.app.start()
        self.runtime = SimpleNamespace(application=self.app)
        self.scope = ServiceScope()
        self.addAsyncCleanup(self.app.stop)
        self.addAsyncCleanup(self.scope.close)

    async def test_distinct_lambdas_with_same_name_do_not_share(self):
        first, second = (lambda runtime: ["first"]), (lambda runtime: ["second"])
        self.assertEqual(first.__qualname__, second.__qualname__)
        self.assertIsNot(self.scope.resolve(first, self.runtime), self.scope.resolve(second, self.runtime))
        self.assertEqual(self.scope.resolve(second, self.runtime), ["second"])

    async def test_same_factory_is_created_once(self):
        calls = []

        def factory(runtime):
            calls.append(runtime)
            return object()

        one = self.scope.resolve(factory, self.runtime)
        self.assertIs(one, self.scope.resolve(factory, self.runtime))
        self.assertEqual(calls, [self.runtime])

    async def test_partial_factories_are_supported_without_qualname(self):
        def factory(value, runtime):
            return value

        first, second = partial(factory, "a"), partial(factory, "b")
        self.assertEqual(self.scope.resolve(first, self.runtime), "a")
        self.assertEqual(self.scope.resolve(second, self.runtime), "b")

    async def test_unhashable_equal_callable_objects_still_have_distinct_identity(self):
        class Factory:
            __hash__ = None

            def __init__(self, value):
                self.value = value

            def __eq__(self, other):
                return True

            def __call__(self, runtime):
                return self.value

        first, second = Factory("a"), Factory("b")
        self.assertEqual(first, second)
        self.assertEqual(self.scope.resolve(first, self.runtime), "a")
        self.assertEqual(self.scope.resolve(second, self.runtime), "b")

    async def test_scopes_do_not_share_instances(self):
        def factory(runtime):
            return []

        other = ServiceScope()
        self.addAsyncCleanup(other.close)
        self.assertIsNot(self.scope.resolve(factory, self.runtime), other.resolve(factory, self.runtime))

    async def test_two_factories_returning_same_owned_instance_close_it_once(self):
        closed = []

        class Owned(Service):
            async def close(self):
                closed.append("closed")

        instance = Owned()
        self.scope.resolve(lambda runtime: instance, self.runtime)
        self.scope.resolve(lambda runtime: instance, self.runtime)
        await self.scope.close()
        await self.scope.close()
        self.assertEqual(closed, ["closed"])

    async def test_async_factory_is_rejected_without_caching(self):
        async def factory(runtime):
            return object()

        with self.assertRaises(TypeError):
            self.scope.resolve(factory, self.runtime)
        self.assertFalse(self.scope._entries)

    async def test_closed_scope_rejects_even_previously_cached_factory(self):
        def factory(runtime):
            return object()

        self.scope.resolve(factory, self.runtime)
        await self.scope.close()
        with self.assertRaises(RuntimeError):
            self.scope.resolve(factory, self.runtime)
