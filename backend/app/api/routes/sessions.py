"""Thin API alias for the chat sessions domain router."""

import sys as _sys

from ...domains.chat import service as _impl

_sys.modules[__name__] = _impl
