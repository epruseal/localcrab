"""Shared, dependency-light utilities reused across opencrab (and crabharness).

These helpers consolidate logic that was previously copy-pasted across modules
(timestamps, file hashing, slug generation). Keep this package free of heavy or
optional imports so any layer can depend on it.
"""
