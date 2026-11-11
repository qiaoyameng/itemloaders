from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

import attr
import pytest
from itemadapter import ItemAdapter
from parsel import Selector

from itemloaders import ItemLoader
from itemloaders.processors import MapCompose

HTML = """
<html>
  <header><h1>Color TV</h1></header>
  <p id="price">$1200</p>
  <p id="currency">USD</p>
  <p id="stock">7</p>
</html>
"""


class Stats:
    def __init__(self) -> None:
        self.values: Counter[str] = Counter()

    def inc_value(self, key: str, count: int = 1) -> None:
        self.values[key] += count


class FailingProcessor:
    """Processor that fails on the first ``failures`` calls, then works."""

    def __init__(self, failures: int = 1) -> None:
        self.failures = failures
        self.calls = 0

    def __call__(self, value: Any) -> Any:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("processor failure")
        return value


class FailingOutputProcessor:
    def __init__(self, failures: int = 1) -> None:
        self.failures = failures
        self.calls = 0

    def __call__(self, values: list[Any]) -> Any:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("output processor failure")
        return values


@dataclass
class Product:
    name: str | None = None
    price: str | None = None
    currency: str | None = None
    stock: str | None = None


@attr.s
class AttrsProduct:
    name = attr.ib(default=None)
    price = attr.ib(default=None)
    currency = attr.ib(default=None)
    stock = attr.ib(default=None)


ITEM_CLASSES = [pytest.param(dict, id="dict"), pytest.param(Product, id="dataclass")]


def _asdict(item: Any) -> dict[str, Any]:
    return ItemAdapter(item).asdict()


def test_processor_failure_rolls_back_added_values() -> None:
    loader = ItemLoader()
    loader.add_value("name", "Color TV")
    stock_processor = FailingProcessor()
    with pytest.raises(ValueError):
        with loader.transaction():
            loader.add_value("price", "1200")
            loader.add_value("currency", "USD")
            loader.add_value("stock", "7", MapCompose(stock_processor))
    assert loader.get_collected_values("price") == []
    assert loader.get_collected_values("currency") == []
    assert loader.get_collected_values("stock") == []
    assert loader.get_collected_values("name") == ["Color TV"]
    assert stock_processor.calls == 1

    loader.add_value("stock", "7", MapCompose(stock_processor))
    assert loader.load_item() == {"name": ["Color TV"], "stock": ["7"]}


def test_loader_is_reusable_after_a_failed_transaction() -> None:
    loader = ItemLoader()
    for price, currency, stock in [("10", "EUR", "1"), ("20", "USD", None)]:
        try:
            with loader.transaction():
                loader.add_value("price", price)
                loader.add_value("currency", currency)
                if stock is None:
                    raise RuntimeError("missing stock")
                loader.add_value("stock", stock)
        except RuntimeError:
            continue
    assert loader.load_item() == {"price": ["10"], "currency": ["EUR"], "stock": ["1"]}


@pytest.mark.parametrize("item_class", ITEM_CLASSES)
def test_failed_replace_keeps_the_previous_value(item_class: type) -> None:
    item = (
        item_class(name="Color TV")
        if item_class is not dict
        else {"name": "Color TV"}
    )
    loader = ItemLoader(item=item)
    loader.add_value("name", "v1")
    processor = FailingProcessor()
    with pytest.raises(ValueError):
        with loader.transaction():
            loader.replace_value("name", "v2", MapCompose(processor))
    assert loader.get_collected_values("name") == ["Color TV", "v1"]
    assert processor.calls == 1
    loader.replace_value("name", "v3", MapCompose(processor))
    assert loader.get_collected_values("name") == ["v3"]


def test_successful_transaction_commits() -> None:
    loader = ItemLoader()
    with loader.transaction():
        loader.add_value("price", "1200")
        loader.replace_value("price", "1300")
        loader.add_value("currency", "USD")
    assert loader.get_collected_values("price") == ["1300"]
    assert loader.get_collected_values("currency") == ["USD"]
    assert loader.load_item() == {"price": ["1300"], "currency": ["USD"]}


def test_nested_loader_shares_the_transaction_value_pool() -> None:
    selector = Selector(text=HTML)
    loader = ItemLoader(selector=selector)
    nested = loader.nested_css("header")
    with pytest.raises(RuntimeError):
        with loader.transaction():
            nested.add_css("name", "h1::text")
            loader.add_css("price", "#price::text")
            raise RuntimeError("boom")
    assert nested.get_collected_values("name") == []
    assert loader.get_collected_values("price") == []
    assert loader.get_collected_values("name") == []

    nested.add_css("name", "h1::text")
    loader.add_css("price", "#price::text")
    assert loader.load_item() == {"name": ["Color TV"], "price": ["$1200"]}


