# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
from . import AIAssistant


def getMetaData():
    return {}


def register(app):
    return {"extension": AIAssistant.AIAssistant()}
