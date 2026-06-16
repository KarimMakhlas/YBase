"""Thin API alias for the ops domain router."""

import sys as _sys

from ...domains.ops import service as _impl

_sys.modules[__name__] = _impl
