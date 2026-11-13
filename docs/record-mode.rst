.. _record-mode:

Record mode
===========

.. versionadded:: VERSION

When a page contains several records (e.g. multiple quotes or product
offers), a common pattern is to extract each field separately and then
reassemble the records. If a field processor filters out a value for one
of the records, stitching the remaining values back together by position
silently misaligns the data.

Record mode avoids this by letting you attribute every collected value to
a stable *record id* of your choice, so values are grouped by identity
instead of by position::

    from itemloaders import ItemLoader
    from itemloaders.processors import TakeFirst

    class QuoteLoader(ItemLoader):
        record_cardinality = {"name": "one", "price": "optional"}
        name_out = TakeFirst()
        price_out = TakeFirst()

    loader = QuoteLoader()
    loader.add_value("name", "Alice", record_id="quote-1")
    loader.add_value("price", "$10", record_id="quote-1")
    loader.add_value("name", "Bob", record_id="quote-2")
    # quote-2 has no price: nothing is collected, nothing is guessed
    loader.load_records()
    # [{"name": "Alice", "price": "$10"}, {"name": "Bob"}]

All ``add_*`` and ``replace_*`` methods accept a ``record_id`` keyword
argument. Values added with a record id are stored per record and never
mix with the loader-wide collection used by :meth:`~ItemLoader.load_item`,
so plain (record-less) usage is unaffected.

Field input processors still see and may return zero to many values; the
results are appended to the field of the given record. Multi-valued fields
stay multi-valued within their own record — values from different fields
are never combined into a Cartesian product of records.

Cardinality rules
-----------------

The :attr:`ItemLoader.record_cardinality` class attribute declares how
many values each field may have per record:

- ``"one"``: exactly one non-empty value (``None`` and ``""`` count as
  empty),
- ``"optional"``: at most one value,
- ``"many"``: any number of values (the default for undeclared fields).

:meth:`~ItemLoader.load_records` and :meth:`~ItemLoader.load_record`
validate every record against these rules and raise
:class:`itemloaders.RecordCardinalityError` on violations. The error
message always identifies the offending record and field, e.g.::

    Missing value: record='quote-2' field='price' requires exactly 1 value, got 0

Batch and repeated collection
-----------------------------

:meth:`~ItemLoader.add_record_values` accepts an iterable of
``(record_id, field_name, value)`` tuples and is equivalent to calling
:meth:`~ItemLoader.add_value` once per entry, so sequential and batch
collection produce the same records.

``replace_*`` calls with a ``record_id`` only replace the data of that
record, leaving other records untouched.

Nested loaders share the record collection of their parent, so a nested
loader scoped to one record's HTML fragment can keep adding values to the
same record id.
