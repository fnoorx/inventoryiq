"""Classify catalogue product names into category and audience for grouping."""

import re

CATEGORY_ORDER = ("apparel", "footwear", "other")
CATEGORY_LABELS = {"apparel": "Apparel", "footwear": "Footwear", "other": "Other"}
GENDER_ORDER = ("mens", "womens", "kids")
GENDER_LABELS = {"mens": "Men's / Unisex", "womens": "Women's", "kids": "Kids"}

FOOTWEAR_RE = re.compile(
    r"\b(shoes?|cleats?|slides?|spikes?|sandals?|boots?|slippers?|sneakers?|trainers?|"
    r"skate shoes?|football boots?)\b",
    re.IGNORECASE,
)
APPAREL_RE = re.compile(
    r"\b(bottoms?|bralettes?|bras?|corsets?|crew|dresses?|fleece|hoodies?|jackets?|"
    r"jerseys?|joggers?|jumpsuits?|leggings?|mid layer|pants?|parkas?|polos?|"
    r"puffers?|shirts?|shorts?|singlets?|skirts?|socks?|sweaters?|sweatpants?|"
    r"sweatshirts?|tanks?|tees?|thermal|tights?|tght|tops?|tracksuits?|vests?)\b",
    re.IGNORECASE,
)
KIDS_RE = re.compile(
    r"\b(bab(?:y|ies)|boys?|girls?|infants?|kids?|preschool|toddlers?|youth)\b",
    re.IGNORECASE,
)
WOMENS_RE = re.compile(r"\b(woman|women|womens|wmns)\b|^W(?:\s|MNS\b)", re.IGNORECASE)


def classify_product_category(product_name) -> str:
    name = str(product_name or "").strip()
    if FOOTWEAR_RE.search(name):
        return "footwear"
    if APPAREL_RE.search(name):
        return "apparel"
    return "other"


def classify_product_gender(product_name) -> str:
    name = str(product_name or "").replace("’", "'").strip()
    if KIDS_RE.search(name):
        return "kids"
    if WOMENS_RE.search(name):
        return "womens"
    # Explicitly unisex and unspecified products share the men's section.
    return "mens"
