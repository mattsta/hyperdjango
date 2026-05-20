#!/usr/bin/env python3
"""Verify _db_query and _db_execute are exported from the native extension."""

from hyperdjango._hyperdjango_native import (
    _db_configure,
    _db_execute,
    _db_query,
)

print("_db_configure:", _db_configure)
print("_db_query:", _db_query)
print("_db_execute:", _db_execute)
print()
print("All database functions exported successfully!")
print()
print("To test against a real database:")
print("  _db_configure('postgresql://user:pass@localhost:5432/dbname', 4)")
print("  rows = _db_query('SELECT 1 AS num', [])")
print("  print(rows)  # [(1,)]")
