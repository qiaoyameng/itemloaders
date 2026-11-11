"""Tests for the asynchronous entry points of :class:`ItemLoader`."""

from __future__ import annotations

import asyncio
import gc
import inspect
import warnings
from typing import Any

import pytest
from parsel import Selector

from itemloaders import ItemLoader
from itemloaders.processors import Compose, MapCompose, TakeFirst


SELECTOR = Selector(
    text="""
<html><body>
<header><div id="id">marta</div></header>
<footer>
<a href="http://www.scrapy.org">homepage</a>
<img src="/images/logo.png" alt="Scrapy">
</footer>
</body></html>
"""
)


async def async_upper(value: Any) -> Any:
    await asyncio.sleep(0)
    return value.upper()


def sync_strip(value: Any) -> Any:
    return value.strip()


async def async_raise(value: Any) -> Any:
    raise KeyError("service down")


def run(coro: Any) -> Any:
    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        return asyncio.run(coro)


class AsyncMapComposeLoader(ItemLoader):
    name_in = MapCompose(sync_strip, async_upper)


class SyncMapComposeLoader(ItemLoader):
    name_in = MapCompose(sync_strip, str.upper)


class TestAsyncSuccess:
    def test_add_value_awaits_coroutine_inside_mapcompose(self):
        async def scenario() -> None:
            loader = AsyncMapComposeLoader()
            await loader.async_add_value("name", " marta ")
            assert loader.get_collected_values("name") == ["MARTA"]
            assert not any(
                inspect.iscoroutine(value)
                for value in loader.get_collected_values("name")
            )

        run(scenario())

    def test_async_chain_matches_equivalent_sync_chain(self):
        async def scenario() -> None:
            sync_loader = SyncMapComposeLoader()
            sync_loader.add_value("name", " marta ")
            sync_loader.add_value("name", [" bob", "jim "])

            async_loader = AsyncMapComposeLoader()
            await async_loader.async_add_value("name", " marta ")
            await async_loader.async_add_value("name", [" bob", "jim "])

            assert async_loader.get_collected_values("name") == (
                sync_loader.get_collected_values("name")
            )

        run(scenario())

    def test_processors_keep_their_order(self):
        async def scenario() -> None:
            events: list[str] = []

            async def first(value: Any) -> Any:
                events.append("first-start")
                await asyncio.sleep(0)
                events.append("first-end")
                return value

            async def second(value: Any) -> Any:
                events.append("second")
                return value

            loader = ItemLoader()
            loader.default_input_processor = MapCompose(first, second)
            await loader.async_add_value("name", ["a", "b"])
            assert events == [
                "first-start",
                "first-end",
                "first-start",
                "first-end",
                "second",
                "second",
            ]

        run(scenario())

    def test_mixed_sync_and_async_processors(self):
        async def scenario() -> None:
            loader = ItemLoader()
            loader.default_input_processor = MapCompose(sync_strip, async_upper)
            await loader.async_add_value("name", " marta ")
            await loader.async_add_value("name", " bob ")
            assert loader.get_collected_values("name") == ["MARTA", "BOB"]

            # a sync-only chain still works through the synchronous entry
            sync_loader = ItemLoader()
            sync_loader.default_input_processor = MapCompose(sync_strip, str.upper)
            sync_loader.add_value("name", "pepe")
            assert sync_loader.get_collected_values("name") == ["PEPE"]

        run(scenario())

    def test_async_output_processor(self):
        async def scenario() -> None:
            async def async_join(values: Any) -> Any:
                await asyncio.sleep(0)
                return "|".join(values)

            loader = ItemLoader()
            loader.default_output_processor = Compose(async_join)
            await loader.async_add_value("name", ["a", "b", "c"])
            assert await loader.async_get_output_value("name") == "a|b|c"
            assert (await loader.async_load_item())["name"] == "a|b|c"

        run(scenario())

    def test_async_output_processor_inside_mapcompose(self):
        async def scenario() -> None:
            loader = ItemLoader()
            loader.default_output_processor = MapCompose(async_upper)
            await loader.async_add_value("name", ["marta", "pepe"])
            assert (await loader.async_load_item())["name"] == ["MARTA", "PEPE"]

            # matches the equivalent synchronous chain
            sync_loader = ItemLoader()
            sync_loader.default_output_processor = MapCompose(str.upper)
            sync_loader.add_value("name", ["marta", "pepe"])
            assert sync_loader.load_item()["name"] == ["MARTA", "PEPE"]

        run(scenario())

    def test_compose_mixed_chain(self):
        async def scenario() -> None:
            loader = ItemLoader()
            loader.default_output_processor = Compose(TakeFirst(), async_upper)
            await loader.async_add_value("name", [" marta "])
            assert (await loader.async_load_item())["name"] == " MARTA "

        run(scenario())

    def test_none_short_circuits_compose(self):
        async def scenario() -> None:
            loader = ItemLoader()
            loader.default_output_processor = Compose(TakeFirst(), async_upper)
            await loader.async_add_value("name", [])
            assert await loader.async_get_output_value("name") is None

        run(scenario())


