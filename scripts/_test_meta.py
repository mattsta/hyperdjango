"""Shared real-metadata builder for tests.

Historically ~30 test files each hand-rolled a ``FakeMeta``/``MockMeta`` that set
``column_names`` as a plain Python list while leaving ``fields = {}``. That
diverges from the real ``hyperdjango.models.TableMeta`` contract, where
``column_names`` is a *property* derived from the ``fields`` dict of real
``FieldMeta`` objects, and ``writable_columns`` / ``pk_fields`` /
``get_fk_fields`` all read off ``fields`` too. A mock that drifts from the real
contract is exactly what let the ``pk_field`` / ``_admin_user`` regressions slip
through green.

These helpers build a REAL ``TableMeta`` populated with REAL ``FieldMeta``
entries, so tests exercise the actual metadata contract instead of a divergent
stand-in.
"""

from hyperdjango.models import FieldMeta, TableMeta


def make_table_meta(
    table: str,
    columns,
    *,
    pk: str = "id",
    auto_pk: bool = True,
) -> TableMeta:
    """Build a real ``TableMeta`` for ``table`` with the given column names.

    The first column matching ``pk`` becomes the primary key (and, when
    ``auto_pk``, the SERIAL auto field). ``column_names`` on the returned meta
    is the genuine derived property — not a hand-written list — and ``fields``
    is populated with real ``FieldMeta`` objects.
    """
    fields: dict[str, FieldMeta] = {}
    for col in columns:
        is_pk = col == pk
        fields[col] = FieldMeta(
            name=col,
            primary_key=is_pk,
            auto=is_pk and auto_pk,
        )
    if pk not in fields:
        raise ValueError(f"pk {pk!r} not present in columns {list(columns)!r}")
    return TableMeta(
        table=table,
        pk_field=pk,
        auto_field=pk if auto_pk else None,
        fields=fields,
    )


def make_model(
    table: str,
    columns,
    *,
    pk: str = "id",
    auto_pk: bool = True,
    class_name: str = "MockModel",
):
    """Return a lightweight model-like class exposing a real ``_meta``.

    Mirrors how a real ``Model`` subclass carries a ``TableMeta`` at
    ``cls._meta`` so callers that read ``Model._meta`` work unchanged, while the
    metadata itself is the real contract (see :func:`make_table_meta`).
    """
    meta = make_table_meta(table, columns, pk=pk, auto_pk=auto_pk)
    return type(class_name, (), {"_meta": meta})
