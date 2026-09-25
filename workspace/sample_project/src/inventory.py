"""A minimal in-memory inventory."""

from utils import format_price


class Inventory:
    def __init__(self) -> None:
        self._items: dict[str, tuple[int, float]] = {}

    def add_item(self, name: str, quantity: int, price: float) -> None:
        # TODO: validate that quantity and price are positive
        current_qty, _ = self._items.get(name, (0, price))
        self._items[name] = (current_qty + quantity, price)

    def remove_item(self, name: str, quantity: int) -> None:
        current_qty, price = self._items[name]
        if quantity > current_qty:
            raise ValueError(f"Not enough {name} in stock")
        self._items[name] = (current_qty - quantity, price)

    def total_value(self) -> float:
        return sum(qty * price for qty, price in self._items.values())

    def report(self) -> str:
        return "\n".join(
            f"{name}: {qty} @ {format_price(price)}" for name, (qty, price) in sorted(self._items.items())
        )
