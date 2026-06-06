──────────────────────────────── Overall Stats ─────────────────────────────────
Num Passed Tests : 3
Num Failed Tests : 1
Num Total  Tests : 4
──────────────────────────────────── Passes ────────────────────────────────────
>> Passed Requirement
assert answers match.
>> Passed Requirement
assert model changes match phone.Alarm.
>> Passed Requirement
obtain updated, removed phone.Alarm records using models.changed_records,
and assert 0 alarms were added or removed.
──────────────────────────────────── Fails ─────────────────────────────────────
>> Failed Requirement
assert set of updated alarm ids match exactly to
private_data.to_disable_alarm_ids (ignore order).
```python
with test(
    """
    assert set of updated alarm ids match exactly to
private_data.to_disable_alarm_ids (ignore order).
    """
):
```
----------
AssertionError:  {744} == {744, 745, 746, 743}

In right but not left:
[745, 746, 743]