"""Circular imports and an inheritance chain -- the graph edge cases
tree-sitter parses fine (it never executes anything) but that a naive
traversal could hang on. See docs/conventions.md's fixture plan and
tests/integration/test_cyclic.py.
"""
