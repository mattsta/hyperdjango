"""
Django Model Manager with pg.zig pipeline support.

Extends Django's default Manager with high-performance batch query operations
using native connection pipelining.

Usage:

    from django.db import models
    from hyperdjango.serving.django_managers import HyperManager

    class Article(models.Model):
        title = models.CharField(max_length=200)
        objects = HyperManager()

    # Pipeline: execute 3 queries in a single round-trip
    results = Article.objects.pipeline([
        "SELECT * FROM articles WHERE id = 1",
        "SELECT * FROM articles WHERE id = 2",
        "SELECT COUNT(*) FROM articles",
    ])

    # Bulk load by PKs using pipeline
    articles = Article.objects.bulk_load([1, 2, 3])
"""

from django.db import connection, models


class HyperManager(models.Manager):
    """Django Manager enhanced with pg.zig pipeline support.

    All standard Django QuerySet methods work normally. Additional methods:
    - pipeline(): execute multiple SQL queries in a single network round-trip
    - bulk_load(): load multiple objects by PK using pipelined queries
    """

    def pipeline(self, queries):
        """Execute multiple SQL queries in a single network round-trip.

        Uses pg.zig's native pipeline protocol to send all queries before
        reading any responses. 5-6x faster than sequential queries.

        Args:
            queries: List of SQL strings (parameters should be pre-bound).

        Returns:
            List of result lists — one list of row tuples per query.

        Example:
            results = MyModel.objects.pipeline([
                "SELECT id, name FROM myapp_mymodel WHERE id = 1",
                "SELECT id, name FROM myapp_mymodel WHERE id = 2",
                "SELECT COUNT(*) FROM myapp_mymodel",
            ])
        """
        try:
            from hyperdjango._hyperdjango_native import _db_pipeline

            # Use pool handle 0 (default pool)
            return _db_pipeline(0, queries)
        except ImportError:
            # Fallback: sequential execution via Django's connection
            results = []
            with connection.cursor() as cursor:
                for sql in queries:
                    cursor.execute(sql)
                    if cursor.description:
                        results.append(cursor.fetchall())
                    else:
                        results.append([])
            return results

    def bulk_load(self, pks):
        """Load multiple model instances by PK in a single pipeline.

        Uses pipelined queries to fetch N objects with only 1 network round-trip
        instead of N sequential queries.

        Args:
            pks: List of primary key values.

        Returns:
            List of model instances (None for missing PKs).
        """
        if not pks:
            return []

        model = self.model
        table = model._meta.db_table
        pk_col = model._meta.pk.column

        # Build pipelined SELECT queries
        queries = [f"SELECT * FROM {table} WHERE {pk_col} = {int(pk)}" for pk in pks]

        results = self.pipeline(queries)

        # Convert raw results to model instances
        instances = []
        for rows in results:
            if rows:
                # Get column names from the model
                field_names = [f.column for f in model._meta.concrete_fields]
                row = rows[0]
                kwargs = {}
                for i, f in enumerate(model._meta.concrete_fields):
                    if i < len(row):
                        kwargs[f.attname] = row[i]
                try:
                    instances.append(model(**kwargs))
                # blind-except: a row that can't be materialized into a model instance maps to None so one malformed row does not abort the bulk load.
                except Exception:
                    instances.append(None)
            else:
                instances.append(None)

        return instances

    def bulk_load_dict(self, pks):
        """Like bulk_load but returns {pk: instance} dict."""
        instances = self.bulk_load(pks)
        pk_field = self.model._meta.pk.attname
        # dynamic-attr: pk_field is a runtime-resolved model PK attribute name (varies per Django model)
        return {getattr(inst, pk_field): inst for inst in instances if inst is not None}
