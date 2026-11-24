# ruff: noqa: E501  (the cases are code samples; long lines are part of the data)
"""Scanner eval cases (spec 08 F8.2–F8.4).

Each case is a tiny repository that uses a real SDK one way. Lines that the scanner
should flag end with `# expect`. The changes are the real ones between two releases:

- stripe 15.6.2 → 16.0.0: `Reversal` renamed to `TransferReversal`, `request()` removed
  from every resource, path parameters made positional-only, `payment_method_types`
  removed from PaymentIntent params;
- openai 2.0.0 → 2.54.0: three keys removed from `ComputerToolParam`, a type and a
  module removed from `openai.types.realtime`.

`in_scope=False` marks a pattern the scanner is not meant to handle (spec 04 out of
scope: wrapped clients and dynamic calls). Those are scored separately, as known limits.
`negative=True` marks a repository that uses the SDK but nothing that changed.
"""

from __future__ import annotations

from guardian.scan.evaluation import Case

SDKS: dict[str, dict[str, str]] = {
    "stripe": {"package": "stripe", "old": "15.6.2", "new": "16.0.0"},
    "openai": {"package": "openai", "old": "2.0.0", "new": "2.54.0"},
}

CASES: list[Case] = [
    # --- stripe, in scope ---------------------------------------------------------
    Case(
        "s01-direct-removed-class",
        "stripe",
        "direct import",
        True,
        {"app.py": 'import stripe\n\nreversal = stripe.Reversal.retrieve("trr_1")  # expect\n'},
    ),
    Case(
        "s02-from-import-removed-class",
        "stripe",
        "aliased import",
        True,
        {
            "app.py": (
                "from stripe import Reversal  # expect\n\n\n"
                "def load(reversal_id):\n"
                "    return Reversal.retrieve(reversal_id)  # expect\n"
            )
        },
    ),
    Case(
        "s03-module-alias",
        "stripe",
        "aliased import",
        True,
        {
            "app.py": "import stripe as st\n\nfor r in st.Reversal.list():  # expect\n    print(r.id)\n"
        },
    ),
    Case(
        "s04-removed-kwarg-one-line",
        "stripe",
        "direct import",
        True,
        {
            "pay.py": (
                "import stripe\n\n"
                'stripe.PaymentIntent.create(amount=1000, currency="usd", payment_method_types=["card"])  # expect\n'
            )
        },
    ),
    Case(
        "s05-removed-kwarg-multi-line",
        "stripe",
        "direct import",
        True,
        {
            "pay.py": (
                "import stripe\n\n\n"
                "def charge(amount):\n"
                "    return stripe.PaymentIntent.create(\n"
                "        amount=amount,\n"
                '        currency="eur",\n'
                '        payment_method_types=["card", "sepa_debit"],  # expect\n'
                "    )\n"
            )
        },
    ),
    Case(
        "s06-inherited-method-via-return-type",
        "stripe",
        "client variable",
        True,
        {
            "sync.py": (
                "import stripe\n\n"
                'customer = stripe.Customer.retrieve("cus_1")\n'
                'customer.request("get", "/v1/customers/cus_1/notes")  # expect\n'
            )
        },
    ),
    Case(
        "s07-stripeclient-positional-only",
        "stripe",
        "client variable",
        True,
        {
            "app.py": (
                "import stripe\n\n"
                'client = stripe.StripeClient("sk_test_123")\n'
                'client.v1.customers.retrieve(customer="cus_1")  # expect\n'
                'client.v1.customers.retrieve("cus_2")\n'
            )
        },
    ),
    Case(
        "s08-self-attribute",
        "stripe",
        "client variable",
        True,
        {
            "billing.py": (
                "import stripe\n\n\n"
                "class Billing:\n"
                "    def __init__(self, key):\n"
                "        self.stripe = stripe.StripeClient(key)\n\n"
                "    def remove(self, customer_id):\n"
                "        return self.stripe.v1.customers.delete(customer=customer_id)  # expect\n"
            )
        },
    ),
    Case(
        "s09-annotated-parameter",
        "stripe",
        "client variable",
        True,
        {
            "jobs.py": (
                "import stripe\n\n\n"
                "def cleanup(client: stripe.StripeClient, customer_id: str) -> None:\n"
                "    client.v1.customers.delete(customer=customer_id)  # expect\n"
            )
        },
    ),
    Case(
        "s10-class-level-annotation",
        "stripe",
        "client variable",
        True,
        {
            "svc.py": (
                "from stripe import StripeClient\n\n\n"
                "class Service:\n"
                "    client: StripeClient\n\n"
                "    def fetch(self, cid):\n"
                "        return self.client.v1.customers.retrieve(customer=cid)  # expect\n"
            )
        },
    ),
    Case(
        "s11-cross-module-client",
        "stripe",
        "cross-module",
        True,
        {
            "app/__init__.py": "",
            "app/clients.py": 'import stripe\n\nclient = stripe.StripeClient("sk_test_123")\n',
            "app/billing.py": (
                "from app.clients import client\n\n\n"
                "def find(cid):\n"
                "    return client.v1.customers.retrieve(customer=cid)  # expect\n"
            ),
        },
    ),
    Case(
        "s12-relative-cross-module-client",
        "stripe",
        "cross-module",
        True,
        {
            "shop/__init__.py": "",
            "shop/stripe_client.py": 'import stripe\n\nSTRIPE = stripe.StripeClient("sk_test_123")\n',
            "shop/orders.py": (
                "from .stripe_client import STRIPE\n\n\n"
                "def drop(cid):\n"
                "    STRIPE.v1.customers.delete(customer=cid)  # expect\n"
            ),
        },
    ),
    Case(
        "s13-classmethod-positional-only",
        "stripe",
        "direct import",
        True,
        {
            "coupons.py": 'import stripe\n\nstripe.Coupon.modify(id="co_1", name="Spring")  # expect\nstripe.Coupon.modify("co_2", name="Summer")\n'
        },
    ),
    Case(
        "s14-async-call",
        "stripe",
        "direct import",
        True,
        {
            "worker.py": (
                "import stripe\n\n\n"
                "async def load(reversal_id):\n"
                "    return await stripe.Reversal.retrieve_async(reversal_id)  # expect\n"
            )
        },
    ),
    Case(
        "s15-type-annotation",
        "stripe",
        "direct import",
        True,
        {
            "types.py": (
                "import stripe\n\n\n"
                "def describe(reversal: stripe.Reversal) -> str:  # expect\n"
                "    return reversal.id\n"
            )
        },
    ),
    # --- stripe, known limits --------------------------------------------------------
    Case(
        "k01-factory-function",
        "stripe",
        "wrapper",
        False,
        {
            "app.py": (
                "import stripe\n\n\n"
                "def get_client():\n"
                '    return stripe.StripeClient("sk_test_123")\n\n\n'
                'get_client().v1.customers.retrieve(customer="cus_1")  # expect\n'
            )
        },
    ),
    Case(
        "k02-getattr",
        "stripe",
        "dynamic",
        False,
        {
            "app.py": 'import stripe\n\nresource = getattr(stripe, "Reversal")  # expect\nresource.retrieve("trr_1")\n'
        },
    ),
    Case(
        "k03-params-dict-splat",
        "stripe",
        "dynamic",
        False,
        {
            "pay.py": (
                "import stripe\n\n"
                'params = {"amount": 100, "currency": "usd", "payment_method_types": ["card"]}  # expect\n'
                "stripe.PaymentIntent.create(**params)\n"
            )
        },
    ),
    Case(
        "k04-wrapper-class",
        "stripe",
        "wrapper",
        False,
        {
            "gateway.py": (
                "import stripe\n\n\n"
                "class Gateway:\n"
                "    def __init__(self):\n"
                '        self._client = stripe.StripeClient("sk_test_123")\n\n'
                "    def client(self):\n"
                "        return self._client\n\n\n"
                'Gateway().client().v1.customers.retrieve(customer="cus_1")  # expect\n'
            )
        },
    ),
    # --- stripe, negatives -------------------------------------------------------------
    Case(
        "n01-unchanged-stripe-usage",
        "stripe",
        "negative",
        True,
        {
            "app.py": (
                "import stripe\n\n"
                'client = stripe.StripeClient("sk_test_123")\n'
                'client.v1.customers.retrieve("cus_1")\n'
                'client.v1.customers.create(params={"email": "a@example.com"})\n'
                'stripe.Customer.create(email="a@example.com")\n'
                'stripe.PaymentIntent.create(amount=100, currency="usd")\n'
                'stripe.Coupon.modify("co_1", name="Spring")\n'
            )
        },
        negative=True,
    ),
    Case(
        "n02-same-names-not-stripe",
        "stripe",
        "negative",
        True,
        {
            "app.py": (
                "import stripe\n\n\n"
                "class Reversal:\n"
                "    def retrieve(self, x):\n"
                "        return x\n\n\n"
                "class Http:\n"
                "    def request(self, method, url):\n"
                "        return method, url\n\n\n"
                "def create(**kwargs):\n"
                "    return kwargs\n\n\n"
                'Reversal().retrieve("trr_1")\n'
                'Http().request("get", "/")\n'
                'create(payment_method_types=["card"], customer="cus_1")\n'
                "stripe.api_key = 'sk_test_123'\n"
            )
        },
        negative=True,
    ),
    Case(
        "n03-requests-library",
        "stripe",
        "negative",
        True,
        {
            "http.py": (
                "import requests\n"
                "import stripe\n\n"
                "session = requests.Session()\n"
                'session.request("get", "https://example.com")\n'
                'requests.request("post", "https://example.com", data={"customer": "cus_1"})\n'
                "stripe.Customer.list(limit=3)\n"
            )
        },
        negative=True,
    ),
    # --- openai, in scope ------------------------------------------------------------
    Case(
        "o01-removed-typeddict-keys",
        "openai",
        "client variable",
        True,
        {
            "agent.py": (
                "from openai import OpenAI\n\n"
                "client = OpenAI()\n"
                "client.responses.create(\n"
                '    model="computer-use-preview",\n'
                "    tools=[\n"
                "        {\n"
                '            "type": "computer_use_preview",\n'
                '            "display_width": 1024,  # expect\n'
                '            "display_height": 768,  # expect\n'
                '            "environment": "browser",  # expect\n'
                "        }\n"
                "    ],\n"
                '    input="Open the settings page.",\n'
                ")\n"
            )
        },
    ),
    Case(
        "o02-removed-type-import",
        "openai",
        "aliased import",
        True,
        {
            "realtime.py": "from openai.types.realtime import RealtimeSessionClientSecret  # expect\n\nSecret = RealtimeSessionClientSecret  # expect\n"
        },
    ),
    Case(
        "o03-removed-module-import",
        "openai",
        "aliased import",
        True,
        {
            "realtime.py": "from openai.types.realtime import realtime_session_client_secret  # expect\n"
        },
    ),
    Case(
        "o04-removed-module-plain-import",
        "openai",
        "direct import",
        True,
        {"realtime.py": "import openai.types.realtime.realtime_session_client_secret  # expect\n"},
    ),
    Case(
        "o05-async-client-keys",
        "openai",
        "client variable",
        True,
        {
            "agent.py": (
                "import openai\n\n\n"
                "async def run(client: openai.AsyncOpenAI):\n"
                "    return await client.responses.create(\n"
                '        model="computer-use-preview",\n'
                '        tools=[{"type": "computer_use_preview", "display_width": 800, "display_height": 600, "environment": "mac"}],  # expect\n'
                '        input="hi",\n'
                "    )\n"
            )
        },
    ),
    # --- openai, negatives -------------------------------------------------------------
    Case(
        "n04-unchanged-openai-usage",
        "openai",
        "negative",
        True,
        {
            "chat.py": (
                "from openai import OpenAI\n\n"
                "client = OpenAI()\n"
                'client.chat.completions.create(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])\n'
                "client.responses.create(\n"
                '    model="gpt-4o",\n'
                '    tools=[{"type": "function", "name": "lookup", "parameters": {}}],\n'
                '    input="hi",\n'
                ")\n"
            )
        },
        negative=True,
    ),
    Case(
        "n05-same-keys-not-openai",
        "openai",
        "negative",
        True,
        {
            "layout.py": (
                "import json\n"
                "import openai\n\n"
                'json.dumps({"display_width": 1024, "display_height": 768, "environment": "browser"})\n\n\n'
                "def layout(display_width, display_height):\n"
                "    return display_width * display_height\n\n\n"
                "layout(display_width=10, display_height=20)\n"
                "client = openai.OpenAI()\n"
            )
        },
        negative=True,
    ),
]
