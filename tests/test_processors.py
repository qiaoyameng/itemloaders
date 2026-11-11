from functools import partial

import pytest

from itemloaders import DROP
from itemloaders.processors import Compose, Identity, Join, MapCompose, TakeFirst


def test_take_first():
    proc = TakeFirst()
    assert proc([None, "", "hello", "world"]) == "hello"
    assert proc([None, "", 0, "hello", "world"]) == 0


def test_take_first_drop_sentinel():
    proc = TakeFirst()
    assert proc([DROP, "", "hello"]) == "hello"
    assert proc([DROP, DROP]) is None


def test_take_first_explicit_drop_none():
    proc = TakeFirst(drop_none=False)
    assert proc([None, "", "hello"]) is None
    assert proc([DROP, None]) is None
    assert proc([DROP, "", 0]) == 0
    assert proc([DROP, DROP]) is None


def test_take_first_falsy_values_are_data():
    proc = TakeFirst(drop_none=False)
    assert proc(["", 0]) == 0
    assert proc([False, "x"]) is False
    assert proc([[]]) == []


def test_identity():
    proc = Identity()
    assert proc([None, "", "hello", "world"]) == [None, "", "hello", "world"]


def test_join():
    proc = Join()
    with pytest.raises(TypeError):
        proc([None, "", "hello", "world"])
    assert proc(["", "hello", "world"]) == " hello world"
    assert proc(["hello", "world"]) == "hello world"
    assert isinstance(proc(["hello", "world"]), str)


def test_compose():
    proc = Compose(lambda v: v[0], str.upper)
    assert proc(["hello", "world"]) == "HELLO"
    proc = Compose(str.upper)
    assert proc(None) is None
    proc = Compose(str.upper, stop_on_none=False)
    with pytest.raises(
        ValueError,
        match=r"Error in Compose with .* error='TypeError: (descriptor 'upper'|'str' object expected)",
    ):
        proc(None)
    proc = Compose(str.upper, lambda x: x + 1)
    with pytest.raises(
        ValueError,
        match=r"Error in Compose with .* error='TypeError: (can only|unsupported operand)",
    ):
        proc("hello")


def test_mapcompose():
    def filter_world(x):
        return None if x == "world" else x

    proc = MapCompose(filter_world, str.upper)
    assert proc(["hello", "world", "this", "is", "scrapy"]) == [
        "HELLO",
        "THIS",
        "IS",
        "SCRAPY",
    ]
    proc = MapCompose(filter_world, str.upper)
    assert proc(None) == []
    proc = MapCompose(filter_world, str.upper)
    with pytest.raises(
        ValueError,
        match=r"Error in MapCompose with .* error='TypeError: (descriptor 'upper'|'str' object expected)",
    ):
        proc([1])
    proc = MapCompose(filter_world, lambda x: x + 1)
    with pytest.raises(
        ValueError,
        match=r"Error in MapCompose with .* error='TypeError: (can only|unsupported operand)",
    ):
        proc("hello")


@pytest.mark.parametrize("processor", [Compose, MapCompose])
def test_partial_processor_keeps_context_with_matching_value(processor):
    def prefix_text(prefix, value, loader_context):
        return prefix + value + loader_context["suffix"]

    proc = processor(partial(prefix_text, "loader_context"))
    value = ["text"] if processor is MapCompose else "text"
    expected = (
        ["loader_contexttext!"] if processor is MapCompose else "loader_contexttext!"
    )
    assert proc(value, {"suffix": "!"}) == expected


def test_mapcompose_drop_sentinel():
    def maybe_drop(value):
        return DROP if value == "drop" else value

    proc = MapCompose(maybe_drop, str.upper)
    assert proc(["keep", "drop", "also"]) == ["KEEP", "ALSO"]
    assert proc(["drop"]) == []


def test_mapcompose_drop_in_input_is_skipped():
    calls = []

    def track(value):
        calls.append(value)
        return value

    proc = MapCompose(track)
    assert proc(["a", DROP, "b"]) == ["a", "b"]
    assert calls == ["a", "b"]


def test_mapcompose_drop_in_later_function():
    proc = MapCompose(str.upper, lambda value: DROP if value == "DROP" else value)
    assert proc(["keep", "drop"]) == ["KEEP"]


def test_mapcompose_drop_none_keeps_none():
    proc = MapCompose(lambda value: None, drop_none=False)
    assert proc(["a", "b"]) == [None, None]
    proc = MapCompose(lambda value: DROP, drop_none=False)
    assert proc(["a", "b"]) == []


def test_mapcompose_drop_none_legacy_default():
    proc = MapCompose(lambda value: None)
    assert proc(["a", "b"]) == []


def test_compose_drop_sentinel_stops_chain():
    calls = []

    def second(value):
        calls.append(value)
        return value

    proc = Compose(lambda value: DROP, second)
    assert proc("x") is DROP
    assert calls == []


def test_compose_drop_none_keeps_none_flowing():
    proc = Compose(
        lambda value: None,
        lambda value: "was none" if value is None else value,
        drop_none=False,
    )
    assert proc("x") == "was none"


def test_compose_drop_none_with_stop_on_none_false():
    proc = Compose(
        lambda value: None,
        lambda value: value,
        stop_on_none=False,
        drop_none=False,
    )
    assert proc("x") is None


def test_compose_drop_none_legacy_default():
    proc = Compose(lambda value: None, str.upper)
    assert proc("x") is None
