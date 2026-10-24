# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Setting schema, safe expression evaluation and the patch compiler against the STE definitions."""

from testutil import resources_dir, raises

from ste_planner import settings_schema as S, compiler as C, materials as M


def _schema():
    return S.load_machine_schema(resources_dir(), "hte530v5.2.4")


def test_definition_chain_and_extruders():
    sch = _schema()
    assert sch.chain == ["fdmprinter", "steprinter_v5", "steprinter530_v5.2.4", "hte530v5.2.4"]
    assert sch.extruder_positions() == ["0", "1"]
    assert sch.metadata["machine_extruder_trains"]["0"] == "stereotech_v5.2.4_extruder_main"


def test_every_value_expression_evaluates():
    sch = _schema()
    for mode in C.ALLOWED_MODES:
        r = S.SettingResolver(sch, {"printing_mode": mode})
        bad = []
        for key, d in sch.definitions.items():
            if d.type in (None, "category"):
                continue
            try:
                r.value(key, extruder="0")
            except S.EvalError as e:
                bad.append((key, str(e)))
        assert not bad, bad[:5]


def test_mode_dependent_machine_values():
    sch = _schema()
    classic = S.SettingResolver(sch, {"printing_mode": "classic"})
    full = S.SettingResolver(sch, {"printing_mode": "cylindrical_full"})
    assert classic.value("machine_height") == 300.0 and full.value("machine_height") == 231.1
    assert classic.value("machine_heated_bed") is True and full.value("machine_heated_bed") is False
    assert full.value("adhesion_type") == "raft" and classic.value("adhesion_type") == "brim"
    assert classic.value("machine_nozzle_size", extruder="0") == 0.4
    assert classic.value("material_diameter", extruder="0") == 1.75
    assert full.enabled("material_bed_temperature") is False
    assert full.enabled("cylindrical_mode_base_diameter") is True
    assert classic.enabled("cylindrical_mode_base_diameter") is False


def test_material_container_values_flow_into_settings():
    sch = _schema()
    petg = M.load_catalog().get("enduse_petg")
    r = S.SettingResolver(sch, {"printing_mode": "classic"}, material_settings=S.material_settings_from_file(petg.slicer))
    assert r.value("material_print_temperature", extruder="0") == 235.0   # via default_material_print_temperature
    assert r.value("cool_fan_speed", extruder="0") == 20.0
    assert r.value("retraction_amount", extruder="0") == 1.5


def test_evaluator_is_sandboxed():
    look = lambda name: 1.0
    for expr in ("__import__('os').system('echo x')", "(1).__class__", "open('x')", "[x for x in ()][0].real",
                 "math.__dict__", "lambda: 1", "f(x=1)"):
        raises(S.EvalError, S.evaluate, expr, look)
    assert S.evaluate("max(1, min(4, 3)) + math.sqrt(16)", look) == 7.0
    assert S.evaluate("sum(x * 2 for x in [1, 2])", look) == 6
    assert S.evaluate("'a' if 2 > 1 else 'b'", look) == "a"


def test_compile_patch_units_scope_and_validation():
    sch = _schema()
    r = S.SettingResolver(sch, {"printing_mode": "classic"})
    patch = C.compile_patch([
        C.Decision("printing_mode", "classic"),
        C.Decision("infill_fraction", 0.35, ["R1"], ["policy"]),
        C.Decision("flow_factor", 0.98),
        C.Decision("layer_height", 0.2),
        C.Decision("wall_count", 4),
        C.Decision("nozzle_temperature", 240.0),
    ], r)
    assert patch["ok"], patch["errors"]
    items = dict((i["key"], i) for i in patch["items"])
    assert items["infill_sparse_density"]["value"] == 35.0
    assert items["material_flow"]["value"] == 98.0                      # never 0.98
    assert items["material_print_temperature"]["scope"] == "extruder"
    assert items["material_print_temperature"]["target"] == "extruder_0"
    assert items["layer_height"]["scope"] == "global"
    assert items["infill_sparse_density"]["reason_requirement_ids"] == ["R1"]
    assert items["layer_height"]["old_effective_value"] == 0.1
    stages = [i["stage"] for i in patch["items"]]
    assert stages == sorted(stages, key=C.STAGES.index)
    assert C.patch_to_slicer_payload(patch)["extruders"]["0"]["material_print_temperature"] == 240.0
    eff = C.effective_settings(patch["resolver"], ["wall_line_count", "top_layers"])
    assert eff["wall_line_count"] == 4 and eff["top_layers"] == 4        # 0.8 mm / 0.2 mm, read back


