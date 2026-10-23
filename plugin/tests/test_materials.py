# Copyright (c) 2026. Released under the terms of the LGPLv3 or higher.
"""Material catalog and three-valued screening."""

import json
import os

from testutil import REPO, example_cases, skip

from ste_planner import materials as M
from ste_planner import requirements as R
from ste_planner.result import PASS, FAIL, UNKNOWN

MACHINE = {"filament_diameter": 1.75, "max_nozzle_temperature": 350, "nozzle_diameters": [0.4],
           "hardened_nozzle": None}


def _cat():
    return M.load_catalog()


def test_catalog_maps_slicer_materials():
    cat = _cat()
    ids = set(r.id for r in cat.records)
    for mid in ("enduse_petg", "fiberpart_pp_g30", "sealant_tpu_A95", "proto_pla", "generic_pla"):
        assert mid in ids
    pp = cat.get("fiberpart_pp_g30")
    assert pp.data["wiki_url"].endswith("/fiberpart/pp-g30")
    assert pp.filler == {"type": "glass", "mass_percent": 30.0}
    assert cat.get("fiberpart_tpu_c5").filler["type"] == "carbon"
    assert cat.get("contifiber_cpa").excluded and not cat.get("contifiber_cpa").selectable


def test_conflicting_sources_are_kept_and_bound_conservatively():
    petg = _cat().get("enduse_petg")
    srcs = petg.sources("tensile_strength_xy")
    assert len(srcs) == 2
    assert any(c["property"] == "tensile_strength_xy" for c in petg.conflicts)
    lo, hi, unit = petg.interval("tensile_strength_xy")
    assert (lo, hi, unit) == (26.0, 36.5, "MPa")
    req = R.Requirement("X", "tensile_strength", ">=", 30.0, "MPa")
    check = M._numeric(req, petg, "tensile_strength_xy")
    assert check["status"] == UNKNOWN                      # sources straddle the limit: no averaging


def test_density_converted_to_canonical_units():
    lo, hi, unit = _cat().get("fiberpart_pp_g30").interval("density")
    assert unit == "g/mm3" and abs(lo - 0.00115) < 1e-12 and abs(hi - 0.00118) < 1e-12


def test_printability_filters():
    cat = _cat()
    assert M.printable(cat.get("generic_pla"), MACHINE)["status"] == FAIL        # 2.85 mm filament
    assert M.printable(cat.get("enduse_petg"), MACHINE)["status"] == PASS
    pp = M.printable(cat.get("fiberpart_pp_g30"), MACHINE)
    assert pp["status"] == UNKNOWN                                                # needs 0.6 nozzle + hardened
    hard = dict(MACHINE, nozzle_change_possible=False)
    assert M.printable(cat.get("fiberpart_pp_g30"), hard)["status"] == FAIL


def test_chemical_resistance_not_proven_at_high_temperature():
    pp = _cat().get("enduse_pp")
    hot = R.parse_requirements("Наименование детали: X\nТемпература среды +75°C...+85°C.\nСреда щелочная, pH 10.")
    cold = R.parse_requirements("Наименование детали: X\nТемпература +20°C ± 2°C.\nСтойкость к изопропиловому спирту (IPA).")
    alk = [c for c in M.screen_material(pp, hot) if c["property"] == "alkali_resistance"][0]
    assert alk["status"] == UNKNOWN and "room temperature" in alk["note"]
    ipa = [c for c in M.screen_material(pp, cold) if c["property"] == "ipa_resistance"][0]
    assert ipa["status"] == PASS                         # Stereotech table row "Enduse PP": resistant


def test_tg_is_not_replaced_by_vicat():
    pc = _cat().get("enduse_pc")                         # Vicat 155 C known, Tg not given
    rs = R.parse_requirements("Наименование детали: X\nТемпература стеклования (Tg) должна быть не ниже 90°C.")
    tg = [c for c in M.screen_material(pc, rs) if c["property"] == "glass_transition_temperature"][0]
    assert tg["status"] == UNKNOWN


def test_screening_never_hides_a_hard_failure():
    cases = example_cases()
    if len(cases) != 7:
        skip("examples not found")
    wilo = R.parse_requirements(R.read_docx_text(cases[4][1]))
    res = M.screen_materials(wilo, _cat(), MACHINE)
    ranked = [e["material"]["id"] for e in res["ranked"]]
    assert ranked == ["fiberpart_pp_g30"]                # the only material without a mandatory failure
    for e in res["ranked"]:
        assert not e["summary"]["mandatory_fail"]
    petg = [e for e in res["rejected"] if e["material"]["id"] == "enduse_petg"][0]
    fails = set(c["property"] for c in petg["checks"] if c["status"] == FAIL)
    assert {"service_temperature", "tensile_modulus", "density"} <= fails
    generic = [e for e in res["rejected"] if e["material"]["id"] == "generic_abs_175"][0]
    assert generic["reason"] == "no manufacturer property data"


def test_colleague_reference_record_fails_wilo_modulus():
    path = os.path.join(REPO, "data", "research", "printing_constants_catalog.json")
    if not os.path.exists(path):
        skip("reference catalog missing")
    with open(path, encoding="utf-8") as f:
        refs = json.load(f)["material_records"]
    pp = [r for r in refs if r["id"].startswith("ultrafuse_ppgf30")][0]
    e = pp["selected_typical_properties"]["tensile_modulus_xy"]["value"]
    req = R.Requirement("R23", "tensile_modulus", ">=", 4500.0, "MPa")
    assert req.check(e) == FAIL                          # BASF PP GF30 2628 MPa < 4500 MPa


def test_part_type_and_application_match():
    cat = _cat()
    rs = R.RequirementSet(part_name="Ведущее колесо конвейерной линии (ролик привода)")
    assert M.part_type(rs) == "wheel_roller"
    assert M.application_match(cat.get("sealant_tpu"), "wheel_roller") == 1.0
    assert M.application_match(cat.get("enduse_petg"), "decorative") == 1.0       # memo: decorative
