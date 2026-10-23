# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Validate untrusted proposals before the existing LLM merge boundary."""
import math
import re
from . import requirements as R
from . import units

MAX_ITEMS = 100
FIELDS = {"property", "fragment", "value", "unit", "comparator", "region", "load_case", "mandatory"}
PROPERTIES = sorted(set([row[0] for row in R._NUMERIC_RULES] +
                        [row[0] for row in R._QUALITATIVE_RULES] +
                        ["dimensional_tolerance", "balance_grade", "batch_quantity", "chemical_ph", "part_mass"]))


def prepare_requirements(text, items=None, source=None):
    reqset = R.normalize_requirements(R.parse_requirements(text, source=source))
    accepted, rejected = [], []
    if items is None:
        return reqset, accepted, rejected
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise ValueError("requirements must be a list with at most 100 items")
    norm_text = re.sub(r"\s+", " ", text).lower()
    qualitative = set(row[0] for row in R._QUALITATIVE_RULES)
    for index, proposal in enumerate(items):
        try:
            if not isinstance(proposal, dict) or set(proposal) - FIELDS:
                raise ValueError("unsupported fields")
            item = dict(proposal)
            prop, fragment = item.get("property"), item.get("fragment")
            if prop not in PROPERTIES:
                raise ValueError("unsupported property")
            if (not isinstance(fragment, str) or not fragment.strip() or len(fragment) > 4000
                    or re.sub(r"\s+", " ", fragment).lower() not in norm_text):
                raise ValueError("fragment not found in source")
            for field in ("region", "load_case"):
                if item.get(field) is not None and (not isinstance(item[field], str) or len(item[field]) > 100):
                    raise ValueError("invalid region/load case")
            if "mandatory" in item and not isinstance(item["mandatory"], bool):
                raise ValueError("mandatory must be boolean")
            value = item.get("value")
            if prop in qualitative:
                if value is not True or item.get("unit") is not None or item.get("comparator", "qual") != "qual":
                    raise ValueError("qualitative property requires true, no unit and qual comparator")
                item.update(kind=R.KIND_QUALITATIVE, comparator="qual")
                meaning_supported = any(row[0] == prop and re.search(row[1], fragment, re.IGNORECASE)
                                        for row in R._QUALITATIVE_RULES)
            else:
                values = value if isinstance(value, list) else [value]
                if (len(values) not in (1, 2) or any(isinstance(v, bool) or not isinstance(v, (float, int))
                        or not math.isfinite(float(v)) for v in values)):
                    raise ValueError("finite numeric value or ordered interval required")
                if len(values) == 2 and values[0] > values[1]:
                    raise ValueError("reversed interval")
                if item.get("comparator") not in ("<=", ">=", "in", "abs_dev_le", "=="):
                    raise ValueError("unsupported comparator")
                canonical, canonical_unit = units.to_canonical(tuple(values) if len(values) == 2 else values[0], item.get("unit"))
                expected = canonical if isinstance(canonical, tuple) else (canonical,)
                grounded = None
                for quantity in R.find_quantities(fragment):
                    qvalue, qunit = quantity.canonical()
                    qvalues = qvalue if isinstance(qvalue, tuple) else (qvalue,)
                    if qunit == canonical_unit and len(qvalues) == len(expected) and all(
                            abs(a - b) <= 1e-8 * max(1, abs(a), abs(b)) for a, b in zip(qvalues, expected)):
                        grounded = quantity
                        break
                if grounded is None:
                    raise ValueError("value and unit are not supported by a source quantity")
                if grounded.comparator is not None and grounded.comparator != item["comparator"]:
                    raise ValueError("comparator conflicts with the cited quantity")
                # The caller cannot supply value_as_written to bypass grounding.
                item["value_as_written"] = grounded.value()
                item["kind"] = next((row[3] for row in R._NUMERIC_RULES if row[0] == prop), R.KIND_CONSTRAINT)
                meaning_supported = (prop == "part_mass" and re.search(r"масс|mass", fragment, re.IGNORECASE)) or any(
                    row[0] == prop and re.search(row[1], fragment, re.IGNORECASE) for row in R._NUMERIC_RULES)
            if R._PREFERENCE_RE.search(fragment):
                item["mandatory"] = False
            item["interpretation_status"] = R.DERIVED if meaning_supported else R.ASSUMED
            if not meaning_supported:
                item["mandatory"] = False
            errors = R.merge_llm_requirements(reqset, [item])
            if errors:
                raise ValueError(errors[0]["reason"])
            accepted.append(index)
        except (ValueError, TypeError, units.UnitError):
            # No arbitrary provider text or source fragments in error logs.
            rejected.append({"index": index, "reason": "invalid, unsupported or duplicate grounded requirement"})
    return R.normalize_requirements(reqset), accepted, rejected
