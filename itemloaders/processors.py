"""
This module provides some commonly used processors for Item Loaders.

See documentation in :ref:`declaring-loaders`.
"""

from __future__ import annotations

import itertools
from collections import ChainMap
from functools import partial
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from itemloaders.common import wrap_loader_context
from itemloaders.utils import arg_to_iter

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, MutableMapping


@runtime_checkable
class BatchProcessor(Protocol):
    """Protocol for functions that process a batch of values at once.

    A batch processor receives a list of input values and must return an
    iterable with exactly one entry per input value, in the same order. Each
    entry maps to the outputs of the corresponding input value: ``None`` or
    an empty iterable means the input produced no output, a plain value means
    exactly one output, and an iterable of values means several outputs.

    Batch processors must not keep state between calls: every call receives a
    self-contained batch of values.

    Use :func:`batch` to mark a function as a batch processor, so that
    :class:`MapCompose` calls it once per batch of values instead of once per
    value.
    """

    def __call__(self, values: list[Any]) -> Iterable[Any]: ...


def batch(function: Callable[[list[Any]], Iterable[Any]]) -> BatchProcessor:
    """Mark *function* as a :class:`BatchProcessor` for :class:`MapCompose`.

    *function* must receive a list of values and return an iterable with one
    entry per input value, as described in :class:`BatchProcessor`. It can be
    used as a decorator::

        @batch
        def strip_all(values):
            return [value.strip() for value in values]
    """
    setattr(function, "__is_batch_processor__", True)
    return cast("BatchProcessor", function)


def _is_batch_processor(function: Callable[..., Any]) -> bool:
    # functools.partial does not forward arbitrary attributes, so a batch
    # processor wrapped with a loader context must be unwrapped first.
    while isinstance(function, partial):
        function = function.func
    return getattr(function, "__is_batch_processor__", False) is True


def _batched(values: Iterable[Any], size: int) -> Iterator[list[Any]]:
    """Yield lists of at most *size* values, consuming *values* only once."""
    iterator = iter(values)
    while batch_values := list(itertools.islice(iterator, size)):
        yield batch_values


class MapCompose:
    """
    A processor which is constructed from the composition of the given
    functions, similar to the :class:`Compose` processor. The difference with
    this processor is the way internal results are passed among functions,
    which is as follows:

    The input value of this processor is *iterated* and the first function is
    applied to each element. The results of these function calls (one for each element)
    are concatenated to construct a new iterable, which is then used to apply the
    second function, and so on, until the last function is applied to each
    value of the list of values collected so far. The output values of the last
    function are concatenated together to produce the output of this processor.

    Each particular function can return a value or a list of values, which is
    flattened with the list of values returned by the same function applied to
    the other input values. The functions can also return ``None`` in which
    case the output of that function is ignored for further processing over the
    chain.

    This processor provides a convenient way to compose functions that only
    work with single values (instead of iterables). For this reason the
    :class:`MapCompose` processor is typically used as input processor, since
    data is often extracted using the
    :meth:`~parsel.selector.Selector.extract` method of `parsel selectors`_,
    which returns a list of strings.

    The example below should clarify how it works:

    >>> def filter_world(x):
    ...     return None if x == 'world' else x
    ...
    >>> from itemloaders.processors import MapCompose
    >>> proc = MapCompose(filter_world, str.upper)
    >>> proc(['hello', 'world', 'this', 'is', 'something'])
    ['HELLO', 'THIS', 'IS', 'SOMETHING']

    As with the Compose processor, functions can receive Loader contexts, and
    ``__init__`` method keyword arguments are used as default context values.
    See :class:`Compose` processor for more info.

    Functions marked with :func:`batch` are treated as batch processors:
    instead of being called once per value, they are called once per batch of
    at most *batch_size* values with the whole batch as a single list. A batch
    processor returns one entry per input value (``None`` or an empty iterable
    to filter a value out, a plain value for one output, or an iterable for
    several outputs), and the outputs are flattened while preserving the input
    order. Ordinary single-value functions can be freely mixed with batch
    processors in the same chain.

    .. _`parsel selectors`: https://parsel.readthedocs.io/en/latest/parsel.html#parsel.selector.Selector.extract
    """

    def __init__(
        self,
        *functions: Callable[..., Any],
        batch_size: int = 100,
        **default_loader_context: Any,
    ):
        if batch_size < 1:
            raise ValueError(
                f"batch_size must be a positive integer, got {batch_size!r}"
            )
        self.functions = functions
        self.batch_size = batch_size
        self.default_loader_context = default_loader_context

    def __call__(
        self, value: Any, loader_context: MutableMapping[str, Any] | None = None
    ) -> Iterable[Any]:
        values = arg_to_iter(value)
        context: MutableMapping[str, Any]
        if loader_context:
            context = ChainMap(loader_context, self.default_loader_context)
        else:
            context = self.default_loader_context
        wrapped_funcs = [wrap_loader_context(f, context) for f in self.functions]
        for func in wrapped_funcs:
            if _is_batch_processor(func):
                values = self._run_batch(func, values)
                continue
            next_values: list[Any] = []
            for v in values:
                try:
                    next_values += arg_to_iter(func(v))
                except Exception as e:
                    raise ValueError(
                        f"Error in MapCompose with "
                        f"{func!s} value={value!r} error='{type(e).__name__}: {e!s}'"
                    ) from e
            values = next_values
        return values

    def _run_batch(
        self, func: Callable[..., Any], values: Iterable[Any]
    ) -> list[Any]:
        next_values: list[Any] = []
        offset = 0
        for batch_values in _batched(values, self.batch_size):
            batch_length = len(batch_values)
            batch_repr = repr(batch_values)
            try:
                results = list(func(batch_values))
            except Exception as e:
                raise ValueError(
                    f"Error in MapCompose batch processor with "
                    f"{func!s} values={batch_repr} "
                    f"error='{type(e).__name__}: {e!s}'"
                ) from e
            if len(results) != batch_length:
                raise ValueError(
                    f"Batch processor {func!s} returned {len(results)} results "
                    f"for {batch_length} input values at offset {offset}: "
                    f"{batch_repr}"
                )
            offset += batch_length
            for result in results:
                next_values += arg_to_iter(result)
        return next_values


