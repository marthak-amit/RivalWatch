"""Jewellery attributes parsed from product titles/descriptions: carat, metal, shape, stone, gem.

"18ct gold" is a karat (UK spelling), not 18 carats; a carat weight is a number with ct/ctw/carat that is not
followed by "gold". Plated and vermeil finishes are kept in the metal name because they price very differently.
"""
import re
from fractions import Fraction

_I = re.IGNORECASE
_KARAT_GOLD = re.compile(r"\b(\d{1,2})\s*(?:k|kt|ct|carat)\b\.?\s*(white|yellow|rose|two[- ]tone)?\s*gold\b", _I)
_COLOR_GOLD = re.compile(r"\b(white|yellow|rose)\s+gold\b", _I)
_METALS = [("platinum", "platinum"), ("palladium", "palladium"), (r"sterling\s+silver|925\s+silver", "sterling silver"),
           ("silver", "silver"), ("titanium", "titanium"), ("tungsten", "tungsten"), ("gold", "gold")]
_FINISH = re.compile(r"\s*[- ]?(plated|vermeil)\b", _I)
_CARAT = re.compile(r"(\d+\s+\d+/\d+|\d+/\d+|\d*\.\d+|\d+)\s*(?:ct\.?\s*t\.?\s*w\.?|cttw|ctw|carats?|cts?)\b", _I)
_SHAPES = ["round", "princess", "cushion", "oval", "pear", "marquise", "radiant", "asscher", "baguette"]
_NEEDS_CUT = ["emerald", "heart"]  # also gem/word meanings, so only count them as a shape with "cut"/"shape"
_STONES = [(r"lab[- ]?(?:grown|created)|laboratory[- ]grown|\blgd\b", "lab_grown"), (r"moissanite", "moissanite"),
           (r"natural\s+diamond|earth[- ]mined|\bmined\b", "natural")]
_GEMS = [(r"cubic\s+zirconia|\bcz\b", "cubic zirconia")] + [(rf"{g}s?", g) for g in (
    "sapphire", "ruby", "emerald", "pearl", "opal", "amethyst", "topaz", "tanzanite", "morganite", "aquamarine", "diamond")]


def _number(s: str) -> float:
    return float(sum(Fraction(p) for p in s.split()))


def _metal(t: str) -> tuple[str | None, re.Match | None]:
    if m := _KARAT_GOLD.search(t):
        name = " ".join(x for x in (f"{m.group(1)}k", (m.group(2) or "").lower(), "gold") if x)
    elif m := _COLOR_GOLD.search(t):
        name = f"{m.group(1).lower()} gold"
    else:
        found = ((re.search(rf"\b(?:{pat})\b", t, _I), name) for pat, name in _METALS)
        m, name = next(((x, n) for x, n in found if x), (None, None))
    if m and (f := _FINISH.match(t, m.end())):
        name = f"{name} {f.group(1).lower()}"
    return name, m


def parse(text: str) -> dict:
    t = text or ""
    metal, m = _metal(t)
    carat_text = t[:m.start()] + " " + t[m.end():] if m and _KARAT_GOLD.match(t, m.start()) else t
    attrs: dict = {"carat": None, "metal": metal, "shape": None, "stone": None, "gem": None}
    if c := _CARAT.search(carat_text):
        attrs["carat"] = round(_number(c.group(1)), 3)
    shape = next((s for s in _SHAPES if re.search(rf"\b{s}\b", t, _I)), None)
    attrs["shape"] = shape or next((s for s in _NEEDS_CUT if re.search(rf"\b{s}[- ](?:cut|shape)", t, _I)), None)
    attrs["stone"] = next((name for pat, name in _STONES if re.search(pat, t, _I)), None)
    gem = next((name for pat, name in _GEMS if re.search(rf"\b(?:{pat})\b", t, _I)), None)
    if gem == "emerald" and attrs["shape"] == "emerald" and not re.search(r"\bemeralds?\b(?![- ](?:cut|shape))", t, _I):
        gem = "diamond" if re.search(r"\bdiamonds?\b", t, _I) else None
    attrs["gem"] = gem
    if attrs["stone"] in ("lab_grown", "natural") and not attrs["gem"]:
        attrs["gem"] = "diamond"
    return attrs
