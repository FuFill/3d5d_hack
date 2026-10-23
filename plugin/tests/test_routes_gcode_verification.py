# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Routes 3D/5D, toolpath inspection, verification and Pareto policy."""

import numpy

from testutil import make_tube, make_box

from ste_planner import geometry as G, routes as RT, gcode, verification as V, optimize as O
from ste_planner import requirements as R
from ste_planner.result import PASS, FAIL, UNKNOWN


def _routes(mesh, text="", ptype=None):
    rs = R.parse_requirements("Наименование детали: X\n" + text)
    return RT.enumerate_routes(mesh, G.analyze_mesh(mesh), G.rotational_features(mesh).value, rs, ptype=ptype)


def test_tube_gets_spiral_with_bore_as_base():
    rs = _routes(make_tube(25.0, 8.0, 20.0, 128), "Текущие колёса выходят из строя из-за расслоения.",
                 "wheel_roller")
    sp = [r for r in rs if r["mode"] == "cylindrical"][0]
    assert sp["feasible"]
    assert abs(sp["parameters"]["base_diameter"] - 16.0) < 0.1
    assert sp["parameters"]["base_diameter"] <= 16.0          # never larger than the bore (would cut the part)
    assert not [r for r in rs if r["mode"] == "cylindrical_full"][0]["feasible"]
    assert RT.choose_route(rs).value == "cylindrical"


def test_solid_shaft_gets_full_with_core_inside():
    rs = _routes(make_tube(10.0, 0.0, 60.0, 96), "Профиль кулачка без ступенек.", "shaft_cam")
    full = [r for r in rs if r["mode"] == "cylindrical_full"][0]
    assert full["feasible"]
    assert 4.0 <= full["parameters"]["base_diameter"] < 20.0
    assert RT.choose_route(rs).value == "cylindrical_full"


def test_classic_without_documented_benefit():
    rs = _routes(make_tube(25.0, 8.0, 20.0, 128))
    assert RT.choose_route(rs).value == "classic"             # 5D feasible but no documented benefit
    box = _routes(make_box(30, 20, 10))
    assert RT.choose_route(box).value == "classic"


def test_plan_5d_places_axis_on_z():
    m = make_tube(20.0, 5.0, 30.0, 64)
    rot90 = numpy.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]], dtype=float)   # axis along Y
    m = m.transformed(rot90)
    routes = RT.enumerate_routes(m, G.analyze_mesh(m), G.rotational_features(m).value,
                                 R.parse_requirements("Наименование детали: колесо\nрасслоения"), ptype="wheel_roller")
    sp = [r for r in routes if r["mode"] == "cylindrical"][0]
    plan = RT.plan_5d(m, sp).value
    placed = m.transformed(numpy.array(plan["rotation"]))
    v = placed.vertices + numpy.array(plan["translation"])
    assert abs(v[:, 2].min()) < 1e-9 and abs(v[:, 2].max() - 30.0) < 1e-6
    assert abs(v[:, 0].mean()) < 1e-6 and abs(v[:, 1].mean()) < 1e-6
    assert any(c["check"] == "collisions" and c["status"] == "unknown" for c in plan["checks"])


GCODE_CLASSIC = """;FLAVOR:RepRap
;TIME:1234
;Filament used: 1.5m
M104 S235
M140 S60
M82
G92 E0
;LAYER:0
G0 F3000 X0 Y0 Z0.3
G1 F1200 X10 Y0 E1.0
G1 X10 Y10 E2.0
;LAYER:1
G0 Z0.5
G1 X0 Y10 E3.0
G1 E2.0
;LAYER:2
G0 Z0.7
M107
"""


def test_gcode_inspection_classic():
    res = gcode.inspect_toolpath(GCODE_CLASSIC, {"printing_mode": "classic", "nozzle_temperature": 235,
                                                 "build_volume": [351, 300, 300]})
    checks = dict((c["check"], c["status"]) for c in res["checks"])
    assert checks["has_layers"] == PASS
    assert checks["no_empty_layers"] == FAIL                 # layer 2 extrudes nothing
    assert checks["nozzle_temperature"] == PASS
    assert checks["mode_consistency"] == PASS
    assert checks["volumetric_flow"] == UNKNOWN              # no calibrated limit
    s = res["summary"]
    assert s["layers"] == 3 and s["time_header"] == "1234" and s["retractions"] == 1
    expected_flow = 1.0 * numpy.pi * 1.75 ** 2 / 4 / (10.0 / 20.0)
    assert abs(s["max_linear_flow_mm3_s"] - expected_flow) < 1e-9


