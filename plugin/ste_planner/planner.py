# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Main algorithm (specification section 13): TZ + model -> Decision.

    requirements -> geometry -> machine -> materials -> routes -> orientation
    -> process decisions -> compiled patch -> effective settings -> verification

Physical slicing (dry_slice, inspect_toolpath on the produced G-code) happens
in the slicer adapter; plan_case() returns everything needed to apply the
patch and to check the result afterwards.
"""

import os

from . import requirements as reqmod
from . import geometry, materials, routes, process_plan, structure, compiler, verification
from .settings_schema import load_machine_schema, SettingResolver, material_settings_from_file
from .result import (PASS, FAIL, UNKNOWN, EVIDENCE_DOCUMENT, EVIDENCE_ANALYTICAL, DECISION_INFEASIBLE, to_json)

DEFAULT_MACHINE = "hte530v5.2.4"

READBACK_KEYS = ["printing_mode", "machine_nozzle_size", "layer_height", "layer_height_0", "line_width",
                 "wall_line_count", "wall_thickness", "top_layers", "bottom_layers", "infill_sparse_density",
                 "infill_pattern", "material_print_temperature", "material_bed_temperature", "material_flow",
                 "speed_print", "cool_fan_enabled", "cool_fan_speed", "retraction_enable", "retraction_amount",
                 "retraction_speed", "support_enable", "support_angle", "adhesion_type",
                 "cylindrical_mode_base_diameter", "non_printing_base_diameter", "cylindrical_mode_overlap",
                 "machine_height", "machine_heated_bed"]


def default_resources_dir():
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.environ.get("STE_RESOURCES_DIR"),
                 os.path.join(here, "..", "..", "STE-Slicer", "STE-Slicer-master", "resources"),
                 r"C:\Program Files\Stereotech STE Slicer 7.10\resources"):
        if cand and os.path.isdir(os.path.join(cand, "definitions")):
            return os.path.abspath(cand)
    raise IOError("STE Slicer resources directory not found; set STE_RESOURCES_DIR")


def plan_case(tz_text, mesh_path, resources_dir=None, machine_id=DEFAULT_MACHINE, catalog=None,
              preferred_materials=None, forced_mode=None, source=None, llm_items=None):
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
    if mode == "classic":
        bv = caps["build_volume"]["classic"]
        orientation, scored = process_plan.choose_orientation(mesh, analysis, reqset, bv)
        rotation = orientation["rotation"] if orientation else [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
        placed, shift = geometry.place_on_bed(mesh, rotation)
        scene_ops.append({"op": "set_transform", "rotation": rotation, "translation": shift.tolist(),
                          "note": orientation.get("choice_note") if orientation else None})
        trace.append({"step": "orientation", "chosen_up": orientation["up"] if orientation else None,
                      "candidates": [{"up": e["up"], "score": e.get("score"), "support_volume": e["support_volume_upper"],
                                      "height": e["height"]} for e in scored[:6]]})
    else:
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
    mat_settings = material_settings_from_file(material.slicer) if material else {}
    resolver = SettingResolver(schema, {"printing_mode": mode}, material_settings=mat_settings)
    decisions, notes = process_plan.plan_process(reqset, material, mode, ptype, analysis, caps, orientation,
                                                 chosen_route) if material else ([], [])
    patch = compiler.compile_patch(decisions, resolver)
    effective = compiler.effective_settings(patch["resolver"], READBACK_KEYS)
    trace.append({"step": "compile_patch", "items": len(patch["items"]), "errors": patch["errors"],
                  "warnings": patch["warnings"], "process_notes": notes})


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
        "scene_operations": scene_ops,
        "structural_checks": struct,
        "verification": ver,
        "trace": trace,
    }
    decision["qualification_plan"] = verification.build_qualification_plan(ver)
    decision["explanation"] = verification.explain_decision(decision)
    return decision


def plan_case_files(docx_path, stl_path, **kw):
    text = reqmod.read_docx_text(docx_path)
    return plan_case(text, stl_path, source=os.path.basename(docx_path), **kw)


def decision_json(decision, indent=1):
    return to_json(decision, indent=indent)
