"""Lecture des étiquettes sur les photos (OCR Tesseract, gratuit, local) et extraction des indices utiles."""

from __future__ import annotations

import io
import re
import unicodedata
from typing import Any

import pytesseract
from PIL import Image, ImageOps

SEASONS = {"FA": "automne", "HO": "hiver", "SP": "printemps", "SU": "été", "F": "automne", "S": "printemps"}
COUNTRIES = ["usa", "mexico", "vietnam", "china", "chine", "england", "france", "bangladesh", "india", "inde",
             "cambodia", "indonesia", "sri lanka", "turkey", "turquie", "romania", "roumanie", "portugal",
             "pakistan", "honduras", "el salvador", "thailand", "philippines", "korea", "taiwan", "italy", "myanmar"]
COMPETITORS = ["champion", "berne", "dickies", "wrangler", "columbia", "the north face", "nike", "adidas",
               "ralph lauren", "tommy hilfiger", "decathlon", "quechua", "uniqlo", "h&m", "zara"]


def _words(text: str) -> int:
    return len(re.findall(r"[A-Za-z]{3,}", text))


def read_image(data: bytes) -> str:
    """Texte lu sur une photo. Essaie plusieurs rotations quand l'étiquette est de travers."""
    img = Image.open(io.BytesIO(data)).convert("L")
    if img.width < 1600:
        f = 1600 / img.width
        img = img.resize((int(img.width * f), int(img.height * f)))
    img = ImageOps.autocontrast(img)
    best = ""
    for angle in (0, 90, 270, 30, -30):
        im = img.rotate(angle, expand=True, fillcolor=255) if angle else img
        try:
            txt = pytesseract.image_to_string(im, lang="eng+fra", config="--psm 11", timeout=20)
        except RuntimeError:  # délai dépassé
            continue
        if _words(txt) > _words(best):
            best = txt
        if _words(best) >= 8:
            break
    return best


def norm(text: str) -> str:
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()


def facts(raw: str) -> dict[str, Any]:
    """Indices fiables tirés du texte lu (on ne garde que ce qui se reconnaît sans ambiguïté)."""
    t = re.sub(r"\s+", " ", norm(raw))
    f: dict[str, Any] = {"brands": [], "competitors": []}
    if m := re.search(r"made in (?:the )?(" + "|".join(COUNTRIES) + r")\b|fabrique (?:aux|au|en) (e\.?\s?u|usa)", t):
        country = (m.group(1) or "usa").replace("chine", "china").replace("e.u", "usa").replace("e u", "usa")
        f["made_in"] = "USA" if country in ("usa", "e.u", "eu") else country.title()
    # "RN# 14806" est souvent lu "RNS 14806" ou "RN 14806" ; on accepte aussi 14806 près de Carhartt/Dearborn
    if re.search(r"\brn\S{0,2}\s*:?\s*14806\b|\b14806\b.{0,120}(carhartt|dearborn)", t):
        f["brands"].append("carhartt")
        f["rn"] = "RN 14806 (Carhartt US)"
        if d := re.search(r"14806\D{0,6}(0[1-9]|1[0-2])(\d{2})\b", t):
            f["date"] = f"fabriquée {d.group(1)}/20{d.group(2)}"
    if re.search(r"\brn\S{0,2}\s*:?\s*51884\b|\b51884\b.{0,120}(patagonia|ventura)", t):
        f["brands"].append("patagonia")
        f["rn"] = "RN 51884 (Patagonia)"
    if s := re.search(r"sty\s*(\d{5})\s*([a-z]{1,2})(\d{2})\b", t):
        f["ref"] = s.group(1)
        f["date"] = f"saison {s.group(2).upper()}{s.group(3)} ({SEASONS.get(s.group(2).upper(), '?')} 20{s.group(3)})"
    if c := re.search(r"\b(ej\d{3}|j\d{2,3}|c\d{3}|b\d{2,3}|r\d{2,3})\s+(blk|brn|crh|nvy|mos|gvl|dkb|crb|gry|olv)\b", t):
        f["ref"] = f"{c.group(1).upper()} {c.group(2).upper()}"
    if re.search(r"ufcw|union labor|union made", t):
        f["union_made"] = True
    if re.search(r"\bwip\b|work in progress", t):
        f["wip"] = True
    for brand in ("carhartt", "patagonia", "levi", "barbour", "laboureur", "polartec"):
        if brand in t and brand not in f["brands"]:
            f["brands"].append(brand)
    if "strauss" in t and "levi" not in f["brands"]:
        f["brands"].append("levi")
    f["competitors"] = [c for c in COMPETITORS if re.search(rf"\b{re.escape(c)}\b", t)]
    return f
