"""Thin API alias for the answer-feedback domain router."""

import sys as _sys

from ...domains.feedback import service as _impl

_sys.modules[__name__] = _impl
