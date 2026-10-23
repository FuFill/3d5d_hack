# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Process decisions for a chosen material/route/orientation (specification section 9).

Every decision carries the requirement ids and model ids that justify it.
Where no calibrated model exists the decision comes from an explicit policy
(class P, POLICY below) or from the manufacturer's material data sheet; this
is reported in the trace and never presented as a proven optimum.
"""

import math

from . import process, geometry
from .compiler import Decision
from .result import is_number

POLICY = {
    "layer_candidates_mm": [0.1, 0.15, 0.2, 0.25, 0.3],
    "functional_h_over_d": 0.5,          # mid level of the DOE seed {0.375, 0.5, 0.625}
    "shell_thickness_mm": {"structural": 1.6, "elastic": 1.2, "decorative": 1.2, "general": 1.2},
    "skin_thickness_mm": {"structural": 1.2, "elastic": 1.0, "decorative": 1.0, "general": 1.0},
    "infill_fraction": {"structural": 1.0, "elastic": 1.0, "decorative": 0.15, "general": 0.3},
    "tolerance_layer_share": 0.5,        # share of a surface tolerance given to the layer staircase
    "support_min_overhang_mm2": 25.0,
    "support_min_volume_mm3": 200.0,
    "orientation_tie": 0.05,             # keep the original orientation within 5 % of the best score
    "min_base_area_mm2": 20.0,
    "min_base_area_share": 0.005,
}

STRUCTURAL_TYPES = ("gear", "pump_impeller", "fan_impeller", "wheel_roller", "shaft_cam")


def load_class(reqset, part_type):
    if part_type == "decorative":
        return "decorative"
    if part_type == "boot_seal" or reqset.has("elasticity_over_range"):
        return "elastic"
    if part_type in STRUCTURAL_TYPES or any(reqset.has(p) for p in ("torque", "radial_load", "pressure")):
        return "structural"
    return "general"


def _ids(reqset, *props):
    return [r.id for r in reqset.requirements if r.property in props]


def choose_orientation(mesh, analysis, reqset, build_volume, support_angle=50.0, layer_height=0.2):
    """Classic orientation: feasibility first, then J_R; original orientation kept on a near tie."""
    cands = geometry.orientation_candidates(mesh)
    if [0.0, 0.0, 1.0] not in [[round(c, 6) for c in x] for x in cands]:
        cands.insert(0, [0.0, 0.0, 1.0])
    evals = [geometry.evaluate_orientation(mesh, c, support_angle, layer_height, build_volume) for c in cands]
    # a stable placement needs a real flat contact, not a line on a curved surface
    stable = [e for e in evals if e["base_area"] >= min(POLICY["min_base_area_mm2"],
                                                        POLICY["min_base_area_share"] * e["size"][0] * e["size"][1])]
    stable = stable or evals
    critical_weight = 2.0 if reqset.has("no_support_marks") else 1.0
    scored = geometry.score_orientations(stable, weights={"support_volume_upper": 1.0 * critical_weight,
                                                          "height": 0.3, "staircase_mean_mm": 0.5})
    if not scored:
        return None, evals
    best = scored[0]
    original = [e for e in scored if e["up"][2] > 0.9999]
    if original and original[0]["score"] <= best["score"] * (1.0 + POLICY["orientation_tie"]) + 1e-9:
        best = original[0]
        best["choice_note"] = "original orientation kept (within %.0f%% of best J_R)" % (POLICY["orientation_tie"] * 100)
    else:
        best["choice_note"] = "minimum J_R among stable orientations"
    return best, scored


def plan_process(reqset, material, route_mode, part_type, analysis, caps, orientation_eval=None, route=None):
    """Return (decisions, notes) for the compiler."""
    decisions, notes = [], []
    lc = load_class(reqset, part_type)
    notes.append({"item": "load_class", "value": lc, "model": "P"})
    decisions.append(Decision("printing_mode", route_mode, model_ids=["choose_route"],
                              note="route selected by documented policy"))
    # nozzle
    installed = caps["nozzle_diameters"][0]
    nz = material.process_window("nozzle_diameter")
    nozzle = installed
    if nz and nz[0] is not None and nz[0] > installed + 1e-9:
        nozzle = nz[0]
        decisions.append(Decision("nozzle_size", nozzle, model_ids=["material_datasheet"],
                                  note="material requires nozzle >= %.1f mm: install it before printing" % nz[0]))
    # layer height
    lmin = material.process_window("layer_height_min")
    h_lo = max(0.1, lmin[0] if lmin and lmin[0] is not None else 0.1)
    h_hi = 0.8 * nozzle
    allowed = [h for h in POLICY["layer_candidates_mm"] if h_lo - 1e-9 <= h <= h_hi + 1e-9]
    reason_ids = []
    if lc == "decorative" or reqset.has("no_visible_layers"):
        h = allowed[0]
        reason_ids = _ids(reqset, "no_visible_layers")
        notes.append({"item": "layer_height", "value": h, "model": "P",
                      "note": "smallest allowed layer for visible surfaces"})
    else:
        h = min(allowed, key=lambda x: abs(x - POLICY["functional_h_over_d"] * nozzle))
        notes.append({"item": "layer_height", "value": h, "model": "P",
                      "note": "h/d = %.2f functional seed" % POLICY["functional_h_over_d"]})
    tol = [r for r in reqset.requirements if r.property == "dimensional_tolerance"]
    for r in tol:
        coincident = (r.region == "bore" and (route_mode == "cylindrical" or route_mode == "classic"))
        if coincident:
            notes.append({"item": "tolerance_layer_budget", "requirement_id": r.id, "model": "A",
                          "note": "bore surface coincides with a layer/build direction: no staircase term"})
            continue
        eps = POLICY["tolerance_layer_share"] * r.value
        hmax = process.max_layer_height_for_step(eps, [(1.0, 0.0, 0.0)], (1.0, 0.0, 0.0))
        pick = process.choose_layer_height(hmax.value, allowed)
        reason_ids.append(r.id)
        if pick.ok:
            h = min(h, pick.value)
            notes.append({"item": "tolerance_layer_budget", "requirement_id": r.id, "model": "A",
                          "note": "h <= %.3f mm from eps_layer=%.3f" % (hmax.value, eps)})
        else:
            h = allowed[0]
            notes.append({"item": "tolerance_layer_budget", "requirement_id": r.id, "model": "A",
                          "status": "infeasible", "note": "required h %.3f mm is below the allowed %.2f mm: the "
                          "tolerance needs compensation or post-machining (no silent clamping)" %
                          (hmax.value, allowed[0])})
    decisions.append(Decision("layer_height", h, reason_requirement_ids=reason_ids,
                              model_ids=["choose_layer_height", "policy:layer"]))
    # walls / skins / infill
    t_shell = POLICY["shell_thickness_mm"][lc]
    wc = process.wall_count(t_shell, nozzle, nozzle)
    decisions.append(Decision("wall_count", wc.value["count"], model_ids=["wall_count", "policy:shell"],
                              note="shell %.2f mm -> %d walls (%.2f mm)" % (t_shell, wc.value["count"],
                                                                         wc.value["thickness"])))
    t_skin = POLICY["skin_thickness_mm"][lc]
    sk = process.skin_layers(t_skin, h)
    decisions.append(Decision("top_layers", sk.value["count"], model_ids=["skin_layers", "policy:skin"]))
    decisions.append(Decision("bottom_layers", sk.value["count"], model_ids=["skin_layers", "policy:skin"]))
    phi = POLICY["infill_fraction"][lc]
    imin = material.process_window("infill_min")
    if imin and imin[0] is not None and phi < imin[0] / 100.0:
        phi = imin[0] / 100.0
        notes.append({"item": "infill", "note": "raised to material minimum %g%%" % imin[0], "model": "S"})
    decisions.append(Decision("infill_fraction", phi, reason_requirement_ids=_ids(
        reqset, "torque", "radial_load", "sealing", "toughness"), model_ids=["policy:infill"],
        note="no calibrated relation load -> infill exists; policy by load class %s" % lc))
    # temperatures inside the material window
    profile_t = _float(material.slicer.get("settings", {}).get("print temperature"))
    win = material.process_window("nozzle_temperature")
    if win and win[0] is not None and profile_t is not None:
        hi = win[1] if win[1] is not None else win[0]
        hi = min(hi, caps["max_nozzle_temperature"])
        if not (win[0] <= profile_t <= hi):
            t = round((win[0] + hi) / 2.0)
            decisions.append(Decision("nozzle_temperature", t, model_ids=["thermal_window"],
                                      note="slicer profile %g C outside data-sheet window %g..%g C" %
                                           (profile_t, win[0], hi)))
        else:
            notes.append({"item": "nozzle_temperature", "value": profile_t, "model": "S",
                          "note": "material profile value inside data-sheet window %g..%g C" % (win[0], hi)})
    if route_mode == "classic":
        bt = _float(material.slicer.get("settings", {}).get("heated bed temperature"))
        bwin = material.process_window("bed_temperature")
        if bt is not None:
            t_bed, note = bt, "material profile value"
            if bwin and bwin[0] is not None:
                bhi = bwin[1] if bwin[1] is not None else bwin[0]
                if not (bwin[0] <= bt <= bhi):
                    t_bed = round((bwin[0] + bhi) / 2.0)
                    note = "slicer profile %g C outside data-sheet window %g..%g C" % (bt, bwin[0], bhi)
            # STE resolves the bed temperature as max() over all enabled extruders, so the
            # material of an idle second extruder could raise it: write it explicitly.
            decisions.append(Decision("bed_temperature", float(t_bed), model_ids=["thermal_window"],
                                      note=note + "; explicit because STE takes max() over extruders"))
    # speed: the material file's 'speed print' is not mapped by XmlMaterialProfile, set it explicitly
    sp = _float(material.slicer.get("settings", {}).get("speed print"))
    swin = material.process_window("print_speed")
    if sp is not None:
        v = sp
        if swin and swin[1] is not None:
            v = min(v, swin[1])
        if swin and swin[0] is not None:
            v = max(v, swin[0])
        decisions.append(Decision("speed_print", float(v), model_ids=["role_speed", "material_datasheet"],
                                  note="material profile/data-sheet speed; Q_limit uncalibrated "
                                       "(needs_calibration)"))
    # supports
    if route_mode == "classic":
        need = (orientation_eval is not None and
                orientation_eval["overhang_area"] > POLICY["support_min_overhang_mm2"] and
                orientation_eval["support_volume_upper"] > POLICY["support_min_volume_mm3"])
        decisions.append(Decision("support_enabled", bool(need), reason_requirement_ids=_ids(reqset, "no_support_marks"),
                                  model_ids=["support_plan"],
                                  note=("overhang %.0f mm2, support volume bound %.0f mm3 (thresholds %.0f mm2, "
                                        "%.0f mm3)" % (orientation_eval["overhang_area"],
                                                        orientation_eval["support_volume_upper"],
                                                        POLICY["support_min_overhang_mm2"],
                                                        POLICY["support_min_volume_mm3"]))
                                  if orientation_eval else None))
        if need and reqset.has("no_support_marks"):
            notes.append({"item": "support", "status": "conflict", "model": "S",
                          "note": "supports are needed for this orientation although marks are not allowed: "
                                  "post-processing or another route must be agreed"})
    else:
        decisions.append(Decision("support_enabled", False, model_ids=["plan_5d"],
                                  note="5D route prints the part without supports"))
    # 5D parameters
    if route_mode in ("cylindrical", "cylindrical_full") and route:
        p = route["parameters"]
        decisions.append(Decision("base_diameter_5d", float(p["base_diameter"]), model_ids=["plan_5d"],
                                  note="Spiral: equals the bore; Full: core inside the smallest section"))
        decisions.append(Decision("non_printing_base_diameter", float(p["non_printing_base_diameter"]),
                                  model_ids=["plan_5d"], note="tooling diameter (%s)" % p.get("mandrel", "6 mm rod")))
    return decisions, notes


def pack_batch(footprint_xy, quantity, bed_xy, spacing=10.0, brim=0.0):
    """Grid packing estimate of identical parts (G); exact placement is done by the slicer arrange."""
    if not quantity or quantity <= 1:
        return {"per_plate": 1, "plates": 1}
    w = footprint_xy[0] + 2 * brim + spacing
    d = footprint_xy[1] + 2 * brim + spacing
    best = 0
    for (a, b) in ((w, d), (d, w)):
        nx = int((bed_xy[0] + spacing) // a)
        ny = int((bed_xy[1] + spacing) // b)
        best = max(best, nx * ny)
    best = max(best, 1)
    return {"per_plate": min(best, int(quantity)), "plates": int(math.ceil(quantity / float(best))),
            "spacing_mm": spacing}


def _float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
