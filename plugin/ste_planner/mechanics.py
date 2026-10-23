# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Analytical screening of loads (specification section 6, document 03 section 5.4).

Units: mm, N, N*mm, MPa, g/mm^3, rad/s. Every function refuses to compute
when an input is missing; coefficients that need calibration (Lewis Y, S-N
constants, cellular C/n, Archard k, friction mu) are never invented.
"""

import math

from .result import (computed, unknown, invalid, is_number, tri_compare, PASS, FAIL, UNKNOWN,
                     MODEL_ANALYTIC, MODEL_EXPERIMENTAL, MODEL_GEOMETRIC)


def _check(model_id, positive=None, nonneg=None):
    missing = [k for k, v in sorted((positive or {}).items()) if not is_number(v)]
    missing += [k for k, v in sorted((nonneg or {}).items()) if not is_number(v)]
    if missing:
        return unknown(model_id, "missing inputs", missing=missing)
    bad = [k for k, v in sorted((positive or {}).items()) if float(v) <= 0]
    bad += [k for k, v in sorted((nonneg or {}).items()) if float(v) < 0]
    if bad:
        return invalid(model_id, "non-physical inputs: %s" % ", ".join(bad))
    return None


def tangential_force(torque_nmm, radius_mm):
    """Ft = M / r at the pitch (or winding) radius."""
    err = _check("tangential_force", {"torque": torque_nmm, "radius": radius_mm})
    if err:
        return err
    return computed(torque_nmm / radius_mm, "N", "tangential_force", MODEL_ANALYTIC,
                    inputs={"M_Nmm": torque_nmm, "r_mm": radius_mm})


def bending_stress(moment_nmm, c_mm, inertia_mm4):
    """sigma = M_b c / I (simple beam)."""
    err = _check("bending_stress", {"c": c_mm, "I": inertia_mm4}, {"M": abs(moment_nmm) if is_number(moment_nmm) else None})
    if err:
        return err
    return computed(moment_nmm * c_mm / inertia_mm4, "MPa", "bending_stress", MODEL_ANALYTIC,
                    inputs={"M": moment_nmm, "c": c_mm, "I": inertia_mm4})


def cantilever_deflection(force_n, length_mm, modulus_mpa, inertia_mm4):
    """delta = F L^3 / (3 E I) for an end load."""
    err = _check("cantilever_deflection", {"L": length_mm, "E": modulus_mpa, "I": inertia_mm4},
                 {"F": abs(force_n) if is_number(force_n) else None})
    if err:
        return err
    return computed(force_n * length_mm ** 3 / (3.0 * modulus_mpa * inertia_mm4), "mm",
                    "cantilever_deflection", MODEL_ANALYTIC,
                    inputs={"F": force_n, "L": length_mm, "E": modulus_mpa, "I": inertia_mm4})


def polar_moment_hollow(d_outer, d_inner=0.0):
    """J = pi (d_o^4 - d_i^4) / 32."""
    err = _check("polar_moment_hollow", {"d_o": d_outer}, {"d_i": d_inner})
    if err:
        return err
    if d_inner >= d_outer:
        return invalid("polar_moment_hollow", "d_i must be smaller than d_o")
    return computed(math.pi * (d_outer ** 4 - d_inner ** 4) / 32.0, "mm^4", "polar_moment_hollow",
                    MODEL_GEOMETRIC, inputs={"d_o": d_outer, "d_i": d_inner})


def shaft_torsion(torque_nmm, d_outer, d_inner=0.0, length_mm=None, shear_modulus_mpa=None):
    """tau_max = M r_o / J; phi = M L / (G J) when L and G are known.

    Not applicable directly to cam profiles, screw zones or keyways
    (stress concentrations / FEA required).
    """
    j = polar_moment_hollow(d_outer, d_inner)
    if not j.ok:
        return j
    err = _check("shaft_torsion", {"torque": abs(torque_nmm) if is_number(torque_nmm) else None})
    if err:
        return err
    tau = torque_nmm * (d_outer / 2.0) / j.value
    value = {"tau_max": tau, "J": j.value, "phi_rad": None}
    if is_number(length_mm) and is_number(shear_modulus_mpa) and shear_modulus_mpa > 0:
        value["phi_rad"] = torque_nmm * length_mm / (shear_modulus_mpa * j.value)
    return computed(value, "MPa", "shaft_torsion", MODEL_ANALYTIC,
                    inputs={"M": torque_nmm, "d_o": d_outer, "d_i": d_inner, "L": length_mm, "G": shear_modulus_mpa},
                    note="Circular section only; concentrators need FEA")


def lewis_bending_stress(tangential_force_n, face_width_mm, module_mm, form_factor_y, k_factor):
    """sigma_b ~= K Ft / (b m Y). Y and K must be determined, never read from a picture."""
    err = _check("lewis_bending_stress", {"Ft": tangential_force_n, "b": face_width_mm, "m": module_mm,
                                          "Y": form_factor_y, "K": k_factor})
    if err:
        return err
    return computed(k_factor * tangential_force_n / (face_width_mm * module_mm * form_factor_y), "MPa",
                    "lewis_bending_stress", MODEL_ANALYTIC,
                    inputs={"Ft": tangential_force_n, "b": face_width_mm, "m": module_mm, "Y": form_factor_y,
                            "K": k_factor})


def euler_buckling(modulus_mpa, inertia_mm4, length_mm, k_l=1.0):
    """P_cr = pi^2 E I / (K_L L)^2 (linear elastic, matching boundary conditions)."""
    err = _check("euler_buckling", {"E": modulus_mpa, "I": inertia_mm4, "L": length_mm, "K_L": k_l})
    if err:
        return err
    return computed(math.pi ** 2 * modulus_mpa * inertia_mm4 / (k_l * length_mm) ** 2, "N", "euler_buckling",
                    MODEL_ANALYTIC, inputs={"E": modulus_mpa, "I": inertia_mm4, "L": length_mm, "K_L": k_l})


def hoop_stress(pressure_mpa, radius_mm, thickness_mm):
    """sigma_hoop ~= p r / t, first screening far from bellows and fits."""
    err = _check("hoop_stress", {"r": radius_mm, "t": thickness_mm}, {"p": pressure_mpa})
    if err:
        return err
    return computed(pressure_mpa * radius_mm / thickness_mm, "MPa", "hoop_stress", MODEL_ANALYTIC,
                    inputs={"p": pressure_mpa, "r": radius_mm, "t": thickness_mm})


def thin_wall_torsion(torque_nmm, area_mid_mm2, thickness_mm):
    """tau ~= M / (2 A_mid t) for a thin closed section."""
    err = _check("thin_wall_torsion", {"A_mid": area_mid_mm2, "t": thickness_mm},
                 {"M": abs(torque_nmm) if is_number(torque_nmm) else None})
    if err:
        return err
    return computed(torque_nmm / (2.0 * area_mid_mm2 * thickness_mm), "MPa", "thin_wall_torsion",
                    MODEL_ANALYTIC, inputs={"M": torque_nmm, "A_mid": area_mid_mm2, "t": thickness_mm})


def rect_shell_section(b, h, t, infill_fraction):
    """Rectangular b x H section with uniform shell t (document 03, 5.4).

    A_shell = bH - (b-2t)(H-2t); I_shell = [bH^3 - (b-2t)(H-2t)^3]/12;
    A_material_est = A_shell + phi A_core.
    """
    err = _check("rect_shell_section", {"b": b, "H": h, "t": t}, {"phi": infill_fraction})
    if err:
        return err
    if infill_fraction > 1:
        return invalid("rect_shell_section", "phi is a fraction in [0, 1]")
    bi, hi = max(b - 2 * t, 0.0), max(h - 2 * t, 0.0)
    a_shell = b * h - bi * hi
    i_shell = (b * h ** 3 - bi * hi ** 3) / 12.0
    a_core = bi * hi
    return computed({"A_shell": a_shell, "I_shell": i_shell, "A_core": a_core,
                     "A_material_est": a_shell + infill_fraction * a_core}, "mm^2|mm^4",
                    "rect_shell_section", MODEL_GEOMETRIC, inputs={"b": b, "H": h, "t": t, "phi": infill_fraction})


def rotating_ring_stress(density_g_mm3, peripheral_speed_mm_s):
    """Hoop stress of a thin rotating ring sigma = rho v^2 (upper screening for rim)."""
    err = _check("rotating_ring_stress", {"rho": density_g_mm3, "v": peripheral_speed_mm_s})
    if err:
        return err
    # 1 g/mm^3 = 1e6 kg/m^3; sigma[Pa] = rho[kg/m^3] * v[m/s]^2
    sigma_pa = density_g_mm3 * 1e6 * (peripheral_speed_mm_s / 1000.0) ** 2
    return computed(sigma_pa * 1e-6, "MPa", "rotating_ring_stress", MODEL_ANALYTIC,
                    inputs={"rho": density_g_mm3, "v": peripheral_speed_mm_s},
                    note="Thin ring; discs/blades require FEA")


def unbalance_force(mass_g, eccentricity_mm, speed_rpm):
    """F = m e omega^2 in consistent SI."""
    err = _check("unbalance_force", {"m": mass_g, "n": speed_rpm}, {"e": eccentricity_mm})
    if err:
        return err
    omega = 2.0 * math.pi * speed_rpm / 60.0
    f = (mass_g / 1000.0) * (eccentricity_mm / 1000.0) * omega ** 2
    return computed(f, "N", "unbalance_force", MODEL_ANALYTIC,
                    inputs={"m_g": mass_g, "e_mm": eccentricity_mm, "n_rpm": speed_rpm})


def permissible_unbalance(grade_mm_s, rotor_mass_kg, speed_rpm):
    """U_per [g mm] = 9549.3 G [mm/s] m [kg] / n [rpm]; e_per = G / omega [mm].

    9549.3 = 1000 * 60 / (2 pi). Whether the balancing standard cited by the
    TZ is current has to be checked separately.
    """
    err = _check("permissible_unbalance", {"G": grade_mm_s, "m": rotor_mass_kg, "n": speed_rpm})
    if err:
        return err
    omega = 2.0 * math.pi * speed_rpm / 60.0
    u = 1000.0 * 60.0 / (2.0 * math.pi) * grade_mm_s * rotor_mass_kg / speed_rpm
    return computed({"U_per_gmm": u, "e_per_mm": grade_mm_s / omega}, "g*mm", "permissible_unbalance",
                    MODEL_ANALYTIC, inputs={"G": grade_mm_s, "m_kg": rotor_mass_kg, "n_rpm": speed_rpm})


def unbalance_vector(masses_g, positions_mm, axis_point=(0.0, 0.0, 0.0), axis=(0.0, 0.0, 1.0)):
    """Static unbalance vector sum(m_i r_i) about an axis, in g*mm."""
    import numpy
    a = numpy.asarray(axis, dtype=float)
    a = a / numpy.linalg.norm(a)
    p = numpy.asarray(positions_mm, dtype=float) - numpy.asarray(axis_point, dtype=float)
    r = p - numpy.outer(p.dot(a), a)
    u = (numpy.asarray(masses_g, dtype=float)[:, None] * r).sum(axis=0)
    return computed({"vector": u.tolist(), "magnitude": float(numpy.linalg.norm(u))}, "g*mm",
                    "unbalance_vector", MODEL_GEOMETRIC)


def rotation_cycles(speed_rpm, hours, cycles_per_rev=1.0):
    """N = n * 60 * hours * cycles_per_rev, if a cycle is really defined by a revolution."""
    err = _check("rotation_cycles", {"n": speed_rpm, "hours": hours, "cycles_per_rev": cycles_per_rev})
    if err:
        return err
    return computed(speed_rpm * 60.0 * hours * cycles_per_rev, "count", "rotation_cycles", MODEL_ANALYTIC,
                    inputs={"n_rpm": speed_rpm, "hours": hours, "cycles_per_rev": cycles_per_rev})


def sn_cycles_to_failure(stress_amplitude_mpa, c_mpa, b_exponent):
    """Calibrated sigma_a = C N^b  ->  N = (sigma_a / C)^(1/b)."""
    err = _check("sn_cycles_to_failure", {"sigma_a": stress_amplitude_mpa, "C": c_mpa})
    if err:
        return err
    if not is_number(b_exponent) or b_exponent >= 0:
        return unknown("sn_cycles_to_failure", "calibrated negative exponent b required", ["b"])
    return computed((stress_amplitude_mpa / c_mpa) ** (1.0 / b_exponent), "count", "sn_cycles_to_failure",
                    MODEL_EXPERIMENTAL, inputs={"sigma_a": stress_amplitude_mpa, "C": c_mpa, "b": b_exponent})


def miner_damage(blocks):
    """D = sum n_i / N_i over (n_i, N_i) blocks; unknown if any N_i is unknown."""
    total = 0.0
    for n_i, cap in blocks:
        if not (is_number(n_i) and is_number(cap)) or cap <= 0:
            return unknown("miner_damage", "every block needs n_i and N_i > 0")
        total += float(n_i) / float(cap)
    return computed(total, "fraction", "miner_damage", MODEL_ANALYTIC,
                    note="Miner rule is an approximation, not a proof of polymer life")


def max_stress_failure_index(stress, strength):
    """FI = max(sigma_i+/Xt_i, sigma_i-/Xc_i, |tau_ij|/S_ij) for the given components.

    stress: {"s1","s2","s3","t12","t23","t13"} in MPa (local material axes).
    strength: {"Xt","Xc","Yt","Yc","Zt","Zc","S12","S23","S13"} in MPa.
    Components whose strength is unknown make the index unknown.
    """
    pairs = [("s1", "Xt", "Xc"), ("s2", "Yt", "Yc"), ("s3", "Zt", "Zc")]
    ratios = {}
    missing = []
    for s, t, c in pairs:
        v = stress.get(s, 0.0) or 0.0
        if v > 0:
            if not is_number(strength.get(t)):
                missing.append(t)
            else:
                ratios[s + "+"] = v / strength[t]
        elif v < 0:
            if not is_number(strength.get(c)):
                missing.append(c)
            else:
                ratios[s + "-"] = -v / strength[c]
    for s, k in (("t12", "S12"), ("t23", "S23"), ("t13", "S13")):
        v = stress.get(s, 0.0) or 0.0
        if v != 0:
            if not is_number(strength.get(k)):
                missing.append(k)
            else:
                ratios[s] = abs(v) / strength[k]
    if missing:
        return unknown("max_stress_failure_index", "missing directional strengths", missing)
    fi = max(ratios.values()) if ratios else 0.0
    governing = max(ratios, key=ratios.get) if ratios else None
    return computed({"FI": fi, "governing": governing, "ratios": ratios}, "fraction",
                    "max_stress_failure_index", MODEL_ANALYTIC)


def safety_check(failure_index, safety_factor):
    """SF * FI <= 1 as a three-valued check (SF is a policy input)."""
    if not (is_number(failure_index) and is_number(safety_factor)):
        return UNKNOWN
    return PASS if safety_factor * failure_index <= 1.0 else FAIL


def cellular_property(solid_value, infill_fraction, coefficient=None, exponent=None):
    """X_core = C X_s phi^n for a calibrated cellular structure; phi=0 is a void."""
    if not is_number(infill_fraction) or not 0 <= infill_fraction <= 1:
        return invalid("cellular_property", "phi must be a fraction in [0, 1]")
    if infill_fraction == 0:
        return computed(0.0, None, "cellular_property", MODEL_EXPERIMENTAL, note="phi = 0: void")
    if not (is_number(coefficient) and is_number(exponent)):
        return unknown("cellular_property", "C and n depend on pattern/material/direction; calibration required",
                       ["C", "n"])
    if not is_number(solid_value):
        return unknown("cellular_property", "solid printed property unknown", ["X_s"])
    value = coefficient * solid_value * infill_fraction ** exponent
    return computed(min(value, solid_value), None, "cellular_property", MODEL_EXPERIMENTAL,
                    inputs={"X_s": solid_value, "phi": infill_fraction, "C": coefficient, "n": exponent})


def traction_check(required_force_n, mu_lower, normal_force_n):
    """F_t_required <= mu_lower * N; mu must come from a test of the actual pair."""
    if not is_number(mu_lower):
        return UNKNOWN
    if not (is_number(required_force_n) and is_number(normal_force_n)):
        return UNKNOWN
    return tri_compare(required_force_n, "<=", mu_lower * normal_force_n)


def archard_wear_volume(k_wear, normal_force_n, sliding_mm, hardness_mpa):
    """V = k F s / H once k has been measured for the mechanism."""
    if not is_number(k_wear):
        return unknown("archard_wear_volume", "wear coefficient k requires a test", ["k"])
    err = _check("archard_wear_volume", {"H": hardness_mpa}, {"F": normal_force_n, "s": sliding_mm})
    if err:
        return err
    return computed(k_wear * normal_force_n * sliding_mm / hardness_mpa, "mm^3", "archard_wear_volume",
                    MODEL_EXPERIMENTAL)


def creep_strain_linear(compliance_per_mpa, stress_mpa):
    """eps(t) = J_creep(t, T, env) sigma within the linear region."""
    if not is_number(compliance_per_mpa):
        return unknown("creep_strain_linear", "creep compliance J(t,T,env) not available", ["J_creep"])
    return computed(compliance_per_mpa * stress_mpa, "fraction", "creep_strain_linear", MODEL_EXPERIMENTAL)


def neo_hookean_energy(mu_mpa, bulk_mpa, deformation_gradient):
    """W = mu/2 (I1_bar - 3) + K/2 (J - 1)^2, MPa; mu and K must be calibrated."""
    import numpy
    if not (is_number(mu_mpa) and is_number(bulk_mpa)):
        return unknown("neo_hookean_energy", "calibrated mu and K required", ["mu", "K"])
    f = numpy.asarray(deformation_gradient, dtype=float)
    j = float(numpy.linalg.det(f))
    if j <= 0:
        return invalid("neo_hookean_energy", "det(F) must be positive")
    i1_bar = j ** (-2.0 / 3.0) * float(numpy.trace(f.T.dot(f)))
    w = mu_mpa / 2.0 * (i1_bar - 3.0) + bulk_mpa / 2.0 * (j - 1.0) ** 2
    return computed(w, "MPa", "neo_hookean_energy", MODEL_EXPERIMENTAL, inputs={"mu": mu_mpa, "K": bulk_mpa})
