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
import unicodedata
import time
from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import anthropic
import httpx
from vinted_mcp.scraper import VintedClient

import ocr

logging.basicConfig(level=logging.INFO, format="%(asctime)s analyst %(levelname)s %(message)s")
log = logging.getLogger("analyst")
# httpx écrit chaque URL appelée, y compris celles de Telegram qui contiennent le jeton du bot : on le fait taire.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# --- Réglages (fichier .env) -------------------------------------------------

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
# "ia" = analyse par Claude (photos comprises, payante) ; "regles" = règles fixes, sans IA, gratuite
MODE = os.environ.get("MODE", "ia").lower()
MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
EFFORT = os.environ.get("CLAUDE_EFFORT", "medium")
# "buy" = seulement les achats immédiats sûrs ; "deals" = ACHETER et OFFRE ; "all" = tout
NOTIFY = os.environ.get("NOTIFY", "buy").lower()
MIN_CONFIDENCE = os.environ.get("MIN_CONFIDENCE", "elevee").lower()  # pour NOTIFY=buy : moyenne ou elevee
CONFIDENCE_RANK = {"faible": 0, "moyenne": 1, "elevee": 2}
MAX_PHOTOS = int(os.environ.get("MAX_PHOTOS", "5"))
# Lecture des étiquettes sur les photos (OCR local et gratuit), utilisée en mode règles
OCR_ON = os.environ.get("OCR", "on").lower() in ("on", "1", "true", "oui")
VINTED_BASE_URL = os.environ.get("VINTED_BASE_URL", "https://www.vinted.fr")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
MIN_MARGIN_EUR = float(os.environ.get("MIN_MARGIN_EUR", "8"))
MIN_MARGIN_PCT = float(os.environ.get("MIN_MARGIN_PCT", "40"))

# Prix Claude en $ par million de tokens (entrée, sortie), pour estimer le coût.
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}

DATA = Path(os.environ.get("DATA_DIR", "/data"))
DATA.mkdir(parents=True, exist_ok=True)
SYSTEM_PROMPT = (Path(__file__).with_name("reference.md")).read_text(encoding="utf-8")

# Plusieurs jeux de règles possibles (ex. appareils photo + mode) : "rules.json,rules-mode.json".
# Un jeu qui a un "prefixe_recherche" (ex. "👗") s'applique aux recherches dont le nom commence par ce préfixe ;
# le premier jeu sans préfixe s'applique aux autres. Pour un lien collé à la main, on essaie chaque jeu.
RULES_FILES = [f.strip() for f in os.environ.get("RULES_FILES", os.environ.get("RULES_FILE", "rules.json,rules-mode.json")).split(",")
               if f.strip() and (Path(__file__).with_name(f.strip())).exists()]
RULESETS = [json.loads((Path(__file__).with_name(f)).read_text(encoding="utf-8")) for f in RULES_FILES]
RULES = RULESETS[0]
claude = anthropic.Anthropic() if MODE == "ia" else None  # lit ANTHROPIC_API_KEY
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
                          "search": payload.get("search", ""), "search_id": payload.get("search_id")})

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


