"""Module A: imports from B, which itself imports back from A -- real
Python would be unhappy about this ordering at runtime. RepoMind's parser
never executes anything, only parses syntax, so the circularity is fine
to index and is exactly the case worth indexing: real repositories do
have circular imports, deliberately or not.
"""

from cyclic.b import B


class A:
    """A widget that can produce a B."""

    def use_b(self) -> B:
        return B()