class Compose:
    """
    A processor which is constructed from the composition of the given
    functions. This means that each input value of this processor is passed to
    the first function, and the result of that function is passed to the second
    function, and so on, until the last function returns the output value of
    this processor.

    By default, stop process on ``None`` value. This behaviour can be changed by
    passing keyword argument ``stop_on_none=False``.

    Example:

    >>> from itemloaders.processors import Compose
    >>> proc = Compose(lambda v: v[0], str.upper)
    >>> proc(['hello', 'world'])
    'HELLO'

    Each function can optionally receive a ``loader_context`` parameter. For
    those which do, this processor will pass the currently active :ref:`Loader
    context <loaders-context>` through that parameter.

    The keyword arguments passed in the ``__init__`` method are used as the default
    Loader context values passed to each function call. However, the final
    Loader context values passed to functions are overridden with the currently
    active Loader context accessible through the :attr:`ItemLoader.context
    <itemloaders.ItemLoader.context>` attribute.
    """

    def __init__(self, *functions: Callable[..., Any], **default_loader_context: Any):
        self.functions = functions
        self.stop_on_none = default_loader_context.get("stop_on_none", True)
        self.default_loader_context = default_loader_context

    def __call__(
        self, value: Any, loader_context: MutableMapping[str, Any] | None = None
    ) -> Any:
        context: MutableMapping[str, Any]
        if loader_context:
            context = ChainMap(loader_context, self.default_loader_context)
        else:
            context = self.default_loader_context
        wrapped_funcs = [wrap_loader_context(f, context) for f in self.functions]
        for func in wrapped_funcs:
            if value is None and self.stop_on_none:
                break
            try:
                value = func(value)
            except Exception as e:
                raise ValueError(
                    f"Error in Compose with "
                    f"{func!s} value={value!r} error='{type(e).__name__}: {e!s}'"
                ) from e
        return value


class TakeFirst:
    """
    Returns the first non-null/non-empty value from the values received,
    so it's typically used as an output processor to single-valued fields.
    It doesn't receive any ``__init__`` method arguments, nor does it accept Loader contexts.

    Example:

    >>> from itemloaders.processors import TakeFirst
    >>> proc = TakeFirst()
    >>> proc(['', 'one', 'two', 'three'])
    'one'
    """

    def __call__(self, values: Any) -> Any:
        for value in values:
            if value is not None and value != "":
                return value
        return None


class Identity:
    """
    The simplest processor, which doesn't do anything. It returns the original
    values unchanged. It doesn't receive any ``__init__`` method arguments, nor does it
    accept Loader contexts.

    Example:

    >>> from itemloaders.processors import Identity
    >>> proc = Identity()
    >>> proc(['one', 'two', 'three'])
    ['one', 'two', 'three']
    """

    def __call__(self, values: Any) -> Any:
        return values


class SelectJmes:
    """
    Query the data structure for a *JMESPath* expression (given at instantiation), and return the answer.

    *Requires*: `JMESPath <https://github.com/jmespath/jmespath>`__

    .. note::

        ``SelectJmes`` accepts only one input element at a time.

    Example:

    >>> from itemloaders.processors import SelectJmes, Compose, MapCompose
    >>> proc = SelectJmes("foo") #for direct use on lists and dictionaries
    >>> proc({'foo': 'bar'})
    'bar'
    >>> proc({'foo': {'bar': 'baz'}})
    {'bar': 'baz'}

    Working with JSON:

    >>> import json
    >>> proc_single_json_str = Compose(json.loads, SelectJmes("foo"))
    >>> proc_single_json_str('{"foo": "bar"}')
    'bar'
    >>> proc_json_list = Compose(json.loads, MapCompose(SelectJmes('foo')))
    >>> proc_json_list('[{"foo":"bar"}, {"baz":"tar"}]')
    ['bar']
    """

    def __init__(self, json_path: str):
        self.json_path: str = json_path
        import jmespath.parser  # noqa: PLC0415

        self.compiled_path: jmespath.parser.ParsedResult = jmespath.compile(
            self.json_path
        )

    def __call__(self, value: Any) -> Any:
        """Query value for the JMESPath query and return answer
        :param value: a data structure (dict, list) to extract from
        :return: Element extracted according to JMESPath query
        """
        return self.compiled_path.search(value)


class Join:
    """
    Returns the values joined with the separator given in the ``__init__`` method, which
    defaults to ``' '``. It doesn't accept Loader contexts.

    When using the default separator, this processor is equivalent to the
    function: ``' '.join``

    Examples:

    >>> from itemloaders.processors import Join
    >>> proc = Join()
    >>> proc(['one', 'two', 'three'])
    'one two three'
    >>> proc = Join('<br>')
    >>> proc(['one', 'two', 'three'])
    'one<br>two<br>three'
    """

    def __init__(self, separator: str = " "):
        self.separator = separator

    def __call__(self, values: Any) -> str:
        return self.separator.join(values)
