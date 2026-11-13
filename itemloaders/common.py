"""Common functions used in Item Loaders code"""

from __future__ import annotations

from collections.abc import Awaitable
from functools import lru_cache, partial, update_wrapper
from typing import TYPE_CHECKING, Any

from itemloaders.utils import get_func_args

if TYPE_CHECKING:
    from collections.abc import Callable, MutableMapping


async def _await_if_needed(value: Any) -> Any:
    """Await *value* when it is awaitable, return it unchanged otherwise."""
    if isinstance(value, Awaitable):
        value = await value
        # A coroutine may itself resolve to another awaitable (e.g. a Future).
        return await _await_if_needed(value)
    return value


class AsyncProcessorError(TypeError):
    """Raised when an awaitable reaches a synchronous loader entry point."""


def _reject_awaitable(value: Any) -> None:
    """Reject awaitable values reached from a synchronous entry point.

    Synchronous code paths must never drive an event loop themselves. When an
    awaitable leaks into a synchronous call, close it when it is a coroutine so
    no un-awaited coroutine warning (and no pending work) is left behind, and
    raise a clear :class:`TypeError` pointing at the async entry points.
    """
    if isinstance(value, Awaitable):
        close = getattr(value, "close", None)
        if close is not None:
            close()
        raise AsyncProcessorError(
            "An async processor returned an awaitable value from a "
            "synchronous method. Use the asynchronous entry points "
            "(async_get_value, async_add_value, async_replace_value, "
            "async_load_item) from within a running event loop instead."
        )


@lru_cache(maxsize=1024)
def _takes_loader_context(function: Callable[..., Any]) -> bool:
    return "loader_context" in get_func_args(function)


def wrap_loader_context(
    function: Callable[..., Any], context: MutableMapping[str, Any]
) -> Callable[..., Any]:
    """Wrap functions that receive loader_context to contain the context
    "pre-loaded" and expose an interface that receives only one argument
    """
    try:
        takes_loader_context = _takes_loader_context(function)
    except TypeError:  # unhashable function
        takes_loader_context = "loader_context" in get_func_args(function)
    if takes_loader_context:
        wrapper = partial(function, loader_context=context)
        update_wrapper(wrapper, function)
        return wrapper
    return function
