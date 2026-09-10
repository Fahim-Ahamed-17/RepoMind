"""Module B: closes the cycle a.py opens -- see its own docstring."""

from cyclic.a import A


class B:
    """A widget that can produce an A."""

    def use_a(self) -> A:
        return A()
