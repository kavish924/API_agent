"""Two versions of a small package laid out like the real SDKs.

Like Stripe, public names are aliases into underscore-private modules and request
params are a TypedDict taken as `**params`. Like openai, a client reaches resources
through cached properties, and `types` holds TypedDict params and response models.

V2 makes one change of every kind the comparison reports, plus the kinds it must ignore.
"""

from __future__ import annotations

from pathlib import Path

V1: dict[str, str] = {
    "pkg/__init__.py": '''
from pkg._client import Client as Client
from pkg._other import Other as Other
from pkg._reversal import Reversal as Reversal
from pkg._errors import OldError as OldError
from pkg._missing import Gone as Gone
from pkg.params import RetrieveParams as RetrieveParams
from httpx import Timeout as Timeout

VERSION = "1.0.0"


def helper(a, b=1):
    """Add things."""
    return a + b


raise RuntimeError("this package must never be imported by the analyser")
''',
    "pkg/_base.py": '''
class BaseResource:
    def request(self, method, url):
        """Send a raw request."""

    def close(self): ...
''',
    "pkg/_compat.py": "from functools import cached_property as cached_property\n",
    "pkg/_types.py": "class NotGiven: ...\n\n\nNOT_GIVEN = NotGiven()\nnot_given = NotGiven()\n",
    "pkg/_client.py": '''
from typing_extensions import Unpack

from pkg._base import BaseResource
from pkg._compat import cached_property
from pkg._types import NOT_GIVEN, NotGiven
from pkg.params import RetrieveParams
from pkg.resources.chat import Chat


class Client(BaseResource):
    """The client."""

    def __init__(self, api_key: str, *, timeout: float | NotGiven = NOT_GIVEN) -> None: ...

    @cached_property
    def chat(self) -> Chat: ...

    def create(self, name: str, *, engine: str, retries: int = 2) -> dict: ...

    def retrieve(self, id: str, **params: Unpack[RetrieveParams]) -> dict: ...

    def legacy(self, x, **kwargs): ...
''',
    "pkg/_other.py": "from pkg._base import BaseResource\n\n\nclass Other(BaseResource): ...\n",
    "pkg/_reversal.py": (
        "from pkg._base import BaseResource\n\n\n"
        "class Reversal(BaseResource):\n    def retrieve(self, id): ...\n"
    ),
    "pkg/_errors.py": "class OldError(Exception): ...\n",
    "pkg/helpers.py": "def is_given(value): ...\n",
    "pkg/params.py": """
from typing_extensions import NotRequired, TypedDict


class RequestOptions(TypedDict):
    api_key: NotRequired[str]


class RetrieveParams(RequestOptions):
    expand: NotRequired[list[str]]
    payment_method_types: NotRequired[list[str]]
""",
    "pkg/resources/__init__.py": "",
    "pkg/resources/chat.py": '''
from pkg._types import NOT_GIVEN, NotGiven
from pkg.helpers import is_given


class Chat:
    def complete(self, *, model: str, engine: str | NotGiven = NOT_GIVEN, n: int = 1) -> str:
        """Create a completion."""
''',
    "pkg/types/__init__.py": (
        "from pkg.types.response import Response as Response\n"
        "from pkg.types.shared import Thing as Thing\n"
        "from pkg.types.tool_param import ToolParam as ToolParam\n"
    ),
    "pkg/types/tool_param.py": """
from typing_extensions import Required, TypedDict


class ToolParam(TypedDict, total=False):
    type: Required[str]
    display_width: int
    environment: str
""",
    "pkg/types/shared.py": "class Thing:\n    value: int\n",
    "pkg/types/response.py": "class Response:\n    id: str\n    client_secret: str\n",
}

V2: dict[str, str] = {
    **V1,
    # `Reversal` renamed, `OldError` removed, `helper` loses a parameter.
    "pkg/__init__.py": '''
from pkg._client import Client as Client
from pkg._other import Other as Other
from pkg._transfer_reversal import TransferReversal as TransferReversal
from pkg._missing import Gone as Gone
from pkg.params import RetrieveParams as RetrieveParams
from httpx import Timeout as Timeout

VERSION = "2.0.0"


def helper(a):
    """Add things."""
    return a
''',
    # `request` removed from the base class every resource inherits.
    "pkg/_base.py": "class BaseResource:\n    def close(self): ...\n",
    # `engine` renamed to `model`; `id` positional-only; placeholder default swapped;
    # `legacy` drops `x` but still takes **kwargs.
    "pkg/_client.py": '''
from typing_extensions import Unpack

from pkg._base import BaseResource
from pkg._compat import cached_property
from pkg._types import NotGiven, not_given
from pkg.params import RetrieveParams
from pkg.resources.chat import Chat


class Client(BaseResource):
    """The client."""

    def __init__(self, api_key: str, *, timeout: float | NotGiven = not_given) -> None: ...

    @cached_property
    def chat(self) -> Chat: ...

    def create(self, name: str, *, model: str, retries: int = 2) -> dict: ...

    def retrieve(self, id: str, /, **params: Unpack[RetrieveParams]) -> dict: ...

    def legacy(self, **kwargs): ...
''',
    "pkg/_transfer_reversal.py": (
        "from pkg._base import BaseResource\n\n\n"
        "class TransferReversal(BaseResource):\n    def retrieve(self, id): ...\n"
    ),
    # `payment_method_types` removed from the params TypedDict.
    "pkg/params.py": """
from typing_extensions import NotRequired, TypedDict


class RequestOptions(TypedDict):
    api_key: NotRequired[str]


class RetrieveParams(RequestOptions):
    expand: NotRequired[list[str]]
""",
    # New required `mode`; a placeholder default and a real default change (both ignored).
    "pkg/resources/chat.py": '''
from pkg._types import NotGiven, not_given


class Chat:
    def complete(
        self, *, model: str, engine: str | NotGiven = not_given, n: int = 3, mode: str
    ) -> str:
        """Create a completion."""
''',
    # `display_width` removed; `environment` now required.
    "pkg/types/tool_param.py": """
from typing_extensions import Required, TypedDict


class ToolParam(TypedDict, total=False):
    type: Required[str]
    environment: Required[str]
""",
    # A generated class became a type alias (ignored).
    "pkg/types/shared.py": "from typing import Union\n\nThing = Union[int, str]\n",
    # A response field removed (counted, not reported).
    "pkg/types/response.py": "class Response:\n    id: str\n",
}
del V2["pkg/_reversal.py"], V2["pkg/_errors.py"]


def write_tree(root: Path, files: dict[str, str]) -> Path:
    """Write a package's source files under `root` and return `root`."""
    for relative, source in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source.lstrip("\n"), encoding="utf-8")
    return root
