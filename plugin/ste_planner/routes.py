# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Printing routes 3D / 5D Spiral / 5D Full (specification sections 7 and 8).

Semantics (STE Slicer user guide, wiki.stereotech.org/steslicer/five-axis, and
the Hybrid 530 manual):
- classic: flat layers on the heated platform;
- cylindrical (5D Spiral): cylindrical layers around a horizontal mandrel; the
  cylinder of `cylindrical_mode_base_diameter` is subtracted from the model, so
  it must not exceed the part's bore;
- cylindrical_full (5D Spiral Full): spiral raft on the mandrel, a core of the
  base diameter printed in flat layers from the mandrel end face, the rest in
  cylindrical layers.
The rotation axis of the part is the machine A-axis tooling axis, i.e. Z of the
scene with the origin at the centre of the tooling end face.

Route preference is an explicit policy (class P): 5D is chosen only when a
documented benefit applies; otherwise the simpler classic route is kept.
"""

import math

import numpy

from . import geometry
from .result import computed, unknown, MODEL_POLICY, MODEL_GEOMETRIC

ALLOWED_MODES = ("classic", "cylindrical", "cylindrical_full")


def machine_capabilities(resolver=None):
    """Capabilities of Stereotech Hybrid 530 V5.2.4 from the definition and the manual.

    Values from the installed definition are read through the resolver when
    given; the manual provides tooling and temperature limits.
    """
    caps = {
        "machine_id": "hte530v5.2.4",
        "filament_diameter": 1.75,
        "nozzle_diameters": [0.4],           # installed by default (manual 3.4)
        "nozzle_change_possible": True,
        "hardened_nozzle": None,             # not stated: unknown
        "max_nozzle_temperature": 350.0,     # manual 3.5.5: thermoplastics up to 350 C
        "heated_bed_modes": ["classic"],
        "mandrel_collet_range_mm": [1.0, 8.0],  # ER11 collet, standard 6 mm rod
        "standard_mandrel_mm": 6.0,
        "printed_mandrels": True,
        "build_volume": {"classic": [351.0, 300.3, 300.0], "cylindrical": [351.0, 300.3, 231.1],
                         "cylindrical_full": [351.0, 300.3, 231.1]},
        "sources": ["definitions/hte530v5.2.4 chain", "Hybrid 530 manual rev. 15.12.2025"],
    }
    if resolver is not None:
        for mode in ALLOWED_MODES:
            r = resolver.copy_with({"printing_mode": mode})
            try:
                caps["build_volume"][mode] = [r.value("machine_width"), r.value("machine_depth"),
                                              r.value("machine_height")]
            except Exception:
                pass
        try:
            caps["nozzle_diameters"] = [resolver.value("machine_nozzle_size", extruder="0")]
        except Exception:
            pass
    return caps


def shaft_core_limit(mesh, axis, center):
    """Largest core radius that stays inside the part along the axis (lateral surfaces only)."""
    return geometry.lateral_min_radius(mesh, axis, center)


def _benefits(reqset, rot, ptype):
    """Documented reasons for and against 5D for this part (policy P)."""
    text = (reqset.text or "").lower() if reqset is not None else ""
    pros, cons = [], []
    if ptype == "pump_impeller":
        pros.append((2, "Stereotech prints pump impellers in 5D without supports (5dtech.pro/printers/solutions/"
                        "pumpwheels)"))
    if rot and (rot.get("outer_cylinder_area_fraction") or 0) >= 0.25 and             (ptype in ("wheel_roller", "shaft_cam") or "протектор" in text):
        pros.append((2, "cylindrical working surface (tread/journal): cylindrical layers follow it"))
    if "расслоен" in text or "отслоени" in text:
        pros.append((1, "TZ reports delamination; cylindrical layers remove radial layer interfaces"))
    if ("ступеньк" in text or "кулач" in text or "копир" in text) and "профил" in text:
        pros.append((2, "working profile must have no layer steps; 5D layers follow the profile"))
    if ptype == "gear" or "эвольвент" in text:
        cons.append((2, "tooth profile accuracy is best in flat layers normal to the axis"))
    if ptype == "boot_seal":
        cons.append((2, "thin elastomer bellows: flat layers keep continuous walls"))
    if ptype == "decorative":
        cons.append((3, "decorative part: classic route recommended by Stereotech"))
    return pros, cons


def enumerate_routes(mesh, analysis, rot, reqset=None, caps=None, ptype=None):
    """Feasible routes with parameters, reasons and a policy score."""
    caps = caps or machine_capabilities()
    rot = rot or {}
    size = analysis["size"]
    routes = []
    # classic
    bv = caps["build_volume"]["classic"]
    fits = sorted(size)[0] <= bv[2] and sorted(size)[1] <= max(bv[0], bv[1]) and sorted(size)[2] <= \
        max(bv[0], bv[1])
    routes.append({"mode": "classic", "feasible": bool(fits), "reasons": ["flat layers, heated platform"],
                   "parameters": {}, "score": 0.0})
    pros, cons = _benefits(reqset, rot, ptype)
    axis = rot.get("axis") if rot.get("is_rotational_candidate") else rot.get("long_axis")
    center = rot.get("center")
    axis_source = "rotational symmetry (inertia + sector radii)"
    if axis is not None and not rot.get("is_rotational_candidate"):
        coax = geometry.coaxial_cylinder_axis(mesh)
        if coax and coax["coaxial_area_fraction"] >= 0.15:
            axis, center = coax["axis"], coax["center"]
            axis_source = "coaxial cylindrical surfaces (%.0f%% of area)" % (100 * coax["coaxial_area_fraction"])
        else:
            axis, center = geometry.fit_axis_from_ends(mesh, axis, center)
            axis_source = "long axis through end sections"
    if axis is None:
        for m in ("cylindrical", "cylindrical_full"):
            routes.append({"mode": m, "feasible": False, "reasons": ["no rotation axis found"], "parameters": {},
                           "score": None})
        return routes
    pts = mesh.vertices[mesh.faces.reshape(-1)]
    t = pts.dot(numpy.asarray(axis, dtype=float))
    length = float(t.max() - t.min())
    score = float(sum(p for p, _ in pros) - sum(c for c, _ in cons))
    reasons = ["axis: " + axis_source] + [r for _, r in pros] + ["against: " + r for _, r in cons]
    h5 = caps["build_volume"]["cylindrical"][2]
    # 5D Spiral: needs a bore that hosts the mandrel; base diameter = bore diameter
    bore_r = rot.get("bore_radius")
    sp = {"mode": "cylindrical", "parameters": {"axis": axis, "center": center}, "reasons": list(reasons),
          "score": score}
    if bore_r and rot.get("is_rotational_candidate"):
        bore_d = math.floor(2.0 * bore_r * 100.0) / 100.0
        sp["feasible"] = bool(length <= h5 and bore_d >= 4.0)
        sp["parameters"].update({"base_diameter": bore_d, "non_printing_base_diameter": bore_d,
                                 "mandrel": "metal rod in ER11 collet" if bore_d <= caps["mandrel_collet_range_mm"][1]
                                 else "printed mandrel of the bore diameter (manual 3.4.14)"})
        sp["reasons"].append("through bore %.2f mm: mandrel defines the bore, base cylinder removes no material"
                             % bore_d)
        if bore_d < 4.0:
            sp["reasons"].append("bore below the schema minimum base diameter 4 mm")
        sp["score"] = score + 0.5  # Stereotech: Spiral is meant for cylindrical parts with holes
    else:
        sp["feasible"] = False
        sp["reasons"].append("no axial bore for a mandrel: 5D Spiral would cut the part")
    routes.append(sp)
    # 5D Full: core printed in flat layers; core must stay inside the part
    core_r = shaft_core_limit(mesh, axis, center)
    full = {"mode": "cylindrical_full", "parameters": {"axis": axis, "center": center}, "reasons": list(reasons),
            "score": score}
    if bore_r:
        full["feasible"] = False
        full["reasons"].append("part has a bore: Spiral is the matching 5D route")
    elif core_r is None or core_r * 2.0 < 4.0:
        full["feasible"] = False
        full["reasons"].append("no core of >= 4 mm fits inside the part along the axis")
    else:
        base_d = max(4.0, math.floor(min(2.0 * core_r * 0.8, 2.0 * core_r - 2.0) * 10.0) / 10.0)
        full["feasible"] = bool(length <= h5)
        full["parameters"].update({"base_diameter": base_d, "non_printing_base_diameter": caps["standard_mandrel_mm"],
                                   "core_radius_limit": core_r})
        full["reasons"].append("core %.1f mm inside min section radius %.2f mm (80%% / 1 mm margin policy)" %
                               (base_d, core_r))
    routes.append(full)
    return routes


def choose_route(routes, min_benefit=2.0):
    """Policy: take the best feasible 5D route only with a documented benefit >= min_benefit."""
    feasible = [r for r in routes if r["feasible"]]
    five = [r for r in feasible if r["mode"] != "classic" and r["score"] is not None and r["score"] >= min_benefit]
    if five:
        best = max(five, key=lambda r: (r["score"], r["mode"] == "cylindrical"))
        return computed(best["mode"], None, "choose_route", MODEL_POLICY,
                        inputs={"min_benefit": min_benefit}, note="; ".join(best["reasons"]))
    classic = [r for r in feasible if r["mode"] == "classic"]
    if classic:
        return computed("classic", None, "choose_route", MODEL_POLICY, inputs={"min_benefit": min_benefit},
                        note="no documented 5D benefit above threshold; classic is simpler")
    return unknown("choose_route", "no feasible route", status="infeasible")


def plan_5d(mesh, route, caps=None, eps_path=0.05, round_segments_max=256):
    """Scene operations and checks for a 5D route.

    The part axis is rotated onto Z and centred on the tooling axis; the part
    starts at z = 0 (tooling end face). `cylindrical_round_segments` is only
    reported: its meaning (float, unit mm in the schema) is ambiguous and must be
    verified in the engine, so it is not written by default.
    """
    p = route["parameters"]
    rot = geometry.rotation_to_z(p["axis"])
    placed, shift = geometry.place_on_axis(mesh, rot, p["center"])
    v = placed.vertices[placed.faces.reshape(-1)]
    r_max = float((v[:, 0] ** 2 + v[:, 1] ** 2).max() ** 0.5)
    chord = geometry.segments_for_chord_error(r_max, eps_path) if r_max > eps_path else None
    checks = []
    if chord is not None and chord.ok:
        n = chord.value
        checks.append({"check": "round_segments_for_chord_error", "required_N": n, "eps_path_mm": eps_path,
                       "schema_max": round_segments_max,
                       "status": "pass" if n <= round_segments_max else "unknown",
                       "note": "only if cylindrical_round_segments means chord count; verify in the engine"})
    checks.append({"check": "collisions", "status": "unknown",
                   "note": "head/tooling collision model not available offline (section 8)"})
    checks.append({"check": "kinematics", "status": "unknown",
                   "note": "A/C axis limits verified only from the engine toolpath"})
    return computed({"rotation": rot.tolist(), "translation": shift.tolist(), "r_max": r_max,
                     "axial_length": float(v[:, 2].max() - v[:, 2].min()), "checks": checks,
                     "base_diameter": p.get("base_diameter"),
                     "non_printing_base_diameter": p.get("non_printing_base_diameter")},
                    None, "plan_5d", MODEL_GEOMETRIC)
