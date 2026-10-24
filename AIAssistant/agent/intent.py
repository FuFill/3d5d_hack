"""Fast, conservative routing of clear questions and explicit project requests.

Ambiguous messages still use the model router. This guard keeps common questions
independent of its availability and of instructions inside attached documents.
"""
import re


_QUOTED = re.compile(r'"[^"\n]*"|«[^»]*»|“[^”]*”|`[^`]*`')
_RU_ACTION = re.compile(
    r"(?:^|[\n,;.!?]\s*|\b(?:и|пожалуйста)\s+)(?:(?:а|ну|тогда|пожалуйста)\s+)?"
    r"(?:измени|поменяй|установи|выставь|подбери|выбери|настрой|подготовь|оптимизируй|ускорь|"
    r"уменьши|увеличь|сделай|поверни|перемести|загрузи|открой|примени|отмени|нарежь|пересчитай|"
    r"сохрани|экспортируй|включи|выключи|продолжай|делай|приступай|исправь)(?:те)?\b", re.I)
_EN_ACTION = re.compile(
    r"(?:^|[\n,;.!?]\s*|\band\s+)(?:please\s+)?(?:change|set|configure|prepare|optimize|speed up|"
    r"reduce|increase|make|rotate|move|load|open|apply|undo|slice|reslice|save|export|enable|disable|continue|fix)\b", re.I)
_REQUEST = re.compile(
    r"\b(?:(?:можешь|можете|прошу|пожалуйста)\s+(?:(?:ты|вы|пожалуйста)\s+)?(?:поменять|изменить|установить|"
    r"подобрать|настроить|подготовить|сделать|ускорить|повернуть|переместить|распечатать|напечатать|"
    r"применить|нарезать|пересчитать|сохранить|экспортировать|исправить)|"
    r"(?:can|could|would|will)\s+you\s+(?:please\s+)?(?:change|set|configure|prepare|optimize|speed up|"
    r"reduce|increase|make|rotate|move|load|open|apply|undo|slice|save|export|continue|fix))\b", re.I)
_NEGATED = re.compile(r"(?:\bне(?:\s+(?:надо|нужно))?|\bdo\s+not|\bdon't)\s*$", re.I)
_QUESTION = re.compile(
    r"\b(?:почему|зачем|отчего|объясни(?:те)?|поясни(?:те)?|расскажи(?:те)?|why|explain|describe)\b|"
    r"^(?:(?:а|ну|но|и|тогда)[,\s]+)*(?:сколько|какой|какая|какие|какое|что|как|когда|где|чем|"
    r"what|when|where|which|how)\b|\b(?:долго\s+ещ[её]|можно\s+ли)\b", re.I)


def clear_intent(text):
    """Return answer/act for clear text, None when semantic context is needed.

    Explicit requests win in mixed messages ("why so long; reduce the layer
    count"). A question mark alone never turns a polite action request into Q&A.
    Quoted commands are discussed as data, not executed.
    """
    text = (text or "").strip()
    if not text:
        return None
    plain = _QUOTED.sub("", text)
    for pattern in (_RU_ACTION, _EN_ACTION, _REQUEST):
        for match in pattern.finditer(plain):
            if not _NEGATED.search(plain[:match.start()]):
                return "act"
    if _QUESTION.search(plain) or "?" in plain:
        return "answer"
    return None
