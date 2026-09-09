"""Module A: a class and a couple of functions."""


class Widget:
    """A simple widget."""

    count: int = 0

    def __init__(self, name: str) -> None:
        self.name = name

    def render(self) -> str:
        """Render the widget as text."""
        return f"Widget({self.name})"


def make_widget(name: str) -> Widget:
    """Factory for a Widget."""
    return Widget(name)


def _private_helper() -> None:
    pass
