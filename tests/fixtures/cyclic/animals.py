"""A three-level inheritance chain, unrelated to a.py/b.py's import cycle
-- for exercising multi-hop INHERITS traversal specifically. Not itself
circular: real Python inheritance cannot be (MRO would fail to resolve).
"""


class Animal:
    """Base class for every animal in this fixture."""

    def speak(self) -> str:
        return "..."


class Dog(Animal):
    """A dog is an animal."""

    def speak(self) -> str:
        return "Woof"


class Puppy(Dog):
    """A puppy is a dog, which is an animal -- two INHERITS hops from
    Animal."""

    def speak(self) -> str:
        return "Yip"