def test_compile_patch_rejections():
    sch = _schema()
    r = S.SettingResolver(sch, {"printing_mode": "cylindrical_full"})
    bad = C.compile_patch([C.Decision("printing_mode", "discrete")], r)
    assert not bad["ok"] and "not allowed" in bad["errors"][0]["error"]
    bad = C.compile_patch([C.Decision("flow_factor", 98)], r)                # percent given as fraction
    assert not bad["ok"]
    bad = C.compile_patch([C.Decision("x", 1, key="hole_xy_offset")], r)      # absent in the schema
    assert not bad["ok"] and "not present" in bad["errors"][0]["error"]
    bad = C.compile_patch([C.Decision("wall_thickness", 1.2), C.Decision("wall_count", 3)], r)
    assert any("dependent" in e["error"] for e in bad["errors"])
    bad = C.compile_patch([C.Decision("layer_height", 0.0)], r)
    assert any("below minimum" in e["error"] for e in bad["errors"])
    warn = C.compile_patch([C.Decision("layer_height", 0.36)], r)             # > 0.8 * nozzle
    assert warn["ok"] and any(w["key"] == "layer_height" for w in warn["warnings"])
    dis = C.compile_patch([C.Decision("bed_temperature", 60.0)], r)           # no heated bed in 5D
    assert any("disabled" in w["warning"] for w in dis["warnings"])


def test_int_settings_are_not_rounded_like_uranium():
    """5D raft_surface_layers evaluates to 29.9: Uranium keeps it, CuraEngine later truncates str(value)."""
    d = S.SettingDefinition("raft_surface_layers", {"type": "int"})
    assert S.coerce(d, 29.900000000000002) == 29.900000000000002
    assert S.coerce(d, "3.7") == 3.7                       # ast.literal_eval of a stored string
    assert S.coerce(d, 3.0) == 3 and isinstance(S.coerce(d, 3.0), int)
    assert S.coerce(d, "3") == 3 and isinstance(S.coerce(d, "3"), int)
    assert S.coerce(d, "=expr") == "=expr"


def test_stack_semantics_found_by_uranium_crosscheck():
    """Rules confirmed against Uranium (tools/uranium_crosscheck.py): 5940 values, 0 mismatches."""
    sch = _schema()
    tpu = S.material_settings_from_file(M.load_catalog().get("sealant_tpu_A95").slicer)   # bed 45 C
    r = S.SettingResolver(sch, {"printing_mode": "classic"}, {"0": {"machine_nozzle_size": 0.6,
                                                                    "speed_print": 30.0}}, material_settings=tpu)
    # settable_per_extruder defaults to True: line width follows the nozzle of extruder 0
    assert r.value("line_width", extruder="0") == 0.6
    # raft speeds are limited to the adhesion extruder and follow its print speed
    assert r.value("raft_speed", extruder="0") == 15.0
    # bed temperature resolves as max() over both enabled extruders; the idle one has the default 60 C
    assert r.value("material_bed_temperature") == 60.0
    explicit = r.copy_with({"material_bed_temperature": 45.0})
    assert explicit.value("material_bed_temperature") == 45.0


def test_uranium_crosscheck_optional():
    """Full comparison with the real Uranium (needs Python <= 3.9 with PyQt5): RUN_URANIUM_CHECK=1."""
    import os
    import subprocess
    import sys
    import tempfile
    from testutil import PLUGIN, REPO, skip
    if os.environ.get("RUN_URANIUM_CHECK") != "1":
        skip("set RUN_URANIUM_CHECK=1 (and URANIUM_PYTHON) to run the Uranium comparison")
    py = os.environ.get("URANIUM_PYTHON", os.path.join(REPO, ".venv38", "Scripts", "python.exe"))
    uranium = os.path.join(REPO, "STE-Slicer", "Uranium-steslicer")
    out = os.path.join(tempfile.mkdtemp(), "uranium.json")
    env = dict(os.environ, STE_RESOURCES_DIR=resources_dir(), PYTHONPATH=os.pathsep.join([PLUGIN, uranium]))
    subprocess.check_call([py, os.path.join(PLUGIN, "tools", "uranium_crosscheck.py"), out], env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import json
    with open(out, encoding="utf-8") as f:
        rep = json.load(f)
    assert rep["total_compared"] > 5000 and rep["total_mismatch"] == 0 and rep["reference_errors"] == 0
