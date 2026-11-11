"""Asynchronous support for :class:`~itemloaders.ItemLoader`.

The asynchronous entry points of the loader use the helpers here to run
processor chains that may mix regular callables and ``async`` callables.

Synchronous code must never run an event loop itself: if a synchronous entry
point meets an awaitable, it raises :class:`TypeError` instead. The helpers in
this module also make sure that a failure or a cancellation closes every
coroutine that will not be awaited, so no task is left dangling.
"""

from __future__ import annotations

import inspect
from collections import ChainMap
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from itemloaders.common import wrap_loader_context
from itemloaders.utils import arg_to_iter

if TYPE_CHECKING:
    from collections.abc import Callable, MutableMapping


def _is_map_compose(processor: Any) -> bool:
    from itemloaders.processors import MapCompose  # noqa: PLC0415

    return isinstance(processor, MapCompose)


def _is_compose(processor: Any) -> bool:
    from itemloaders.processors import Compose  # noqa: PLC0415

    return isinstance(processor, Compose)


async def call_processor(
    processor: Any, value: Any, context: MutableMapping[str, Any]
) -> Any:
    """Invoke *processor* asynchronously.

    :class:`~itemloaders.processors.MapCompose` and
    :class:`~itemloaders.processors.Compose` run through their own
    asynchronous orchestration so ``async`` functions can be mixed into the
    chain. Any other callable is invoked directly, awaiting its result when
    it returns an awaitable.
    """
    if _is_map_compose(processor):
        return await run_map_compose(processor, value, context)
    if _is_compose(processor):
        return await run_compose(processor, value, context)
    wrapped = wrap_loader_context(processor, context)
    return await resolve_awaitables(wrapped(value))


def close_awaitable(value: Any) -> None:
    """Best-effort close of an awaitable that is not going to be awaited."""
    close = getattr(value, "close", None)
    if callable(close):
        with suppress(Exception):
            close()


def close_awaitables(value: Any, seen: set[int] | None = None) -> None:
    """Close every awaitable reachable from *value*, recursively.

    Awaitables are traversed too: a failed coroutine may hold further
    coroutines in its frame locals, and simply closing the outer one leaves
    the inner ones never awaited.
    """
    if seen is None:
        seen = set()
    if value is seen:
        return
    value_id = id(value)
    if value_id in seen:
        return
    seen.add(value_id)
    if inspect.isawaitable(value):
        frame = getattr(value, "cr_frame", None) or getattr(value, "ag_frame", None)
        if frame is not None:
            for referenced in frame.f_locals.values():
                close_awaitables(referenced, seen)
        close_awaitable(value)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            close_awaitables(item, seen)
    elif isinstance(value, dict):
        for item in value.values():
            close_awaitables(item, seen)


async def resolve_awaitables(value: Any) -> Any:
    """Await every awaitable reachable from *value*, recursively.

    If resolving fails or is cancelled partway, every awaitable that was not
    reached yet is closed, so nothing is left dangling.
    """
    if inspect.isawaitable(value):
        try:
            return await value
        except BaseException as e:
            # The failed awaitable may itself hold further awaitables (e.g. an
            # async processor wrapping the coroutine of a previous stage).
            # Once a coroutine has started, its frame moves to the exception
            # traceback, so the frames there are traversed as well.
            seen: set[int] = set()
            close_awaitables(value, seen)
            tb = e.__traceback__
            while tb is not None:
                for referenced in tb.tb_frame.f_locals.values():
                    close_awaitables(referenced, seen)
                tb = tb.tb_next
            raise
    if isinstance(value, (list, tuple, set)):
        resolved = []
        items = list(value)
        try:
            for item in items:
                resolved.append(await resolve_awaitables(item))
        except BaseException:
            for item in items[len(resolved) :]:
                close_awaitables(item)
            raise
        return type(value)(resolved)
    if isinstance(value, dict):
        resolved = {}
        items = list(value.items())
        try:
            for key, item in items:
                resolved[key] = await resolve_awaitables(item)
        except BaseException:
            for _, item in items[len(resolved) :]:
                close_awaitables(item)
            raise
        return resolved
    return value


def _chain_functions(
    processor: Any, context: MutableMapping[str, Any]
) -> tuple[Callable[..., Any], ...]:
    """Return the context-wrapped functions of a
    :class:`~itemloaders.processors.MapCompose` or
    :class:`~itemloaders.processors.Compose` processor."""
    chained: MutableMapping[str, Any]
    if context:
        chained = ChainMap(context, processor.default_loader_context)
    else:
        chained = processor.default_loader_context
    return tuple(wrap_loader_context(func, chained) for func in processor.functions)


async def run_map_compose(
    processor: Any, value: Any, context: MutableMapping[str, Any]
) -> Any:
    """Asynchronous counterpart of calling a MapCompose processor.

    Each function is applied to every element of the current stage, in the
    same order as the synchronous implementation. Awaitable results are
    awaited before the next function runs, so regular and ``async`` functions
    can be mixed freely within the chain.
    """
    wrapped_funcs = _chain_functions(processor, context)
    values = arg_to_iter(value)
    for func in wrapped_funcs:
        next_values: list[Any] = []
        for element in values:
            try:
                result = func(element)
                next_values += arg_to_iter(await resolve_awaitables(result))
            except Exception as e:
                raise ValueError(
                    f"Error in MapCompose with "
                    f"{func!s} value={value!r} error='{type(e).__name__}: {e!s}'"
                ) from e
        values = next_values
    return values


async def run_compose(
    processor: Any, value: Any, context: MutableMapping[str, Any]
) -> Any:
    """Asynchronous counterpart of calling a Compose processor."""
    wrapped_funcs = _chain_functions(processor, context)
    for func in wrapped_funcs:
        if value is None and processor.stop_on_none:
            break
        try:
            value = await resolve_awaitables(func(value))
        except Exception as e:
            raise ValueError(
                f"Error in Compose with "
                f"{func!s} value={value!r} error='{type(e).__name__}: {e!s}'"
            ) from e
    return value
