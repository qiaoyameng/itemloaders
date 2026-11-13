from __future__ import annotations

import pytest
from parsel import Selector

from itemloaders import ItemLoader, RecordCardinalityError
from itemloaders.processors import Identity, TakeFirst


def keep_priced(values):
    """Input processor that drops values that do not look like a price."""
    return [v for v in values if isinstance(v, str) and v.startswith("$")]


class QuoteLoader(ItemLoader):
    default_input_processor = Identity()
    default_output_processor = Identity()
    record_cardinality = {"name": "one", "price": "optional", "alias": "many"}

    price_in = keep_priced
    name_out = TakeFirst()
    price_out = TakeFirst()


# Three quotes from the same page: the middle one has no price (its raw
# value is filtered out by the price input processor), and the last one
# has two aliases.
QUOTE_ENTRIES = [
    ("quote-1", "name", "Alice"),
    ("quote-1", "price", "$10"),
    ("quote-2", "name", "Bob"),
    ("quote-2", "price", "N/A"),  # filtered out by price_in
    ("quote-3", "name", "Carol"),
    ("quote-3", "price", "$30"),
    ("quote-3", "alias", "C."),
    ("quote-3", "alias", "Carolita"),
]


def load_quotes_sequentially():
    loader = QuoteLoader()
    for record_id, field_name, value in QUOTE_ENTRIES:
        loader.add_value(field_name, value, record_id=record_id)
    return loader.load_records()


def test_records_grouped_by_record_id_not_position():
    records = load_quotes_sequentially()
    assert records == [
        {"name": "Alice", "price": "$10"},
        {"name": "Bob"},  # no placeholder price guessed for the gap
        {"name": "Carol", "price": "$30", "alias": ["C.", "Carolita"]},
    ]


def test_sequential_and_batch_append_produce_same_records():
    sequential = load_quotes_sequentially()

    batched = QuoteLoader()
    batched.add_record_values(QUOTE_ENTRIES)

    assert batched.load_records() == sequential


def test_multi_value_field_does_not_create_cartesian_product():
    records = load_quotes_sequentially()
    # Two aliases on quote-3 must not multiply it into extra records.
    assert len(records) == 3
    assert records[2]["alias"] == ["C.", "Carolita"]


def test_missing_required_field_error_identifies_record_and_field():
    class StrictPriceLoader(QuoteLoader):
        record_cardinality = {"name": "one", "price": "one"}

    loader = StrictPriceLoader()
    loader.add_record_values(QUOTE_ENTRIES)
    with pytest.raises(RecordCardinalityError) as excinfo:
        loader.load_records()
    message = str(excinfo.value)
    assert "record='quote-2'" in message
    assert "field='price'" in message


def test_multi_value_error_identifies_record_and_field():
    class SingleAliasLoader(QuoteLoader):
        record_cardinality = {"alias": "optional"}

    loader = SingleAliasLoader()
    loader.add_record_values(QUOTE_ENTRIES)
    with pytest.raises(RecordCardinalityError) as excinfo:
        loader.load_records()
    message = str(excinfo.value)
    assert "record='quote-3'" in message
    assert "field='alias'" in message


def test_empty_value_error_identifies_record_and_field():
    loader = QuoteLoader()
    loader.add_value("name", "", record_id="quote-1")
    with pytest.raises(RecordCardinalityError) as excinfo:
        loader.load_records()
    message = str(excinfo.value)
    assert "record='quote-1'" in message
    assert "field='name'" in message


def test_invalid_cardinality_rule():
    class BrokenLoader(QuoteLoader):
        record_cardinality = {"name": "single"}

    loader = BrokenLoader()
    loader.add_value("name", "Alice", record_id="quote-1")
    with pytest.raises(ValueError, match="Invalid record cardinality rule"):
        loader.load_records()


def test_replace_value_only_affects_its_record():
    loader = QuoteLoader()
    loader.add_record_values(QUOTE_ENTRIES)
    loader.replace_value("name", "Robert", record_id="quote-2")
    loader.replace_value("price", "$25", record_id="quote-2")
    records = loader.load_records()
    assert records[0]["name"] == "Alice"
    assert records[1] == {"name": "Robert", "price": "$25"}
    assert records[2]["name"] == "Carol"


def test_replace_value_on_unknown_record_creates_it():
    loader = QuoteLoader()
    loader.replace_value("name", "Alice", record_id="quote-1")
    assert loader.load_records() == [{"name": "Alice"}]


def test_add_value_with_none_field_name_and_record_id():
    loader = QuoteLoader()
    loader.add_value(None, {"name": "Alice", "price": "$10"}, record_id="quote-1")
    assert loader.load_records() == [{"name": "Alice", "price": "$10"}]


def test_nested_loader_shares_record_identity():
    html = (
        '<div class="quote" data-qid="quote-1">'
        "<p>Alice</p><span>$10</span>"
        "</div>"
    )
    loader = QuoteLoader(selector=Selector(text=html))
    nested = loader.nested_css("div.quote")
    nested.add_css("name", "p::text", record_id="quote-1")
    nested.add_css("price", "span::text", record_id="quote-1")
    assert loader.load_records() == [{"name": "Alice", "price": "$10"}]


def test_record_mode_does_not_leak_into_plain_mode():
    loader = QuoteLoader()
    loader.add_value("name", "page title")
    loader.add_value("name", "Alice", record_id="quote-1")
    assert loader.load_item() == {"name": "page title"}
    assert loader.load_records() == [{"name": "Alice"}]


def test_plain_mode_without_record_id_keeps_working():
    loader = QuoteLoader()
    loader.add_value("name", "Alice")
    loader.add_value("price", "$10")
    assert loader.load_item() == {"name": "Alice", "price": "$10"}
    assert loader.load_records() == []


def test_load_unknown_record():
    loader = QuoteLoader()
    with pytest.raises(KeyError, match="Unknown record"):
        loader.load_record("nope")


def test_get_record_collected_values():
    loader = QuoteLoader()
    loader.add_record_values(QUOTE_ENTRIES)
    assert loader.get_record_collected_values("quote-3", "alias") == [
        "C.",
        "Carolita",
    ]
    assert loader.get_record_collected_values("quote-2", "price") == []
    assert loader.get_record_collected_values("unknown", "name") == []


def test_record_output_processor_error_identifies_record_and_field():
    def broken_out(values):
        raise RuntimeError("boom")

    class BrokenOutLoader(QuoteLoader):
        name_out = broken_out

    loader = BrokenOutLoader()
    loader.add_value("name", "Alice", record_id="quote-1")
    with pytest.raises(ValueError, match=r"record='quote-1' field='name'"):
        loader.load_records()
