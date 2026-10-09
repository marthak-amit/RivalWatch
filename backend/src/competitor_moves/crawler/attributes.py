"""Jewellery attributes parsed from product titles/descriptions: carat, metal, shape, stone, gem."""
import re
from fractions import Fraction

_CARAT = re.compile(r"(\d+\s+\d+/\d+|\d+/\d+|\d*\.\d+|\d+)\s*(?:ct\.?\s*t\.?\s*w\.?|cttw|ctw|carats?|cts?)\b", re.IGNORECASE)
_KARAT_GOLD = re.compile(r"\b(\d{1,2})\s*k(?:t|arat)?\s*(white|yellow|rose|two[- ]tone)?\s*gold\b", re.IGNORECASE)
_COLOR_GOLD = re.compile(r"\b(white|yellow|rose)\s+gold\b", re.IGNORECASE)
_METALS = [("platinum", "platinum"), ("palladium", "palladium"), (r"sterling\s+silver|925\s+silver", "sterling silver"),
           ("silver", "silver"), ("titanium", "titanium"), ("tungsten", "tungsten"), ("gold", "gold")]
_SHAPES = ["round", "princess", "cushion", "oval", "pear", "marquise", "radiant", "asscher", "baguette"]
_NEEDS_CUT = ["emerald", "heart"]  # also gem/word meanings, so only count them as a shape with "cut"/"shape"
_STONES = [(r"lab[- ]?(?:grown|created)|laboratory[- ]grown|\blgd\b", "lab_grown"), (r"moissanite", "moissanite"),
           (r"natural\s+diamond|earth[- ]mined|\bmined\b", "natural")]
_GEMS = ["sapphire", "ruby", "emerald", "pearl", "opal", "amethyst", "topaz", "tanzanite", "morganite", "aquamarine",
         "diamond"]


def _number(s: str) -> float:
    return float(sum(Fraction(p) for p in s.split()))


def parse(text: str) -> dict:
    t = text or ""
    attrs: dict = {"carat": None, "metal": None, "shape": None, "stone": None, "gem": None}
    if m := _CARAT.search(t):
        attrs["carat"] = round(_number(m.group(1)), 3)
    if m := _KARAT_GOLD.search(t):
        attrs["metal"] = " ".join(x for x in (f"{m.group(1)}k", (m.group(2) or "").lower(), "gold") if x)
    elif m := _COLOR_GOLD.search(t):
        attrs["metal"] = f"{m.group(1).lower()} gold"
    else:
        attrs["metal"] = next((name for pat, name in _METALS if re.search(rf"\b(?:{pat})\b", t, re.IGNORECASE)), None)
    shape = next((s for s in _SHAPES if re.search(rf"\b{s}\b", t, re.IGNORECASE)), None)
    shape = shape or next((s for s in _NEEDS_CUT if re.search(rf"\b{s}[- ](?:cut|shape)", t, re.IGNORECASE)), None)
    attrs["shape"] = shape
    attrs["stone"] = next((name for pat, name in _STONES if re.search(pat, t, re.IGNORECASE)), None)
    gem = next((g for g in _GEMS if re.search(rf"\b{g}s?\b", t, re.IGNORECASE)), None)
    if gem == "emerald" and attrs["shape"] == "emerald" and not re.search(r"\bemeralds?\b(?![- ](?:cut|shape))", t, re.IGNORECASE):
        gem = "diamond" if re.search(r"\bdiamonds?\b", t, re.IGNORECASE) else None
    attrs["gem"] = gem
    if attrs["stone"] in ("lab_grown", "natural") and not attrs["gem"]:
        attrs["gem"] = "diamond"
    return attrs
