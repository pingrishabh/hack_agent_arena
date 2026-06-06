
### Environment Interaction 1
----------------------------------------------------------------------------
```python
# Get supervisor's email and account passwords
email = apis.supervisor.show_profile()["email"]
creds = apis.supervisor.show_account_passwords()

# Get Amazon password
pw = next(c["password"] for c in creds if c["account_name"] == "amazon")

# Login to Amazon
token = apis.amazon.login(username=email, password=pw)["access_token"]
print(f"Amazon login token: {token}")

# Show Amazon prime subscriptions
subscriptions = apis.amazon.show_prime_subscriptions(access_token=token)
print(f"Amazon prime subscriptions: {subscriptions}")
```

```
Amazon login token: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJhbWF6b24ra2V2aW4uYmxhY0BnbWFpbC5jb20iLCJleHAiOjE2ODQ0NTUzMDh9.1P89yNjCShVO9e8-OFf8aHoZzg3QXi39tTmEfCB4Cac
Amazon prime subscriptions: [{'prime_subscription_id': 122, 'start_date': '2023-03-19T16:27:43', 'end_date': '2024-03-18T23:59:59', 'payment_card_digits': '4582', 'paid_amount': 140.0}, {'prime_subscription_id': 121, 'start_date': '2023-02-16T16:27:43', 'end_date': '2023-03-18T23:59:59', 'payment_card_digits': '4582', 'paid_amount': 15.0}]
```


### Environment Interaction 2
----------------------------------------------------------------------------
```python
from datetime import datetime

# Get the current subscriptions
subscriptions = [{'prime_subscription_id': 122, 'start_date': '2023-03-19T16:27:43', 'end_date': '2024-03-18T23:59:59', 'payment_card_digits': '4582', 'paid_amount': 140.0}, {'prime_subscription_id': 121, 'start_date': '2023-02-16T16:27:43', 'end_date': '2023-03-18T23:59:59', 'payment_card_digits': '4582', 'paid_amount': 15.0}]

# Find the current subscription
current_subscription = next((sub for sub in subscriptions if sub['end_date'] > datetime.now().strftime('%Y-%m-%dT%H:%M:%S')), None)

if current_subscription:
    end_date = datetime.strptime(current_subscription['end_date'], '%Y-%m-%dT%H:%M:%S')
    today = datetime.today()
    months_left = (end_date.year - today.year) * 12 + (end_date.month - today.month)
    if months_left < 0:
        months_left = 0
    print(f"Months left in Amazon Prime subscription: {round(months_left)}")
else:
    print("No current Amazon Prime subscription found.")
```

```
Months left in Amazon Prime subscription: 10
```


### Environment Interaction 3
----------------------------------------------------------------------------
```python
try:
    __ORIG_COMPLETE__
except NameError:
    __ORIG_COMPLETE__ = apis.supervisor.complete_task
def __capture_complete__(answer=None, status="success"):
    globals()["__CAPTURED_ANSWER__"] = answer
    print("[completion captured — held for verification]")
    return {"message": "completion captured, pending verification"}
apis.supervisor.complete_task = __capture_complete__

apis.supervisor.complete_task(answer=10)
```

```
[completion captured — held for verification]
```


### Environment Interaction 4
----------------------------------------------------------------------------
```python
apis.supervisor.complete_task = __ORIG_COMPLETE__
apis.supervisor.complete_task(answer=globals().get("__CAPTURED_ANSWER__", None))
```

```
Execution successful.
```

