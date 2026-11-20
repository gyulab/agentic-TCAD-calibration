"""The device wording of the agent instructions: every [[NAME]] place in templates/ and skills/ has a value in
config.TEXT (from devices/<name>/device.toml), and the J terms follow the calibration drain biases of the device."""
from pathlib import Path

from tcadcal import config as C

HERE = Path(__file__).resolve().parents[1]


def test_instruction_places_are_filled():
    files = sorted((HERE / "templates").glob("*")) + sorted((HERE / "skills").glob("*/SKILL.md"))
    assert files
    for f in files:
        assert "[[" not in C.render_text(f.read_text()), f.name


def test_j_terms_follow_the_calibration_biases():
    assert len(C.J_TERMS) == 14 and len(C.VD_KEYS) == 2
    for vd in C.VD_KEYS:
        assert {f"V_th@{vd}", f"SS_profile@{vd}", f"region:on@{vd}"} <= set(C.J_TERMS)
