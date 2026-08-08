"""Thin API alias for the analytics domain router."""

import sys as _sys

from ...domains.analytics import service as _impl

_sys.modules[__name__] = _impl
