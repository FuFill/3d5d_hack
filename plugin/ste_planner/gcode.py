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


def inspect_support_contacts(text, regions, line_width_mm, top_distance_mm, layer_height_mm):
    """Capsule/triangle contact check for horizontal underside patches in G-code coordinates.

    Includes the line radius and top-gap rounding by one layer. This checks
    contact presence/absence, not complete support coverage or load capacity.
    Other coordinate systems, spatial resets or non-horizontal patches remain
    unknown. TYPE identifies support; actual extruded segment geometry decides
    contact. Region triangles must include the engine's bed-origin offset once.
    """
    import numpy as np
    params = (line_width_mm, top_distance_mm, layer_height_mm)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in params):
        raise ValueError("finite contact dimensions required")
    if line_width_mm <= 0 or top_distance_mm < 0 or layer_height_mm <= 0:
        raise ValueError("invalid contact dimensions")
    parsed = parse_gcode(text, collect_segments=True)
    segments = [s for s in parsed["extrusion_segments"] if s["feature"].startswith("SUPPORT")]

    def cross(a, b):
        return a[0] * b[1] - a[1] * b[0]

    def point_distance(p, a, b):
        v = b - a
        t = max(0., min(1., float((p - a).dot(v)) / max(float(v.dot(v)), 1e-30)))
        return float(np.linalg.norm(p - a - t * v))

    def inside(p, tri):
        signs = [cross(tri[(i + 1) % 3] - tri[i], p - tri[i]) for i in range(3)]
        return min(signs) >= -1e-9 or max(signs) <= 1e-9

    def distance(a, b, tri):
        if inside(a, tri) or inside(b, tri):
            return 0.
        result = float("inf")
        for i in range(3):
            c, d = tri[i], tri[(i + 1) % 3]
            ab, cd = b - a, d - c
            den = cross(ab, cd)
            if abs(den) > 1e-12:
                t, u = cross(c - a, cd) / den, cross(c - a, ab) / den
                if 0 <= t <= 1 and 0 <= u <= 1:
                    return 0.
            result = min(result, point_distance(a, c, d), point_distance(b, c, d),
                         point_distance(c, a, b), point_distance(d, a, b))
        return result

    output = []
    for region in regions:
        triangles = np.asarray(region["triangles_mm"], dtype=float)
        valid = (triangles.ndim == 3 and triangles.shape[1:] == (3, 3) and len(triangles)
                 and np.isfinite(triangles).all())
        planar = valid and all(np.ptp(t[:, 2]) < 1e-6 and abs(cross(t[1, :2] - t[0, :2], t[2, :2] - t[0, :2])) > 1e-10 for t in triangles)
        uncertain = (not planar or parsed["spatial_resets"] or parsed["parse_errors"] or
                     any(s["cs"] != "G54" or s["rotary"] or abs(s["start"][2] - s["end"][2]) > .011 for s in segments))
        contacts = 0
        samples = []
        if not uncertain:
            for segment in segments:
                a, b = np.asarray(segment["start"]), np.asarray(segment["end"])
                for triangle in triangles:
                    gap = triangle[0, 2] - a[2]
                    if (-.011 <= gap <= top_distance_mm + layer_height_mm + .011 and
                            distance(a[:2], b[:2], triangle[:, :2]) <= line_width_mm / 2. + 1e-6):
                        contacts += 1
                        if len(samples) < 5:
                            samples.append({"start": segment["start"], "end": segment["end"], "gap_mm": float(gap)})
                        break
        required = region["role"] == "required"
        if region["role"] not in ("required", "forbidden"):
            raise ValueError("contact role must be required or forbidden")
        output.append({"id": region["id"], "role": region["role"], "contact_segments": contacts,
                       "contact_samples": samples,
                       "status": "unknown" if uncertain else ("pass" if bool(contacts) == required else "fail")})
    return {"regions": output, "support_segments": len(segments),
            "scope": "horizontal patch capsule intersection; no support coverage/strength or physical qualification"}

