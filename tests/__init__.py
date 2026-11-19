"""Test-only AstrBot API stubs for running without a host installation."""

import importlib.util
import sys
import types
from unittest.mock import Mock

if importlib.util.find_spec("astrbot") is None and "astrbot.api" not in sys.modules:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = Mock(name="astrbot_logger")
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
