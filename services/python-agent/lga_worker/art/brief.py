"""The art brief: what is being asked for, decided before a single stroke is planned.

Classification of the request (existing artwork / real subject / invention / school / medium), identification and
verification state of a referenced work, the goal (reproduction, study, interpretation, adaptation, inspired by), the
medium and the rendering mode, and the questions that really are the user's to answer.

The planner (a language model) may pass its own analysis; this module validates it, cross-checks it against the user's
words (an explicit "bút chì" beats a planner that picked FLAT_COLOR) and fills what is missing, deterministically.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from lga_worker.art import modes
from lga_worker.art.knowledge import MEDIA, MOVEMENTS, Artwork, ArtKnowledgeStore
from lga_worker.art.modes import SPECS, Mode, normalize


class Intent(str, Enum):
    REPRODUCTION = "REPRODUCTION"
    STUDY = "STUDY"
    INTERPRETATION = "INTERPRETATION"
    ADAPTATION = "ADAPTATION"
    INSPIRED_BY = "INSPIRED_BY"


class Verification(str, Enum):
    USER_PROVIDED = "USER_PROVIDED"  # the picture came from the user
    REFERENCE_FETCHED = "REFERENCE_FETCHED"  # an image was downloaded and its title matches; identity not proven by pixels
    BUILTIN_KNOWLEDGE = "BUILTIN_KNOWLEDGE"  # recognised from the built-in table only
    UNVERIFIED = "UNVERIFIED"  # looks like a real work, nothing confirms it
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"  # an invention or a plain subject


_STYLE_OF = re.compile(r"\b(theo phong cach|phong cach|in the style of|style of|kieu|nhu tranh)\b")
_INSPIRED = re.compile(r"\b(lay cam hung|cam hung tu|inspired by|dua tren y tuong)\b")
_STUDY = re.compile(r"\b(nghien cuu|study of|ky hoa lai|phac cau truc|phan tich bo cuc)\b")
_ADAPT = re.compile(r"\b(hoat hinh|cartoon|anime|pixel|chibi|phien ban|chuyen the|adaptation|doi sang)\b")
_FANTASY = re.compile(r"\b(rong|dragon|giai tuong|fantasy|sinh vat|phuong hoang|tien nu|than tien|quai vat|phep thuat|"
                      r"tren may|tren mat trang|tuong tuong|vu tru|robot|alien|nguoi ngoai hanh tinh)\b")
_SCHOOL = re.compile(r"\b(truong phai|an tuong|impression|hau an tuong|post impression|lap the|cubis|truu tuong|abstract|"
                     r"sieu thuc|surreal|bieu hien|expression|phuc hung|renaissance|baroque|pop art|dan gian|ukiyo)\b")
_ARTWORK_WORDS = re.compile(r"\b(tranh|buc tranh|tac pham|painting|artwork|masterpiece|kiet tac)\b")
_BY = re.compile(r"\b(cua|by|do)\s+[a-z]")


@dataclass
class ArtBrief:
    request: str
    kinds: list[str] = field(default_factory=list)  # A..E
    subject: str = ""
    artwork: dict[str, Any] | None = None
    resolution: str = "NOT_ARTWORK"  # IDENTIFIED | AMBIGUOUS | UNKNOWN_ARTWORK | NOT_ARTWORK
    intent: str | None = None
    verification: str = Verification.NOT_APPLICABLE.value
    confidence: float = 0.0
    movement: str | None = None
    medium: str | None = None
    mode: str = Mode.FLAT_COLOR.value
    mode_label: str = ""
    mode_reasons: list[str] = field(default_factory=list)
    brushes: list[str] = field(default_factory=list)
    no_colour: bool = False
    outline_only: bool = False
    needs_reference: bool = False
    reference_queries: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def mode_enum(self) -> Mode:
        return Mode(self.mode)


def _artwork_summary(work: Artwork) -> dict[str, Any]:
    return {"key": work.key, "title": work.titles[0], "artist": work.artist, "year": work.year, "movement": work.movement,
            "genre": work.genre, "medium": work.medium, "size": work.size, "location": work.location,
            "signature": list(work.signature), "palette": list(work.palette), "technique": list(work.technique),
            "versions": work.versions, "note": work.note}


def _movement_key(label: str) -> str | None:
    n = normalize(label)
    for key, info in MOVEMENTS.items():
        if normalize(info["label"]).split(" (")[0] in n or key.replace("_", " ") in n:
            return key
    if "post impression" in n:
        return "post_impressionism"
    if "impression" in n:
        return "impressionism"
    return None


def _medium_key(text: str) -> str | None:
    n = normalize(text)
    for key, words in (("oil", ("oil", "son dau")), ("acrylic", ("acrylic",)), ("watercolor", ("watercolo", "mau nuoc")),
                       ("lacquer", ("lacquer", "son mai")), ("pencil", ("pencil", "graphite", "but chi")),
                       ("charcoal", ("charcoal", "than chi")), ("ink", ("ink", "but muc")), ("pixel", ("pixel",))):
        if any(w in n for w in words):
            return key
    return None


def build_brief(request: str, *, store: ArtKnowledgeStore | None = None, subject: str | None = None,
                artwork_title: str | None = None, artist: str | None = None, intent: str | None = None,
                medium: str | None = None, mode: str | None = None, style: str | None = None,
                has_reference: bool = False, reference_verified: bool = False, offline: bool = False) -> ArtBrief:
    """Everything decidable before drawing, from the user's words and what the planner already worked out."""
    store = store or ArtKnowledgeStore()
    text = " ".join(x for x in (request, subject, artwork_title, artist, style) if x)
    t = normalize(text)
    brief = ArtBrief(request=request, subject=(subject or artwork_title or request).strip())

    # ---- identity of a referenced work ------------------------------------------------------------------------
    candidates = store.find(" ".join(x for x in (request, artwork_title, artist) if x))
    work: Artwork | None = None
    if candidates:
        top = candidates[0]
        close = [c for c in candidates if top[1] - c[1] < 0.1 and c[0].key != top[0].key]
        work = top[0]
        brief.confidence = top[1]
        if work.versions > 1 or close:
            brief.resolution = "AMBIGUOUS"
            what = work.titles[0] if work.versions > 1 else " / ".join([work.titles[0]] + [c[0].titles[0] for c in close])
            brief.questions.append(
                f"“{what}” does not name one work ({work.note or 'several works share this name'}). Which one do you mean?")
        else:
            brief.resolution = "IDENTIFIED"
        brief.artwork = _artwork_summary(work)
    else:
        named = store.artist_mentioned(t)
        if named and (_ARTWORK_WORDS.search(t) or _BY.search(t)):
            brief.resolution = "UNKNOWN_ARTWORK"
            brief.notes.append(f"the request names {named} and a painting this build does not know: nothing about it is verified")
            brief.confidence = 0.3

    # ---- kinds (a request may be several) --------------------------------------------------------------------
    if brief.resolution in ("IDENTIFIED", "AMBIGUOUS", "UNKNOWN_ARTWORK"):
        brief.kinds.append("A")
    if _FANTASY.search(t):
        brief.kinds.append("C")
    if _SCHOOL.search(t) or (work is None and _STYLE_OF.search(t)):
        brief.kinds.append("D")
    explicit = modes.modes_in(text)
    if explicit or medium:
        brief.kinds.append("E")
    if not brief.kinds:
        brief.kinds.append("B")

    # ---- intent --------------------------------------------------------------------------------------------------
    chosen_intent = (intent or "").upper() or None
    if chosen_intent not in {i.value for i in Intent}:
        chosen_intent = None
    if work is not None or brief.resolution == "UNKNOWN_ARTWORK":
        if _INSPIRED.search(t):
            derived = Intent.INSPIRED_BY
        elif _STUDY.search(t):
            derived = Intent.STUDY
        elif _ADAPT.search(t) or (_STYLE_OF.search(t) and not has_reference and (explicit or style)):
            derived = Intent.ADAPTATION
        elif _STYLE_OF.search(t):
            derived = Intent.INTERPRETATION
        else:
            derived = Intent.REPRODUCTION
        if chosen_intent and chosen_intent != derived.value:
            # the user's words decide for a request that says so; otherwise the planner's reading stands
            if derived != Intent.REPRODUCTION:
                brief.notes.append(f"intent {chosen_intent} replaced by {derived.value} (the request says so)")
                chosen_intent = derived.value
        else:
            chosen_intent = derived.value
        brief.intent = chosen_intent
    else:
        brief.intent = chosen_intent

    # ---- movement and medium -----------------------------------------------------------------------------------------
    if work is not None:
        brief.movement = work.movement
        brief.medium = work.medium
        brief.facts["movement"] = MOVEMENTS.get(_movement_key(work.movement) or "", {})
    elif style:
        key = _movement_key(style)
        brief.movement = MOVEMENTS[key]["label"] if key else style
        brief.facts["movement"] = MOVEMENTS.get(key or "", {})
    mkey = _medium_key(" ".join(x for x in (medium, text) if x))
    if medium and not brief.medium:
        brief.medium = medium
    elif mkey and not brief.medium:
        brief.medium = mkey
    if mkey:
        brief.facts["medium"] = MEDIA[mkey]

    # ---- rendering mode --------------------------------------------------------------------------------------------
    brief.no_colour = modes.wants_no_colour(text)
    brief.outline_only = modes.wants_outline_only(text)
    spoken = explicit[0] if explicit else None
    given = modes.parse_mode(mode)
    reasons: list[str] = []
    final: Mode
    if spoken is not None:
        final = spoken
        reasons.append(f"the request names it ({SPECS[spoken].label})")
        if given and given != spoken:
            reasons.append(f"the planner's {given.value} was replaced: the user's words decide")
    elif given is not None:
        final = given
        reasons.append("chosen by the planner from the request")
    elif work is not None and brief.intent in (Intent.REPRODUCTION.value, Intent.STUDY.value, None):
        final = work.mode
        reasons.append(f"how {work.titles[0]} is made ({work.medium}) -> simulated as {SPECS[work.mode].label}")
    elif mkey:
        final = MEDIA[mkey]["mode"]
        reasons.append(f"medium {mkey}")
    elif work is not None:
        final = work.mode
        reasons.append("the work's own mode as a starting point")
    else:
        final = Mode.FLAT_COLOR
        reasons.append("no medium named: an illustration in flat colour regions with outlines")
    if brief.no_colour and SPECS[final].colour in ("full", "limited"):
        replacement = Mode.GRAYSCALE if re.search(r"\b(xam|gr[ae]y)\b", t) else Mode.BLACK_WHITE
        reasons.append(f"“no colour” requested: {final.value} -> {replacement.value}")
        final = replacement
    brief.mode = final.value
    brief.mode_label = SPECS[final].label
    brief.mode_reasons = reasons
    brief.brushes = list(SPECS[final].brushes)
    brief.notes.append("simulation: " + SPECS[final].honesty)

    # ---- reference needs ------------------------------------------------------------------------------------------
    if work is not None and brief.resolution == "IDENTIFIED":
        reproduces = brief.intent in (Intent.REPRODUCTION.value, Intent.STUDY.value, Intent.ADAPTATION.value, Intent.INTERPRETATION.value)
        brief.needs_reference = reproduces and not has_reference
        if work.commons_query:
            brief.reference_queries = [work.commons_query, f"{work.titles[0]} {work.artist}"]
    if has_reference:
        brief.verification = (Verification.REFERENCE_FETCHED if reference_verified else Verification.USER_PROVIDED).value
    elif work is not None and brief.resolution == "IDENTIFIED":
        brief.verification = Verification.BUILTIN_KNOWLEDGE.value
    elif brief.resolution in ("UNKNOWN_ARTWORK", "AMBIGUOUS"):
        brief.verification = Verification.UNVERIFIED.value
    if brief.needs_reference and offline:
        brief.notes.append("offline: no reference picture can be fetched; the work is drawn from the built-in description (UNVERIFIED likeness)")
    return brief
