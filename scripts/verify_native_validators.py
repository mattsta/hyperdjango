#!/usr/bin/env python3
"""Verify native Zig validators are wired into the validation core."""

from hyperdjango.validation.core import networks

print(f"networks._native: {networks._native}")

# Test that EmailStr validation goes through native
from hyperdjango.validation.core import BaseModel, EmailStr


class User(BaseModel):
    email: EmailStr


# Valid
u = User(email="test@example.com")
print(f"Valid email accepted: {u.email}")

# Invalid
try:
    User(email="not-email")
    print("ERROR: invalid email accepted!")
except (Exception,) as e:
    print(f"Invalid email rejected: {type(e).__name__}")

# Performance comparison
import time

N = 100_000
start = time.perf_counter()
for _ in range(N):
    User(email="alice@example.com")
elapsed = time.perf_counter() - start
print(f"\nEmail validation: {N / elapsed:.0f} ops/sec ({elapsed:.3f}s for {N})")
