# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Analytical structural screening per load case (specification section 6).

The screen uses bounds so that a verdict is only issued when it is proven in
the conservative direction:
- FAIL when an optimistic (lower-bound) stress already exceeds the
  optimistic (upper-bound) strength;
- PASS only with an explicit safety factor against the lower-bound strength
  for checks whose model is itself conservative (thin rotating ring);
- otherwise UNKNOWN with the computed margin in the trace.
Geometric quantities taken from the mesh (pitch radius, face width, module)
are interpretations and are marked as assumed.
"""

import math

from . import mechanics, geometry
from .result import PASS, FAIL, UNKNOWN, is_number

# Lewis form factor Y for 20 deg full-depth involute teeth (standard table, load at tip).
LEWIS_Y_20DEG = [(12, 0.245), (13, 0.261), (14, 0.277), (15, 0.290), (16, 0.296), (17, 0.303), (18, 0.309),
                 (19, 0.314), (20, 0.322), (21, 0.328), (22, 0.331), (24, 0.337), (26, 0.346), (28, 0.353),
                 (30, 0.359), (34, 0.371), (38, 0.384), (43, 0.397), (50, 0.409), (60, 0.422), (75, 0.435),
                 (100, 0.447), (150, 0.460), (300, 0.472)]

POLICY_SF_ROTATING = 4.0  # policy P for the thin-ring rim screen of rotating parts


def lewis_y(z):
    """Linear interpolation of the 20 deg full-depth table; None outside the table."""
    if z is None or z < LEWIS_Y_20DEG[0][0] or z > LEWIS_Y_20DEG[-1][0]:
        return None
    for (z0, y0), (z1, y1) in zip(LEWIS_Y_20DEG, LEWIS_Y_20DEG[1:]):
        if z0 <= z <= z1:
            return y0 + (y1 - y0) * (z - z0) / float(z1 - z0)
    return None


def _strength_bounds(material, prop="tensile_strength_xy", cap=None):
    """(lower, upper, source). Without material data the upper bound falls back to the
    catalog-wide maximum `cap`, which can still prove a failure but never a pass."""
    iv = material.interval(prop) if material is not None else None
    if not iv or iv[0] is None:
        return None, cap, ("catalog maximum" if cap is not None else None)
    return iv[0], (iv[1] if iv[1] is not None else iv[0]), "material data sheet"


def gear_tooth_screen(reqset, mesh, rot, material, strength_cap=None):
    """Lewis bending screen of teeth for each torque load case."""
    out = []
    teeth = geometry.count_teeth(mesh, rot["axis"], rot["center"])
    if not teeth:
        return [{"check": "gear_tooth_bending", "status": UNKNOWN, "note": "tooth count not detected"}]
    z = teeth["count"]
    d_a = 2.0 * teeth["r_tip"]
    m = d_a / (z + 2.0)                      # standard addendum = m (assumed)
    r_pitch = m * z / 2.0
    b = rot["axial_length"]                  # face width = axial length (assumed, upper bound)
    y = lewis_y(z)
    s_lo, s_hi, s_src = _strength_bounds(material, cap=strength_cap)
    assumptions = ["z=%d from the outer contour harmonic" % z, "module m=d_a/(z+2)=%.3f mm" % m,
                   "20 deg full-depth involute (Lewis Y table)", "face width = axial length %.2f mm (upper bound)" % b,
                   "dynamic/load factor K=1 (lower bound of stress)", "strength = XY tensile strength of the "
                   "material data sheet (upper bound)"]
    for r in reqset.requirements:
        if r.property != "torque" or not is_number(r.upper):
            continue
        ft = mechanics.tangential_force(r.upper, r_pitch)
        sig = mechanics.lewis_bending_stress(ft.value, b, m, y, 1.0) if y else None
        rec = {"check": "gear_tooth_bending", "requirement_id": r.id, "load_case": r.load_case,
               "torque_Nmm": r.upper, "pitch_radius_mm": r_pitch, "Ft_N": ft.value, "module_mm": m, "teeth": z,
               "Y": y, "assumptions": assumptions, "evidence": "analytical_screen"}
        if sig is None or not sig.ok or s_hi is None:
            rec.update({"status": UNKNOWN, "note": "missing Y or strength"})
        else:
            rec["sigma_lower_MPa"] = sig.value
            rec["strength_upper_MPa"] = s_hi
            rec["strength_source"] = s_src
            rec["ratio_lower"] = sig.value / s_hi
            if sig.value > s_hi:
                rec["status"] = FAIL
                rec["note"] = ("lower-bound bending stress %.0f MPa exceeds upper-bound strength %.0f MPa (%s): "
                               "the tooth cannot carry %.0f N*m in this size/material" %
                               (sig.value, s_hi, s_src, r.upper / 1000.0))
            else:
                rec["status"] = UNKNOWN
                rec["note"] = ("lower-bound stress %.0f MPa below strength %.0f MPa (%s); K, fatigue, contact and "
                               "interlayer stresses unknown" % (sig.value, s_hi, s_src))
        out.append(rec)
    return out


def shaft_torsion_screen(reqset, mesh, axis, center, material, strength_cap=None):
    """Torsion of the smallest circular section of a shaft (journals), cams excluded."""
    prof = geometry.radial_profile(mesh, axis, center, 40)
    if not prof:
        return []
    inner = prof[2:-2] or prof
    d_min = 2.0 * min(p["r_outer"] for p in inner)
    s_lo, s_hi, s_src = _strength_bounds(material, cap=strength_cap)
    out = []
    for r in reqset.requirements:
        if r.property != "torque" or not is_number(r.upper):
            continue
        tor = mechanics.shaft_torsion(r.upper, d_min)
        rec = {"check": "shaft_torsion", "requirement_id": r.id, "load_case": r.load_case,
               "torque_Nmm": r.upper, "d_min_mm": d_min, "evidence": "analytical_screen",
               "assumptions": ["solid circular section at the smallest outer radius along the axis",
                               "no stress concentration (keyways/cam transitions need FEA)"]}
        if not tor.ok:
            rec.update({"status": UNKNOWN, "note": "torsion not computable"})
        else:
            tau = tor.value["tau_max"]
            rec["tau_max_MPa"] = tau
            if s_hi is not None and tau > s_hi:
                rec.update({"status": FAIL, "note": "shear stress %.1f MPa exceeds tensile strength %.1f MPa (%s); "
                            "shear strength is assumed not to exceed tensile strength" % (tau, s_hi, s_src)})
            else:
                rec.update({"status": UNKNOWN,
                            "note": "tau=%.1f MPa; shear strength of the printed structure unknown (ratio to "
                                    "tensile %s); von Mises is not applied to FDM automatically" %
                                    (tau, ("%.2f" % (tau / s_lo)) if s_lo else "n/a")})
        out.append(rec)
    return out


def rotating_rim_screen(reqset, material, r_max_mm):
    """Thin-ring hoop stress rho*v^2 (conservative for discs) against strength with policy SF."""
    v = None
    for r in reqset.requirements:
        if r.property == "peripheral_speed" and is_number(r.upper):
            v = r.upper
    n = None
    for r in reqset.requirements:
        if r.property == "rotation_speed" and is_number(r.upper):
            n = r.upper  # rad/s
    if v is None and n is not None and r_max_mm:
        v = n * r_max_mm
    rho = material.interval("density") if material is not None else None
    if v is None or not rho:
        return []
    s_lo, _, _ = _strength_bounds(material)
    sig = mechanics.rotating_ring_stress(rho[1], v)
    rec = {"check": "rotating_rim", "peripheral_speed_mm_s": v, "sigma_MPa": sig.value, "evidence": "analytical_screen",
           "policy_safety_factor": POLICY_SF_ROTATING,
           "assumptions": ["thin ring rho*v^2 bounds disc hoop stress", "blades and hub not covered"]}
    if s_lo is None:
        rec.update({"status": UNKNOWN, "note": "strength unknown"})
    elif POLICY_SF_ROTATING * sig.value <= s_lo:
        rec.update({"status": PASS, "note": "rim stress %.2f MPa, SF %.0f against %.1f MPa" %
                    (sig.value, POLICY_SF_ROTATING, s_lo)})
    else:
        rec.update({"status": UNKNOWN, "note": "rim stress %.2f MPa too close to strength %.1f MPa" % (sig.value, s_lo)})
    return [rec]


def balance_screen(reqset, material, volume_mm3):
    """Permissible unbalance from the G grade vs the numeric limit of the TZ."""
    grade = reqset.first("balance_grade")
    limit = reqset.first("residual_unbalance")
    speed = reqset.first("rotation_speed")
    rho = material.interval("density") if material is not None else None
    if not (grade and speed and rho and volume_mm3):
        return []
    n_rpm = speed.upper * 60.0 / (2.0 * math.pi)
    mass_kg = rho[1] * volume_mm3 / 1000.0
    per = mechanics.permissible_unbalance(grade.upper, mass_kg, n_rpm)
    rec = {"check": "balance_grade_vs_limit", "evidence": "analytical_screen", "mass_kg_upper": mass_kg,
           "speed_rpm": n_rpm, "U_per_gmm": per.value["U_per_gmm"], "e_per_mm": per.value["e_per_mm"],
           "status": UNKNOWN}
    note = "G%.1f at %.0f rpm and %.3f kg gives U_per = %.2f g*mm" % (grade.upper, n_rpm, mass_kg,
                                                                    per.value["U_per_gmm"])
    if limit:
        note += "; TZ states %.2f g*mm: the stricter value governs balancing" % limit.upper
        rec["governing_limit_gmm"] = min(limit.upper, per.value["U_per_gmm"])
    rec["note"] = note + "; residual unbalance of the printed part must be measured"
    return [rec]


def catalog_strength_cap(catalog, prop="tensile_strength_xy"):
    """Largest documented strength among selectable materials (upper bound for unknown data)."""
    vals = []
    for m in catalog.selectable():
        iv = m.interval(prop)
        if iv and iv[1] is not None:
            vals.append(iv[1])
    return max(vals) if vals else None


def screen_structure(reqset, mesh, rot, material, route_mode, part_type, volume_mm3, route_axis=None,
                     route_center=None, strength_cap=None):
    checks = []
    if part_type == "gear" and rot and rot.get("is_rotational_candidate"):
        checks += gear_tooth_screen(reqset, mesh, rot, material, strength_cap)
    elif part_type == "shaft_cam" and route_axis is not None:
        checks += shaft_torsion_screen(reqset, mesh, route_axis, route_center, material, strength_cap)
    if rot and part_type in ("pump_impeller", "fan_impeller", "wheel_roller"):
        checks += rotating_rim_screen(reqset, material, rot.get("r_max"))
        checks += balance_screen(reqset, material, volume_mm3)
    for r in reqset.requirements:
        if r.property == "operating_hours" and reqset.first("rotation_speed"):
            n_rpm = reqset.first("rotation_speed").upper * 60.0 / (2.0 * math.pi)
            cyc = mechanics.rotation_cycles(n_rpm, r.upper / 3600.0)
            checks.append({"check": "fatigue_cycles", "requirement_id": r.id, "cycles": cyc.value,
                           "status": UNKNOWN, "evidence": "analytical_screen",
                           "note": "%.3g revolutions; S-N data of the printed material required" % cyc.value})
            break
    return checks
