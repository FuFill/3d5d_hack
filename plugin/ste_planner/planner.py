# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Main algorithm (specification section 13): TZ + model -> Decision.

    requirements -> geometry -> machine -> materials -> routes -> orientation
    -> process decisions -> compiled patch -> effective settings -> verification

Physical slicing (dry_slice, inspect_toolpath on the produced G-code) happens
in the slicer adapter; plan_case() returns everything needed to apply the
patch and to check the result afterwards.
"""

import hashlib
import os

import numpy

from . import requirements as reqmod
from . import geometry, materials, routes, process_plan, structure, compiler, verification, calibration, functional, robustness
from . import presets
from .settings_schema import load_machine_schema, SettingResolver, material_settings_from_file
from .result import (PASS, FAIL, UNKNOWN, EVIDENCE_DOCUMENT, EVIDENCE_ANALYTICAL, DECISION_INFEASIBLE, to_json)

DEFAULT_MACHINE = "hte530v5.2.4"

READBACK_KEYS = ["printing_mode", "machine_nozzle_size", "layer_height", "layer_height_0", "line_width",
                 "wall_line_count", "wall_thickness", "top_layers", "bottom_layers", "infill_sparse_density",
                 "infill_pattern", "material_print_temperature", "material_bed_temperature", "material_flow",
                 "speed_print", "cool_fan_enabled", "cool_fan_speed", "retraction_enable", "retraction_amount",
                 "retraction_speed", "support_enable", "support_angle", "adhesion_type",
                 "cylindrical_mode_base_diameter", "non_printing_base_diameter", "cylindrical_mode_overlap",
                 "machine_height", "machine_heated_bed", "machine_width", "machine_depth", "machine_center_is_zero",
                 "support_type", "support_line_width", "support_top_distance", "support_bottom_distance",
                 "support_xy_distance", "support_interface_enable", "support_roof_enable", "support_bottom_enable"]


def default_resources_dir():
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.environ.get("STE_RESOURCES_DIR"),
                 os.path.join(here, "..", "..", "STE-Slicer", "STE-Slicer-master", "resources"),
                 r"C:\Program Files\Stereotech STE Slicer 7.10\resources"):
        if cand and os.path.isdir(os.path.join(cand, "definitions")):
            return os.path.abspath(cand)
    raise IOError("STE Slicer resources directory not found; set STE_RESOURCES_DIR")


def material_file_id(material):
    name = os.path.basename((material.slicer if material else {}).get("file") or "")
    return name.replace(".xml.fdm_material", "").replace(".fdm_material", "") or None


def _preset_plan(schema, catalog, machine_id, material, mode, decisions, load_class, allowed_quality_changes):
    """Select the native base P and the minimal validated Delta of the plan over it."""
    context = {"machine_id": machine_id, "extruder": "0", "material_file_id": material_file_id(material),
               "material_guid": (material.slicer if material else {}).get("guid"), "printing_mode": mode,
               "required_nozzle_mm": next((d.value for d in decisions if d.quantity == "nozzle_size"), None),
               "allowed_quality_changes": list(allowed_quality_changes or [])}
    if material is None:
        return presets._needs_input("no admissible material", ["see verification and rejected materials"], []), None

    def evaluate(candidate):
        base = presets.resolver_for(schema, catalog, candidate)
        full = compiler.compile_patch(decisions, base)
        if full["errors"]:
            return {"delta_count": None, "errors": full["errors"]}
        minimal = presets.minimal_patch(full["items"], base, load_class)
        return {"delta_count": len(minimal["items"]), "errors": minimal["errors"], "patch": minimal,
                "resolver": base}

    selection = presets.select_base(catalog, context, evaluate)
    return selection, (selection.get("evaluation") or {}).get("resolver")


def plan_case(tz_text, mesh_path, resources_dir=None, machine_id=DEFAULT_MACHINE, catalog=None,
              preferred_materials=None, forced_mode=None, source=None, calibration_data=None, setting_changes=None,
              functional_spec=None, llm_items=None, noise_spec=None, robustness_limits=None,
              preset_catalog=None, allowed_quality_changes=None, orientation=None):
    """Decision for one TZ and STL.

    orientation: None/"balanced" (automatic: supports, height, staircase and loads along
    the layers), "strength" (loads along the layers first) or {"rotation": 3x3} - a pose
    requested by the user (part -> printer frame, e.g. the current scene pose), used as is
    in classic mode after a build-volume check.

    preset_catalog: application-owned snapshot of the installed STE registry
    (presets.CATALOG_SCHEMA). With it the plan inherits a compatible native
    printer/variant/material/quality base and writes only a minimal Delta.
    allowed_quality_changes: custom profile names the user explicitly allowed.
    Without a catalog the previous planner process profile is produced and
    marked as such (preset_selection.status = "not_requested").
    """
    orientation_request = orientation
    trace = []
    resources_dir = resources_dir or default_resources_dir()
    catalog = catalog or materials.load_catalog()

    # requirements
    from .llm_requirements import prepare_requirements
    reqset, llm_accepted, llm_rejected = prepare_requirements(tz_text, llm_items, source)
    issues = reqmod.validate_requirements(reqset)
    ptype = materials.part_type(reqset)
    trace.append({"step": "parse_requirements", "count": len(reqset.requirements), "issues": issues,
                  "part_type": ptype})
    if llm_items is not None:
        trace.append({"step": "merge_llm_requirements", "accepted_indices": llm_accepted,
                      "rejected": llm_rejected})

    # geometry
    mesh = geometry.load_stl(mesh_path)
    analysis = geometry.analyze_mesh(mesh)
    rot_r = geometry.rotational_features(mesh)
    rot = rot_r.value if rot_r.ok else {}
    trace.append({"step": "analyze_mesh", "status": analysis["status"], "errors": analysis["errors"],
                  "warnings": analysis["warnings"], "volume_mm3": analysis["volume_mm3"], "size": analysis["size"],
                  "rotational": {k: rot.get(k) for k in ("is_rotational_candidate", "axis", "bore_radius", "r_max",
                                                        "axial_length", "sector_cv", "moment_mismatch")}})

    # machine
    schema = load_machine_schema(resources_dir, machine_id)
    if preset_catalog is not None:
        catalog_errors = presets.validate_catalog(preset_catalog)
        if catalog_errors:
            raise ValueError("invalid preset catalog: " + "; ".join(catalog_errors[:5]))
        # Installed nozzle and machine settings, independent of material and quality.
        base = presets.resolver_for(schema, preset_catalog)
    else:
        base = SettingResolver(schema)
    caps = routes.machine_capabilities(base)
    trace.append({"step": "inspect_machine", "machine": machine_id, "chain": schema.chain,
                  "nozzles": caps["nozzle_diameters"], "build_volume": caps["build_volume"],
                  "note": "offline schema from the slicer resources; installed runtime confirmed by read-back"})

    # materials
    screening = materials.screen_materials(reqset, catalog, caps, preferred=preferred_materials)
    chosen_entry = screening["ranked"][0] if screening["ranked"] else None
    prototype_only = False
    if chosen_entry is None:
        with_checks = [e for e in screening["rejected"] if "summary" in e]
        if with_checks:
            chosen_entry = sorted(with_checks, key=lambda e: (len(e["summary"]["mandatory_fail"]),
                                                              e["material"]["id"]))[0]
            prototype_only = True
    material = catalog.get(chosen_entry["material"]["id"]) if chosen_entry else None
    trace.append({"step": "screen_materials", "ranked": [e["material"]["id"] for e in screening["ranked"][:8]],
                  "rejected": len(screening["rejected"]), "chosen": material.id if material else None,
                  "prototype_only": prototype_only, "policy": screening["policy"]})

    # routes
    route_list = routes.enumerate_routes(mesh, analysis, rot, reqset, caps, ptype)
    if forced_mode:
        chosen_route = next((r for r in route_list if r["mode"] == forced_mode), None)
        if chosen_route is None or not chosen_route["feasible"]:
            raise ValueError("requested route %s is infeasible: %s" %
                             (forced_mode, (chosen_route or {}).get("reasons", ["unsupported mode"])))
        route_note = "mode requested by the user"
        mode = forced_mode
    else:
        ch = routes.choose_route(route_list)
        mode = ch.value
        route_note = ch.trace[0].get("note") if ch.trace else None
        chosen_route = next((r for r in route_list if r["mode"] == mode), None)
    trace.append({"step": "enumerate_routes", "routes": [{"mode": r["mode"], "feasible": r["feasible"],
                                                          "score": r["score"], "reasons": r["reasons"]}
                                                         for r in route_list], "chosen": mode})

    # orientation / scene
    scene_ops = [{"op": "load_mesh", "path": mesh_path, "units": "mm",
                  "coordinates": "printer (Z up); the adapter converts to the Uranium scene (Y up)"}]
    orientation = None
    priority, fixed_rotation = _orientation_request(orientation_request)
    load_dirs = geometry.load_directions(mesh, rot)
    orientation_summary = {"request": "user_pose" if fixed_rotation is not None else priority,
                           "load_directions": load_dirs["kind"],
                           "model": "analytical screen: interlayer exposure of geometry-implied load directions"}
    if mode == "classic":
        bv = caps["build_volume"]["classic"]
        orientation, scored = process_plan.choose_orientation(
            mesh, analysis, reqset, bv, load_dirs=load_dirs,
            load_class_name=process_plan.load_class(reqset, ptype), priority=priority, fixed_rotation=fixed_rotation)
        rotation = orientation["rotation"] if orientation else [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
        placed, shift = geometry.place_on_bed(mesh, rotation)
        scene_ops.append({"op": "set_transform", "rotation": rotation, "translation": shift.tolist(),
                          "note": orientation.get("choice_note") if orientation else None})
        trace.append({"step": "orientation", "chosen_up": orientation["up"] if orientation else None,
                      "request": orientation_summary["request"], "load_directions": load_dirs["kind"],
                      "weights": orientation.get("weights") if orientation else None,
                      "candidates": [{"up": e["up"], "score": e.get("score"), "support_volume": e["support_volume_upper"],
                                      "height": e["height"], "interlayer_exposure": e.get("interlayer_exposure")}
                                     for e in scored[:6]]})
        if orientation:
            orientation_summary.update({k: orientation.get(k) for k in (
                "up", "interlayer_exposure", "support_volume_upper", "height", "base_area", "choice_note", "auto_best")})
    else:
        if fixed_rotation is not None:
            orientation_summary["note"] = "5D: the part axis follows the tooling axis; the requested pose is not used"
        p5 = routes.plan_5d(mesh, chosen_route, caps).value
        scene_ops.append({"op": "set_transform", "rotation": p5["rotation"], "translation": p5["translation"],
                          "note": "part axis on the tooling axis (scene Z), starts at the tooling end face"})
        trace.append({"step": "plan_5d", "checks": p5["checks"], "r_max": p5["r_max"],
                      "axial_length": p5["axial_length"]})
    batch = reqset.first("batch_quantity")
    if batch and mode == "classic":
        fp = sorted(orientation["size"][:2]) if orientation else analysis["size"][:2]
        pack = process_plan.pack_batch(fp, int(batch.value), caps["build_volume"]["classic"][:2])
        scene_ops.append({"op": "multiply", "count": pack["per_plate"], "then": "arrange",
                          "plates": pack["plates"], "requirement_id": batch.id})
    else:
        scene_ops.append({"op": "arrange"} if mode == "classic" else {"op": "center_on_axis"})

    # structural screening: a hard structural failure is never hidden by the material ranking
    route_axis = chosen_route["parameters"].get("axis") if chosen_route else None
    route_center = chosen_route["parameters"].get("center") if chosen_route else None
    struct = []
    structural_rejections = []
    cap = structure.catalog_strength_cap(catalog)
    for entry in (screening["ranked"][:8] if not prototype_only else [chosen_entry]):
        cand = catalog.get(entry["material"]["id"])
        checks = structure.screen_structure(reqset, mesh, rot, cand, mode, ptype, analysis["volume_mm3"],
                                            route_axis, route_center, cap)
        if any(c["status"] == FAIL for c in checks):
            structural_rejections.append({"material": cand.id, "checks": [c["check"] for c in checks
                                                                         if c["status"] == FAIL]})
            continue
        if cand.id != material.id:
            trace.append({"step": "structure_reselect", "from": material.id, "to": cand.id})
        material, chosen_entry, struct = cand, entry, checks
        break
    else:
        if chosen_entry is not None:
            struct = structure.screen_structure(reqset, mesh, rot, material, mode, ptype, analysis["volume_mm3"],
                                                route_axis, route_center, cap)
    trace.append({"step": "solve_structure", "rejected_by_structure": structural_rejections,
                  "chosen": material.id if material else None})

    # process decisions and patch
    decisions, notes = process_plan.plan_process(reqset, material, mode, ptype, analysis, caps, orientation,
                                                 chosen_route) if material else ([], [])
    load_class = next((n["value"] for n in notes if n.get("item") == "load_class"), "general")
    selection, base_resolver = None, None
    if preset_catalog is not None:
        selection, base_resolver = _preset_plan(schema, preset_catalog, machine_id, material, mode, decisions,
                                                load_class, allowed_quality_changes)
        trace.append({"step": "select_native_preset", "status": selection["status"],
                      "selected": selection.get("selected"), "reason": selection.get("reason"),
                      "alternatives": selection.get("alternatives"), "rejected": selection.get("rejected")})
    if base_resolver is not None:
        resolver = base_resolver.copy_with({"printing_mode": mode})
        patch = selection["evaluation"]["patch"]
    else:
        mat_settings = material_settings_from_file(material.slicer) if material else {}
        resolver = SettingResolver(schema, {"printing_mode": mode}, material_settings=mat_settings)
        patch = compiler.compile_patch(decisions, resolver)
        if selection is not None:
            # No compatible base: the self-made profile below is only an analysis
            # preview and cannot be applied (patch error blocks the runtime).
            patch["errors"].append({"error": "needs_input: no compatible native preset base: " + selection["reason"]})
    effective = compiler.effective_settings(patch["resolver"], READBACK_KEYS)
    trace.append({"step": "compile_patch", "items": len(patch["items"]), "errors": patch["errors"],
                  "warnings": patch["warnings"], "process_notes": notes,
                  "inherited_from_base": len(patch.get("inherited") or [])})

    # Calibration concerns this exact part/material/route and the planned process.
    # These are effects of proposed changes, not evidence of absolute part strength.
    digest = hashlib.sha256()
    with open(mesh_path, "rb") as mesh_file:
        for block in iter(lambda: mesh_file.read(1024 * 1024), b""):
            digest.update(block)
    functional_context = {"machine_id": machine_id, "material_id": material.id if material else None,
                          "printing_mode": mode, "geometry_sha256": digest.hexdigest()}
    service_temperatures = [value for r in reqset.requirements if r.property == "service_temperature"
                            for value in (r.lower, r.upper) if value is not None]
    if service_temperatures:
        functional_context["required_temperature_range_C"] = [min(service_temperatures), max(service_temperatures)]
    functional_result, functional_patch = functional.optimize(
        mesh, functional_spec, functional_context, scene_ops[1]["rotation"], mode, chosen_route,
        caps, patch, resolver)
    if functional_patch is not None:
        selected = functional_result.value["selected"]
        patch = functional_patch
        if base_resolver is not None:
            patch = presets.minimal_patch(functional_patch["items"], base_resolver, load_class)
        effective = compiler.effective_settings(patch["resolver"], READBACK_KEYS)
        scene_ops[1].update({"rotation": selected["rotation"], "translation": selected["translation"],
                             "note": "functional orientation under explicit mechanical/surface constraints"})
        # Batch placement follows the final orientation, not the preliminary one.
        if batch and mode == "classic":
            placed = mesh.transformed(selected["rotation"])
            footprint = (placed.vertices.max(axis=0) - placed.vertices.min(axis=0))[:2]
            pack = process_plan.pack_batch(footprint, int(batch.value), caps["build_volume"]["classic"][:2])
            scene_ops[2].update({"count": pack["per_plate"], "plates": pack["plates"]})
        scene_ops.extend(functional_result.value["support_modifier_operations"])
    trace.append({"step": "functional_orientation", "status": functional_result.status,
                  "applied": bool(functional_patch), "note": "simulation estimates; no qualification promotion"})
    calibration_context = {"machine_id": machine_id, "material_id": material.id if material else None,
                           "printing_mode": mode, "geometry_sha256": digest.hexdigest(),
                           "rotation": scene_ops[1]["rotation"], "settings": dict(effective)}
    base_presets = None
    if base_resolver is not None:
        base_presets = presets.base_presets_record(preset_catalog, selection["candidate"])
        # DML coefficients transfer only within the same native containers P.
        calibration_context["preset_base"] = presets.base_identity(base_presets)
    causal = calibration.assess_double_ml(calibration_data, calibration_context, patch["resolver"], setting_changes)
    trace.append({"step": "assess_double_ml", "status": causal.status,
                  "note": "independent single-setting comparisons; deterministic patch and qualification unchanged"})

    # verification
    evidence = []
    if chosen_entry:
        for c in chosen_entry["checks"]:
            evidence.append({"requirement_id": c["requirement_id"], "status": c["status"], "evidence": c["evidence"],
                             "source": "material:" + material.id, "note": c["note"]})
    for s in struct:
        if s.get("requirement_id"):
            evidence.append({"requirement_id": s["requirement_id"], "status": s["status"],
                             "evidence": s.get("evidence"), "source": s["check"], "note": s.get("note")})
    for n in notes:
        if n.get("requirement_id") and n.get("status") == "infeasible":
            evidence.append({"requirement_id": n["requirement_id"], "status": UNKNOWN, "evidence": None,
                             "source": "layer_budget", "note": n["note"]})
    ver = verification.verify_requirements(reqset, evidence)
    known_geometry = {}
    if ptype == "gear" and any(s["check"] == "gear_tooth_bending" for s in struct):
        known_geometry.update({"pitch_radius": "assumed", "face_width": "assumed", "module": "assumed",
                               "tooth_geometry": "assumed"})
    if rot.get("is_rotational_candidate"):
        known_geometry.update({"axis": "mesh", "mass": "density x volume", "center_of_mass": "mesh"})
    missing = reqmod.missing_inputs(reqset, known_geometry)
    calibration_needed = reqset.has("dimensional_tolerance") or reqset.has("blade_thickness_variation")
    status = verification.decision_status(ver, missing, calibration_needed)
    if prototype_only:
        status = DECISION_INFEASIBLE
    if selection is not None and selection["status"] != "selected" and status != DECISION_INFEASIBLE:
        status = "needs_input"
    if functional_spec is not None and status != DECISION_INFEASIBLE:
        if functional_result.status in ("invalid_input", "out_of_domain", "needs_input"):
            status = "needs_input"
        elif functional_result.status == "needs_calibration":
            status = "needs_calibration"
        elif functional_result.status == "infeasible":
            status = "search_incomplete"  # no feasible sampled candidate is not a proof of global infeasibility
    levels = [v["evidence_level"] for v in ver if v["evidence_level"]]
    evidence_level = EVIDENCE_ANALYTICAL if EVIDENCE_ANALYTICAL in levels else (EVIDENCE_DOCUMENT if levels else None)
    assumptions = []
    for s in struct:
        assumptions.extend(s.get("assumptions") or [])
    assumptions.append("material properties are manufacturer typical values (Stereotech wiki), not batch data")
    assumptions.append("process settings start from the Stereotech material profile; volumetric flow limit uncalibrated")

    decision = {
        "status": status,
        "evidence_level": evidence_level,
        "part_name": reqset.part_name,
        "part_type": ptype,
        "requirements": reqset.to_dict()["requirements"],
        "requirement_issues": issues,
        "assumptions": sorted(set(assumptions)),
        "missing_inputs": missing,
        "candidates": {"materials": [{"id": e["material"]["id"], "support": e.get("support"),
                                      "summary": e["summary"]} for e in screening["ranked"][:6]],
                       "routes": [{"mode": r["mode"], "feasible": r["feasible"], "score": r["score"]}
                                  for r in route_list]},
        "chosen_material": material.to_summary() if material else None,
        "materials_considered": len(screening["ranked"]),
        "materials_rejected": len([e for e in screening["rejected"] if "summary" in e]),
        "prototype_only": prototype_only,
        "route": {"mode": mode, "note": route_note, "parameters": {k: v for k, v in (
            chosen_route["parameters"] if chosen_route else {}).items() if k not in ("axis", "center")}},
        "setting_patch": {"items": patch["items"], "errors": patch["errors"], "warnings": patch["warnings"],
                          "payload": compiler.patch_to_slicer_payload(patch)},
        "effective_settings": effective,
        "base_presets": base_presets,
        "preset_selection": _selection_summary(selection),
        "calibration_context": calibration_context,
        "causal_analysis": causal.to_dict(),
        "functional_analysis": functional_result.to_dict(),
        "scene_operations": scene_ops,
        "orientation": orientation_summary,
        "structural_checks": struct,
        "verification": ver,
        "trace": trace,
    }
    decision["qualification_plan"] = verification.build_qualification_plan(ver)
    if functional_spec is not None:
        decision["qualification_plan"].append({"property": "functional_strength_stiffness",
                                              "reason": "simulation_requires_validation", "mandatory": True,
                                              "test": "Статическое нагружение напечатанной детали и контроль прогиба/поверхностей",
                                              "conditions": "те же закрепления, нагрузки, ориентация и профиль; Ra и плоскостность измерить"})
    if base_resolver is not None:
        keys = sorted(set(READBACK_KEYS) | set(i["key"] for i in patch["items"]))
        decision["baseline_effective_settings"] = compiler.effective_settings(base_resolver, keys)
        decision["setting_patch"]["changes"] = presets.change_records(patch)
        decision["setting_patch"]["inherited_items"] = patch.get("inherited") or []
        decision["setting_provenance"] = presets.provenance(base_resolver, patch, keys)
    decision["explanation"] = verification.explain_decision(decision)
    if selection is not None:
        decision["explanation"] = presets.explain(selection, patch) + decision["explanation"]
    # A separate final assessment: uncertain manufacturing inputs never become
    # invented material properties or silently replace the accepted process.
    functional_value = (functional_result.value or {})
    chosen = functional_value.get("selected")
    if chosen:
        nominal = dict(effective)
        proposed = (functional_value.get("proposed_patch") or {}).get("payload")
        if proposed:
            preview = resolver.copy_with(proposed["global"], proposed["extruders"])
            nominal = compiler.effective_settings(preview, READBACK_KEYS)
        mechanical_cases = chosen["mechanics"]["cases"]
        assessments = [robustness.assess_process_stability(nominal, c, noise_spec, robustness_limits).to_dict()
                       for c in mechanical_cases]
        decision["robustness_analysis"] = {"status": "needs_calibration", "profile_id": chosen["profile_id"],
            "cases": assessments, "setting_approval": False, "physical_stability_verified": False}
        if noise_spec is not None:
            profiles = dict((p["id"], p["settings"]) for p in (functional_spec or {}).get("profiles", []))
            candidates = []
            for candidate in functional_value.get("candidates", []):
                if candidate.get("rejected_reasons") or not candidate.get("mechanics"):
                    continue
                settings = dict(effective)
                settings.update(profiles.get(candidate["profile_id"], {}))
                cases = candidate["mechanics"]["cases"]
                worst = {"compliance_Nmm": sum(c["compliance_Nmm"] for c in cases),
                    "effective_stiffness_N_per_mm": min(c["effective_stiffness_N_per_mm"] for c in cases),
                    "max_displacement_mm": max(c["max_displacement_mm"] for c in cases),
                    "safety_factor_min": min(c["safety_factor_min"] for c in cases)}
                candidates.append({"id": candidate["id"], "settings": settings, "mechanics": worst})
            decision["robustness_analysis"]["conditional_candidate_selection"] = robustness.choose_stable_candidate(
                candidates, noise_spec, robustness_limits)
    else:
        decision["robustness_analysis"] = robustness.assess_process_stability(effective, {}, noise_spec, robustness_limits).to_dict()
    return decision


def _selection_summary(selection):
    if selection is None:
        return {"status": "not_requested", "native_preset": False,
                "note": "no preset catalog: legacy planner process profile over definition defaults and "
                        "material profile values; it is not a selected native preset"}
    out = dict((k, v) for k, v in selection.items() if k not in ("candidate", "evaluation"))
    out["native_preset"] = selection["status"] == "selected"
    return out


def _orientation_request(request):
    """(priority, fixed rotation or None) from the plan_case orientation argument."""
    if request is None:
        return "balanced", None
    if isinstance(request, str):
        if request not in process_plan.ORIENTATION_PRIORITIES:
            raise ValueError("orientation must be one of %s or {'rotation': 3x3}" % (process_plan.ORIENTATION_PRIORITIES,))
        return request, None
    if isinstance(request, dict) and set(request) == {"rotation"}:
        r = numpy.asarray(request["rotation"], dtype=float)
        if (r.shape != (3, 3) or not numpy.isfinite(r).all() or
                not numpy.allclose(r.T.dot(r), numpy.eye(3), atol=1e-6) or abs(numpy.linalg.det(r) - 1) > 1e-6):
            raise ValueError("requested orientation must be a proper rotation (no scale or mirroring)")
        # re-orthonormalise the float32 scene matrix so downstream checks see an exact rotation
        u, _, vt = numpy.linalg.svd(r)
        return "balanced", u.dot(vt).tolist()
    raise ValueError("orientation must be 'balanced', 'strength' or {'rotation': 3x3}")


def plan_case_files(docx_path, stl_path, **kw):
    text = reqmod.read_docx_text(docx_path)
    return plan_case(text, stl_path, source=os.path.basename(docx_path), **kw)


def decision_json(decision, indent=1):
    return to_json(decision, indent=indent)
