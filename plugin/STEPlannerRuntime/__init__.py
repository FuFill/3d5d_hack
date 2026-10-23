from .RuntimeExtension import RuntimeExtension


def getMetaData():
    return {}


def register(app):
    return {"extension": RuntimeExtension(app)}
