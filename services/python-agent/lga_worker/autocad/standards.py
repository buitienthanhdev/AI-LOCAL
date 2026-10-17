"""Project Standards Profile (spec section 1): the configuration that must be known before anything is drawn.

`check_profile()` takes whatever the user has said so far, lists what is missing / assumed / invalid / legally
unverified, and produces the set of clarification questions (suggested options, multi-select flag, "Khac" free text
and "Chua biet - giu UNKNOWN"). It never fills a missing value and never offers a legal requirement as a suggestion:
fields about codes, zoning, fire safety and local rules have NO suggested options, only "Khac" and "Giu UNKNOWN".
A code entry that is not marked as verified by the lead designer is reported as `unverified_legal` and must not be
presented as a legal requirement.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

TIER_DRAW = "DRAW"              # needed before geometry is generated
TIER_PLOT = "PLOT"              # needed before layouts / PDF
TIER_CODE = "CODE_REVIEW"       # needed before any code-compliance statement can be reviewed
TIERS = (TIER_DRAW, TIER_PLOT, TIER_CODE)

OTHER_OPTION = {"id": "other", "label": "Khác (nhập tự do)", "label_en": "Other (free text)"}
UNKNOWN_OPTION = {"id": "unknown", "label": "Chưa biết — giữ UNKNOWN", "label_en": "Not known — keep UNKNOWN"}

UNIT_NAMES = ("mm", "cm", "m", "in", "ft")
PAPER_SIZES = ("A0", "A1", "A2", "A3", "A4")
SCALE_RE = re.compile(r"^\s*1\s*[:/]\s*\d+(\.\d+)?\s*$")
MISSING_TOKENS = {"", "unknown", "none", "null", "chua biet", "chưa biết", "n/a", "?"}


@dataclass(frozen=True)
class FieldSpec:
    field_id: str
    group: str
    tier: str
    label_vi: str
    label_en: str
    options: tuple[tuple[str, str], ...] = ()  # (id, Vietnamese label); suggestions only, never legal values
    multi: bool = False
    legal: bool = False                         # a claim about codes / law: needs `verified_by`
    validator: str = ""                          # name of a value check in _VALIDATORS


def _f(field_id: str, group: str, tier: str, vi: str, en: str, options: tuple[tuple[str, str], ...] = (),
       multi: bool = False, legal: bool = False, validator: str = "") -> FieldSpec:
    return FieldSpec(field_id, group, tier, vi, en, options, multi, legal, validator)


FIELD_SPECS: tuple[FieldSpec, ...] = (
    # --- administration
    _f("country", "admin", TIER_DRAW, "Quốc gia", "Country"),
    _f("locality", "admin", TIER_DRAW, "Địa phương (tỉnh/thành, quận/huyện)", "Locality"),
    _f("authority", "admin", TIER_DRAW, "Cơ quan quản lý / cấp phép", "Governing authority"),
    _f("building_type", "admin", TIER_DRAW, "Loại công trình", "Building type",
       (("residential", "Nhà ở"), ("residential_renovation", "Nhà ở — cải tạo"))),
    _f("design_stage", "admin", TIER_DRAW, "Giai đoạn hồ sơ", "Design stage",
       (("concept", "Ý tưởng / phương án"), ("schematic", "Thiết kế cơ sở"), ("technical", "Thiết kế kỹ thuật"),
        ("construction_docs", "Bản vẽ thi công"), ("as_built", "Hoàn công"))),
    # --- building
    _f("house_type", "building", TIER_DRAW, "Loại nhà", "House type",
       (("townhouse", "Nhà phố"), ("villa", "Biệt thự"), ("single_storey", "Nhà cấp 4"), ("garden_house", "Nhà vườn"))),
    _f("floors", "building", TIER_DRAW, "Số tầng", "Number of floors", validator="positive_int"),
    _f("design_scope", "building", TIER_DRAW, "Phạm vi thiết kế", "Design scope",
       (("new_build", "Xây mới"), ("renovation", "Cải tạo"), ("extension", "Mở rộng"), ("interior", "Nội thất"),
        ("demolition", "Phá dỡ")), multi=True),
    _f("disciplines", "building", TIER_DRAW, "Bộ môn thuộc phạm vi", "Disciplines in scope",
       (("architecture", "Kiến trúc"), ("structure", "Kết cấu"), ("electrical", "Điện"), ("hvac", "HVAC"),
        ("plumbing", "Cấp thoát nước"), ("fire", "PCCC")), multi=True),
    # --- codes (legal: never suggested, must be verified by the lead designer)
    _f("building_code", "codes", TIER_CODE, "Quy chuẩn / tiêu chuẩn xây dựng áp dụng", "Applicable building code",
       legal=True),
    _f("zoning", "codes", TIER_CODE, "Quy hoạch (mật độ, chiều cao, khoảng lùi)", "Zoning / planning", legal=True),
    _f("fire_code", "codes", TIER_CODE, "Yêu cầu PCCC", "Fire safety requirements", legal=True),
    _f("accessibility", "codes", TIER_CODE, "Yêu cầu tiếp cận", "Accessibility requirements", legal=True),
    _f("structure_code", "codes", TIER_CODE, "Tiêu chuẩn kết cấu", "Structural code", legal=True),
    _f("mep_code", "codes", TIER_CODE, "Tiêu chuẩn MEP", "MEP code", legal=True),
    _f("local_requirements", "codes", TIER_CODE, "Yêu cầu địa phương", "Local requirements", legal=True),
    # --- CAD setup
    _f("autocad_version", "cad", TIER_DRAW, "Phiên bản / edition AutoCAD", "AutoCAD version / edition"),
    _f("file_format", "cad", TIER_DRAW, "Định dạng DWG/DXF (phiên bản)", "DWG/DXF format",
       (("dwg", "DWG"), ("dxf", "DXF")), multi=True),
    _f("units", "cad", TIER_DRAW, "Đơn vị bản vẽ", "Drawing units",
       (("mm", "Milimét (mm)"), ("cm", "Centimét (cm)"), ("m", "Mét (m)"), ("in", "Inch")), validator="unit"),
    _f("coordinate_system", "cad", TIER_DRAW, "Hệ tọa độ", "Coordinate system",
       (("local_wcs", "Cục bộ, gốc (0,0) theo WCS"), ("project_survey", "Hệ tọa độ dự án / khảo sát (nhập chi tiết)"))),
    _f("layer_convention", "cad", TIER_DRAW, "Quy ước layer", "Layer convention",
       (("spec_sample", "Mẫu A-WALL-EXT, A-DOOR, ... (tên mẫu trong đặc tả)"),
        ("office_template", "Template của văn phòng (ánh xạ sang chuẩn hiện hữu)"))),
    _f("area_rule", "cad", TIER_DRAW, "Tiêu chí tính diện tích", "Area measurement rule",
       (("wall_centerline", "Theo tim tường"), ("wall_outer_face", "Theo mép ngoài tường"),
        ("wall_inner_face", "Theo mép trong (thông thủy)"), ("finish_face", "Theo mép hoàn thiện")), multi=True),
    _f("paper_size", "plot", TIER_PLOT, "Khổ giấy", "Paper size", tuple((p, p) for p in PAPER_SIZES), multi=True,
       validator="paper"),
    _f("scale", "plot", TIER_PLOT, "Tỷ lệ bản vẽ", "Drawing scale",
       (("1:20", "1:20"), ("1:50", "1:50"), ("1:100", "1:100"), ("1:200", "1:200"), ("1:500", "1:500")), multi=True,
       validator="scale"),
    _f("text_font", "plot", TIER_PLOT, "Font chữ (hỗ trợ tiếng Việt)", "Text font",
       (("truetype", "TrueType / Unicode"), ("shx", "SHX"))),
    _f("linetype", "plot", TIER_PLOT, "Linetype", "Linetypes"),
    _f("lineweight", "plot", TIER_PLOT, "Lineweight", "Lineweights"),
    _f("plot_style", "plot", TIER_PLOT, "CTB / STB", "Plot style table",
       (("ctb", "CTB"), ("stb", "STB"))),
    _f("title_block", "plot", TIER_PLOT, "Title block", "Title block"),
    _f("presentation_convention", "plot", TIER_PLOT, "Quy ước trình bày", "Presentation conventions"),
)

SPEC_BY_ID = {s.field_id: s for s in FIELD_SPECS}


def _positive_int(value: Any) -> str | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return f"{value!r} is not a number"
    if number != int(number) or number < 1:
        return f"{value!r} is not a positive whole number"
    return None


def _unit(value: Any) -> str | None:
    return None if str(value).strip().lower() in UNIT_NAMES else f"{value!r} is not one of {', '.join(UNIT_NAMES)}"


def _paper(value: Any) -> str | None:
    items = value if isinstance(value, (list, tuple)) else [value]
    bad = [str(v) for v in items if str(v).strip().upper() not in PAPER_SIZES]
    return f"unsupported paper size(s) {bad}; known: {', '.join(PAPER_SIZES)} (use Khac for others)" if bad else None


def _scale(value: Any) -> str | None:
    items = value if isinstance(value, (list, tuple)) else [value]
    bad = [str(v) for v in items if not SCALE_RE.match(str(v))]
    return f"{bad} is not written as 1:N" if bad else None


_VALIDATORS = {"positive_int": _positive_int, "unit": _unit, "paper": _paper, "scale": _scale}


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in MISSING_TOKENS
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def _unwrap(raw: Any) -> tuple[Any, str | None, Mapping[str, Any]]:
    """A profile entry may be a bare value or {"value", "status", "verified_by", "source"}."""
    if isinstance(raw, Mapping) and ("value" in raw or "status" in raw):
        return raw.get("value"), (str(raw["status"]).upper() if raw.get("status") else None), raw
    return raw, None, {}


@dataclass
class FieldState:
    field_id: str
    tier: str
    state: str               # PROVIDED | ASSUMED | MISSING | INVALID
    value: Any = None
    issues: list[str] = field(default_factory=list)
    legal_verified: bool | None = None  # only for legal fields

    def to_dict(self) -> dict[str, Any]:
        return {"field_id": self.field_id, "tier": self.tier, "state": self.state, "value": self.value,
                "issues": self.issues, "legal_verified": self.legal_verified}


@dataclass
class ProfileReport:
    fields: dict[str, FieldState]
    missing: dict[str, list[str]]            # tier -> field ids that are missing or invalid
    assumed: list[str]
    invalid: list[str]
    unverified_legal: list[str]
    unknown_fields: list[str]
    questions: list[dict[str, Any]]
    ready: dict[str, bool]                   # tier -> ready

    def to_dict(self) -> dict[str, Any]:
        return {
            "fields": {k: v.to_dict() for k, v in self.fields.items()}, "missing": self.missing,
            "assumed": self.assumed, "invalid": self.invalid, "unverified_legal": self.unverified_legal,
            "unknown_fields": self.unknown_fields, "questions": self.questions,
            "ready_to_draw": self.ready[TIER_DRAW], "ready_for_plot": self.ready[TIER_PLOT],
            "ready_for_code_review": self.ready[TIER_CODE],
        }


def build_question(spec: FieldSpec, reason: str) -> dict[str, Any]:
    """One clarification question. Legal fields get no suggestions; all get Khac + Giu UNKNOWN."""
    options: list[dict[str, str]] = [{"id": oid, "label": label} for oid, label in spec.options]
    options += [dict(OTHER_OPTION), dict(UNKNOWN_OPTION)]
    return {
        "field_id": spec.field_id, "tier": spec.tier, "group": spec.group,
        "prompt": f"{spec.label_vi}?", "prompt_en": f"{spec.label_en}?", "reason": reason,
        "options": options, "multi_select": spec.multi, "allow_free_text": True, "default_if_skipped": "UNKNOWN",
        "legal": spec.legal,
        "note": ("Yêu cầu pháp lý: chỉ ghi khi chủ trì thiết kế xác nhận; không có gợi ý mặc định."
                 if spec.legal else ""),
    }


def check_profile(profile: Mapping[str, Any] | None) -> ProfileReport:
    """Validate a (possibly partial) profile and generate the question set for what is missing or doubtful."""
    profile = dict(profile or {})
    states: dict[str, FieldState] = {}
    questions: list[dict[str, Any]] = []
    assumed: list[str] = []
    invalid: list[str] = []
    unverified: list[str] = []
    missing: dict[str, list[str]] = {t: [] for t in TIERS}
    for spec in FIELD_SPECS:
        value, status, wrapper = _unwrap(profile.get(spec.field_id))
        if status == "UNKNOWN" or _is_missing(value):
            states[spec.field_id] = FieldState(spec.field_id, spec.tier, "MISSING")
            missing[spec.tier].append(spec.field_id)
            questions.append(build_question(spec, "missing"))
            continue
        issues: list[str] = []
        problem = _VALIDATORS[spec.validator](value) if spec.validator else None
        if problem:
            states[spec.field_id] = FieldState(spec.field_id, spec.tier, "INVALID", value, [problem])
            invalid.append(spec.field_id)
            missing[spec.tier].append(spec.field_id)
            questions.append(build_question(spec, f"invalid: {problem}"))
            continue
        state = "ASSUMED" if status == "ASSUMED" else "PROVIDED"
        legal_verified: bool | None = None
        if spec.legal:
            legal_verified = bool(str(wrapper.get("verified_by") or "").strip())
            if not legal_verified:
                issues.append("not verified by the lead designer: do not present as a legal requirement")
                unverified.append(spec.field_id)
                questions.append(build_question(spec, "unverified legal value: who verified it?"))
                missing[spec.tier].append(spec.field_id)
        if state == "ASSUMED":
            assumed.append(spec.field_id)
            issues.append("assumed value: confirm with evidence before relying on it")
        states[spec.field_id] = FieldState(spec.field_id, spec.tier, state, value, issues, legal_verified)
    ready = {tier: not missing[tier] for tier in TIERS}
    unknown_fields = sorted(set(profile) - set(SPEC_BY_ID))
    return ProfileReport(states, missing, assumed, invalid, unverified, unknown_fields, questions, ready)


def profile_value(profile: Mapping[str, Any] | None, field_id: str) -> Any:
    """The plain value of a profile field, or None when missing/unknown (never a default)."""
    value, status, _ = _unwrap((profile or {}).get(field_id))
    return None if status == "UNKNOWN" or _is_missing(value) else value