def test_nested_transaction_inner_success_is_bound_by_outer_failure() -> None:
    loader = ItemLoader()
    with pytest.raises(RuntimeError):
        with loader.transaction():
            loader.add_value("price", "1200")
            with loader.transaction():
                loader.add_value("currency", "USD")
            assert loader.get_collected_values("currency") == ["USD"]
            raise RuntimeError("outer boom")
    assert loader.get_collected_values("price") == []
    assert loader.get_collected_values("currency") == []


def test_inner_failure_does_not_discard_the_outer_changes() -> None:
    loader = ItemLoader()
    with loader.transaction():
        loader.add_value("price", "1200")
        with pytest.raises(RuntimeError):
            with loader.transaction():
                loader.add_value("currency", "USD")
                raise RuntimeError("inner boom")
        loader.add_value("stock", "7")
    assert loader.load_item() == {
        "price": ["1200"],
        "stock": ["7"],
    }


def test_stats_are_rolled_back_with_the_transaction() -> None:
    stats = Stats()
    selector = Selector(text=HTML)
    loader = ItemLoader(selector=selector, stats=stats)
    loader.add_css("name", "header h1::text")
    name_key = "parser/name/css/header h1::text"
    assert stats.values == Counter({name_key: 1})
    with pytest.raises(RuntimeError):
        with loader.transaction():
            loader.add_css("price", "#price::text")
            loader.add_css("missing", "#does-not-exist::text")
            raise RuntimeError("boom")
    assert stats.values == Counter({name_key: 1})

    loader.add_css("price", "#price::text")
    assert stats.values == Counter(
        {name_key: 1, "parser/price/css/#price::text": 1}
    )


def test_stats_from_nested_loader_are_rolled_back() -> None:
    stats = Stats()
    selector = Selector(text=HTML)
    loader = ItemLoader(selector=selector, stats=stats)
    nested = loader.nested_xpath("//header")
    with pytest.raises(RuntimeError):
        with loader.transaction():
            nested.add_xpath("name", "h1/text()")
            raise RuntimeError("boom")
    assert stats.values == Counter()


def test_failed_load_item_restores_item_fields_and_collected_values() -> None:
    output_processor = FailingOutputProcessor()

    class Loader(ItemLoader):
        price_out = output_processor

    loader = Loader(item={"name": "Color TV"})
    loader.add_value("currency", "USD")
    with pytest.raises(ValueError):
        with loader.transaction():
            loader.add_value("price", "1200")
            loader.load_item()
    assert output_processor.calls == 1
    assert "currency" not in loader.item
    assert loader.item == {"name": "Color TV"}
    assert loader.get_collected_values("price") == []
    assert loader.get_collected_values("currency") == ["USD"]

    loader.load_item()
    assert loader.item == {"name": ["Color TV"], "currency": ["USD"]}


@pytest.mark.parametrize(
    "item_class",
    ITEM_CLASSES + [pytest.param(AttrsProduct, id="attrs")],
)
def test_end_to_end_matches_the_control_run(item_class: type) -> None:
    records = [
        {"price": "100", "currency": "USD", "stock": "3"},
        {"price": "200", "currency": "EUR", "stock": "5"},
        {"price": "300", "currency": "JPY", "stock": "9"},
    ]

    def parse(fail: bool, use_transaction: bool) -> tuple[dict[str, Any], Counter[str]]:
        stats = Stats()
        stock_processor = FailingProcessor(failures=1 if fail else 0)
        output_processor = FailingOutputProcessor(failures=1 if fail else 0)

        class OneShotLoader(ItemLoader):
            stock_in = MapCompose(stock_processor)
            price_out = output_processor

        loader = OneShotLoader(item=item_class(), stats=stats)
        for index, record in enumerate(records):
            # A failed transaction leaves no trace, so the record is retried
            # once. Plain calls are not retried: their partial writes stay.
            attempts = 2 if use_transaction else 1
            for _attempt in range(attempts):
                try:
                    if use_transaction:
                        with loader.transaction():
                            loader.add_value("stock", record["stock"])
                            loader.add_value("price", record["price"])
                            loader.add_value("currency", record["currency"])
                            if fail and index == 1:
                                loader.load_item()
                    else:
                        loader.add_value("stock", record["stock"])
                        loader.add_value("price", record["price"])
                        loader.add_value("currency", record["currency"])
                        if fail and index == 1:
                            loader.load_item()
                except (RuntimeError, ValueError):
                    continue
                break
        return _asdict(loader.load_item()), stats.values

    # One failing processor (record 0) and one failing item assignment
    # (record 1), each caught once, then parsing continues on the same loader.
    failed_item, failed_stats = parse(fail=True, use_transaction=True)
    clean_item, clean_stats = parse(fail=False, use_transaction=True)
    assert failed_item == clean_item
    assert failed_stats == clean_stats
    assert failed_item["price"] == ["100", "200", "300"]
    assert failed_item["currency"] == ["USD", "EUR", "JPY"]
    assert failed_item["stock"] == ["3", "5", "9"]

    # Without transactions the same failure points keep their legacy behavior:
    # record 0 leaves nothing (the input processor failed first), but record 1
    # keeps the values collected before its load_item() failed.
    plain_item, _plain_stats = parse(fail=True, use_transaction=False)
    assert plain_item["price"] == ["200", "300"]
    assert plain_item["currency"] == ["EUR", "JPY"]
    assert plain_item["stock"] == ["5", "9"]


