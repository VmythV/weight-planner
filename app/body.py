"""身体成分估算：BMI、体脂率、肌肉量、水分、骨量等。

两种来源：
- 有阻抗（体重秤自动上报）：小米体脂秤开源社区逆向的 BIA 公式（openScale、body_metrics 等项目通用）。
  与米家 App 的新算法不完全一致（实测 S400 Pro 体脂率差约 2–3 个百分点），只适合看趋势。
- 只有体重：Deurenberg (1991) 公式，按 BMI、年龄、性别估算体脂率，误差更大，同样只看趋势。
"""

from __future__ import annotations

from dataclasses import dataclass

# BMI 分级，中国成人标准（WS/T 428-2013）
BMI_BANDS = [
    (18.5, "偏瘦"),
    (24.0, "正常"),
    (28.0, "超重"),
    (None, "肥胖"),
]

# 体脂率分级（Gallagher 2000），按年龄段：偏低 / 健康 / 偏高 / 肥胖 的分界
FAT_BANDS = {
    "male": [(39, (8, 20, 25)), (59, (11, 22, 28)), (200, (13, 25, 30))],
    "female": [(39, (21, 33, 39)), (59, (23, 34, 40)), (200, (24, 36, 42))],
}
FAT_LABELS = ["偏低", "健康", "偏高", "肥胖"]

# 体水分率的常见参考区间
WATER_RANGE = {"male": (55.0, 65.0), "female": (45.0, 60.0)}


@dataclass(frozen=True)
class Person:
    sex: str  # "male" | "female"
    age: int
    height_cm: float


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def bmi(weight: float, p: Person) -> float:
    return weight / (p.height_cm / 100.0) ** 2


def bmi_level(value: float) -> int:
    for i, (upper, _) in enumerate(BMI_BANDS):
        if upper is None or value < upper:
            return i
    return len(BMI_BANDS) - 1


def bmi_weight_range(p: Person, lo: float = 18.5, hi: float = 24.0) -> tuple[float, float]:
    """BMI 正常范围对应的体重区间。"""
    h2 = (p.height_cm / 100.0) ** 2
    return lo * h2, hi * h2


def fat_cutoffs(p: Person) -> tuple[float, float, float]:
    for max_age, cuts in FAT_BANDS[p.sex]:
        if p.age <= max_age:
            return cuts
    return FAT_BANDS[p.sex][-1][1]


def fat_level(value: float, p: Person) -> int:
    for i, cut in enumerate(fat_cutoffs(p)):
        if value < cut:
            return i
    return 3


def fat_pct_from_bmi(weight: float, p: Person) -> float:
    """Deurenberg 公式：体脂率 = 1.2·BMI + 0.23·年龄 − 10.8·性别(男=1) − 5.4。"""
    sex = 1.0 if p.sex == "male" else 0.0
    return _clamp(1.2 * bmi(weight, p) + 0.23 * p.age - 10.8 * sex - 5.4, 3.0, 75.0)


# ---------- BIA（需要 50 kHz 阻抗） ----------


def _lbm_coefficient(weight: float, impedance: float, p: Person) -> float:
    lbm = (p.height_cm * 9.058 / 100.0) * (p.height_cm / 100.0)
    lbm += weight * 0.32 + 12.226
    lbm -= impedance * 0.0068
    lbm -= p.age * 0.0542
    return lbm


def fat_pct_from_impedance(weight: float, impedance: float, p: Person) -> float:
    if p.sex == "female":
        const = 9.25 if p.age <= 49 else 7.25
    else:
        const = 0.8
    coefficient = 1.0
    if p.sex == "male" and weight < 61:
        coefficient = 0.98
    elif p.sex == "female" and weight > 60:
        coefficient = 0.96 * (1.03 if p.height_cm > 160 else 1.0)
    elif p.sex == "female" and weight < 50:
        coefficient = 1.02 * (1.03 if p.height_cm > 160 else 1.0)
    lbm = _lbm_coefficient(weight, impedance, p)
    return _clamp((1.0 - (lbm - const) * coefficient / weight) * 100.0, 5.0, 75.0)


def water_pct(fat_pct: float) -> float:
    water = (100.0 - fat_pct) * 0.7
    water *= 1.02 if water <= 50 else 0.98
    return _clamp(water, 35.0, 75.0)


def bone_mass(weight: float, impedance: float, p: Person) -> float:
    base = 0.245691014 if p.sex == "female" else 0.18016894
    bone = (_lbm_coefficient(weight, impedance, p) * 0.05158) - base
    bone += 0.1 if bone > 2.2 else -0.1
    return _clamp(bone, 0.5, 8.0)


def muscle_mass(weight: float, fat_pct: float, bone: float) -> float:
    return _clamp(weight - weight * fat_pct / 100.0 - bone, 10.0, 120.0)


# ---------- 汇总 ----------


def bmr_katch(lean_mass: float) -> float:
    """Katch-McArdle 基础代谢：按去脂体重计算，体脂率越准越可靠。"""
    return 370.0 + 21.6 * lean_mass


def composition(weight: float, p: Person, impedance: float | None = None) -> dict:
    """一次称重的身体成分。impedance 为 50 kHz 阻抗，没有时只给出按 BMI 估算的部分。"""
    b = bmi(weight, p)
    has_bia = impedance is not None and impedance > 0
    fat = fat_pct_from_impedance(weight, impedance, p) if has_bia else fat_pct_from_bmi(weight, p)
    fat_mass = weight * fat / 100.0
    lean = weight - fat_mass
    lo, hi = bmi_weight_range(p)
    out = {
        "method": "bia" if has_bia else "bmi",
        "weight": round(weight, 2),
        "bmi": round(b, 1),
        "bmi_level": bmi_level(b),
        "bmi_bands": [18.5, 24.0, 28.0],
        "bmi_labels": [label for _, label in BMI_BANDS],
        "healthy_weight": [round(lo, 1), round(hi, 1)],
        "ideal_weight": round(22.0 * (p.height_cm / 100.0) ** 2, 1),
        "fat_pct": round(fat, 1),
        "fat_level": fat_level(fat, p),
        "fat_bands": list(fat_cutoffs(p)),
        "fat_labels": FAT_LABELS,
        "fat_mass": round(fat_mass, 1),
        "lean_mass": round(lean, 1),
        "bmr_katch": round(bmr_katch(lean)),
        "water_pct": None,
        "water_range": list(WATER_RANGE[p.sex]),
        "muscle_mass": None,
        "muscle_pct": None,
        "bone_mass": None,
    }
    if has_bia:
        bone = bone_mass(weight, impedance, p)
        muscle = muscle_mass(weight, fat, bone)
        out.update(
            water_pct=round(water_pct(fat), 1),
            bone_mass=round(bone, 2),
            muscle_mass=round(muscle, 1),
            muscle_pct=round(muscle / weight * 100.0, 1),
        )
    return out