class TestSyncRejection:
    def test_add_value_rejects_coroutine_processor(self):
        loader = ItemLoader()
        with pytest.raises(TypeError, match="async_\\* counterparts"):
            loader.add_value("name", "marta", async_upper)
        gc.collect()

    def test_nested_coroutine_in_input_processor_is_rejected_and_closed(self):
        loader = ItemLoader()
        loader.default_input_processor = MapCompose(sync_strip, async_upper)
        with pytest.raises(TypeError):
            loader.add_value("name", "marta")
        assert loader.get_collected_values("name") == []
        gc.collect()

    def test_replace_value_rejects_coroutine(self):
        loader = ItemLoader()
        loader.add_value("name", "old")
        with pytest.raises(TypeError):
            loader.replace_value("name", "new", async_upper)
        assert loader.get_collected_values("name") == ["old"]
        gc.collect()

    def test_load_item_rejects_async_output_processor(self):
        loader = ItemLoader()
        loader.add_value("name", "marta")
        loader.default_output_processor = Compose(async_upper)
        with pytest.raises(TypeError):
            loader.load_item()
        gc.collect()

    def test_get_value_rejects_coroutine(self):
        loader = ItemLoader()
        with pytest.raises(TypeError):
            loader.get_value("marta", async_upper)
        gc.collect()

    def test_rejected_coroutine_is_closed_without_event_loop(self):
        async def never_run() -> Any:  # pragma: no cover
            raise AssertionError("must never be awaited")

        loader = ItemLoader()
        coroutine = never_run()
        with pytest.raises(TypeError):
            loader.add_value("name", "marta", lambda _: coroutine)
        assert coroutine.cr_frame is None
        gc.collect()


class TestFailureAtomicity:
    def test_input_processor_failure_keeps_collected_values(self):
        async def scenario() -> None:
            loader = ItemLoader()
            loader.add_value("name", "old")
            loader.default_input_processor = MapCompose(async_upper, async_raise)
            with pytest.raises(ValueError) as exc_info:
                await loader.async_add_value("name", ["a", "b"])
            assert isinstance(exc_info.value.__cause__, ValueError)
            assert isinstance(exc_info.value.__cause__.__cause__, KeyError)
            assert loader.get_collected_values("name") == ["old"]

        run(scenario())

    def test_replace_failure_keeps_previous_values(self):
        async def scenario() -> None:
            loader = ItemLoader()
            loader.add_value("name", "old")
            loader.default_input_processor = MapCompose(async_raise)
            with pytest.raises(ValueError):
                await loader.async_replace_value("name", "new")
            assert loader.get_collected_values("name") == ["old"]

        run(scenario())

    def test_processor_position_is_preserved_in_error_message(self):
        async def scenario() -> None:
            loader = ItemLoader()
            loader.default_input_processor = MapCompose(async_upper, async_raise)
            with pytest.raises(ValueError, match="input processor MapCompose"):
                await loader.async_add_value("name", "a")
            try:
                await loader.async_get_value("a", async_raise)
            except ValueError as exc:
                assert "processor function" in str(exc)
                assert "KeyError" in str(exc)
            else:  # pragma: no cover
                pytest.fail("async_get_value did not raise")

        run(scenario())

    def test_output_processor_failure_leaves_item_untouched(self):
        async def scenario() -> None:
            async def failing_output(values: Any) -> Any:
                raise RuntimeError("boom")

            loader = ItemLoader()
            loader.add_value("ok", "value")
            loader.default_output_processor = Compose(failing_output)
            with pytest.raises(ValueError) as exc_info:
                await loader.async_load_item()
            cause = exc_info.value
            while cause.__cause__ is not None:
                cause = cause.__cause__
            assert isinstance(cause, RuntimeError)
            assert "ok" not in loader.item

        run(scenario())

    def test_no_dangling_coroutines_after_failure(self):
        async def scenario() -> None:
            loader = ItemLoader()
            loader.default_input_processor = MapCompose(async_upper, async_raise)
            with pytest.raises(ValueError):
                await loader.async_add_value("name", ["a", "b", "c"])
            assert loader.get_collected_values("name") == []

        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            asyncio.run(scenario())
            gc.collect()


