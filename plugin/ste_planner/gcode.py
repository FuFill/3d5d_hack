# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Toolpath inspection of STE Slicer G-code (specification section 14).

The parser understands the RepRap/Marlin subset written by STE Slicer plus
the 5D specifics (rotary A/C axes, coordinate systems G54/G55/G56). Linear
volumetric flow is computed only for moves without rotary components; moves
with A/C need the machine kinematics and are reported, not converted.
"""

import math
import re

KNOWN_G = {"0", "1", "4", "10", "11", "21", "28", "54", "55", "56", "90", "91", "92"}
KNOWN_M = {"82", "83", "84", "104", "105", "106", "107", "109", "110", "117", "140", "190", "141", "191",
           "204", "205", "220", "221", "400", "18", "201", "203"}
KNOWN_WORDS = {"START", "END"}

_WORD = re.compile(r"([A-Z])\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)")


def parse_gcode(text, filament_diameter=1.75):
    state = {"abs_xyz": True, "abs_e": True, "pos": {"X": 0.0, "Y": 0.0, "Z": 0.0, "A": 0.0, "C": 0.0},
             "e": 0.0, "f": 0.0, "tool": 0, "cs": "G54"}
    a_fil = math.pi * filament_diameter ** 2 / 4.0
    info = {"header": {}, "layers": [], "temps_nozzle": [], "temps_bed": [], "fans": [], "unsupported": {},
            "extruded_mm": {}, "coordinate_systems": set(), "extrusion_modes": set(), "tools": set(),
            "bbox": {}, "rotary": {"A": [None, None], "C": [None, None]}, "max_linear_flow_mm3_s": 0.0,
            "rotary_extrusion_moves": 0, "moves": 0, "retractions": 0, "lines": 0}
    layer = None
    for raw in text.splitlines():
        info["lines"] += 1
        line = raw.strip()
        if not line:
            continue
        if line.startswith(";"):
            m = re.match(r";\s*([A-Za-z_ ]+?)\s*:\s*(.*)$", line)
            if m:
                key, val = m.group(1).strip().upper(), m.group(2).strip()
                if key == "LAYER":
                    try:
                        layer = {"index": int(val), "extruded_mm": 0.0, "moves": 0, "z": []}
                        info["layers"].append(layer)
                    except ValueError:
                        pass
                elif key in ("TIME", "FILAMENT USED", "FLAVOR", "LAYER_COUNT", "PRINT.TIME", "MINX", "MAXX",
                             "MINY", "MAXY", "MINZ", "MAXZ", "GENERATED WITH", "TIME_ELAPSED"):
                    info["header"].setdefault(key, val)
            continue
        code = line.split(";", 1)[0].strip().upper()
        if not code:
            continue
        if code in KNOWN_WORDS:
            continue
        m = re.match(r"([GMT])(\d+)", code)
        if not m:
            info["unsupported"][code.split()[0]] = info["unsupported"].get(code.split()[0], 0) + 1
            continue
        letter, num = m.group(1), m.group(2)
        words = dict((k, float(v)) for k, v in _WORD.findall(code[m.end():]))
        if letter == "T":
            state["tool"] = int(num)
            info["tools"].add(int(num))
            continue
        if letter == "M":
            if num not in KNOWN_M:
                info["unsupported"]["M" + num] = info["unsupported"].get("M" + num, 0) + 1
            if num in ("104", "109") and "S" in words:
                info["temps_nozzle"].append((state["tool"], words["S"]))
            elif num in ("140", "190") and "S" in words:
                info["temps_bed"].append(words["S"])
            elif num == "106":
                info["fans"].append(words.get("S", 255.0))
            elif num == "107":
                info["fans"].append(0.0)
            elif num == "82":
                state["abs_e"] = True
                info["extrusion_modes"].add("absolute")
            elif num == "83":
                state["abs_e"] = False
                info["extrusion_modes"].add("relative")
            continue
        if num not in KNOWN_G:
            info["unsupported"]["G" + num] = info["unsupported"].get("G" + num, 0) + 1
            continue
        if num == "90":
            state["abs_xyz"] = True
            continue
        if num == "91":
            state["abs_xyz"] = False
            continue
        if num in ("54", "55", "56"):
            state["cs"] = "G" + num
            info["coordinate_systems"].add("G" + num)
            continue
        if num == "92":
            if "E" in words:
                state["e"] = words["E"]
            for ax in ("X", "Y", "Z", "A", "C"):
                if ax in words:
                    state["pos"][ax] = words[ax]
            continue
        if num in ("0", "1"):
            info["moves"] += 1
            old = dict(state["pos"])
            for ax in ("X", "Y", "Z", "A", "C"):
                if ax in words:
                    state["pos"][ax] = words[ax] if state["abs_xyz"] else state["pos"][ax] + words[ax]
            if "F" in words:
                state["f"] = words["F"]
            de = 0.0
            if "E" in words:
                de = words["E"] - state["e"] if state["abs_e"] else words["E"]
                state["e"] = words["E"] if state["abs_e"] else state["e"] + words["E"]
            if de < 0:
                info["retractions"] += 1
            if de > 0:
                info["extruded_mm"][state["tool"]] = info["extruded_mm"].get(state["tool"], 0.0) + de
                if layer is not None:
                    layer["extruded_mm"] += de
                    layer["moves"] += 1
                cs = state["cs"]
                bb = info["bbox"].setdefault(cs, [[1e18] * 3, [-1e18] * 3])
                for i, ax in enumerate(("X", "Y", "Z")):
                    bb[0][i] = min(bb[0][i], old[ax], state["pos"][ax])
                    bb[1][i] = max(bb[1][i], old[ax], state["pos"][ax])
                rotary = any(abs(state["pos"][ax] - old[ax]) > 1e-9 for ax in ("A", "C"))
                if rotary:
                    info["rotary_extrusion_moves"] += 1
                else:
                    dist = math.sqrt(sum((state["pos"][ax] - old[ax]) ** 2 for ax in ("X", "Y", "Z")))
                    if dist > 1e-6 and state["f"] > 0:
                        t = dist / (state["f"] / 60.0)
                        info["max_linear_flow_mm3_s"] = max(info["max_linear_flow_mm3_s"], de * a_fil / t)
            if layer is not None and "Z" in words:
                layer["z"].append(state["pos"]["Z"])
            for ax in ("A", "C"):
                lo, hi = info["rotary"][ax]
                v = state["pos"][ax]
                info["rotary"][ax] = [v if lo is None else min(lo, v), v if hi is None else max(hi, v)]
    info["coordinate_systems"] = sorted(info["coordinate_systems"])
    info["extrusion_modes"] = sorted(info["extrusion_modes"])
    info["tools"] = sorted(info["tools"])
    info["filament_volume_mm3"] = dict((t, l * a_fil) for t, l in info["extruded_mm"].items())
    return info


def inspect_toolpath(text, expected=None, filament_diameter=1.75):
    """Checks of the sliced program against expected effective settings.

    expected: {"printing_mode", "nozzle_temperature", "bed_temperature", "build_volume": [x, y, z],
               "q_limit": mm3/s or None, "layer_height"}
    """
    expected = expected or {}
    info = parse_gcode(text, filament_diameter)
    checks = []

    def add(name, status, note):
        checks.append({"check": name, "status": status, "note": note})

    add("has_layers", "pass" if info["layers"] else "fail", "%d layers" % len(info["layers"]))
    empty = [l["index"] for l in info["layers"] if l["extruded_mm"] <= 0]
    add("no_empty_layers", "pass" if not empty else "fail",
        "empty layers: %s" % (empty[:10] if empty else "none"))
    add("extrusion_present", "pass" if info["extruded_mm"] else "fail", "filament mm per tool: %s" %
        dict((k, round(v, 1)) for k, v in info["extruded_mm"].items()))
    add("supported_commands", "pass" if not info["unsupported"] else "unknown",
        "unrecognised: %s" % (sorted(info["unsupported"].items())[:10] or "none"))
    add("single_extrusion_mode", "pass" if len(info["extrusion_modes"]) <= 1 else "fail",
        "modes: %s" % info["extrusion_modes"])
    t = expected.get("nozzle_temperature")
    if t is not None:
        temps = [s for _, s in info["temps_nozzle"] if s > 0]
        ok = any(abs(s - t) < 0.51 for s in temps)
        add("nozzle_temperature", "pass" if ok else "fail", "expected %g, found %s" % (t, sorted(set(temps))[:6]))
    mode = expected.get("printing_mode")
    if mode:
        five = bool(info["rotary_extrusion_moves"]) or "G56" in info["coordinate_systems"] or \
            "G55" in info["coordinate_systems"]
        if mode == "classic":
            add("mode_consistency", "pass" if not info["rotary_extrusion_moves"] else "fail",
                "rotary extrusion moves: %d" % info["rotary_extrusion_moves"])
        else:
            add("mode_consistency", "pass" if five else "fail",
                "5D markers: coordinate systems %s, rotary extrusion moves %d" %
                (info["coordinate_systems"], info["rotary_extrusion_moves"]))
    q = expected.get("q_limit")
    if q:
        add("volumetric_flow", "pass" if info["max_linear_flow_mm3_s"] <= q else "fail",
            "max linear %.2f mm3/s vs limit %.2f (rotary moves not converted)" % (info["max_linear_flow_mm3_s"], q))
    else:
        add("volumetric_flow", "unknown", "max linear %.2f mm3/s; no calibrated Q_limit" %
            info["max_linear_flow_mm3_s"])
    if mode == "classic" and expected.get("build_volume") and "G54" in info["bbox"]:
        bb = info["bbox"]["G54"]
        bv = expected["build_volume"]
        z_ok = bb[1][2] <= bv[2] + 1e-6
        add("z_within_build_volume", "pass" if z_ok else "fail", "max Z %.2f vs %.1f" % (bb[1][2], bv[2]))
    return {"checks": checks, "summary": {
        "layers": len(info["layers"]), "time_header": info["header"].get("TIME"),
        "filament_used_header": info["header"].get("FILAMENT USED"), "filament_volume_mm3": info["filament_volume_mm3"],
        "coordinate_systems": info["coordinate_systems"], "rotary_ranges": info["rotary"],
        "max_linear_flow_mm3_s": info["max_linear_flow_mm3_s"], "retractions": info["retractions"],
        "bbox": info["bbox"], "label": "slicer estimate"}}