def test_non_transaction_calls_keep_the_legacy_behavior() -> None:
    processor = FailingProcessor()
    loader = ItemLoader()
    loader.add_value("name", "Color TV")
    with pytest.raises(ValueError):
        loader.add_value("stock", "7", MapCompose(processor))
    assert loader.get_collected_values("name") == ["Color TV"]
    assert loader.get_collected_values("stock") == []
    assert processor.calls == 1

    with pytest.raises(ValueError):
        loader.replace_value("name", "new", MapCompose(FailingProcessor()))
    assert loader.get_collected_values("name") == ["Color TV"]
    assert loader.load_item() == {"name": ["Color TV"]}


def test_user_side_effects_are_not_rolled_back() -> None:
    seen: list[Any] = []

    def side_effect(value: Any) -> Any:
        seen.append(value)
        raise RuntimeError("boom")

    loader = ItemLoader()
    with pytest.raises(ValueError):
        with loader.transaction():
            loader.add_value("name", "Color TV", MapCompose(side_effect))
    assert loader.get_collected_values("name") == []
    assert seen == ["Color TV"]


def test_transaction_started_on_a_nested_loader_uses_the_shared_pool() -> None:
    selector = Selector(text=HTML)
    loader = ItemLoader(selector=selector)
    nested = loader.nested_xpath("//header")
    with pytest.raises(RuntimeError):
        with nested.transaction():
            nested.add_xpath("name", "h1/text()")
            loader.add_css("price", "#price::text")
            raise RuntimeError("boom")
    assert nested.get_collected_values("name") == []
    assert loader.get_collected_values("price") == []

    nested.add_xpath("name", "h1/text()")
    assert loader.load_item() == {"name": ["Color TV"]}


def test_committed_transaction_stats_match_plain_calls() -> None:
    def collect(use_transaction: bool) -> Counter[str]:
        stats = Stats()
        loader = ItemLoader(selector=Selector(text=HTML), stats=stats)
        if use_transaction:
            with loader.transaction():
                loader.add_xpath("name", "//header/h1/text()")
                loader.add_css("missing", "#does-not-exist::text")
        else:
            loader.add_xpath("name", "//header/h1/text()")
            loader.add_css("missing", "#does-not-exist::text")
        return stats.values

    assert collect(use_transaction=True) == collect(use_transaction=False)


def test_the_transaction_stack_is_removed_after_the_block() -> None:
    loader = ItemLoader()
    with loader.transaction():
        loader.add_value("name", "Color TV")
    assert loader._transaction_stack == []
    try:
        with loader.transaction():
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert loader._transaction_stack == []


def test_keyboard_interrupt_also_triggers_a_rollback() -> None:
    loader = ItemLoader()
    with pytest.raises(KeyboardInterrupt):
        with loader.transaction():
            loader.add_value("name", "Color TV")
            raise KeyboardInterrupt
    assert loader.get_collected_values("name") == []


def test_failed_replace_xpath_keeps_the_previous_value_and_stats() -> None:
    stats = Stats()
    selector = Selector(text=HTML)
    loader = ItemLoader(selector=selector, stats=stats)
    loader.add_xpath("name", "//header/h1/text()")
    with pytest.raises(ValueError):
        with loader.transaction():
            loader.replace_xpath("name", "//p/text()", MapCompose(FailingProcessor()))
    assert loader.get_collected_values("name") == ["Color TV"]
    assert stats.values == Counter({"parser/name/xpath///header/h1/text()": 1})
