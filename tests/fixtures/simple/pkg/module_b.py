"""Module B: uses module_a."""

from pkg.module_a import Widget, make_widget

DEFAULT_NAME = "default"


def build_default() -> Widget:
    return make_widget(DEFAULT_NAME)
