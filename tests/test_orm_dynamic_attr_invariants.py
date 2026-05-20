"""Invariants that the ORM getattr/setattr enforcement sweep relied on.

Several dynamic-attr removals in models.py / query.py / public_id.py replaced a
defensive ``getattr(obj, "x", default)`` with direct attribute access. Each such
removal is only behavior-preserving because a static fact holds — that a field
is a *declared* dataclass field, that a dataclass is *not frozen*, or that a
registry only ever holds a particular type. These tests pin those facts so a
future refactor that breaks one fails loudly here instead of silently
reintroducing the bug the ``getattr`` default used to paper over.
"""

from __future__ import annotations

import dataclasses as dc

from hyperdjango.models import TableMeta
from hyperdjango.public_id import IDConfig, IDManager, KeySlot
from hyperdjango.validation.core import FieldInfo


def test_tablemeta_declares_cache_ttl_and_routing_fields():
    """query.py reads ``self._model._meta.cache_ttl`` directly (was a getattr
    with a None default). Direct access is only correct because TableMeta
    declares these as real fields (each defaulting to None)."""
    names = {f.name for f in dc.fields(TableMeta)}
    assert {"cache_ttl", "database", "pk_field", "auto_field"} <= names


def test_fieldinfo_declares_db_metadata_fields():
    """models.py DDL helpers (_field_to_sql_type / _auto_pk_sql_type /
    _fk_column_sql_type / vector_columns) now guard with isinstance(FieldInfo)
    and read these attributes directly instead of getattr(default). That is
    behavior-preserving only if they are declared FieldInfo fields."""
    names = {f.name for f in dc.fields(FieldInfo)}
    assert {"db_type", "vector_dimensions", "big", "custom_field"} <= names


def test_idconfig_and_idmanager_are_not_frozen():
    """public_id.py __post_init__ hooks switched from object.__setattr__ to
    plain ``self.x = ...``. That is only valid because neither dataclass is
    frozen."""
    assert dc.fields(IDConfig)  # sanity: it is a dataclass
    assert IDConfig.__dataclass_params__.frozen is False
    assert IDManager.__dataclass_params__.frozen is False


def test_idconfig_post_init_normalizes_string_hmac_keys():
    """Exercises the converted direct assignment in IDConfig.__post_init__:
    string hmac_keys are normalized to KeySlot in place."""
    cfg = IDConfig(alphabet="abcdefghijklmnopqrstuvwxyz", hmac_keys=["secret-key"])
    assert cfg.hmac_keys
    assert all(isinstance(k, KeySlot) for k in cfg.hmac_keys)


def test_model_registry_entries_all_carry_a_tablemeta():
    """models.py _fk_column_sql_type dropped a ``getattr(target_cls, "_meta",
    None) is not None`` guard because _register_model only ever stores concrete
    Model classes, each of which is registered *after* ModelMeta assigns a
    TableMeta. Assert the registry upholds that so the direct ``target_cls._meta``
    stays safe."""
    from hyperdjango.query import _model_registry, _model_registry_lock

    with _model_registry_lock:
        snapshot = list(_model_registry.values())
    for cls in snapshot:
        meta = cls._meta
        assert isinstance(meta, TableMeta), (
            f"{cls!r} is registered but its _meta is {type(meta)!r}, not TableMeta"
        )