class TestCancellation:
    def test_cancelled_chain_keeps_old_values_and_cleans_up(self):
        async def scenario() -> None:
            loader = ItemLoader()
            loader.add_value("name", "old")
            blocker = asyncio.get_running_loop().create_future()

            async def blocked(value: Any) -> Any:
                await blocker
                return value

            loader.default_input_processor = MapCompose(blocked)
            task = asyncio.create_task(
                loader.async_add_value("name", ["a", "b"])
            )
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert loader.get_collected_values("name") == ["old"]
            assert not [
                task_
                for task_ in asyncio.all_tasks()
                if task_ is not asyncio.current_task()
            ]

        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            asyncio.run(scenario())
            gc.collect()

    def test_cancelled_load_item_does_not_partially_populate(self):
        async def scenario() -> None:
            blocker = asyncio.get_running_loop().create_future()

            async def blocked_output(values: Any) -> Any:
                await blocker
                return values

            loader = ItemLoader()
            loader.add_value("a", "1")
            loader.add_value("b", "2")
            loader.default_output_processor = Compose(blocked_output)
            task = asyncio.create_task(loader.async_load_item())
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert "a" not in loader.item
            assert "b" not in loader.item

        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            asyncio.run(scenario())
            gc.collect()


class TestNestedAndContext:
    def test_nested_loader_async_entries_share_values(self):
        async def scenario() -> None:
            loader = ItemLoader(selector=SELECTOR)
            nested = loader.nested_xpath("//footer")
            nested.default_input_processor = MapCompose(async_upper)
            await nested.async_add_xpath("url", "a/text()")
            await loader.async_add_xpath("name", "//header/div/text()")
            item = await loader.async_load_item()
            assert item["url"] == ["HOMEPAGE"]
            assert item["name"] == ["marta"]
            assert item is nested.item

        run(scenario())

    def test_nested_css_async_entries(self):
        async def scenario() -> None:
            loader = ItemLoader(selector=SELECTOR)
            nested = loader.nested_css("footer")
            await nested.async_add_css("url", "a::attr(href)")
            await nested.async_replace_css("url", "img::attr(src)")
            assert loader.get_collected_values("url") == ["/images/logo.png"]

        run(scenario())

    def test_jmes_async_entry(self):
        async def scenario() -> None:
            selector = Selector(text='{"name": "marta"}', type="json")
            loader = ItemLoader(selector=selector)
            loader.default_input_processor = MapCompose(async_upper)
            await loader.async_add_jmes("name", "name")
            assert (await loader.async_load_item())["name"] == ["MARTA"]

        run(scenario())

    def test_loader_context_reaches_async_functions(self):
        async def scenario() -> None:
            def with_context(value: Any, loader_context: Any) -> Any:
                return loader_context["prefix"] + ":" + value

            loader = ItemLoader(prefix="ctx")
            loader.default_input_processor = MapCompose(with_context, async_upper)
            await loader.async_add_value("name", "v")
            assert loader.get_collected_values("name") == ["CTX:V"]

        run(scenario())

    def test_mapcompose_default_loader_context_in_async_chain(self):
        async def scenario() -> None:
            loader = ItemLoader()
            loader.default_input_processor = MapCompose(
                lambda value, loader_context: loader_context["key"] + value,
                async_upper,
                key="k:",
            )
            await loader.async_add_value("name", "v")
            assert loader.get_collected_values("name") == ["K:V"]

        run(scenario())

    def test_async_regex_extraction(self):
        async def scenario() -> None:
            loader = ItemLoader()
            await loader.async_add_value(
                "name", ["SKU: 1234"], TakeFirst(), re=r"SKU: (.+)"
            )
            assert loader.get_collected_values("name") == ["1234"]

        run(scenario())

    def test_field_name_none_dict_async(self):
        async def scenario() -> None:
            loader = ItemLoader()
            await loader.async_add_value(None, "jim", lambda x: {"name": x})
            await loader.async_replace_value(None, "bob", lambda x: {"name": x})
            assert loader.get_collected_values("name") == ["bob"]

        run(scenario())


class TestAsyncParity:
    def test_full_pipeline_matches_sync_equivalent(self):
        async def async_pipeline() -> dict[str, Any]:
            loader = ItemLoader()
            loader.default_input_processor = MapCompose(sync_strip, async_upper)
            loader.default_output_processor = TakeFirst()
            await loader.async_add_value("name", " marta ")
            await loader.async_add_value("name", ["pepe"])
            await loader.async_add_value("empty", None)
            return await loader.async_load_item()

        sync_loader = ItemLoader()
        sync_loader.default_input_processor = MapCompose(sync_strip, str.upper)
        sync_loader.default_output_processor = TakeFirst()
        sync_loader.add_value("name", " marta ")
        sync_loader.add_value("name", ["pepe"])
        sync_loader.add_value("empty", None)

        assert run(async_pipeline()) == sync_loader.load_item()
