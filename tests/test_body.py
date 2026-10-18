import pytest

from app import body

MALE = body.Person(sex="male", age=35, height_cm=175)
FEMALE = body.Person(sex="female", age=45, height_cm=165)


def test_bmi_and_levels():
    assert body.bmi(73.5, MALE) == pytest.approx(24.0)
    assert [body.bmi_level(v) for v in (18.4, 18.5, 23.9, 24.0, 27.9, 28.0)] == [0, 1, 1, 2, 2, 3]
    lo, hi = body.bmi_weight_range(MALE)
    assert lo == pytest.approx(56.66, abs=0.01) and hi == pytest.approx(73.5)


def test_fat_cutoffs_depend_on_sex_and_age():
    assert body.fat_cutoffs(MALE) == (8, 20, 25)
    assert body.fat_cutoffs(FEMALE) == (23, 34, 40)
    assert body.fat_level(19.9, MALE) == 1 and body.fat_level(25, MALE) == 3


def test_deurenberg():
    # 1.2·30.04 + 0.23·35 − 10.8 − 5.4
    assert body.fat_pct_from_bmi(92.0, MALE) == pytest.approx(27.90, abs=0.01)


def test_bia_formula_output_is_locked():
    # 92.0 kg、50 kHz 阻抗 480 Ω 的样例。开源公式和米家新算法可能相差 2–3 个百分点，
    # 这里锁定公式本身的输出，防止改动时无意中变化
    c = body.composition(92.0, MALE, 480.0)
    assert c["method"] == "bia"
    assert c["fat_pct"] == pytest.approx(31.0, abs=0.05)
    assert c["muscle_pct"] == pytest.approx(65.4, abs=0.05)
    assert c["bone_mass"] == pytest.approx(3.23, abs=0.01)
    assert c["water_pct"] == pytest.approx(49.2, abs=0.05)
    assert c["fat_mass"] + c["lean_mass"] == pytest.approx(92.0, abs=0.1)


def test_composition_without_impedance_has_no_bia_fields():
    c = body.composition(70, MALE)
    assert c["method"] == "bmi"
    assert c["muscle_mass"] is None and c["water_pct"] is None and c["bone_mass"] is None
