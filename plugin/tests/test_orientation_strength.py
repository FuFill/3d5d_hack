# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Orientation for stiffness: loads along the layers, strength priority and a user pose."""

import numpy

from testutil import make_box, make_tube, raises

from ste_planner import geometry as G, process_plan as PP, planner
from ste_planner.requirements import parse_requirements

BED = [300.0, 300.0, 300.0]


def _merge(*meshes):
    vertices, faces, offset = [], [], 0
    for m in meshes:
        vertices.append(m.vertices)
        faces.append(numpy.asarray(m.faces) + offset)
        offset += len(m.vertices)
    return G.Mesh(numpy.vstack(vertices), numpy.vstack(faces))


def _post_on_foot():
    # A slender post standing on a small foot: upright needs no supports, but every
    # bending/axial load of the post then pulls the layers apart.
    return _merge(make_box(30, 30, 3, origin=(-15, -15, 0)), make_box(10, 10, 100, origin=(-5, -5, 3)))


def test_load_directions_from_geometry():
    beam = G.load_directions(make_box(100, 10, 10))
    assert beam["kind"] == "slender" and abs(abs(beam["axis"][0]) - 1) < 1e-6
    plate = G.load_directions(make_box(80, 60, 4))
    assert plate["kind"] == "plate" and abs(abs(plate["axis"][2]) - 1) < 1e-6
    assert G.load_directions(make_box(20, 20, 20))["kind"] == "none"
    tube = make_tube(20, 8, 10)
    wheel = G.load_directions(tube, G.rotational_features(tube).value)
    assert wheel["kind"] == "rotational_plane"
    assert numpy.allclose(numpy.asarray(wheel["directions"]).dot(wheel["axis"]), 0, atol=1e-9)


def test_interlayer_exposure_is_the_layer_traction():
    along_x = numpy.array([[1.0, 0.0, 0.0]])
    assert G.interlayer_exposure(along_x, numpy.eye(3)) == 0.0            # load along the layers
    up_x = G.rotation_to_z([1.0, 0.0, 0.0])
    assert abs(G.interlayer_exposure(along_x, up_x) - 1.0) < 1e-12         # load pulls layers apart
    tilt = G.rotation_to_z([1.0, 0.0, 1.0])
    assert abs(G.interlayer_exposure(along_x, tilt) - numpy.sqrt(0.5)) < 1e-12
    assert G.interlayer_exposure(numpy.zeros((0, 3)), numpy.eye(3)) is None


def _post_on_wide_foot():
    # The foot makes the upright pose the geometric optimum (no supports, original kept);
    # a service load along the post then pulls the layers apart.
    return _merge(make_box(50, 50, 3, origin=(-25, -25, 0)), make_box(10, 10, 60, origin=(-5, -5, 3)))


POST_LOAD = {"kind": "slender", "directions": numpy.array([[0.0, 0.0, 1.0]])}   # e.g. from the specification


def test_strength_priority_lays_the_post_along_the_layers():
    mesh = _post_on_wide_foot()
    reqs = parse_requirements("Кронштейн")
    plain, _ = PP.choose_orientation(mesh, G.analyze_mesh(mesh), reqs, BED)
    assert plain["up"][2] > 0.999 and "original" in plain["choice_note"]   # geometry alone: standing
    strong, _ = PP.choose_orientation(mesh, G.analyze_mesh(mesh), reqs, BED, load_dirs=POST_LOAD, priority="strength")
    assert strong["interlayer_exposure"] < 0.05       # post lies along the layers
    assert strong["support_volume_upper"] > plain["support_volume_upper"]   # the price is support, made explicit
    assert "strength" in strong["choice_note"]
    raises(ValueError, PP.choose_orientation, mesh, G.analyze_mesh(mesh), reqs, BED, priority="fastest")


def test_load_class_sets_the_weight_of_loads_across_layers():
    mesh = _post_on_wide_foot()
    reqs = parse_requirements("Кронштейн")
    decorative, _ = PP.choose_orientation(mesh, G.analyze_mesh(mesh), reqs, BED, load_dirs=POST_LOAD,
                                          load_class_name="decorative")
    assert decorative["up"][2] > 0.999 and decorative["interlayer_exposure"] == 1.0   # term off, but reported
    structural, _ = PP.choose_orientation(mesh, G.analyze_mesh(mesh), reqs, BED, load_dirs=POST_LOAD,
                                          load_class_name="structural")
    assert structural["interlayer_exposure"] < 0.05


def test_slender_parts_lie_along_the_layers_automatically():
    mesh = _post_on_foot()
    dirs = G.load_directions(mesh)
    assert dirs["kind"] == "slender"
    chosen, _ = PP.choose_orientation(mesh, G.analyze_mesh(mesh), parse_requirements("Стойка"), BED, load_dirs=dirs)
    assert chosen["interlayer_exposure"] < 0.05


def test_user_pose_is_kept_and_compared_with_the_automatic_one():
    mesh = _post_on_foot()
    reqs = parse_requirements("Кронштейн")
    dirs = G.load_directions(mesh)
    pose = G.rotation_to_z([0.0, 1.0, 0.0])            # user laid the post down along X
    chosen, _ = PP.choose_orientation(mesh, G.analyze_mesh(mesh), reqs, BED, load_dirs=dirs,
                                      fixed_rotation=pose)
    assert numpy.allclose(chosen["rotation"], pose)
    assert "requested by the user" in chosen["choice_note"]
    assert chosen["auto_best"] is not None and "interlayer_exposure" in chosen["auto_best"]
    tiny = [50.0, 50.0, 50.0]
    raises(ValueError, PP.choose_orientation, mesh, G.analyze_mesh(mesh), reqs, tiny, fixed_rotation=pose)


def test_planner_orientation_argument_is_validated():
    assert planner._orientation_request(None) == ("balanced", None)
    assert planner._orientation_request("strength") == ("strength", None)
    r = G.rotation_to_z([1.0, 0.0, 0.0])
    priority, fixed = planner._orientation_request({"rotation": (numpy.asarray(r, dtype=numpy.float32)).tolist()})
    assert priority == "balanced" and numpy.allclose(fixed, r, atol=1e-6)
    assert abs(numpy.linalg.det(fixed) - 1) < 1e-12
    raises(ValueError, planner._orientation_request, "upside_down")
    raises(ValueError, planner._orientation_request, {"rotation": numpy.diag([1.0, 1.0, -1.0]).tolist()})
    raises(ValueError, planner._orientation_request, {"rotation": (2 * numpy.eye(3)).tolist()})
