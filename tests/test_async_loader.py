import asyncio

import pytest
from parsel import Selector

from itemloaders import ItemLoader
from itemloaders.common import AsyncProcessorError
from itemloaders.processors import Compose, Identity, MapCompose, TakeFirst


def run(coro):
    return asyncio.run(coro)


async def async_upper(value):
    await asyncio.sleep(0)
    return value.upper()


class AsyncMapLoader(ItemLoader):
    name_in = MapCompose(str.strip, async_upper)
    default_output_processor = TakeFirst()


HTML = """
<html>
    <body>
        <header><div id="id">marta</div></header>
        <p class="price">1200</p>
    </body>
</html>
"""


class TestAsyncAddValue:
    def test_async_add_matches_sync_chain(self):
        class SyncLoader(ItemLoader):
            name_in = MapCompose(str.strip, str.upper)
            default_output_processor = TakeFirst()

        sync_loader = SyncLoader()
        sync_loader.add_value("name", "  marta ")
        sync_loader.add_value("name", "bob")
        expected = sync_loader.load_item()

        async def build():
            loader = AsyncMapLoader()
            await loader.async_add_value("name", "  marta ")
            await loader.async_add_value("name", "bob")
            return await loader.async_load_item()

        assert run(build()) == expected

    def test_async_add_and_replace_and_get_value(self):
        async def build():
            loader = ItemLoader()
            assert await loader.async_get_value("name", async_upper) == "NAME"
            await loader.async_add_value("name", "name", async_upper)
            await loader.async_add_value("name", "other", async_upper)
            assert loader.get_collected_values("name") == ["NAME", "OTHER"]
            await loader.async_replace_value("name", "replaced", async_upper)
            assert loader.get_collected_values("name") == ["REPLACED"]
            return await loader.async_load_item()

        assert run(build()) == {"name": ["REPLACED"]}

    def test_mixed_sync_and_async_processors(self):
        async def build():
            loader = ItemLoader()
            await loader.async_add_value(
                "name", "  marta  ", str.strip, async_upper, str.lower
            )
            return loader.get_collected_values("name")

        assert run(build()) == ["marta"]

    def test_async_none_does_not_touch_collected_values(self):
        async def build():
            loader = ItemLoader()
            loader.add_value("name", "keep")
            await loader.async_add_value("name", None)
            await loader.async_replace_value("name", None)
            return loader.get_collected_values("name")

        assert run(build()) == ["keep"]

    def test_async_multi_field_dict(self):
        async def build():
            loader = ItemLoader()
            await loader.async_add_value(
                None, {"name": "MARTA", "city": "MADRID"}
            )
            assert loader.get_collected_values("name") == ["MARTA"]
            assert loader.get_collected_values("city") == ["MADRID"]
            await loader.async_replace_value(None, {"name": "PEPE"})
            assert loader.get_collected_values("name") == ["PEPE"]
            assert loader.get_collected_values("city") == ["MADRID"]

        run(build())

    def test_async_jmes_entry(self):
        async def build():
            selector = Selector(text='{"name": "marta"}', type="json")
            loader = ItemLoader(selector=selector)
            await loader.async_add_jmes("name", "name")
            assert await loader.async_get_jmes("name") == ["marta"]
            await loader.async_replace_jmes("name", "name")
            return loader.get_collected_values("name")

        assert run(build()) == ["marta"]


