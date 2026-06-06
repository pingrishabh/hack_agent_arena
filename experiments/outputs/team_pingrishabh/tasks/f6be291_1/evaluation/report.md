──────────────────────────────── Overall Stats ─────────────────────────────────
Num Passed Tests : 4
Num Failed Tests : 4
Num Total  Tests : 8
──────────────────────────────────── Passes ────────────────────────────────────
>> Passed Requirement
assert model changes match amazon.Order, amazon.Product, file_system.File
gmail.Attachment, file_system.Directory, ignoring amazon.CartEntry,
amazon.Address.
>> Passed Requirement
assert 1 record has been added to amazon.Order using models.changed_records.
>> Passed Requirement
assert the order's delivery address is main_user.home_address.text
(normalize_text=True).
>> Passed Requirement
assert 0 records have been updated or deleted from amazon.Address using
models.changed_records.
──────────────────────────────────── Fails ─────────────────────────────────────
>> Failed Requirement
assert answers match.
```python
with test(
    """
    assert answers match.
    """
):
    test.answer(predicted_answer, ground_truth_answer)
```
----------
AssertionError:  '<<not_given>>' == 'null'
>> Failed Requirement
assert the product_ids from this new order matches key of
private_data.checklist_product_id_to_quantity.
ignoring the order.
```python
with test(
    """
    assert the product_ids from this new order matches key of
private_data.checklist_product_id_to_quantity.
    ignoring the order.
    """
):
    order = added_orders[0]
    test.case(
```
----------
AssertionError:  [457, 1206] == [322, 563, 666, 2389]

In left but not right:
[457, 1206]

In right but not left:
[322, 563, 666, 2389]

Original values:
[457, 1206] == [666, 2389, 322, 563]
>> Failed Requirement
assert the quantities from this new order matches value of
private_data.checklist_product_id_to_quantity.
ignoring the order.
```python
with test(
    """
    assert the quantities from this new order matches value of
private_data.checklist_product_id_to_quantity.
    ignoring the order.
    """
):
    test.case(
        list_of(order.order_items, "ordered_quantity"),
```
----------
AssertionError:  [1, 1] == [1, 1, 2, 3]

In right but not left:
[2, 3]

Original values:
[1, 1] == [1, 3, 2, 1]
>> Failed Requirement
assert the product_id_to_quantity from this new order matches
private_data.checklist_product_id_to_quantity.
```python
with test(
    """
    assert the product_id_to_quantity from this new order matches
private_data.checklist_product_id_to_quantity.
    """
):
    order_product_id_to_quantity = dict_of(order.order_items, "product_id",
"ordered_quantity")
    test.case(order_product_id_to_quantity, "==",
private_data.checklist_product_id_to_quantity)
```
----------
AssertionError:  {457: 1, 1206: 1} == Munch({666: 1, 2389: 3, 322: 2, 563: 1})