def test_gcode_inspection_5d_markers():
    text = "G55\n;LAYER:0\nG1 F600 X1 Y0 Z1 E0.5\nG1 A10 C90 E1.0\n"
    res = gcode.inspect_toolpath(text, {"printing_mode": "cylindrical_full", "q_limit": 50.0})
    checks = dict((c["check"], c["status"]) for c in res["checks"])
    assert checks["mode_consistency"] == PASS
    assert res["summary"]["rotary_ranges"]["C"] == [0.0, 90.0]
    wrong = gcode.inspect_toolpath(text, {"printing_mode": "classic"})
    assert dict((c["check"], c["status"]) for c in wrong["checks"])["mode_consistency"] == FAIL


def test_verification_merges_evidence_conservatively():
    rs = R.RequirementSet([R.Requirement("A", "density", "<=", 0.0012, "g/mm3"),
                           R.Requirement("B", "uv_resistance", "qual", True, None, kind="qualitative"),
                           R.Requirement("C", "torque", "<=", 1000.0, "N*mm", kind="condition"),
                           R.Requirement("D", "esd", "qual", True, None, kind="qualitative", mandatory=False)])
    ev = [{"requirement_id": "A", "status": PASS, "evidence": "document"},
          {"requirement_id": "A", "status": FAIL, "evidence": "analytical_screen"},
          {"requirement_id": "B", "status": UNKNOWN, "evidence": None}]
    ver = dict((v["requirement_id"], v) for v in V.verify_requirements(rs, ev))
    assert ver["A"]["status"] == FAIL                         # fail dominates pass
    assert ver["B"]["status"] == UNKNOWN                      # unknown stays unknown
    assert ver["C"]["status"] == "input"                      # service condition, not a limit
    plan = V.build_qualification_plan(list(ver.values()))
    props = [p["property"] for p in plan]
    assert "density" in props and "uv_resistance" in props and "torque" not in props
    assert plan[0]["reason"] == "fail"


def test_decision_status_precedence():
    fail = [{"status": FAIL, "mandatory": True}]
    ok = [{"status": PASS, "mandatory": True}]
    opt_fail = [{"status": FAIL, "mandatory": False}]
    assert V.decision_status(fail, [{"f": 1}], True) == "infeasible"
    assert V.decision_status(ok, [{"f": 1}], True) == "needs_input"
    assert V.decision_status(ok, [], True) == "needs_calibration"
    assert V.decision_status(opt_fail, [], False) == "candidate"
    assert V.decision_status(ok, [], False, sliced=True) == "slice_verified"


def test_pareto_never_admits_hard_failure():
    cands = [{"id": "a", "hard": {"strength": FAIL}, "objectives": {"time": 1, "mass": 1}},
             {"id": "b", "hard": {"strength": UNKNOWN}, "objectives": {"time": 5, "mass": 3}},
             {"id": "c", "hard": {"strength": PASS}, "objectives": {"time": 3, "mass": 2}},
             {"id": "d", "hard": {}, "objectives": {"time": 4, "mass": 4}}]
    front = O.pareto_front(cands, ["time", "mass"])
    ids = sorted(c["id"] for c in front)
    assert ids == ["c"]                                       # a is infeasible, b and d dominated by c
    assert O.choose_by_policy(front, {"time": 1.0, "mass": 1.0})["id"] == "c"
    assert O.domain_check({"h": 0.2, "T": 240}, {"h": (0.1, 0.3), "T": (220, 245)}) == PASS
    assert O.domain_check({"h": 0.4}, {"h": (0.1, 0.3)}) == FAIL
    assert O.domain_check({}, {"h": (0.1, 0.3)}) == UNKNOWN
