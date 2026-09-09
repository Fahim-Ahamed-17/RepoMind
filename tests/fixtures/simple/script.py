"""A top-level script outside the package."""

from pkg.module_b import build_default


def main() -> None:
    widget = build_default()
    print(widget.render())


if __name__ == "__main__":
    main()
