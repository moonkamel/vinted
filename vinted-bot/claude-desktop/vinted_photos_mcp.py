"""Serveur MCP local pour Claude Desktop : donne à Claude une annonce Vinted complète, photos comprises.

Outil : voir_annonce(lien) -> description, prix, vendeur + les photos en images.
Tu colles un lien Vinted dans Claude Desktop, Claude appelle l'outil et voit les photos :
l'analyse passe par ton abonnement Claude, sans clé API.

Lancé par Claude Desktop via uv (voir claude_desktop_config.json) ; rien ne tourne en permanence.
"""

from __future__ import annotations

import io
import json
import os

import httpx
from mcp.server.fastmcp import FastMCP, Image
from PIL import Image as PILImage
from vinted_mcp.scraper import VintedClient

MAX_PHOTOS = int(os.environ.get("MAX_PHOTOS", "8"))
MAX_SIDE = int(os.environ.get("MAX_SIDE", "1200"))  # px : assez pour lire une étiquette, sans alourdir

mcp = FastMCP("vinted-photos")
vinted = VintedClient(os.environ.get("VINTED_BASE_URL", "https://www.vinted.fr"))


def _photo(url: str) -> Image | None:
    try:
        r = httpx.get(url, timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        img = PILImage.open(io.BytesIO(r.content)).convert("RGB")
        img.thumbnail((MAX_SIDE, MAX_SIDE))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=85)
        return Image(data=out.getvalue(), format="jpeg")
    except Exception:  # noqa: BLE001 — une photo illisible ne doit pas bloquer les autres
        return None


@mcp.tool()
def voir_annonce(lien: str) -> list:
    """Ouvre une annonce Vinted et renvoie ses informations (titre, description, prix, état, taille,
    vendeur avec note et nombre d'avis) puis TOUTES ses photos en images, dans l'ordre de l'annonce.
    Utilise-le dès qu'on te donne un lien vinted.fr/items/… à analyser : examine chaque photo
    (étiquettes, état, défauts) avant de conclure.

    Args:
        lien: le lien de l'annonce (https://www.vinted.fr/items/…) ou son numéro.
    """
    item = vinted.get_listing(lien)
    seller = item.get("seller") or {}
    if seller.get("id"):
        try:
            seller |= vinted.get_seller(seller["id"])
        except Exception:  # noqa: BLE001
            pass
    price = item.get("price") or 0
    info = {
        "titre": item.get("title"),
        "prix": price,
        "prix_total_protection_comprise": round(price * 1.05 + 0.70, 2) if price else None,
        "description": item.get("description"),
        "marque": item.get("brand"),
        "taille": item.get("size"),
        "etat": item.get("condition"),
        "couleur": item.get("colour"),
        "publiee": item.get("uploaded"),
        "favoris": item.get("favourites"),
        "reservee": item.get("reserved"),
        "vendeur": {k: seller.get(k) for k in ("username", "rating_stars", "reviews", "positive_reviews",
                                                "negative_reviews", "location", "items_sold", "business")},
        "lien": item.get("url") or lien,
    }
    photos = item.get("photos") or []
    images = [img for img in (_photo(u) for u in photos[:MAX_PHOTOS]) if img]
    info["photos"] = f"{len(images)} photo(s) jointe(s) sur {len(photos)}"
    return [json.dumps(info, ensure_ascii=False, indent=1), *images]


if __name__ == "__main__":
    mcp.run()
