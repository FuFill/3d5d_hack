# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Purpose/regions + scoped measured process profiles -> constrained orientation.

FEA owns mechanics, the compiler owns settings, the slicer owns actual supports.
This module compares finite candidates and never trades hard limits for looks.
Unmeasured Ra/flatness and unverified 5D kinematics stay unknown.
"""

import copy
import math

import numpy as np

from . import compiler, elasticity, geometry
from .result import computed, unknown, invalid, MODEL_ANALYTIC, EVIDENCE_SIMULATION

PROCESS_KEYS = ("machine_nozzle_size", "line_width", "layer_height", "wall_line_count", "top_layers",
                "bottom_layers", "infill_sparse_density", "infill_pattern", "material_print_temperature",
                "material_bed_temperature", "speed_print", "cool_fan_speed")

# Reserved geometric separation, ten times native G-code's 0.001 mm rounding.
# This is an algorithmic margin, not a claim about printer positioning accuracy.
SUPPORT_NUMERICAL_CLEARANCE_MM = 0.01


def _native_decision(key, value, original=None):
    converted = value
    for spec in compiler.QUANTITIES.values():
        if spec[0] == key and spec[2] == "percent":
            converted = float(value) / 100.0
    original = original or {}
    return compiler.Decision("functional", converted, key=key,
                             reason_requirement_ids=original.get("reason_requirement_ids"),
                             model_ids=original.get("model_ids", []) + ["functional_orientation"],
                             note=original.get("note") or "scoped functional process profile")


def _compile(base_patch, profile, mode, support, base_resolver):
    items = dict((i["key"], i) for i in base_patch["items"])
    values = dict((k, v["value"]) for k, v in items.items())
    settings = profile["settings"]
    if not isinstance(settings, dict) or any(k not in PROCESS_KEYS for k in settings):
        raise ValueError("profile contains unsupported process settings")
    if any(k not in settings for k in PROCESS_KEYS):
        raise ValueError("profile must scope every required process setting")
    values.update((k, v) for k, v in settings.items()
                  if k in items or not _same(v, base_patch["resolver"].value(k, extruder="0")))
    values.update({"printing_mode": mode, "support_enable": support})
    patch = compiler.compile_patch([_native_decision(k, values[k], None if k in settings else items.get(k))
                                    for k in sorted(values)], base_resolver)
    if patch["errors"] or any(i["enabled_after_patch"] is not True for i in patch["items"]):
        raise ValueError("profile fails installed-slicer validation: " + str(patch["errors"]))
    if any(not _same(v, patch["resolver"].value(k, extruder="0")) for k, v in settings.items()):
        raise ValueError("resolved settings differ from the calibrated process profile")
    return patch


def _same(a, b):
    if isinstance(a, (int, float)) and not isinstance(a, bool) and isinstance(b, (int, float)) and not isinstance(b, bool):
        return abs(a - b) <= 1e-8 * max(1.0, abs(a), abs(b))
    return a == b


def layer_normals(centers, rotation, mode, route):
    if mode == "classic":
        return np.tile(np.asarray(rotation)[2], (len(centers), 1))
    p = route["parameters"]
    axis = np.asarray(p["axis"], dtype=float)
    axis /= np.linalg.norm(axis)
    relative = centers - np.asarray(p["center"], dtype=float)
    radial = relative - np.outer(relative.dot(axis), axis)
    radii = np.linalg.norm(radial, axis=1)
    core_radius = float(p["base_diameter"]) / 2
    core = radii < core_radius if mode == "cylindrical_full" else np.zeros(len(centers), dtype=bool)
    if ((radii < 1e-9) & ~core).any():
        raise ValueError("undefined cylindrical layer normal on the axis")
    normals = radial / np.maximum(radii[:, None], 1e-12)
    normals[core] = axis
    return normals


def _rotations(mesh, mode, initial, route, refine, mechanical_ups=None):
    if mode != "classic":
        for end in (np.eye(3), np.diag([1.0, -1.0, -1.0])):
            for phase in (0, math.pi / 2, math.pi, 3 * math.pi / 2):
                yaw = np.array([[math.cos(phase), -math.sin(phase), 0],
                                [math.sin(phase), math.cos(phase), 0], [0, 0, 1]])
                yield yaw.dot(end).dot(initial)
        return
    candidates = [np.asarray(initial, dtype=float)]
    for up in geometry.orientation_candidates(mesh) + list(mechanical_ups or []):
        rotation = geometry.rotation_to_z(up)
        for angle in (0, math.pi / 2):
            yaw = np.array([[math.cos(angle), -math.sin(angle), 0],
                            [math.sin(angle), math.cos(angle), 0], [0, 0, 1]])
            candidates.append(yaw.dot(rotation))
    # Optional single refinement ring; bounded finite search, never a global optimum claim.
    for rotation in candidates:
        yield rotation
    if refine:
        for axis in (0, 1):
            for angle in (-math.radians(10), math.radians(10)):
                c, s = math.cos(angle), math.sin(angle)
                delta = np.array([[1, 0, 0], [0, c, -s], [0, s, c]]) if axis == 0 else np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
                yield delta.dot(initial)


def _regions(mesh, regions):
    ids, out = set(), []
    for region in regions:
        rid = region["id"]
        if not isinstance(rid, str) or not rid.strip() or rid in ids:
            raise ValueError("distinct region ids required")
        ids.add(rid)
        faces = region["face_ids"]
        if not isinstance(faces, list) or not faces or any(isinstance(i, bool) or not isinstance(i, int)
                                                         or i < 0 or i >= len(mesh.faces) for i in faces):
            raise ValueError("region face_ids must address this geometry")
        if len(set(faces)) != len(faces):
            raise ValueError("duplicate face ids in a region")
        weight = float(region.get("importance", 1.0))
        if not math.isfinite(weight) or weight <= 0:
            raise ValueError("positive finite region importance required")
        if not isinstance(region.get("forbid_support_contact", False), bool):
            raise ValueError("forbid_support_contact must be boolean")
        out.append(dict(region, importance=weight))
    return out


def _surface(mesh, rotation, mode, route, h, regions, caps):
    normals, areas = geometry.face_normals_areas(mesh)
    centers = mesh.triangles.mean(axis=1)
    b = layer_normals(centers, rotation, mode, route)
    cosine = np.abs((normals * b).sum(axis=1))
    cusp = h * cosine * (cosine < 0.999)
    placed, shift = (geometry.place_on_bed(mesh, rotation) if mode == "classic" else
                     geometry.place_on_axis(mesh, rotation, route["parameters"]["center"]))
    if mode == "classic":
        detail = geometry.evaluate_orientation(mesh, np.asarray(rotation)[2], layer_height=h,
                                               build_volume=caps["build_volume"][mode], rotation=rotation)
        pn, _ = geometry.face_normals_areas(placed)
        pz = placed.triangles[:, :, 2].mean(axis=1)
        over = geometry.overhang_mask(pn, pz, float(placed.vertices[:, 2].min()), 50.0)
        contact = np.where(over)[0].tolist()
    else:
        vertices = placed.vertices
        rmax = np.linalg.norm(vertices[:, :2], axis=1).max()
        bv = caps["build_volume"][mode]
        fits = bool(2 * rmax <= min(bv[:2]) and vertices[:, 2].max() - vertices[:, 2].min() <= bv[2])
        detail = {"fits_build_volume": fits, "height": float(np.ptp(vertices[:, 2])),
                  "support_volume_upper": None, "base_area": None}
        contact = None
    summaries, forbidden = [], set()
    for region in regions:
        idx = np.asarray(region["face_ids"], dtype=int)
        support_area = float(areas[idx[np.isin(idx, contact)]].sum()) if contact is not None else None
        if region.get("forbid_support_contact"):
            forbidden.update(idx.tolist())
        summaries.append({"id": region["id"], "importance": region["importance"],
                          "staircase_mean_mm": float(np.average(cusp[idx], weights=areas[idx])),
                          "staircase_max_mm": float(cusp[idx].max()), "support_contact_upper_mm2": support_area,
                          "Ra_um": None, "flatness_mm": None,
                          "surface_evidence": "geometric_proxy; Ra and thermal flatness require calibration"})
    if contact is not None and forbidden.intersection(contact):
        detail["forbidden_contact"] = True
    detail["regions"] = summaries
    detail["staircase_mean_mm"] = float(np.average(cusp, weights=areas))
    detail["visible_staircase_mm"] = (sum(r["importance"] * r["staircase_mean_mm"] for r in summaries)
                                         / sum(r["importance"] for r in summaries)) if summaries else detail["staircase_mean_mm"]
    detail["support_plan"] = {"required_face_ids": contact, "blocked_face_ids": sorted(forbidden),
                              "status": "needs_slice_verification" if mode == "classic" else "needs_kinematics",
                              "note": "face masks are geometric requests, not an engine-verified support toolpath"}
    return detail, shift.tolist()


def support_modifiers(mesh, selected, thickness_mm=0.4):
    """Closed modifier prisms in printer Z-up coordinates, ready for a slicer adapter.

    Enforcers extend from the bed through the required underside. Blockers form a
    thin closed volume across protected faces. Engine support/readback verification
    remains mandatory; the planner does not claim these prisms are actual supports.
    """
    if selected["mode"] != "classic":
        return []
    placed, _ = geometry.place_on_bed(mesh, selected["rotation"])
    normals, _ = geometry.face_normals_areas(placed)
    plan = selected["surface"]["support_plan"]
    operations = []
    topology = [[0, 2, 1], [3, 4, 5], [0, 1, 4], [0, 4, 3],
                [1, 2, 5], [1, 5, 4], [2, 0, 3], [2, 3, 5]]
    for role, ids in (("support_enforcer", plan["required_face_ids"]), ("support_blocker", plan["blocked_face_ids"])):
        vertices, faces = [], []
        for face_id in ids:
            triangle = placed.triangles[face_id]
            # A protected face lying on the build plate cannot receive support
            # from below. A prism straddling Z=0 unnecessarily extends the group
            # bounding box and can add a phantom layer in the native engine.
            if role == "support_blocker" and float(np.max(triangle[:, 2])) <= 1e-8:
                continue
            if role == "support_enforcer":
                margin = float(selected.get("support_clearance_mm", 0.0))
                if margin > 0:
                    edges = [triangle[(i + 1) % 3] - triangle[i] for i in range(3)]
                    lengths = [np.linalg.norm(e) for e in edges]
                    inradius = np.linalg.norm(np.cross(edges[0], edges[1])) / sum(lengths)
                    if inradius <= margin:
                        selected.setdefault("support_modifier_warnings", []).append(
                            {"face_id": face_id, "reason": "required patch too narrow for reserved boundary clearance"})
                        continue
                    inset = triangle.copy()
                    for i in range(3):
                        a = np.cross(edges[(i - 1) % 3] / lengths[(i - 1) % 3], normals[face_id])
                        b = np.cross(edges[i] / lengths[i], normals[face_id])
                        inset[i] += np.linalg.solve(np.vstack([a, b, normals[face_id]]), [-margin, -margin, 0.])
                    triangle = inset
                lower, upper = triangle.copy(), triangle.copy()
                lower[:, 2] = 0.0
                upper[:, 2] += thickness_mm
            else:
                # Native support polygons can grow around an overhang boundary.
                # Expand protected masks by the resolved XY clearance + line
                # radius, and include the rounded top gap in their depth.
                margin = float(selected.get("support_clearance_mm", 0.0))
                depth = max(thickness_mm / 2, float(selected.get("support_depth_mm", 0.0)))
                normal = normals[face_id]
                expanded = triangle.copy()
                if margin > 0:
                    for i in range(3):
                        prev = triangle[i] - triangle[(i - 1) % 3]
                        following = triangle[(i + 1) % 3] - triangle[i]
                        a, b = np.cross(prev / np.linalg.norm(prev), normal), np.cross(following / np.linalg.norm(following), normal)
                        expanded[i] += np.linalg.solve(np.vstack([a, b, normal]), [margin, margin, 0.])
                lower = expanded - depth * normal
                upper = expanded + depth * normal
            offset = len(vertices)
            prism = np.vstack([lower, upper])
            oriented = topology
            if geometry.signed_volume(geometry.Mesh(prism, topology)) < 0:
                oriented = [face[::-1] for face in topology]
            vertices.extend(prism.tolist())
            faces.extend([[i + offset for i in face] for face in oriented])
        if vertices:
            operations.append({"op": "add_modifier_mesh", "role": role, "parent_model_index": 0,
                               "coordinates": "printer Z up; adapter must parent/transform with the model",
                               "vertices_mm": vertices, "faces": faces,
                               "settings": ({"support_mesh": True, "support_mesh_drop_down": True} if role == "support_enforcer"
                                            else {"anti_overhang_mesh": True}),
                               "verification": "needs_slice_verification"})
    return operations


def _limits(candidate, baseline, limits, preserve):
    failures = []
    for case_index, case in enumerate(candidate["mechanics"]["cases"]):
        base = baseline["mechanics"]["cases"][case_index]
        sf = case["safety_factor_min"]
        if sf is None or sf < limits["safety_factor"] - 1e-8:
            failures.append("safety_factor:" + case["id"])
        if case["max_displacement_mm"] > limits["max_displacement_mm"] + 1e-8:
            failures.append("displacement:" + case["id"])
        if preserve and (case["compliance_Nmm"] > base["compliance_Nmm"] * (1 + 1e-7)
                         or case["max_displacement_mm"] > base["max_displacement_mm"] * (1 + 1e-7)
                         or sf + 1e-7 < base["safety_factor_min"]):
            failures.append("mechanical_regression:" + case["id"])
        if preserve:
            before_blocks = dict((b["id"], b) for b in base["blocks"])
            for block in case["blocks"]:
                before = before_blocks[block["id"]]
                if (block["failure_index_max"] > before["failure_index_max"] * (1 + 1e-7)
                        or block["max_displacement_mm"] > before["max_displacement_mm"] * (1 + 1e-7)):
                    failures.append("block_regression:" + block["id"])
    for i, region in enumerate(candidate["surface"]["regions"]):
        if preserve and region["staircase_mean_mm"] > baseline["surface"]["regions"][i]["staircase_mean_mm"] + 1e-9:
            failures.append("surface_regression:" + region["id"])
        if preserve and region["staircase_max_mm"] > baseline["surface"]["regions"][i]["staircase_max_mm"] + 1e-9:
            failures.append("surface_peak_regression:" + region["id"])
        old_contact = baseline["surface"]["regions"][i]["support_contact_upper_mm2"]
        new_contact = region["support_contact_upper_mm2"]
        if preserve and old_contact is not None and new_contact is not None and new_contact > old_contact + 1e-9:
            failures.append("surface_contact_regression:" + region["id"])
        if region["staircase_max_mm"] > limits.get("max_staircase_mm", float("inf")):
            failures.append("staircase:" + region["id"])
    return failures


def optimize(mesh, spec, context, initial_rotation, mode, route, caps, base_patch, base_resolver):
    """Return full candidate/proposal and fields; ordinary planning is unchanged without spec.

    The first stage preserves the selected material/route. Classic searches full
    rotations; 5D evaluates its radial/core field, but cannot apply an unverified
    kinematic plan. No numerical estimate is promoted to physical qualification.
    """
    if spec is None:
        return unknown("functional_orientation", "purpose, service loads and scoped printed properties required",
                       ["functional_spec"], status="needs_input"), None
    try:
        if not isinstance(spec, dict) or not isinstance(spec.get("purpose"), str) or not spec["purpose"].strip():
            raise ValueError("functional_spec requires an explicit purpose")
        if spec.get("geometry_sha256") != context["geometry_sha256"]:
            return unknown("functional_orientation", "functional regions/volume mesh belong to another geometry",
                           status="out_of_domain"), None
        topology = geometry.edge_topology(mesh)
        if topology["boundary_edges"] or topology["nonmanifold_edges"] or topology["duplicate_directed_edges"] or geometry.signed_volume(mesh) <= 0:
            raise ValueError("functional planning requires a closed outward-oriented surface mesh")
        volume = spec.get("volume_mesh")
        if not isinstance(volume, dict):
            return unknown("functional_orientation", "conforming volume mesh required", ["volume_mesh"], status="needs_input"), None
        if volume.get("geometry_sha256") != context["geometry_sha256"]:
            raise ValueError("volume mesh geometry hash differs")
        nodes, elements = elasticity._mesh(volume)
        centers = nodes[elements[:, :4]].mean(axis=1)
        mesh_volumes = np.abs(np.linalg.det(nodes[elements[:, 1:4]] - nodes[elements[:, :1]])) / 6.0
        stl_volume = abs(geometry.signed_volume(mesh))
        if not stl_volume or abs(mesh_volumes.sum() - stl_volume) / stl_volume > 0.01:
            raise ValueError("volume model differs from STL volume")
        regions = _regions(mesh, spec.get("surface_regions", []))
        conditions = spec.get("service_conditions")
        if not isinstance(conditions, dict) or not isinstance(conditions.get("temperature_C"), (int, float)) or isinstance(conditions.get("temperature_C"), bool):
            return unknown("functional_orientation", "explicit service temperature and environment required",
                           ["service_conditions"], status="needs_input"), None
        if not math.isfinite(conditions["temperature_C"]) or conditions["temperature_C"] <= -273.15:
            raise ValueError("invalid service temperature")
        if not isinstance(conditions.get("environment"), str) or not conditions["environment"].strip() or conditions.get("load_regime") != "static":
            raise ValueError("nonempty environment and static load regime required; creep/fatigue are separate models")
        temperature_range = context.get("required_temperature_range_C")
        if temperature_range and any(not _same(conditions["temperature_C"], value) for value in temperature_range):
            return unknown("functional_orientation", "printed properties do not cover all required service temperatures",
                           status="out_of_domain"), None
        limits = spec.get("limits", {})
        if any(not isinstance(limits.get(k), (float, int)) or isinstance(limits.get(k), bool)
               or not math.isfinite(limits[k]) or limits[k] <= 0 for k in ("safety_factor", "max_displacement_mm")):
            raise ValueError("finite positive safety_factor and max_displacement_mm limits required")
        if limits["safety_factor"] < 1:
            raise ValueError("safety factor must be at least one")
        if "max_Ra_um" in limits or "max_flatness_mm" in limits:
            return unknown("functional_orientation", "absolute Ra/flatness need a measured surface/thermal model",
                           status="needs_calibration"), None
        profiles = spec.get("profiles")
        if not isinstance(profiles, list) or not profiles or len(profiles) > 8:
            raise ValueError("one to eight measured process profiles required")
        profile_ids = [p["id"] for p in profiles]
        if any(not isinstance(p, str) or not p for p in profile_ids) or len(set(profile_ids)) != len(profile_ids):
            raise ValueError("distinct process profile ids required")
        baseline_id = spec.get("baseline_profile_id")
        if baseline_id not in profile_ids:
            raise ValueError("baseline_profile_id must identify a supplied process profile")
        baseline_profile = profiles[profile_ids.index(baseline_id)]
        for profile in profiles:
            if (profile.get("source") not in ("physical_test", "software_fixture")
                    or not isinstance(profile.get("protocol"), str) or not profile["protocol"].strip()):
                return unknown("functional_orientation", "printed properties require a test protocol and provenance",
                               status="needs_calibration"), None
            if any(profile.get("context", {}).get(k) != context[k] for k in ("machine_id", "material_id", "printing_mode")):
                return unknown("functional_orientation", "constitutive profile context differs", status="out_of_domain"), None
            if profile.get("service_conditions") != conditions:
                return unknown("functional_orientation", "constitutive profile service conditions differ",
                               status="out_of_domain"), None
            elasticity.constitutive(profile["elastic"])
            elasticity.failure_index(np.zeros(6), profile["strength"])
        # Baseline is the current proposed print, not a silently substituted stronger profile.
        if any(not _same(v, base_patch["resolver"].value(k, extruder="0")) for k, v in baseline_profile["settings"].items()):
            return unknown("functional_orientation", "baseline process profile differs from current effective settings",
                           status="out_of_domain"), None
        candidates, solutions, seen, mechanical_cache = [], {}, set(), {}

        def evaluate(rotation, profile, evaluate_baseline=False):
            marker = (tuple(np.round(rotation, 8).reshape(-1)), profile["id"])
            if marker in seen:
                return None
            seen.add(marker)
            surface, shift = _surface(mesh, rotation, mode, route, float(profile["settings"]["layer_height"]), regions, caps)
            candidate = {"id": "candidate_%03d" % len(candidates), "profile_id": profile["id"], "mode": mode,
                         "rotation": np.asarray(rotation).tolist(), "translation": shift, "surface": surface,
                         "rejected_reasons": [], "source": profile["source"]}
            candidates.append(candidate)
            if not surface["fits_build_volume"]:
                candidate["rejected_reasons"].append("build_volume")
            if mode == "classic" and surface["base_area"] <= 1e-8:
                candidate["rejected_reasons"].append("no_stable_bed_contact")
            if surface.get("forbidden_contact"):
                candidate["rejected_reasons"].append("forbidden_support_contact")
            try:
                patch = _compile(base_patch, profile, mode, bool(surface["support_plan"]["required_face_ids"]), base_resolver)
            except (ValueError, TypeError) as exc:
                candidate["rejected_reasons"].append(str(exc))
                return candidate
            if candidate["rejected_reasons"] and not evaluate_baseline:
                return candidate
            normal_key = tuple(np.round(np.outer(rotation[2], rotation[2]), 8).reshape(-1)) if mode == "classic" else mode
            cache_key = (profile["id"], normal_key)
            if cache_key not in mechanical_cache:
                mechanical_cache[cache_key] = elasticity.solve(volume, profile["elastic"], profile["strength"],
                                           layer_normals(centers, rotation, mode, route), spec.get("load_cases"),
                                           spec.get("fixed"), spec.get("blocks"))
            solved = mechanical_cache[cache_key]
            if not solved.ok:
                candidate["rejected_reasons"].append("mechanics:" + solved.trace[0]["reason"])
                return candidate
            mechanics = copy.deepcopy(solved.value)
            maps = {"nodes_mm": mechanics.pop("nodes_mm"), "elements": mechanics.pop("elements"),
                    "boundary_faces": mechanics.pop("boundary_faces"),
                    "boundary_element_ids": mechanics.pop("boundary_element_ids"),
                    "element_centers_mm": mechanics.pop("element_centers_mm"),
                    "element_blocks": mechanics.pop("element_blocks"), "cases": []}
            mechanics.pop("element_volumes_mm3")
            for case in mechanics["cases"]:
                maps["cases"].append(dict(case.pop("maps"), id=case["id"]))
            candidate["mechanics"] = mechanics
            candidate["objective"] = [surface["visible_staircase_mm"], surface["support_volume_upper"] or 0.0,
                                      sum(c["compliance_Nmm"] for c in mechanics["cases"]), surface["height"]]
            solutions[candidate["id"]] = (maps, patch)
            return candidate

        baseline = evaluate(np.asarray(initial_rotation), baseline_profile, evaluate_baseline=True)
        if not baseline or "mechanics" not in baseline:
            result = computed({"candidates": candidates, "selected": None, "baseline": baseline,
                               "applied": False}, None, "functional_orientation", MODEL_ANALYTIC, evidence=None)
            result.status = "needs_input"
            return result, None
        preserve = True
        if spec.get("preserve_baseline", True) is not True:
            raise ValueError("preserving baseline mechanics and protected surfaces is mandatory")
        # Stress-informed normal candidates minimize weighted (tension_direction . normal)^2.
        # They are proposals only; every candidate gets a new anisotropic FE solution.
        direction_tensor = np.zeros((3, 3))
        baseline_maps = solutions[baseline["id"]][0]
        for case in baseline_maps["cases"]:
            for direction, value in zip(case["principal_tension_direction"], case["principal_tension_MPa"]):
                d = np.asarray(direction)
                direction_tensor += value ** 2 * np.outer(d, d)
        _, eigenvectors = np.linalg.eigh(direction_tensor)
        mechanical_ups = [eigenvectors[:, i].tolist() for i in range(2)]
        mechanical_ups += [(-eigenvectors[:, i]).tolist() for i in range(2)]
        for rotation in _rotations(mesh, mode, initial_rotation, route, spec.get("refine", True), mechanical_ups):
            for profile in profiles:
                candidate = evaluate(rotation, profile)
                if candidate and "mechanics" in candidate:
                    candidate["rejected_reasons"].extend(_limits(candidate, baseline, limits, preserve))
        baseline["rejected_reasons"].extend(_limits(baseline, baseline, limits, preserve))
        feasible = [c for c in candidates if "mechanics" in c and not c["rejected_reasons"]]
        selected = min(feasible, key=lambda c: (c["objective"], c["id"])) if feasible else None
        maps, patch = solutions[selected["id"]] if selected else (None, None)
        applied = bool(selected and selected["source"] == "physical_test" and mode == "classic")
        convergence = {"status": "needs_input", "note": "independent refined volume mesh required"}
        if selected and isinstance(spec.get("convergence_mesh"), dict):
            refined = spec["convergence_mesh"]
            if refined.get("geometry_sha256") != context["geometry_sha256"]:
                raise ValueError("convergence mesh geometry hash differs")
            refined_nodes, refined_elements = elasticity._mesh(refined)
            if len(refined_elements) <= len(elements):
                raise ValueError("convergence mesh must be finer than the analysis mesh")
            p = refined_nodes[refined_elements[:, :4]]
            rv = np.abs(np.linalg.det(p[:, 1:] - p[:, :1])).sum() / 6.0
            if abs(rv - stl_volume) / stl_volume > 0.01:
                raise ValueError("convergence mesh differs from STL volume")
            refined_centers = p.mean(axis=1)
            profile = profiles[profile_ids.index(selected["profile_id"])]
            checked = elasticity.solve(refined, profile["elastic"], profile["strength"],
                                       layer_normals(refined_centers, selected["rotation"], mode, route),
                                       spec["load_cases"], spec["fixed"], spec.get("blocks"))
            if not checked.ok:
                convergence = {"status": "invalid_input", "reason": checked.trace[0]["reason"]}
            else:
                differences = []
                for coarse_case, fine_case in zip(selected["mechanics"]["cases"], checked.value["cases"]):
                    differences.append({"id": fine_case["id"], "compliance_relative_change":
                                        abs(coarse_case["compliance_Nmm"] - fine_case["compliance_Nmm"]) / fine_case["compliance_Nmm"],
                                        "displacement_relative_change": abs(coarse_case["max_displacement_mm"] - fine_case["max_displacement_mm"]) / fine_case["max_displacement_mm"],
                                        "failure_index_relative_change": abs(coarse_case["failure_index_max"] - fine_case["failure_index_max"]) / fine_case["failure_index_max"]})
                converged = all(d["compliance_relative_change"] <= 0.05 and d["displacement_relative_change"] <= 0.05
                                and d["failure_index_relative_change"] <= 0.10 for d in differences)
                convergence = {"status": "pass" if converged else "fail", "cases": differences,
                               "coarse_elements": len(elements), "fine_elements": len(refined_elements),
                               "tolerances": {"compliance": 0.05, "displacement": 0.05, "failure_index": 0.10}}
                # Finer-grid limits must also pass; convergence alone is insufficient.
                fine_candidate = dict(selected, mechanics={"cases": checked.value["cases"]})
                fine_baseline_result = elasticity.solve(
                    refined, baseline_profile["elastic"], baseline_profile["strength"],
                    layer_normals(refined_centers, initial_rotation, mode, route),
                    spec["load_cases"], spec["fixed"], spec.get("blocks"))
                if not fine_baseline_result.ok:
                    raise ValueError("baseline refinement failed: " + fine_baseline_result.trace[0]["reason"])
                fine_baseline = dict(baseline, mechanics={"cases": fine_baseline_result.value["cases"]})
                refined_failures = _limits(fine_candidate, fine_baseline, limits, preserve)
                convergence["refined_limit_failures"] = refined_failures
                if refined_failures:
                    convergence["status"] = "fail"
        applied = applied and convergence["status"] == "pass"
        value = {"purpose": spec["purpose"], "selected": selected, "baseline": baseline,
                 "candidates": candidates, "candidate_count": len(candidates), "feasible_count": len(feasible),
                 "applied": applied, "maps": maps, "mesh_convergence": convergence,
                 "proposed_patch": ({"items": patch["items"], "errors": patch["errors"],
                                      "warnings": patch["warnings"], "payload": compiler.patch_to_slicer_payload(patch)}
                                     if patch else None),
                 "metrics": {"hard_constraint_violation_rate_selected": 0.0 if selected else None,
                             "selection_optimality_gap_tested_candidates": 0.0 if selected else None,
                             "Ra_um": None, "flatness_mm": None},
                 "limitations": ["finite candidate search; geometric smoothness proxy, not measured Ra",
                                 "linear static printed-property model; no fatigue or thermal warping",
                                 "5D kinematics and support geometry need authoritative slicer verification"]}
        if selected:
            resolved = patch["resolver"]
            selected["support_clearance_mm"] = (resolved.value("support_xy_distance", extruder="0") +
                                                 resolved.value("support_line_width", extruder="0") / 2. + SUPPORT_NUMERICAL_CLEARANCE_MM)
            selected["support_depth_mm"] = (resolved.value("support_top_distance", extruder="0") +
                                             resolved.value("layer_height", extruder="0") +
                                             resolved.value("support_line_width", extruder="0") / 2.)
        value["support_modifier_operations"] = support_modifiers(mesh, selected) if selected else []
        result = computed(value, None, "functional_orientation", MODEL_ANALYTIC, evidence=EVIDENCE_SIMULATION)
        if not selected:
            result.status = "infeasible"
        elif selected["source"] != "physical_test":
            result.status = "needs_calibration"
        elif mode != "classic" or convergence["status"] != "pass":
            result.status = "needs_input"
        return result, patch if applied else None
    except (ValueError, TypeError, KeyError, IndexError, np.linalg.LinAlgError, OverflowError) as exc:
        return invalid("functional_orientation", str(exc)), None
