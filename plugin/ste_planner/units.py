# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Units and conversions (specification section 2).

Internal system: mm, N, N*mm, MPa, g/mm^3, s, degC, rad/s, g, mm/s, mm^3/s,
dimensionless fractions. Percentages are converted only at the slicer boundary.
"""

import math
import re

STANDARD_GRAVITY = 9.80665  # m/s^2, exact by definition

# unit -> (dimension, factor to canonical unit, offset)
_UNITS = {
    "mm": ("length", 1.0), "m": ("length", 1000.0), "cm": ("length", 10.0), "um": ("length", 0.001),
    "N": ("force", 1.0), "kN": ("force", 1000.0),
    "N*mm": ("torque", 1.0), "N*m": ("torque", 1000.0),
    "MPa": ("stress", 1.0), "GPa": ("stress", 1000.0), "kPa": ("stress", 0.001), "Pa": ("stress", 1e-6),
    "bar": ("stress", 0.1),
    "g/mm3": ("density", 1.0), "g/cm3": ("density", 0.001), "kg/m3": ("density", 1e-6),
    "s": ("time", 1.0), "min": ("time", 60.0), "h": ("time", 3600.0),
    "rad/s": ("angular_speed", 1.0), "rpm": ("angular_speed", 2.0 * math.pi / 60.0),
    "Hz": ("angular_speed", 2.0 * math.pi),
    "mm/s": ("speed", 1.0), "m/s": ("speed", 1000.0),
    "g": ("mass", 1.0), "kg": ("mass", 1000.0),
    "g*mm": ("unbalance", 1.0), "kg*m": ("unbalance", 1e6),
    "mm3/s": ("flow", 1.0), "m3/h": ("flow", 1e9 / 3600.0), "l/min": ("flow", 1e6 / 60.0),
    "fraction": ("ratio", 1.0), "%": ("ratio", 0.01),
    "deg": ("angle", 1.0), "rad": ("angle", 180.0 / math.pi),
    "degC": ("temperature", 1.0),
    "count": ("count", 1.0),
    "pH": ("pH", 1.0),
}

CANONICAL = {
    "length": "mm", "force": "N", "torque": "N*mm", "stress": "MPa", "density": "g/mm3",
    "time": "s", "angular_speed": "rad/s", "speed": "mm/s", "mass": "g", "unbalance": "g*mm",
    "flow": "mm3/s", "ratio": "fraction", "angle": "deg", "temperature": "degC", "count": "count",
    "pH": "pH",
}


class UnitError(ValueError):
    pass


def dimension(unit):
    if unit == "K":
        return "temperature"
    try:
        return _UNITS[unit][0]
    except KeyError:
        raise UnitError("Unknown unit %r" % (unit,))


def convert(value, from_unit, to_unit):
    """Convert value between compatible units; raises UnitError otherwise."""
    if from_unit == to_unit:
        return float(value)
    if "K" in (from_unit, to_unit) and dimension(from_unit) == dimension(to_unit) == "temperature":
        return float(value) + 273.15 if to_unit == "K" else float(value) - 273.15
    d_from, f_from = _UNITS.get(from_unit, (None, None))
    d_to, f_to = _UNITS.get(to_unit, (None, None))
    if d_from is None or d_to is None:
        raise UnitError("Unknown unit in %r -> %r" % (from_unit, to_unit))
    if d_from != d_to:
        raise UnitError("Incompatible units %r (%s) and %r (%s)" % (from_unit, d_from, to_unit, d_to))
    return float(value) * f_from / f_to


def to_canonical(value, unit):
    d = dimension(unit)
    canon = CANONICAL[d]
    if isinstance(value, (tuple, list)):
        return tuple(convert(v, unit, canon) for v in value), canon
    return convert(value, unit, canon), canon


def mass_to_weight(mass_g):
    """Weight force in N of a static mass in g (a derived interpretation of 'load in kg')."""
    return float(mass_g) / 1000.0 * STANDARD_GRAVITY


def rpm_to_rad_s(n_rpm):
    return 2.0 * math.pi * float(n_rpm) / 60.0


def fraction_to_percent(x):
    return float(x) * 100.0


def percent_to_fraction(p):
    return float(p) / 100.0


# Unit tokens as written in Russian/English technical requirements.
# Order matters: longer and more specific patterns first.
_TOKEN_PATTERNS = [
    (r"г\s*[·*.]?\s*мм", "g*mm"),
    (r"н\s*[·*.]?\s*мм", "N*mm"),
    (r"н\s*[·*.]?\s*м(?![а-яa-z])", "N*m"),
    (r"г\s*/?\s*см\s*(?:³|3|\^3)", "g/cm3"),
    (r"кг\s*/\s*м\s*(?:³|3|\^3)", "kg/m3"),
    (r"м\s*(?:³|3|\^3)\s*/\s*ч", "m3/h"),
    (r"об\s*/?\s*мин", "rpm"),
    (r"rpm", "rpm"),
    (r"мм\s*/\s*с", "mm/s"),
    (r"м\s*/?\s*с(?![а-яa-z])", "m/s"),
    (r"мпа|mpa", "MPa"),
    (r"гпа|gpa", "GPa"),
    (r"бар(?![а-яa-z])|bar(?![a-z])", "bar"),
    (r"°\s*c|°\s*с|градус\w*\s+цельс\w*", "degC"),
    (r"мкм|µm|um(?![a-z])", "um"),
    (r"мм(?![а-яa-z])|mm(?![a-z])", "mm"),
    (r"кг(?![а-яa-z])|kg(?![a-z])", "kg"),
    (r"град\w*|°", "deg"),
    (r"%", "%"),
    (r"моточас\w*|час\w*|ч(?![а-яa-z])|h(?![a-z])", "h"),
    (r"мин(?![а-яa-z])", "min"),
    (r"шт\w*|штук\w*", "count"),
    (r"гц|hz", "Hz"),
    (r"(?<![а-яa-z])н(?![а-яa-z])|(?<![a-z])n(?![a-z])", "N"),
    (r"(?<![а-яa-z])м(?![а-яa-z])", "m"),
    (r"(?<![а-яa-z])г(?![а-яa-z])", "g"),
]
_TOKEN_RE = [(re.compile(p, re.IGNORECASE | re.UNICODE), u) for p, u in _TOKEN_PATTERNS]


def match_unit(text):
    """Return (unit, matched_length) for a unit token at the start of text, else (None, 0)."""
    s = text.lstrip()
    skipped = len(text) - len(s)
    for rx, unit in _TOKEN_RE:
        m = rx.match(s)
        if m:
            return unit, skipped + m.end()
    return None, 0


def parse_number(text):
    """Parse a number with optional sign and decimal comma."""
    return float(text.replace(",", ".").replace("−", "-").replace("+", ""))