def fetch(url: str) -> tuple[str, bytes] | None:
    try:
        r = httpx.get(url, timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        media = r.headers.get("content-type", "image/jpeg").split(";")[0]
        if media not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
            media = "image/jpeg"
        return media, r.content
    except Exception as exc:  # noqa: BLE001
        log.warning("photo non téléchargée : %s", exc)
        return None


def download(url: str) -> tuple[str, str] | None:
    if got := fetch(url):
        return got[0], base64.standard_b64encode(got[1]).decode()
    return None


def read_labels(photos: list[str]) -> dict[str, Any]:
    """Lit le texte des étiquettes sur chaque photo et fusionne les indices trouvés."""
    merged: dict[str, Any] = {"brands": [], "competitors": [], "photos_lues": 0, "texte": ""}
    for url in photos:
        got = fetch(url)
        if not got:
            continue
        try:
            raw = ocr.read_image(got[1])
            f = ocr.facts(raw)
        except Exception as exc:  # noqa: BLE001
            log.warning("OCR impossible sur une photo : %s", exc)
            continue
        merged["photos_lues"] += 1
        flat = re.sub(r"\s+", " ", ocr.norm(raw))
        # Lettres espacées (logo "s a n d r o") : on ajoute une version recollée pour retrouver la marque.
        glued = re.sub(r"\b(?:\w ){2,}\w\b", lambda m: m.group(0).replace(" ", ""), flat)
        merged["texte"] = (merged["texte"] + " " + flat + (" " + glued if glued != flat else ""))[:6000]
        for key in ("brands", "competitors"):
            merged[key] += [x for x in f.get(key, []) if x not in merged[key]]
        for key in ("made_in", "rn", "ref", "date"):
            if f.get(key) and not merged.get(key):
                merged[key] = f[key]
        for key in ("union_made", "wip"):
            merged[key] = merged.get(key) or f.get(key, False)
    return merged


def refined_title(name: str, info: dict[str, Any], f: dict[str, Any]) -> str:
    """Titre construit à partir de ce qui est lu sur les étiquettes : base pour ta future annonce."""
    parts = [re.sub(r"\s*\(.*?\)", "", name).strip()]
    if f.get("wip") and "WIP" not in parts[0]:
        parts[0] = parts[0].replace("Carhartt", "Carhartt WIP")
    if f.get("union_made"):
        parts.append("Union Made")
    if f.get("made_in") and f"Made in {f['made_in']}" not in parts[0]:
        parts.append(f"Made in {f['made_in']}")
    if f.get("date"):
        parts.append(f["date"])
    if f.get("ref"):
        parts.append(f"réf. {f['ref']}")
    if info.get("size"):
        parts.append(f"taille {info['size']}")
    return " · ".join(parts)


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
        "search_id": job.get("search_id"),
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


# --- Mode règles (sans IA) ---------------------------------------------------

def norm(text: str) -> str:
    """Minuscules, sans accents, pour comparer des mots."""
    return unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()


def has(text: str, group: str) -> bool:
    """Un des mots du groupe "a|b" est présent (début de mot : "levi" trouve "levis").
    Mot entier seulement pour les mots courts (usa, wip, lee) et ceux qui finissent par "!" ("vest!")."""
    for alt in (a.strip() for a in group.split("|")):
        whole = alt.endswith("!") or len(alt) <= 3
        alt = alt.rstrip("!")
        if alt and re.search(rf"\b{re.escape(alt)}" + (r"\b" if whole else ""), text):
            return True
    return False


def mentioned_defects(desc: str, words: list[str] | None = None) -> list[str]:
    """Défauts cités dans la description, en ignorant "pas de tache", "sans trou", "ni trou"…"""
    found = []
    for word in (RULES["mots_defauts"] if words is None else words):
        whole = word.endswith("!")
        pattern = (r"\b" + re.escape(word.rstrip("!")) + r"\b") if whole else re.escape(word)
        for m in re.finditer(pattern, desc):
            before = desc[max(0, m.start() - 25):m.start()]
            if not re.search(r"\b(pas|sans|aucun|aucune|ni|zero|0)\b", before):
                found.append(word.rstrip("!").strip())
                break
    return found


def rules_verdict(info: dict[str, Any]) -> dict[str, Any]:
    title, desc, brand = norm(info["title"]), norm(info["description"]), norm(info["brand"])
    labels: dict[str, Any] = info.get("labels") or {}
    label_text = " ".join(labels.get("brands", []) + (["made in usa"] if labels.get("made_in") == "USA" else [])
                          + (["union made"] if labels.get("union_made") else []) + (["wip"] if labels.get("wip") else []))
    label_text += " " + labels.get("texte", "")
    text = f"{title} {desc} {label_text}"
    proof = RULES.get("libelle_preuve", "étiquette")
    # Accessoire vendu seul (housse, notice, bouchon…) : le titre en parle sans parler de l'objet lui-même
    acc = RULES.get("accessoires_titre", [])
    head = " ".join(title.split()[:3])
    main_words = RULES.get("objet_principal", []) + [g for mm in RULES["modeles"] for g in mm["tous"][1:2]]
    # Accessoires "forts" (notice, manual, guide, affiche…) : accessoire seul même si le titre cite le modèle,
    # sauf si le titre parle aussi de l'appareil, ou si l'objet est vendu AVEC ("avec notice", "+ manual").
    strong = RULES.get("accessoires_forts", [])
    bundled = lambda w: re.search(r"(avec|\+|with|inclus|incl\.?|et|and|plus|con|mit)\s+(sa |son |ses |la |le |les |l')?"
                                  + re.escape(w.rstrip("!")), title)
    strong_hit = [w for w in strong if has(title, w) and not bundled(w)]
    is_strong = bool(strong_hit) and (any(has(head, w) for w in strong_hit)
                                      or not any(has(title, o) for o in RULES.get("objet_principal", [])))
    if is_strong or (acc and (any(has(head, a) for a in acc)
                     or (any(has(title, a) for a in acc) and not any(has(title, o) for o in main_words)))):
        return {
            "modele_identifie": "accessoire seul", "dans_les_niches": False, "authenticite": "doute",
            "points_verifies": [], "signaux_alerte": ["le titre décrit un accessoire, pas l'objet lui-même"],
            "photos_manquantes": [], "revente_realiste_eur": 0, "port_entrant_eur": 0,
            "calcul_revente": "aucune référence", "verdict": "PASSER", "confiance": "faible",
            "resume": "Accessoire vendu seul.", "message_vendeur": "", "titre_affine": "",
        }
    # La marque du modèle (1er groupe) doit être dans le titre ou le champ marque, pas seulement
    # dans la description : sinon une liste de hashtags "Carhartt Patagonia TNF…" suffirait.
    def find(brand_zone: str, zone: str) -> dict[str, Any] | None:
        return next((m for m in RULES["modeles"]
                     if has(brand_zone, m["tous"][0]) and all(has(zone, g) for g in m["tous"][1:])
                     and not any(has(zone, w) for w in m["aucun"])), None)
    # Le titre et la description priment ; le texte lu sur les photos ne sert qu'à compléter un titre vague.
    model = find(f"{title} {brand}", f"{title} {desc}") or find(f"{title} {brand} {label_text}", text)
    flags: list[str] = []
    seller = info["seller"] or {}
    reviews = seller.get("reviews") or 0
    if seller.get("business") or seller.get("business_seller") or reviews >= RULES["vendeur_pro_avis"]:
        flags.append(f"vendeur professionnel ou en volume ({reviews} avis) : prix déjà au marché")
    for w in RULES["mots_suspects_titre"]:
        if w in f"{title} ":
            flags.append(f"titre suspect : « {w.strip()} »")
    for w in RULES["mots_suspects"]:
        if w in text:
            flags.append(f"mot suspect : « {w} »")
    for c in RULES["concurrents"]:
        if re.search(rf"\b{re.escape(c)}\b", f"{brand} {title}"):
            flags.append(f"autre marque : {c}")
    if model and brand and model["tous"][0].split("|")[0] not in brand:
        flags.append(f"marque renseignée : {info['brand']}")
    model_brand = model["tous"][0].split("|")[0] if model else ""
    if panne := mentioned_defects(f"{title} {desc}", RULES.get("mots_panne", [])):
        flags.append("fonctionnement douteux : " + ", ".join(panne))
    if untested := mentioned_defects(f"{title} {desc}", RULES.get("mots_non_teste", [])):
        flags.append("non testé : " + ", ".join(untested) + " (risque de panne)")
    for c in (labels.get("competitors", []) if RULES.get("verifier_concurrents_ocr", True) else []):
        if c != model_brand:
            flags.append(f"étiquette d'une autre marque lue sur les photos : {c}")
    if model and "USA" in model["nom"] and labels.get("made_in") and labels["made_in"] != "USA":
        flags.append(f"étiquette : Made in {labels['made_in']}, pas USA")
    cited = [b for b in RULES["marques_courantes"] if has(desc, b)]
    if len(cited) >= 4:
        flags.append(f"description bourrée de marques ({', '.join(cited[:5])}…) : annonce peu fiable")
    if defects := mentioned_defects(desc):
        flags.append("défaut mentionné : " + ", ".join(defects))
    cond = norm(info["condition"])
    if model and model.get("neuf_suspect") and cond.startswith("neuf"):
        flags.append("« neuf » à petit prix sur une marque copiée : contrefaçon possible, vérifie l'étiquette")
    if model and "USA" in model["nom"] and cond.startswith("neuf"):
        flags.append("« neuf » sur un modèle vintage : à vérifier")
    if not model:
        return {
            "modele_identifie": "modèle non reconnu par les règles", "dans_les_niches": False,
            "authenticite": "doute", "points_verifies": [], "signaux_alerte": flags, "photos_manquantes": [],
            "revente_realiste_eur": 0, "port_entrant_eur": 0, "calcul_revente": "aucune référence",
            "verdict": "PASSER", "confiance": "faible",
            "resume": "Aucun modèle de rules.json ne correspond au titre, à la description ni aux étiquettes.",
            "message_vendeur": "", "titre_affine": refined_title(info["title"], info, labels) if labels.get("photos_lues") else "",
        }
    resale = model.get("revente") or ((model["p25"] - 0.70) / 1.05) * 0.83
    how = (f"vente connue {model['revente']} €" if model.get("revente")
           else f"P25 {model['p25']} € → ((P25 − 0,70) ÷ 1,05) × 0,83")
    factor = next((f for k, f in RULES["etat"].items() if cond.startswith(k)), 1.0)
    if factor < 1:
        resale *= factor
        how += f" × {factor} (état)"
    if any(re.search(rf"\b{re.escape(s)}\b", norm(info["size"])) for s in RULES["tailles_peu_demandees"]):
        resale *= RULES["decote_taille"]
        how += f" × {RULES['decote_taille']} (taille peu demandée)"
    suspicious = any(f.startswith(("titre suspect", "mot suspect", "autre marque", "marque renseignée", "étiquette"))
                     for f in flags)
    ocr_text = labels.get("texte", "")
    label_ok = model_brand in labels.get("brands", []) or (bool(ocr_text) and has(ocr_text, model["tous"][0]))
    model_read = bool(ocr_text) and len(model["tous"]) > 1 and has(ocr_text, model["tous"][1])
    seen = [x for x in (labels.get("rn"), labels.get("made_in") and f"Made in {labels['made_in']}",
                        labels.get("union_made") and "Union Made", labels.get("date"),
                        labels.get("ref") and f"réf. {labels['ref']}") if x]
    checked = [f"modèle reconnu : {model['nom']}", f"état : {info['condition']}"]
    if label_ok:
        checked.append(f"{proof} lu(e) sur les photos : " + (", ".join(seen) if seen else model_brand)
                       + (" + nom du modèle" if model_read else ""))
    return {
        "modele_identifie": model["nom"], "dans_les_niches": True,
        "authenticite": "doute" if suspicious else "probable",
        "points_verifies": checked,
        "signaux_alerte": flags,
        "photos_manquantes": [] if label_ok else [f"{proof} non lu(e) sur les photos : vérifie toi-même"],
        "revente_realiste_eur": round(resale, 2), "port_entrant_eur": model["port"], "calcul_revente": how,
        "verdict": ("PASSER" if any(f.startswith("fonctionnement douteux") for f in flags)
                    else "ACHETER" if not flags else "OFFRE"),
        # élevée seulement si l'étiquette de la marque a été lue sur une photo
        "confiance": "faible" if flags else ("elevee" if label_ok else "moyenne"),
        "resume": (f"{proof.capitalize()} lu(e) sur les photos et aucun signal d'alerte." if label_ok else
                   f"Aucun signal d'alerte dans le texte, mais {proof} n'a pas pu être lu(e).") if not flags
                  else "Signaux d'alerte à vérifier avant tout achat.",
        "message_vendeur": "",
        "titre_affine": refined_title(model["nom"], info, labels),
        "seuil_eur": model.get("marge_min_eur", RULES.get("marge_min_eur")),
        "seuil_pct": model.get("marge_min_pct", RULES.get("marge_min_pct")),
    }


def rules_verdict_auto(info: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    """Choisit le jeu de règles : par préfixe du nom de recherche, sinon en essayant chacun (lien manuel)."""
    global RULES
    search = job.get("search") or ""
    candidates = [r for r in RULESETS if r.get("prefixe_recherche") and search.startswith(r["prefixe_recherche"])]
    if not candidates and not job.get("manual"):
        candidates = [next((r for r in RULESETS if not r.get("prefixe_recherche")), RULESETS[0])]
    first = None
    for rs in candidates or RULESETS:
        RULES = rs
        v = rules_verdict(info)
        first = first or v
        if v["revente_realiste_eur"] > 0 or v["modele_identifie"] == "accessoire seul":
            return v
    return first


def apply_rule(info: dict[str, Any], v: dict[str, Any]) -> dict[str, Any]:
    """Calcule la marge et applique la règle d'achat, quel que soit l'avis de Claude."""
    resale, port = float(v["revente_realiste_eur"]), float(v["port_entrant_eur"])
    cost = info["total_price"] + port
    margin = resale - cost
    pct = 100 * margin / cost if cost else 0
    min_eur = float(v.get("seuil_eur") or MIN_MARGIN_EUR)
    min_pct = float(v.get("seuil_pct") or MIN_MARGIN_PCT)
    max_cost = min(resale / (1 + min_pct / 100), resale - min_eur)
    max_price = price_from_total(max_cost - port)
    ok = margin >= min_eur and pct >= min_pct
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
        *([f"🏷️ Titre affiné : <code>{escape(v['titre_affine'])}</code>"] if v.get("titre_affine") else []),
        f"{escape(info['title'])} · {escape(str(info['size']))} · {escape(str(info['condition']))}",
        f"💶 {info['price']:.2f} € ({info['total_price']:.2f} € tout compris) · vendeur "
        f"{escape(str(seller.get('username') or seller.get('login') or '?'))} "
        f"{seller.get('rating_stars') or seller.get('stars') or '?'}★ ({seller.get('reviews', '?')} avis)",
        (f"📈 Revente ~{v['revente_realiste_eur']:.0f} € · coût ~{v['cout']:.0f} € → "
         f"<b>marge {v['marge']:+.0f} € ({v['marge_pct']:.0f} %)</b>\n<i>{escape(v['calcul_revente'])}</i>")
        if v["revente_realiste_eur"] > 0 else "📈 Pas de référence pour ce modèle : marge non calculée",
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
    if info.get("search_id"):
        lines.append(f"🛑 Acheté ? Pour arrêter la recherche « {escape(info['search'])} », envoie au bot Vinted : "
                     f"<code>/remove {info['search_id']}</code> (ou <code>/pause {info['search_id']}</code>)")
    lines.append("<i>analyse par règles (gratuite)</i>" if MODE == "regles" else f"<i>analyse {MODEL} ≈ {cost:.3f} $</i>")
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


def should_notify(v: dict[str, Any]) -> bool:
    if NOTIFY == "all":
        return True
    if NOTIFY == "deals":
        return v["verdict"] != "PASSER"
    # "buy" : à acheter tout de suite, sans aucun doute
    return (v["verdict"] == "ACHETER" and v["regle_ok"] and v["authenticite"] == "probable"
            and CONFIDENCE_RANK.get(v["confiance"], 0) >= CONFIDENCE_RANK.get(MIN_CONFIDENCE, 2))


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
            if MODE == "regles":
                if OCR_ON:
                    info["labels"] = read_labels(info["photos"])
                verdict, cost = rules_verdict_auto(info, job), 0.0
            else:
                verdict, cost = ask_claude(info)
            verdict = apply_rule(info, verdict)
            journal(info, verdict, cost)
            log.info("%s · %s · marge %.0f € · %.3f $", verdict["verdict"], info["title"], verdict["marge"], cost)
            if job["manual"] or should_notify(verdict):
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
    log.info("Analyste prêt (mode %s, modèle %s, notifications : %s).", MODE, MODEL if MODE == "ia" else "-", NOTIFY)
    ThreadingHTTPServer(("0.0.0.0", 8080), Hook).serve_forever()


if __name__ == "__main__":
    main()
