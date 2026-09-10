"""Embedder protocol and the local fastembed implementation.

RM-031 (M3). Runs entirely locally, CPU, no egress -- see AGENTS.md
invariant 1 and docs/design.md section 8. fastembed, not the originally
pinned sentence-transformers -- see embed/local.py's own module docstring
for why that was resolved before this ticket landed rather than after.
"""