_WORD = re.compile(r"([A-Z])\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)")
_NONFINITE = re.compile(r"[XYZACEFS]\s*[-+]?(?:NAN|INF(?:INITY)?)(?=$|\s)")


def parse_gcode(text, filament_diameter=1.75, collect_segments=False, segment_callback=None, physical_rotary_resets=False):
    if (isinstance(filament_diameter, bool) or not isinstance(filament_diameter, (int, float))
            or not math.isfinite(filament_diameter) or filament_diameter <= 0):
        raise ValueError("finite positive filament diameter required")
    state = {"abs_xyz": True, "abs_e": True, "pos": {"X": 0.0, "Y": 0.0, "Z": 0.0, "A": 0.0, "C": 0.0},
             "e": 0.0, "f": 0.0, "tool": 0, "cs": "G54"}
    a_fil = math.pi * filament_diameter ** 2 / 4.0
    info = {"header": {}, "layers": [], "temps_nozzle": [], "temps_bed": [], "fans": [], "unsupported": {},
            "extruded_mm": {}, "coordinate_systems": set(), "extrusion_modes": set(), "tools": set(),
            "bbox": {}, "rotary": {"A": [None, None], "C": [None, None]}, "max_linear_flow_mm3_s": 0.0,
            "rotary_extrusion_moves": 0, "moves": 0, "retractions": 0, "lines": 0,
            "feature_extruded_mm": {}, "parse_errors": [], "spatial_resets": 0,
            "spatial_reset_events": [], "opaque_macros": [], "physical_controls": []}
    retraction_debt = {}
    rotary_offset = {"A": 0., "C": 0.}
    layer = None
    if collect_segments:
        info["extrusion_segments"] = []
    feature = "UNKNOWN"
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
                        layer = {"index": int(val), "extruded_mm": 0.0, "moves": 0, "z": [],
                                 "extrusion_z": []}
                        info["layers"].append(layer)
                        feature = "UNKNOWN"
                    except ValueError:
                        pass
                elif key == "TYPE":
                    feature = val.upper()
                elif key in ("TIME", "FILAMENT USED", "FLAVOR", "LAYER_COUNT", "PRINT.TIME", "MINX", "MAXX",
                             "MINY", "MAXY", "MINZ", "MAXZ", "GENERATED WITH", "TIME_ELAPSED"):
                    info["header"].setdefault(key, val)
            continue
        code = line.split(";", 1)[0].strip().upper()
        if not code:
            continue
        if code in KNOWN_WORDS:
            info["opaque_macros"].append(code)
            continue
        m = re.match(r"([GMT])(\d+)", code)
        if not m:
            info["unsupported"][code.split()[0]] = info["unsupported"].get(code.split()[0], 0) + 1
            continue
        letter, num = m.group(1), m.group(2)
        words = dict((k, float(v)) for k, v in _WORD.findall(code[m.end():]))
        if _NONFINITE.search(code) or any(not math.isfinite(v) for v in words.values()):
            info["parse_errors"].append(info["lines"])
            continue
        if letter == "T":
            state["tool"] = int(num)
            info["tools"].add(int(num))
            continue
        if letter == "M":
            if num == "221" and words.get("S", 100.) != 100.:
                info["physical_controls"].append({"line": info["lines"], "command": code})
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
        if num in ("10", "11"):
            info["physical_controls"].append({"line": info["lines"], "command": code})
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
                    tracked = physical_rotary_resets and ax in rotary_offset
                    if tracked:
                        rotary_offset[ax] += state["pos"][ax] - words[ax]
                    state["pos"][ax] = words[ax]
                    info["spatial_resets"] += 1
                    info["spatial_reset_events"].append({"line": info["lines"], "axis": ax, "value": words[ax], "tracked": tracked})
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
                retraction_debt[state["tool"]] = retraction_debt.get(state["tool"], 0.) - de
            if de > 0:
                consumed = min(de, retraction_debt.get(state["tool"], 0.))
                retraction_debt[state["tool"]] = retraction_debt.get(state["tool"], 0.) - consumed
                deposited = de - consumed
                dist = math.sqrt(sum((state["pos"][ax] - old[ax]) ** 2 for ax in ("X", "Y", "Z")))
                rotary_motion = any(abs(state["pos"][a] - old[a]) > 1e-9 for a in ("A", "C"))
                if (collect_segments or segment_callback is not None) and (dist > 1e-6 or rotary_motion) and deposited > 1e-12:
                    # With synchronous linear E/joint interpolation, material
                    # appears only after the retraction debt is repaid.
                    start = dict((a, old[a] + consumed/de * (state["pos"][a]-old[a])) for a in old)
                    end_axes = dict(state["pos"])
                    if physical_rotary_resets:
                        for axis in rotary_offset:
                            start[axis] += rotary_offset[axis]
                            end_axes[axis] += rotary_offset[axis]
                    record = {"start": [start[a] for a in ("X", "Y", "Z")],
                        "end": [state["pos"][a] for a in ("X", "Y", "Z")], "feature": feature,
                        "cs": state["cs"], "rotary": rotary_motion, "start_axes": start,
                        "end_axes": end_axes, "deposited_filament_mm": deposited,
                        "tool": state["tool"], "feed_mm_min": state["f"], "line": info["lines"],
                        "layer": layer["index"] if layer else None}
                    if collect_segments:
                        info["extrusion_segments"].append(record)
                    if segment_callback is not None:
                        segment_callback(record)
                if dist > 1e-6:
                    info["feature_extruded_mm"][feature] = info["feature_extruded_mm"].get(feature, 0.0) + de
                    if layer is not None:
                        layer["extrusion_z"].append(state["pos"]["Z"])
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
               "q_limit": mm3/s or None, "layer_height", "xy_centered": bool}

    Temperatures are commanded setpoints, not sensor measurements. Feature
    extrusion comes from TYPE comments; its amount does not prove support
    contact clearance. Spatial G92 leaves coordinate-dependent checks unknown.
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
    add("finite_coordinates", "fail" if info["parse_errors"] else "pass",
        "nonfinite numbers on lines: %s" % info["parse_errors"][:10])
    add("supported_commands", "pass" if not info["unsupported"] else "unknown",
        "unrecognised: %s" % (sorted(info["unsupported"].items())[:10] or "none"))
    add("single_extrusion_mode", "pass" if len(info["extrusion_modes"]) <= 1 else "fail",
        "modes: %s" % info["extrusion_modes"])
    t = expected.get("nozzle_temperature")
    if t is not None:
        temps = [s for _, s in info["temps_nozzle"] if s > 0]
        ok = any(abs(s - t) < 0.51 for s in temps)
        add("nozzle_temperature", "pass" if ok else "fail", "expected %g, found %s" % (t, sorted(set(temps))[:6]))
    bed = expected.get("bed_temperature")
    if bed is not None:
        ok = any(abs(s - bed) < 0.51 for s in info["temps_bed"])
        add("bed_temperature", "pass" if ok else "fail", "expected %g, found %s" % (bed, sorted(set(info["temps_bed"]))[:6]))
    mode = expected.get("printing_mode")
    if mode:
        five = bool(info["rotary_extrusion_moves"]) or "G56" in info["coordinate_systems"] or \
            "G55" in info["coordinate_systems"]
        if mode == "classic":
            add("mode_consistency", "pass" if not five else "fail",
                "rotary extrusion moves: %d; systems: %s" % (info["rotary_extrusion_moves"], info["coordinate_systems"]))
        else:
            add("mode_consistency", "pass" if five else "fail",
                "5D markers: coordinate systems %s, rotary extrusion moves %d" %
                (info["coordinate_systems"], info["rotary_extrusion_moves"]))
    if mode == "classic":
        indices = [l["index"] for l in info["layers"] if l["index"] >= 0]
        try:
            declared = int(info["header"].get("LAYER_COUNT", len(indices)))
        except (TypeError, ValueError):
            declared = -1
        complete = bool(indices) and indices == list(range(indices[-1] + 1))
        add("layer_sequence", "pass" if complete else "fail",
            "observed %s..%s (%d); declared %d" % (indices[0] if indices else None,
              indices[-1] if indices else None, len(indices), declared))
        add("declared_layer_count", "pass" if len(indices) == declared else "unknown",
            "native header declares %d; %d extruding layer records; mismatch requires review" % (declared, len(indices)))
        terminated = bool(re.search(r";\s*End of Gcode\s*$", text, re.I | re.M))
        add("program_termination", "pass" if terminated else "unknown", "native end-of-program marker present=%s" % terminated)
        if expected.get("model_top_z") is not None:
            zs = [z for layer in info["layers"] for z in layer["extrusion_z"]]
            tolerance = float(expected.get("layer_height", 0)) / 2 + .011
            covered = bool(zs) and abs(max(zs) - expected["model_top_z"]) <= tolerance
            add("model_height_coverage", "unknown" if info["spatial_resets"] else ("pass" if covered else "fail"),
                "top extrusion Z=%s vs model top=%s, tolerance=%g" % (max(zs) if zs else None, expected["model_top_z"], tolerance))
    q = expected.get("q_limit")
    if q:
        status = "fail" if info["max_linear_flow_mm3_s"] > q else ("unknown" if info["rotary_extrusion_moves"] else "pass")
        add("volumetric_flow", status,
            "max linear %.2f mm3/s vs limit %.2f (rotary moves not converted)" % (info["max_linear_flow_mm3_s"], q))
    else:
        add("volumetric_flow", "unknown", "max linear %.2f mm3/s; no calibrated Q_limit" %
            info["max_linear_flow_mm3_s"])
    if mode == "classic" and expected.get("build_volume") and "G54" in info["bbox"]:
        bb = info["bbox"]["G54"]
        bv = expected["build_volume"]
        z_ok = bb[1][2] <= bv[2] + 1e-6
        add("z_within_build_volume", "unknown" if info["spatial_resets"] else ("pass" if z_ok else "fail"),
            "max Z %.2f vs %.1f" % (bb[1][2], bv[2]))
        if "xy_centered" in expected:
            lower = [-bv[0] / 2, -bv[1] / 2, 0] if expected["xy_centered"] else [0, 0, 0]
            upper = [lower[i] + bv[i] for i in range(3)]
            fits = all(bb[0][i] >= lower[i] - 0.01 and bb[1][i] <= upper[i] + 0.01 for i in range(3))
            add("extrusion_within_build_volume", "unknown" if info["spatial_resets"] else ("pass" if fits else "fail"),
                "extrusion bounds %s; spatial G92 needs a coordinate transform" % bb)
    if mode == "classic" and expected.get("layer_height") is not None:
        layers = [l for l in info["layers"] if l["index"] >= 0 and l["extrusion_z"]]
        planar = all(max(l["extrusion_z"]) - min(l["extrusion_z"]) <= 0.011 for l in layers)
        heights = [max(l["extrusion_z"]) for l in layers]
        deltas = [b - a for a, b in zip(heights, heights[1:])]
        ok = planar and bool(deltas) and all(abs(h - expected["layer_height"]) <= 0.011 for h in deltas)
        status = "unknown" if info["spatial_resets"] or (planar and not deltas) else ("pass" if ok else "fail")
        add("layer_schedule", status,
            "planar=%s; observed increments=%s mm" % (planar, sorted(set(round(h, 4) for h in deltas))[:10]))
    return {"checks": checks, "summary": {
        "layers": len(info["layers"]), "time_header": info["header"].get("TIME"),
        "filament_used_header": info["header"].get("FILAMENT USED"), "filament_volume_mm3": info["filament_volume_mm3"],
        "coordinate_systems": info["coordinate_systems"], "rotary_ranges": info["rotary"],
        "max_linear_flow_mm3_s": info["max_linear_flow_mm3_s"], "retractions": info["retractions"],
        "bbox": info["bbox"], "feature_extruded_mm": info["feature_extruded_mm"],
        "support_extruded_mm": sum(v for k, v in info["feature_extruded_mm"].items() if k.startswith("SUPPORT")),
        "label": "slicer estimate"}}
