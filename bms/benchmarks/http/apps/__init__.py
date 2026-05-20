"""Identical benchmark apps across frameworks.

Every app exposes the same three endpoints so the comparison measures the
framework, not the workload:
  GET /health            -> readiness probe ({"ok": true})
  GET /json?n=<bytes>    -> JSON body {"data": "x" * n} (payload-size sweep)
  GET /plaintext         -> "Hello, World!" (tiny fixed response)
"""
