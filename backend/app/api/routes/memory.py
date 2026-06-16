"""Thin API alias for the memory views domain router."""

import sys as _sys

from ...domains.memory import view_service as _impl

_sys.modules[__name__] = _impl
