.. currentmodule:: itemloaders

.. _transactions:

Transactions
============

When several fields are parsed together, a failing processor or a failing
assignment during :meth:`~ItemLoader.load_item` can leave a partially filled
Item Loader behind. The :meth:`~ItemLoader.transaction` context manager groups
the changes made inside it into an atomic unit::

    loader = ItemLoader(selector=selector)
    for page in pages:
        try:
            with loader.transaction():
                loader.add_value("price", page.price)
                loader.add_value("currency", page.currency)
                loader.add_value("stock", page.stock)
                loader.load_item()
        except ValueError:
            continue

If the ``with`` block raises, the exception is re-raised after the loader is
restored to the state it had when the block started, so parsing can continue
with the next record without creating a new loader. If the block completes
without an exception, the changes are kept.

What is covered
---------------

The transaction covers the state managed by the Item Loader:

-   the values collected by :meth:`~ItemLoader.add_value`,
    :meth:`~ItemLoader.add_xpath`, :meth:`~ItemLoader.add_css`,
    :meth:`~ItemLoader.add_jmes` and their ``replace_*`` counterparts,
    including the values shared with :ref:`nested loaders <nested-loaders>`;

-   the fields of the item that :meth:`~ItemLoader.load_item` assigns; and

-   the rule usage statistics passed through the ``stats`` parameter of
    :class:`ItemLoader`, see :ref:`rule-usage`.

Nothing is re-run: every processor is still called exactly once per ``add_*``
call. A failed :meth:`~ItemLoader.replace_value` (or any other ``replace_*``
method) therefore keeps the previous value instead of removing it first.

Both the ``add_*`` and ``replace_*`` methods and :meth:`~ItemLoader.load_item`
are covered, whether they are called on the loader or on one of its nested
loaders. Transactions can be nested: an inner transaction that completes
successfully keeps its changes only if every enclosing transaction also
completes successfully.

What is not covered
-------------------

Only the state managed by the Item Loader is restored. Side effects that user
code performs outside of it are not tracked, for example an input processor
appending to a log, incrementing a counter, or mutating an external object. If
you need those to be rolled back as well, handle them in your own code.

Calls made outside of a :meth:`~ItemLoader.transaction` block keep their
previous behavior, including leaving partially collected values behind when
a processor or an item assignment fails.
