"""Thin API alias for the workspace domain router."""

import sys as _sys

from ...domains.workspace import service as _impl

_sys.modules[__name__] = _impl
