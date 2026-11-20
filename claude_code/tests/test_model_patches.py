"""Regression checks for model patches using synthetic decks; no simulator or model API calls.

The synthetic patch exercises the static guard and patch application only. It is not a suggested model.
"""

import pytest

from tcadcal import deck as D
from tcadcal import proposals as P
from tcadcal.state import START_PARAMS, StateError, Workspace, init_workspace_state
from tcadcal.util import sha256_file, write_json


TOKENS = {spec["token"].strip("@") for spec in START_PARAMS.values()}


@pytest.mark.parametrize("old,new", [
    ("{ Poisson Electron Hole }", "{ Poisson Electron }"),
    ("{ Poisson Electron Hole }", "{ Hole Electron Poisson }"),
    ("{ Poisson Electron Hole }", "{ Poisson Electron Hole Hole }"),
    ("LineSearchDamping=0.01", "LineSearchDamping=0.02"),
    ("Voltage=2.30000", "Voltage=2.40000"),
    ("Increment = 1.35", "Increment = 1.40"),
    ("Coupled { Poisson }", ""),
])
def test_equation_additions_do_not_unlock_solve_steps(old, new):
    templates = D.read_templates(D.DECK0)
    assert old in templates["sdevice"]
    templates["sdevice"] = templates["sdevice"].replace(old, new, 1)
    assert any("Solve section changed" in error for error in D.check_template(templates, TOKENS))


@pytest.mark.parametrize("key,old,new,error", [
    ("sde", None, None, "structure"),
    ("sdevice", "AreaFactor  = 5", "AreaFactor  = 6", "AreaFactor"),
    ("sdevice", "Temperature = 300", "Temperature = 301", "300"),
    ("sdevice", 'Name="source" Voltage=0', 'Name="source" Voltage=1', "start bias"),
])
def test_measurement_and_geometry_checks_remain(key, old, new, error):
    templates = D.read_templates(D.DECK0)
    if old is None:
        templates[key] += "\n; synthetic geometry edit\n"
    else:
        assert old in templates[key]
        templates[key] = templates[key].replace(old, new, 1)
    assert any(error in message for message in D.check_template(templates, TOKENS))


@pytest.fixture()
def patch_fixture(tmp_path):
    write_json(tmp_path / ".tcadcal_workspace.json", {"program": "main"})
    init_workspace_state(tmp_path, cap=0)
    ws = Workspace(tmp_path)
    pid = "P001"
    # Exercise a new uniform interface-charge parameter, rather than a comment-only patch.
    old = D.read_templates(D.DECK0)["sdevice"]
    write_json(P.proposal_path(ws, pid), {
        "type": "model_patch", "title": "Synthetic patch regression",
        "patch": {"base_deck": "v000", "edits": [{"file": "sdevice", "old": old, "new": old +
            '\nPhysics (RegionInterface="Gox1/Semiconductor") {\n'
            '   Traps (FixedCharge Conc=@charge_fixture@)\n}\n'}],
            "new_params": [{"name": "charge_fixture", "token": "@charge_fixture@", "init": 1.0,
                            "lo": -2.0, "hi": 2.0, "scale": "linear", "unit": "cm^-2",
                            "meaning": "synthetic test parameter", "basis": "synthetic fixture"}]}})
    return ws, pid


def author_decision(ws, pid, decision="approve"):
    # This artifact belongs only to a temporary synthetic test, never to an actual calibration run.
    write_json(ws.root / "approvals" / f"{pid}.json", {
        "approver": "author", "decision": decision, "proposal_sha256": sha256_file(P.proposal_path(ws, pid))})


def test_approved_patch_applies_and_registers_new_parameter(patch_fixture):
    ws, pid = patch_fixture
    author_decision(ws, pid)
    result = P.apply_patch(ws, pid)
    assert result == {"deck": "v001", "parent": "v000", "new_params": ["charge_fixture"]}
    registry = ws.registry()
    assert registry["decks"]["v001"]["proposal"] == pid
    assert registry["params"]["charge_fixture"]["since"] == "v001"
    assert "@charge_fixture@" in D.read_templates(ws.deck_dir("v001"))["sdevice"]
    assert ws.accepted()["deck"] == "v000"  # Applying a patch is not accepting a calibrated card.
    with pytest.raises(StateError, match="already applied"):
        P.apply_patch(ws, pid)


@pytest.mark.parametrize("review", ["missing", "reject", "stale"])
def test_unapproved_patch_creates_no_deck(patch_fixture, review):
    ws, pid = patch_fixture
    if review != "missing":
        author_decision(ws, pid, "reject" if review == "reject" else "approve")
    if review == "stale":
        path = P.proposal_path(ws, pid)
        path.write_text(path.read_text() + "\n")
    with pytest.raises(StateError):
        P.apply_patch(ws, pid)
    assert ws.deck_versions() == ["v000"]
    assert "charge_fixture" not in ws.registry()["params"]
