"""Deterministic local OCR profiles, script routing and text quality gates.

The configured ``spa+eng`` contract remains the default.  Multilingual
routing is opt-in and never sends every installed language to one Tesseract
recognition pass: OSD selects one bounded profile and at most one measured
fallback is attempted.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

OcrProfileName = Literal[
    "configured",
    "latin",
    "han-simplified",
    "han-traditional",
    "auto-multilingual",
]

OCR_PROFILE_CHOICES: tuple[OcrProfileName, ...] = (
    "configured",
    "latin",
    "han-simplified",
    "han-traditional",
    "auto-multilingual",
)
OCR_PROFILE_VERSION = "tesseract-script-routing-v1"

LATIN_LANGUAGES = ("spa", "eng", "deu")
HAN_SIMPLIFIED_LANGUAGES = ("chi_sim", "eng")
HAN_TRADITIONAL_LANGUAGES = ("chi_tra", "eng")
OSD_LANGUAGE = "osd"
MAX_RECOGNITION_ATTEMPTS = 2

_LANGUAGE_CODE = re.compile(r"[A-Za-z0-9_]+\Z")
_OSD_VALUE = re.compile(r"^([^:]+):\s*(.*?)\s*$")
_MOJIBAKE_MARKERS = ("ï¿½", "Ã", "Â", "â€", "\ufffd")
_TRADITIONAL_ONLY_HINTS = frozenset(
    "萬與專業東絲兩嚴喪個豐臨為麗舉義烏樂喬習鄉書買亂爭於亞產畝親億僅從倉"
    "儀價眾優會傘偉傳傷倫體餘來債傾僑儘償兒黨內關興養獸岡冊寫軍農凍減幾"
    "劃劇劉劍辦務動勞勢勵勸區醫華協單賣衛卻廠歷壓厭縣參雙發變敘臺葉號後"
    "徑從復徵德憶應懷態總恆惡愛愜慘慮慶憂戲戶撫擁擇擔據擬擴攝擺損數斷無"
    "時晝曆書會機權條極標樣樓檔檢歐歡歲歸殘殼氣漢湯溝滅滬灣濾燈爐爭牆獨"
    "環現電畫異當疇癡發盜監盤睏礎禮種穩窮競筆築簡糧糾紀約紅級紙紋納紐線"
    "組經綜綠維網緊緒續總繪繫繳羅罰職聯聽肅脫腦腳臺與舊艦艱藝節範築類糧"
    "粵糞紛紀級納紹終組結絕統絲經綁綠維綱網綜緊緒線編緩練縣縮縱總績繫續"
    "纖纜缽罷羅義習翻聖聞聯聲聽職肅腦臉臘舉舊艙艦艱艷藝藥處虛號蟲衝補裝"
    "製複見規覺覽觀觸訂計訊討訓記託設許譯評詞試詩話誠語說課調請論諸諾謀"
    "謝證識譜讀變讓貝財責賬貨貫貴貸費賀賓賣賠賢質賴贊趕趨車軌軟輕載較輛"
    "輔輸轉辦邊遙遞遠適選遺鄉鄭釋裡鑒針鈣鈴鉛銀銅銷鋼錄錶錯鍋鍵鎖鏡鐵鑄"
    "鑽長門閉開閒間閥閣隊陽陰陳陸險隨隱雜離難電霧靜頂項順須預領頭頻顆題"
    "額顏風飛飯飲館馬駕驗驅體鬥魚鳥鳴麥黃點黨齊齒龍龜"
)


def parse_language_spec(language: str) -> tuple[str, ...]:
    """Return unique Tesseract language codes in caller order."""

    if not isinstance(language, str):
        raise ValueError("OCR language specification must be a string")
    values: list[str] = []
    seen: set[str] = set()
    for raw in language.split("+"):
        value = raw.strip()
        if not value:
            continue
        if _LANGUAGE_CODE.fullmatch(value) is None:
            raise ValueError(f"invalid OCR language code: {value!r}")
        if value in seen:
            continue
        seen.add(value)
        values.append(value)
    if not values:
        raise ValueError("OCR language specification must not be empty")
    return tuple(values)


def language_spec(languages: tuple[str, ...]) -> str:
    if not languages:
        raise ValueError("effective OCR languages must not be empty")
    return "+".join(languages)


@dataclass(frozen=True, slots=True)
class OcrProfilePlan:
    profile: OcrProfileName
    configured_languages: tuple[str, ...]
    required_languages: tuple[str, ...]
    osd_enabled: bool

    @property
    def required_language_spec(self) -> str:
        return language_spec(self.required_languages)


def resolve_ocr_profile(
    profile: OcrProfileName,
    configured_language: str,
) -> OcrProfilePlan:
    """Resolve preflight requirements without probing or mutating the runtime."""

    if profile not in OCR_PROFILE_CHOICES:
        raise ValueError(f"unsupported OCR profile: {profile}")
    configured = parse_language_spec(configured_language)
    if profile == "configured":
        return OcrProfilePlan(profile, configured, configured, False)
    by_profile = {
        "latin": LATIN_LANGUAGES,
        "han-simplified": HAN_SIMPLIFIED_LANGUAGES,
        "han-traditional": HAN_TRADITIONAL_LANGUAGES,
    }
    selected = by_profile.get(profile)
    if selected is not None:
        return OcrProfilePlan(profile, configured, (*selected, OSD_LANGUAGE), True)
    return OcrProfilePlan(
        profile,
        configured,
        (
            *LATIN_LANGUAGES,
            *tuple(value for value in HAN_SIMPLIFIED_LANGUAGES if value != "eng"),
            *tuple(value for value in HAN_TRADITIONAL_LANGUAGES if value != "eng"),
            OSD_LANGUAGE,
        ),
        True,
    )


@dataclass(frozen=True, slots=True)
class OcrOrientation:
    orientation_degrees: int = 0
    rotate_degrees: int = 0
    orientation_confidence: float = 0.0
    script: str = "unknown"
    script_confidence: float = 0.0
    available: bool = False
    unavailable_reason: str | None = None


def _bounded_confidence(value: str | None) -> float:
    if value is None:
        return 0.0
    try:
        parsed = float(value)
    except ValueError:
        return 0.0
    if not math.isfinite(parsed):
        return 0.0
    return round(max(0.0, parsed), 3)


def _quarter_turn(value: str | None) -> int:
    if value is None:
        return 0
    try:
        parsed = int(value) % 360
    except ValueError:
        return 0
    return parsed if parsed in {0, 90, 180, 270} else 0


def parse_osd_output(output: str) -> OcrOrientation:
    """Parse Tesseract OSD text without trusting locale-dependent line order."""

    values: dict[str, str] = {}
    for line in output.splitlines():
        match = _OSD_VALUE.match(line.strip())
        if match is not None:
            values[match.group(1).strip().casefold()] = match.group(2).strip()
    script = values.get("script", "unknown").strip() or "unknown"
    available = script.casefold() != "unknown" or "rotate" in values
    return OcrOrientation(
        orientation_degrees=_quarter_turn(values.get("orientation in degrees")),
        rotate_degrees=_quarter_turn(values.get("rotate")),
        orientation_confidence=_bounded_confidence(values.get("orientation confidence")),
        script=script,
        script_confidence=_bounded_confidence(values.get("script confidence")),
        available=available,
        unavailable_reason=None if available else "osd_result_unrecognized",
    )


@dataclass(frozen=True, slots=True)
class OcrRoutingDecision:
    profile: OcrProfileName
    requested_languages: tuple[str, ...]
    primary_languages: tuple[str, ...]
    fallback_languages: tuple[str, ...] | None
    script: str
    reason: str

    @property
    def primary_language_spec(self) -> str:
        return language_spec(self.primary_languages)

    @property
    def fallback_language_spec(self) -> str | None:
        if self.fallback_languages is None:
            return None
        return language_spec(self.fallback_languages)


def _normalized_script(script: str) -> str:
    return re.sub(r"[^a-z]", "", unicodedata.normalize("NFKD", script).casefold())


def route_ocr_languages(
    plan: OcrProfilePlan,
    orientation: OcrOrientation | None,
) -> OcrRoutingDecision:
    """Select one primary and at most one fallback language combination."""

    script = "unknown" if orientation is None else orientation.script
    if plan.profile == "configured":
        return OcrRoutingDecision(
            plan.profile,
            plan.required_languages,
            plan.configured_languages,
            None,
            script,
            "configured_language_contract",
        )
    primary: tuple[str, ...]
    fallback: tuple[str, ...] | None
    reason: str
    if plan.profile == "latin":
        primary, fallback, reason = LATIN_LANGUAGES, None, "explicit_latin_profile"
    elif plan.profile == "han-simplified":
        primary, fallback, reason = (
            HAN_SIMPLIFIED_LANGUAGES,
            None,
            "explicit_han_simplified_profile",
        )
    elif plan.profile == "han-traditional":
        primary, fallback, reason = (
            HAN_TRADITIONAL_LANGUAGES,
            None,
            "explicit_han_traditional_profile",
        )
    else:
        normalized = _normalized_script(script)
        if normalized in {"latin", "fraktur"}:
            primary, fallback, reason = LATIN_LANGUAGES, None, "osd_latin"
        elif normalized in {"hant", "hantraditional", "traditionalhan"}:
            primary, fallback, reason = (
                HAN_TRADITIONAL_LANGUAGES,
                None,
                "osd_han_traditional",
            )
        elif normalized in {"hans", "hansimplified", "simplifiedhan"}:
            primary, fallback, reason = (
                HAN_SIMPLIFIED_LANGUAGES,
                None,
                "osd_han_simplified",
            )
        elif normalized in {"han", "chinese"}:
            primary, fallback, reason = (
                HAN_SIMPLIFIED_LANGUAGES,
                HAN_TRADITIONAL_LANGUAGES,
                "osd_han_bounded_variant_fallback",
            )
        else:
            primary, fallback, reason = (
                LATIN_LANGUAGES,
                HAN_SIMPLIFIED_LANGUAGES,
                "osd_unknown_bounded_script_fallback",
            )
    return OcrRoutingDecision(
        plan.profile,
        plan.required_languages,
        primary,
        fallback,
        script,
        reason,
    )


def contains_traditional_han(text: str) -> bool:
    return any(character in _TRADITIONAL_ONLY_HINTS for character in text)


def should_use_ocr_fallback(
    *,
    recognized_text: str,
    character_count: int,
    mean_confidence: float,
) -> bool:
    """Return a conservative quality decision, not a confidence probability."""

    if character_count < 4 or len(recognized_text.strip()) < 4:
        return True
    replacement_count = recognized_text.count("\ufffd")
    if replacement_count / max(1, len(recognized_text)) > 0.01:
        return True
    return mean_confidence < 45.0


@dataclass(frozen=True, slots=True)
class NativeTextQuality:
    normalized_characters: int
    alphanumeric_ratio: float
    suspicious_ratio: float
    unique_character_ratio: float
    maximum_run: int
    usable: bool
    reason: str


def _maximum_character_run(text: str) -> int:
    maximum = current = 0
    prior = None
    for character in text:
        if character == prior:
            current += 1
        else:
            prior = character
            current = 1
        maximum = max(maximum, current)
    return maximum


def _is_han_character(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x323AF
    )


def native_text_quality(text: str, *, min_characters: int) -> NativeTextQuality:
    """Detect substantial but corrupt native PDF text before skipping OCR."""

    normalized = " ".join(text.split())
    length = len(normalized)
    han_characters = sum(_is_han_character(character) for character in normalized)
    effective_minimum = (
        min(min_characters, 12)
        if han_characters >= 4 and han_characters / max(1, length) >= 0.3
        else min_characters
    )
    if length < effective_minimum:
        return NativeTextQuality(length, 0.0, 0.0, 0.0, 0, False, "too_short")
    alphanumeric = sum(character.isalnum() for character in normalized)
    suspicious = sum(
        unicodedata.category(character) in {"Cc", "Co", "Cs"} or character == "\ufffd"
        for character in normalized
    )
    suspicious += sum(normalized.count(marker) for marker in _MOJIBAKE_MARKERS)
    meaningful = tuple(character for character in normalized if not character.isspace())
    unique_ratio = len(set(meaningful)) / max(1, len(meaningful))
    alphanumeric_ratio = alphanumeric / max(1, length)
    suspicious_ratio = suspicious / max(1, length)
    maximum_run = _maximum_character_run(normalized)
    reason = "usable"
    usable = True
    if suspicious_ratio > 0.01:
        usable, reason = False, "suspicious_unicode_or_mojibake"
    elif alphanumeric_ratio < 0.35:
        usable, reason = False, "low_alphanumeric_density"
    elif length >= 40 and unique_ratio < 0.035:
        usable, reason = False, "low_character_diversity"
    elif maximum_run >= max(16, int(length * 0.25)):
        usable, reason = False, "pathological_character_run"
    return NativeTextQuality(
        length,
        round(alphanumeric_ratio, 5),
        round(suspicious_ratio, 5),
        round(unique_ratio, 5),
        maximum_run,
        usable,
        reason,
    )


__all__ = (
    "HAN_SIMPLIFIED_LANGUAGES",
    "HAN_TRADITIONAL_LANGUAGES",
    "LATIN_LANGUAGES",
    "MAX_RECOGNITION_ATTEMPTS",
    "OCR_PROFILE_CHOICES",
    "OCR_PROFILE_VERSION",
    "OSD_LANGUAGE",
    "NativeTextQuality",
    "OcrOrientation",
    "OcrProfileName",
    "OcrProfilePlan",
    "OcrRoutingDecision",
    "contains_traditional_han",
    "language_spec",
    "native_text_quality",
    "parse_language_spec",
    "parse_osd_output",
    "resolve_ocr_profile",
    "route_ocr_languages",
    "should_use_ocr_fallback",
)
