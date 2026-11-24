"""Analyste Claude pour vinted-bot.

Reçoit les alertes de vinted-bot (destination webhook), complète l'annonce
(description, photos, profil vendeur), la fait analyser par Claude et envoie
le verdict sur Telegram. On peut aussi envoyer un lien Vinted au bot Telegram
de l'analyste pour une analyse à la demande.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import json
import logging
import os
import queue
import re
import threading
import time
from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import anthropic
import httpx
from vinted_mcp.scraper import VintedClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s analyst %(levelname)s %(message)s")
log = logging.getLogger("analyst")

# --- Réglages (fichier .env) -------------------------------------------------

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("CLAUDE_EFFORT", "medium")
NOTIFY = os.environ.get("NOTIFY", "deals").lower()  # "deals" = ACHETER/OFFRE seulement, "all" = tout
MAX_PHOTOS = int(os.environ.get("MAX_PHOTOS", "5"))
VINTED_BASE_URL = os.environ.get("VINTED_BASE_URL", "https://www.vinted.fr")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
MIN_MARGIN_EUR = float(os.environ.get("MIN_MARGIN_EUR", "8"))
MIN_MARGIN_PCT = float(os.environ.get("MIN_MARGIN_PCT", "40"))

# Prix Claude en $ par million de tokens (entrée, sortie), pour estimer le coût.
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}

DATA = Path(os.environ.get("DATA_DIR", "/data"))
DATA.mkdir(parents=True, exist_ok=True)
SYSTEM_PROMPT = (Path(__file__).with_name("reference.md")).read_text(encoding="utf-8")

claude = anthropic.Anthropic()  # lit ANTHROPIC_API_KEY
vinted = VintedClient(VINTED_BASE_URL)
jobs: queue.Queue[dict[str, Any]] = queue.Queue()
seen_lock = threading.Lock()
seen: set[int] = set()

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "modele_identifie": {"type": "string", "description": "Marque et modèle réels, ex. 'Carhartt Detroit EJ001 Union Made USA'"},
        "dans_les_niches": {"type": "boolean"},
        "authenticite": {"type": "string", "enum": ["probable", "doute", "contrefacon_probable"]},
        "points_verifies": {"type": "array", "items": {"type": "string"}},
        "signaux_alerte": {"type": "array", "items": {"type": "string"}},
        "photos_manquantes": {"type": "array", "items": {"type": "string"}},
        "revente_realiste_eur": {"type": "number"},
        "port_entrant_eur": {"type": "number"},
        "calcul_revente": {"type": "string", "description": "Comment la revente a été estimée (référence, formule)"},
        "verdict": {"type": "string", "enum": ["ACHETER", "OFFRE", "PASSER"]},
        "confiance": {"type": "string", "enum": ["faible", "moyenne", "elevee"]},
        "resume": {"type": "string"},
        "message_vendeur": {"type": "string", "description": "Message à envoyer au vendeur, vide si inutile"},
    },
    "required": [
        "modele_identifie", "dans_les_niches", "authenticite", "points_verifies", "signaux_alerte",
        "photos_manquantes", "revente_realiste_eur", "port_entrant_eur", "calcul_revente",
        "verdict", "confiance", "resume", "message_vendeur",
    ],
    "additionalProperties": False,
}


# --- Telegram -----------------------------------------------------------------

def tg(method: str, **params: Any) -> dict[str, Any]:
    r = httpx.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}", json=params, timeout=70)
    return r.json()


def chat_id() -> str:
    if CHAT_ID:
        return CHAT_ID
    f = DATA / "chat_id"
    return f.read_text().strip() if f.exists() else ""


def send(text: str) -> None:
    cid = chat_id()
    if not cid:
        log.warning("Pas encore de conversation Telegram : envoie /start au bot de l'analyste.")
        return
    res = tg("sendMessage", chat_id=cid, text=text, parse_mode="HTML", disable_web_page_preview=False)
    if not res.get("ok"):
        log.error("Telegram a refusé le message : %s", res)


def telegram_loop() -> None:
    """Lit les messages envoyés au bot : /start enregistre la conversation, un lien Vinted lance une analyse."""
    offset = 0
    while True:
        try:
            res = tg("getUpdates", offset=offset, timeout=50)
            for upd in res.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                text = (msg.get("text") or "").strip()
                cid = str(msg.get("chat", {}).get("id", ""))
                if not cid:
                    continue
                if not chat_id():
                    (DATA / "chat_id").write_text(cid)
                    log.info("Conversation Telegram enregistrée.")
                if str(cid) != chat_id():
                    continue  # on ne répond qu'au propriétaire
                if text.startswith("/start"):
                    send("✅ Analyste connecté. Je t'enverrai un verdict pour chaque alerte. "
                         "Tu peux aussi me coller un lien Vinted pour une analyse à la demande.")
                elif m := re.search(r"https?://(?:www\.)?vinted\.[a-z.]+/items/(\d+)\S*", text):
                    send("🔎 Analyse en cours…")
                    jobs.put({"manual": True, "url": m.group(0), "id": int(m.group(1))})
                elif text:
                    send("Colle-moi un lien d'annonce Vinted (…/items/…) et je l'analyse.")
        except Exception as exc:  # noqa: BLE001
            log.warning("Telegram injoignable (%s), nouvel essai dans 10 s", exc)
            time.sleep(10)


# --- Webhook de vinted-bot ------------------------------------------------------

class Hook(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if WEBHOOK_SECRET and not self._genuine(body):
            self.send_response(401)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return
        if "status" in payload and not payload.get("items"):
            send(f"ℹ️ vinted-bot : {escape(str(payload['status']))}")
            return
        items = payload.get("items") or []
        if payload.get("search_id") is None and all(i.get("kind") == "listing" for i in items):
            send("✅ Webhook reçu : vinted-bot est bien relié à l'analyste (alerte de test, pas d'analyse).")
            return
        for item in items:
            if item.get("kind") in ("listing", "price_drop", "seller_upload"):
                jobs.put({"manual": False, "id": item["id"], "url": item["url"], "alert": item,
                          "search": payload.get("search", "")})

    def _genuine(self, body: bytes) -> bool:
        ts = self.headers.get("X-Vinted-Bot-Timestamp", "")
        sig = self.headers.get("X-Vinted-Bot-Signature", "")
        if not ts.isdigit() or abs(time.time() - int(ts)) > 300:
            return False
        expected = hmac.new(WEBHOOK_SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(f"sha256={expected}", sig)

    def log_message(self, *args: Any) -> None:
        pass


# --- Analyse --------------------------------------------------------------------

def total_from_price(price: float) -> float:
    return round(price * 1.05 + 0.70, 2)


def price_from_total(total: float) -> float:
    return max(0.0, (total - 0.70) / 1.05)


def download(url: str) -> tuple[str, str] | None:
    try:
        r = httpx.get(url, timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        media = r.headers.get("content-type", "image/jpeg").split(";")[0]
        if media not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
            media = "image/jpeg"
        return media, base64.standard_b64encode(r.content).decode()
    except Exception as exc:  # noqa: BLE001
        log.warning("photo non téléchargée : %s", exc)
        return None


def gather(job: dict[str, Any]) -> dict[str, Any]:
    """Assemble les infos de l'annonce : alerte de vinted-bot + page complète Vinted."""
    alert = job.get("alert") or {}
    listing: dict[str, Any] = {}
    try:
        listing = vinted.get_listing(job["url"])
        if listing.get("seller", {}).get("id"):
            try:
                listing["seller"] |= vinted.get_seller(listing["seller"]["id"])
            except Exception as exc:  # noqa: BLE001
                log.warning("profil vendeur indisponible : %s", exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("page de l'annonce indisponible (%s), analyse avec l'alerte seule", exc)
    price = float(listing.get("price") or alert.get("price") or 0)
    total = float(alert.get("total_price") or 0) or total_from_price(price)
    photos = listing.get("photos") or alert.get("photos") or ([alert["photo_url"]] if alert.get("photo_url") else [])
    return {
        "url": job["url"],
        "title": listing.get("title") or alert.get("title", ""),
        "description": listing.get("description", ""),
        "brand": listing.get("brand") or alert.get("brand", ""),
        "size": listing.get("size") or alert.get("size", ""),
        "condition": listing.get("condition") or alert.get("condition", ""),
        "colour": listing.get("colour", ""),
        "price": price,
        "total_price": total,
        "favourites": listing.get("favourites", alert.get("favourites")),
        "uploaded": listing.get("uploaded", ""),
        "reserved": listing.get("reserved", False),
        "seller": listing.get("seller") or alert.get("seller_profile") or {"login": alert.get("seller")},
        "search": job.get("search", ""),
        "photos": photos[:MAX_PHOTOS],
    }


def ask_claude(info: dict[str, Any]) -> tuple[dict[str, Any], float]:
    content: list[dict[str, Any]] = []
    for url in info["photos"]:
        if img := download(url):
            content.append({"type": "image", "source": {"type": "base64", "media_type": img[0], "data": img[1]}})
    facts = {k: v for k, v in info.items() if k != "photos"}
    content.append({"type": "text", "text": (
        f"Annonce à analyser ({len(content)} photos ci-dessus, dans l'ordre de l'annonce) :\n"
        f"{json.dumps(facts, ensure_ascii=False, indent=1)}\n\n"
        "Rends ton verdict selon tes instructions."
    )})
    resp = claude.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": VERDICT_SCHEMA}},
        messages=[{"role": "user", "content": content}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude a refusé d'analyser cette annonce")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("réponse de Claude tronquée")
    text = next(b.text for b in resp.content if b.type == "text")
    pin, pout = PRICES.get(MODEL, (4.0, 20.0))
    u = resp.usage
    cost = ((u.input_tokens + (u.cache_creation_input_tokens or 0) * 1.25 + (u.cache_read_input_tokens or 0) * 0.1) * pin
            + u.output_tokens * pout) / 1e6
    return json.loads(text), cost


def apply_rule(info: dict[str, Any], v: dict[str, Any]) -> dict[str, Any]:
    """Calcule la marge et applique la règle d'achat, quel que soit l'avis de Claude."""
    resale, port = float(v["revente_realiste_eur"]), float(v["port_entrant_eur"])
    cost = info["total_price"] + port
    margin = resale - cost
    pct = 100 * margin / cost if cost else 0
    max_cost = min(resale / (1 + MIN_MARGIN_PCT / 100), resale - MIN_MARGIN_EUR)
    max_price = price_from_total(max_cost - port)
    ok = margin >= MIN_MARGIN_EUR and pct >= MIN_MARGIN_PCT
    v.update(cout=cost, marge=margin, marge_pct=pct, prix_max_annonce=max_price, regle_ok=ok)
    if v["verdict"] == "ACHETER" and not ok:
        v["verdict"] = "OFFRE" if max_price >= 0.6 * info["price"] else "PASSER"
        v["signaux_alerte"].append("Marge sous la règle au prix actuel : verdict abaissé par le programme.")
    return v


def render(info: dict[str, Any], v: dict[str, Any], cost: float) -> str:
    icon = {"ACHETER": "✅", "OFFRE": "💬", "PASSER": "❌"}[v["verdict"]]
    head = v["verdict"]
    if v["verdict"] == "OFFRE":
        if v["prix_max_annonce"] >= info["price"]:
            # Le prix convient : Claude veut une vérification (étiquette, photo) avant l'achat.
            head = f"VÉRIFIER avant d'acheter · prix OK jusqu'à {v['prix_max_annonce']:.0f} €"
        elif v["prix_max_annonce"] > 0:
            head = f"OFFRE à {v['prix_max_annonce']:.0f} € max (demandé {info['price']:.0f} €)"
    seller = info["seller"] or {}
    lines = [
        f"{icon} <b>{head}</b> · confiance {v['confiance']}",
        f"<b>{escape(v['modele_identifie'])}</b>",
        f"{escape(info['title'])} · {escape(str(info['size']))} · {escape(str(info['condition']))}",
        f"💶 {info['price']:.2f} € ({info['total_price']:.2f} € tout compris) · vendeur "
        f"{escape(str(seller.get('username') or seller.get('login') or '?'))} "
        f"{seller.get('rating_stars') or seller.get('stars') or '?'}★ ({seller.get('reviews', '?')} avis)",
        f"📈 Revente ~{v['revente_realiste_eur']:.0f} € · coût ~{v['cout']:.0f} € → "
        f"<b>marge {v['marge']:+.0f} € ({v['marge_pct']:.0f} %)</b>",
        f"<i>{escape(v['calcul_revente'])}</i>",
        "",
        escape(v["resume"]),
    ]
    if v["points_verifies"]:
        lines.append("✔️ " + escape(" · ".join(v["points_verifies"][:5])))
    if v["signaux_alerte"]:
        lines.append("⚠️ " + escape(" · ".join(v["signaux_alerte"][:5])))
    if v["photos_manquantes"]:
        lines.append("📷 À demander : " + escape(", ".join(v["photos_manquantes"][:4])))
    if v["message_vendeur"]:
        lines.append(f"✉️ <code>{escape(v['message_vendeur'])}</code>")
    lines.append(f"\n{escape(info['url'])}")
    lines.append(f"<i>analyse {MODEL} ≈ {cost:.3f} $</i>")
    return "\n".join(lines)


def journal(info: dict[str, Any], v: dict[str, Any], cost: float) -> None:
    f = DATA / "verdicts.csv"
    new = not f.exists()
    with f.open("a", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        if new:
            w.writerow(["date", "verdict", "confiance", "modele", "prix_total", "revente", "marge", "marge_pct", "cout_api_usd", "url"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), v["verdict"], v["confiance"], v["modele_identifie"],
                    f"{info['total_price']:.2f}", f"{v['revente_realiste_eur']:.0f}", f"{v['marge']:.0f}",
                    f"{v['marge_pct']:.0f}", f"{cost:.4f}", info["url"]])


def worker() -> None:
    while True:
        job = jobs.get()
        with seen_lock:
            if not job["manual"] and job["id"] in seen:
                continue
            seen.add(job["id"])
        try:
            info = gather(job)
            if info.get("reserved") and not job["manual"]:
                log.info("annonce déjà réservée, ignorée : %s", info["url"])
                continue
            verdict, cost = ask_claude(info)
            verdict = apply_rule(info, verdict)
            journal(info, verdict, cost)
            log.info("%s · %s · marge %.0f € · %.3f $", verdict["verdict"], info["title"], verdict["marge"], cost)
            if job["manual"] or NOTIFY == "all" or verdict["verdict"] != "PASSER":
                send(render(info, verdict, cost))
        except anthropic.AuthenticationError:
            send("⛔ Clé API Claude refusée : vérifie ANTHROPIC_API_KEY dans le fichier .env.")
        except anthropic.APIStatusError as exc:
            log.error("erreur API Claude : %s", exc)
            if job["manual"]:
                send(f"⛔ Erreur Claude ({exc.status_code}). Réessaie dans un moment.")
        except Exception as exc:  # noqa: BLE001
            log.exception("analyse impossible")
            if job["manual"]:
                send(f"⛔ Analyse impossible : {escape(str(exc))}")


def main() -> None:
    threading.Thread(target=telegram_loop, daemon=True).start()
    threading.Thread(target=worker, daemon=True).start()
    log.info("Analyste prêt (modèle %s, effort %s, notifications : %s).", MODEL, EFFORT, NOTIFY)
    ThreadingHTTPServer(("0.0.0.0", 8080), Hook).serve_forever()


if __name__ == "__main__":
    main()
