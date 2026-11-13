.. _transactions:

============
Transactions
============

.. versionadded:: VERSION

When a single record is populated through several loader calls, a failure
halfway through can leave earlier values already collected. Wrapping the
block in :meth:`ItemLoader.transaction` makes the group all-or-nothing: if
an exception escapes the block, the collected values, the item fields and
the :ref:`rule-usage <rule-usage>` stats are restored to the state they had
when the transaction started, and the same loader instance remains usable
for the next record::

    for record in records:
        try:
            with loader.transaction():
                loader.add_value("price", record["price"])
                loader.add_value("currency", record["currency"])
                loader.add_value("stock", record["stock"])
                item = loader.load_item()
        except ValueError:
            continue  # the failed record left no trace on the loader

Transactions started on :ref:`nested loaders <nested-loaders>` cover the
value pool shared with their parent loader, and transactions can be nested:
an inner transaction that exits cleanly is still rolled back if an outer
transaction fails.

Only state managed by the loader itself is rolled back. Side effects of
user callbacks (e.g. processors writing to files, sockets or databases) are
outside the scope of this guarantee.
