"""Explicit drop sentinel shared by loaders and processors."""

from __future__ import annotations


class _Drop:
    """Sentinel meaning that a value must be dropped instead of collected.

    Unlike ``None``, an empty string, ``0`` or ``False``, which are all
    regular data values, :data:`DROP` explicitly means "do not produce a
    value".
    """

    _instance: _Drop | None = None

    def __new__(cls) -> _Drop:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "DROP"

    def __bool__(self) -> bool:
        return False


DROP = _Drop()
"""Sentinel returned by processors to explicitly drop a value.

It is the only value that means "no value is produced". ``None``, empty
strings, ``0``, ``False`` and empty lists remain ordinary data and are kept
when a loader runs in explicit drop mode.
"""

#: Context key carrying the explicit drop mode to processors and nested loaders.
EXPLICIT_DROP_CONTEXT_KEY = "explicit_drop"