class TestManualFutureBlocking:
    def test_add_waits_for_manual_future_before_collecting(self):
        async def build():
            loader = ItemLoader()
            future: asyncio.Future = asyncio.get_running_loop().create_future()

            async def resolve(value):
                return future

            task = asyncio.create_task(
                loader.async_add_value("name", "x", resolve, async_upper)
            )
            await asyncio.sleep(0)
            # Blocked in the middle of the processor chain: nothing collected.
            assert not task.done()
            assert loader.get_collected_values("name") == []
            future.set_result("done")
            await task
            assert loader.get_collected_values("name") == ["DONE"]

        run(build())

    def test_input_processor_failure_leaves_previous_values_intact(self):
        async def failing(value):
            await asyncio.sleep(0)
            if value == "bad":
                raise RuntimeError("boom")
            return value

        class FailingLoader(ItemLoader):
            name_in = MapCompose(failing)

        async def build():
            loader = FailingLoader()
            await loader.async_add_value("name", "good")
            assert loader.get_collected_values("name") == ["good"]
            with pytest.raises(ValueError, match="Error with input processor"):
                await loader.async_add_value("name", "bad")
            # No half-written value, original values preserved.
            assert loader.get_collected_values("name") == ["good"]

        run(build())

    def test_replace_failure_keeps_original_value(self):
        async def failing(value):
            await asyncio.sleep(0)
            if value == "new":
                raise RuntimeError("boom")
            return value

        class FailingLoader(ItemLoader):
            name_in = MapCompose(failing)

        async def build():
            loader = FailingLoader()
            await loader.async_add_value("name", "keep")
            with pytest.raises(ValueError):
                await loader.async_replace_value("name", "new")
            assert loader.get_collected_values("name") == ["keep"]

        run(build())

    def test_cancel_keeps_original_exception_and_values(self):
        async def build():
            loader = ItemLoader()
            loader.add_value("name", "keep")
            future: asyncio.Future = asyncio.get_running_loop().create_future()

            async def block(value):
                return await future

            task = asyncio.create_task(
                loader.async_replace_value("name", "new", block)
            )
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert loader.get_collected_values("name") == ["keep"]

        run(build())

    def test_no_dangling_tasks_after_failure(self):
        async def failing(value):
            await asyncio.sleep(0)
            raise RuntimeError("boom")

        async def build():
            loader = ItemLoader()
            before = asyncio.all_tasks()
            with pytest.raises(ValueError, match="Error with processor"):
                await loader.async_add_value("name", "x", failing)
            # Give the loop a chance to surface any orphaned task.
            await asyncio.sleep(0.01)
            assert asyncio.all_tasks() == before
            assert loader.get_collected_values("name") == []

        run(build())


class TestAsyncOutputProcessor:
    def test_async_output_processor(self):
        class OutLoader(ItemLoader):
            name_out = Compose(TakeFirst(), async_upper)

        async def build():
            loader = OutLoader()
            loader.add_value("name", "marta")
            return await loader.async_load_item()

        assert run(build()) == {"name": "MARTA"}

    def test_async_output_failure_keeps_item_unchanged(self):
        async def failing(values):
            await asyncio.sleep(0)
            raise RuntimeError("boom")

        class OutLoader(ItemLoader):
            name_out = failing
            other_out = TakeFirst()

        async def build():
            loader = OutLoader()
            loader.add_value("name", "marta")
            loader.add_value("other", "value")
            item = loader.item
            with pytest.raises(ValueError, match="Error with output processor"):
                await loader.async_load_item()
            assert "name" not in item

        run(build())


class TestSyncRejection:
    def test_sync_add_value_rejects_coroutine(self):
        with pytest.raises(AsyncProcessorError):
            ItemLoader().add_value("name", "x", async_upper)

    def test_sync_input_processor_rejects_coroutine(self):
        loader = AsyncMapLoader()
        with pytest.raises(AsyncProcessorError):
            loader.add_value("name", "x")

    def test_sync_output_processor_rejects_coroutine(self):
        class OutLoader(ItemLoader):
            name_out = async_upper

        loader = OutLoader()
        loader.add_value("name", "x")
        with pytest.raises(AsyncProcessorError):
            loader.get_output_value("name")

    def test_rejected_coroutine_is_closed(self):
        async def tracked(value):
            return value

        loader = ItemLoader()
        with pytest.raises(AsyncProcessorError):
            loader.add_value("name", "x", tracked)


class TestAsyncNestedLoader:
    def test_nested_loader_async_entries(self):
        selector = Selector(text=HTML)

        async def build():
            loader = ItemLoader(selector=selector)
            nested = loader.nested_xpath("//header")
            await nested.async_add_xpath("name", "div/text()")
            await nested.async_add_css("name_div", "#id")
            return await loader.async_load_item()

        item = run(build())
        assert item["name"] == ["marta"]
        assert item["name_div"] == ['<div id="id">marta</div>']

    def test_async_css_and_jmes_entries(self):
        selector = Selector(text=HTML)

        async def build():
            loader = ItemLoader(selector=selector)
            await loader.async_add_css("price", "p.price::text")
            assert await loader.async_get_css("p.price::text") == ["1200"]
            await loader.async_replace_css("price", "p.price::text")
            return await loader.async_load_item()

        assert run(build()) == {"price": ["1200"]}


class TestAsyncLoaderContext:
    def test_loader_context_reaches_async_processor(self):
        async def with_context(value, loader_context):
            await asyncio.sleep(0)
            return value + loader_context["suffix"]

        class ContextLoader(ItemLoader):
            name_in = MapCompose(with_context)

        async def build():
            loader = ContextLoader(suffix="!")
            await loader.async_add_value("name", "hi")
            return loader.get_collected_values("name")

        assert run(build()) == ["hi!"]
