from inventory import Inventory


def test_total_value():
    inventory = Inventory()
    inventory.add_item("widget", 3, 2.50)
    assert inventory.total_value() == 7.50
