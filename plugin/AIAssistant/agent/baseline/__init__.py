# -*- coding: utf-8 -*-
"""Детерминированный бэйзлайн модели плагина AI Assistant для STE Slicer.

    from baseline import recommend, explain_decision
    decision = recommend(tz_text, model_path="part.stl", hint="5D")

Совместим с Python 3.5 (интерпретатор STE Slicer); зависит только от numpy.
"""

from .decision import VERSION, recommend  # noqa: F401
from .explain import explain_decision  # noqa: F401
from .tools import TOOLS, call_tool, openai_tools  # noqa: F401
