import asyncio

import pytest

from itemloaders.common import AsyncProcessorError
from itemloaders.processors import Compose, MapCompose


def run(coro):
    return asyncio.run(coro)


async def async_upper(value):
    await asyncio.sleep(0)
    return value.upper()


def sync_title(value):
    return value.title()


def _is_closed(coro):
    # Coroutines expose cr_frame only while they can still be awaited.
    return coro.cr_frame is None


class TestAsyncMapCompose:
    def test_mixed_sync_and_async_chain(self):
        proc = MapCompose(str.strip, sync_title, async_upper)
        result = run(proc.async_call(["  hello ", "world"]))
        assert result == ["HELLO", "WORLD"]

    def test_async_none_values_are_dropped(self):
        async def drop_world(value):
            await asyncio.sleep(0)
            return None if value == "world" else value

        proc = MapCompose(drop_world, async_upper)
        assert run(proc.async_call(["hello", "world"])) == ["HELLO"]

    def test_async_result_lists_are_flattened(self):
        async def explode(value):
            await asyncio.sleep(0)
            return [value, value]

        proc = MapCompose(explode, async_upper)
        assert run(proc.async_call(["a"])) == ["A", "A"]

    def test_chain_awaited_in_order(self):
        order = []

        async def step(value):
            order.append(value)
            await asyncio.sleep(0)
            return value + 1

        proc = MapCompose(step, step, step)
        assert run(proc.async_call([0])) == [3]
        assert order == [0, 1, 2]

    def test_async_error_keeps_valueerror_wrapping(self):
        async def boom(value):
            await asyncio.sleep(0)
            raise RuntimeError("boom")

        proc = MapCompose(boom)
        with pytest.raises(ValueError, match="Error in MapCompose"):
            run(proc.async_call(["x"]))

    def test_loader_context_is_passed(self):
        async def with_context(value, loader_context):
            await asyncio.sleep(0)
            return value + loader_context["suffix"]

        proc = MapCompose(with_context)
        assert run(proc.async_call(["a"], {"suffix": "!"})) == ["a!"]

    def test_sync_call_rejects_coroutine_and_closes_it(self):
        proc = MapCompose(async_upper)
        with pytest.raises(AsyncProcessorError):
            proc(["hello"])

    def test_sync_call_closes_the_rejected_coroutine(self):
        created = []

        async def tracked(value):
            return value

        original = tracked

        def factory(value):
            coro = original(value)
            created.append(coro)
            return coro

        proc = MapCompose(factory)
        with pytest.raises(AsyncProcessorError):
            proc(["a", "b", "c"])
        assert len(created) == 1
        assert _is_closed(created[0])

    def test_sync_call_closes_coroutine_after_plain_values(self):
        created = []

        async def tracked(value):
            return value

        def mixed(value):
            if value == "sync":
                return value
            coro = tracked(value)
            created.append(coro)
            return coro

        proc = MapCompose(mixed)
        with pytest.raises(AsyncProcessorError):
            proc(["sync", "a", "b"])
        assert len(created) == 1
        assert _is_closed(created[0])


class TestAsyncCompose:
    def test_mixed_sync_and_async_chain(self):
        proc = Compose(sync_title, async_upper)
        assert run(proc.async_call("hello")) == "HELLO"

    def test_stop_on_none(self):
        proc = Compose(async_upper)
        assert run(proc.async_call(None)) is None

    def test_async_error_keeps_valueerror_wrapping(self):
        async def boom(value):
            await asyncio.sleep(0)
            raise RuntimeError("boom")

        proc = Compose(boom)
        with pytest.raises(ValueError, match="Error in Compose"):
            run(proc.async_call("x"))

    def test_loader_context_is_passed(self):
        async def with_context(value, loader_context):
            await asyncio.sleep(0)
            return value + loader_context["suffix"]

        proc = Compose(with_context)
        assert run(proc.async_call("a", {"suffix": "!"})) == "a!"

    def test_sync_call_rejects_coroutine(self):
        proc = Compose(async_upper)
        with pytest.raises(AsyncProcessorError):
            proc("hello")
