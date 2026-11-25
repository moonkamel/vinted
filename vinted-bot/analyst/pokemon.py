"""Cartes Pokémon : retrouve la carte d'une annonce Vinted et son prix Cardmarket grâce à l'API TCGdex.

- Index local de toutes les extensions et cartes (noms français et anglais), reconstruit chaque semaine.
- Identification par numéro ("199/165", "TG13/TG30", "SVP 085", "SWSH050"), confirmé par le nom de la carte,
  ou à défaut par nom de carte + nom d'extension.
- Prix Cardmarket (tendance et moyenne 30 jours) mis en cache 12 h.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

import httpx

API = "https://api.tcgdex.net/v2"
INDEX_MAX_AGE = 7 * 86400
PRICE_MAX_AGE = 12 * 3600
log = logging.getLogger("pokemon")

# Préfixe écrit dans les titres -> extension de promos TCGdex
PROMO_SETS = {"svp": "svp", "mep": "mep", "swsh": "swshp", "sm": "smp", "xy": "xyp", "bw": "bwp",
              "hgss": "hgssp", "dp": "dpp"}
# Noms d'extension trop vagues pour identifier une carte à eux seuls
VAGUE_SET_NAMES = {"promo", "promos", "base", "set de base", "base set", "black star promos", "mcdonald s collection"}

# Préfixe collé au numéro seulement (TG13/TG30, RC5/RC32) : "ex 199/165" ne doit pas donner "ex199".
NUMBER = re.compile(r"(?<![a-z0-9/])([a-z]{0,2})0*(\d{1,3})\s?/\s?([a-z]{0,2})0*(\d{2,3})(?![a-z0-9/])")
PROMO = re.compile(r"(?<![a-z0-9])(svp|mep|swsh|hgss|sm|xy|bw|dp)\s?(?:promo\s?)?0*(\d{1,3})(?![a-z0-9/])")


def norm(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9/&]+", " ", t)).strip()


def _key(local_id: str) -> tuple[str, int] | None:
    m = re.fullmatch(r"([A-Za-z]*)0*(\d+)[a-z]?", local_id or "")
    return (m.group(1).lower(), int(m.group(2))) if m else None


def _phrase(text: str, phrase: str) -> bool:
    return bool(phrase) and f" {phrase} " in f" {text} "


class TCGdex:
    def __init__(self, data_dir: Path) -> None:
        self.index_file = data_dir / "tcgdex_index.json"
        self.price_file = data_dir / "tcgdex_prix.json"
        self.ready = threading.Event()
        self.lock = threading.Lock()
        self.sets: dict[str, dict[str, Any]] = {}
        self.by_number: dict[tuple[str, int, int], list[dict[str, Any]]] = {}
        self.promo: dict[tuple[str, int], list[dict[str, Any]]] = {}
        try:
            self.prices: dict[str, Any] = json.loads(self.price_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            self.prices = {}

    # --- Index -------------------------------------------------------------------

    def start(self) -> None:
        threading.Thread(target=self._load, daemon=True).start()

    def _get(self, path: str) -> Any:
        r = httpx.get(API + path, timeout=30, headers={"User-Agent": "vinted-analyste"})
        r.raise_for_status()
        return r.json()

    def _load(self) -> None:
        while True:
            data = None
            try:
                data = json.loads(self.index_file.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                pass
            if not data or time.time() - data.get("built", 0) > INDEX_MAX_AGE:
                if data:
                    self._prepare(data)  # l'ancien index sert pendant la reconstruction
                    self.ready.set()
                try:
                    log.info("Construction de l'index TCGdex (1 à 3 minutes)…")
                    data = self._build()
                    self.index_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                    log.info("Index TCGdex prêt : %d extensions.", len(data["sets"]))
                except Exception as exc:  # noqa: BLE001
                    log.error("index TCGdex impossible à construire : %s", exc)
                    if not self.ready.is_set():
                        time.sleep(300)
                        continue
            self._prepare(data)
            self.ready.set()
            time.sleep(86400)

    def _build(self) -> dict[str, Any]:
        sets: dict[str, dict[str, Any]] = {}
        for lang in ("en", "fr"):
            for s in self._get(f"/{lang}/sets"):
                try:
                    d = self._get(f"/{lang}/sets/{s['id']}")
                except Exception:  # noqa: BLE001
                    continue
                count = d.get("cardCount") or {}
                e = sets.setdefault(s["id"], {"names": [], "abbr": [], "official": count.get("official"),
                                              "total": count.get("total"), "date": d.get("releaseDate", ""),
                                              "cards": {}})
                if d.get("name") and d["name"] not in e["names"]:
                    e["names"].append(d["name"])
                for a in (d.get("abbreviation") or {}).values():
                    if a and a not in e["abbr"]:
                        e["abbr"].append(a)
                for c in d.get("cards") or []:
                    cc = e["cards"].setdefault(c["localId"], {"id": c["id"], "names": []})
                    if c.get("name") and c["name"] not in cc["names"]:
                        cc["names"].append(c["name"])
                time.sleep(0.05)
        return {"built": time.time(), "sets": sets}

    def _prepare(self, data: dict[str, Any]) -> None:
        sets, by_number, promo = {}, {}, {}
        promo_ids = set(PROMO_SETS.values())
        for sid, s in data["sets"].items():
            s = dict(s, id=sid, norm_names=[norm(n) for n in s["names"]])
            sets[sid] = s
            for local_id, c in s["cards"].items():
                key = _key(local_id)
                if not key:
                    continue
                card = {"id": c["id"], "local_id": local_id, "set": sid, "display": (c["names"] or ["?"])[-1],
                        "norm_names": [n for n in {norm(x) for x in c["names"]} if n]}
                if sid in promo_ids:
                    promo.setdefault((sid, key[1]), []).append(card)
                for count in {s.get("official"), s.get("total")}:
                    if count:
                        by_number.setdefault((key[0], key[1], int(count)), []).append(card)
        with self.lock:
            self.sets, self.by_number, self.promo = sets, by_number, promo

    # --- Identification -------------------------------------------------------------

    def _set_mentioned(self, sid: str, text: str, raw_title: str) -> bool:
        s = self.sets[sid]
        if any(len(n) >= 3 and n not in VAGUE_SET_NAMES and _phrase(text, n) for n in s["norm_names"]):
            return True
        # Abréviations officielles (MEW, PAF, OBF…) seulement écrites en majuscules dans le titre
        return any(len(a) >= 3 and re.search(rf"(?<![A-Za-z]){re.escape(a)}(?![A-Za-z])", raw_title)
                   for a in s["abbr"] if a.isupper())

    def identify(self, title: str, desc: str = "", ocr: str = "") -> list[dict[str, Any]]:
        """Cartes reconnues dans l'annonce. Chaque groupe = une carte (plusieurs candidats si réimpression)."""
        if not self.ready.is_set():
            return []
        text = norm(f"{title} {desc}")
        ocr_text = norm(ocr)
        every = f"{text} {ocr_text}"
        groups: list[dict[str, Any]] = []
        taken: set[str] = set()

        def add(cands: list[dict[str, Any]], via: str, sure: bool) -> None:
            cands = [c for c in cands if c["id"] not in taken]
            if cands:
                taken.update(c["id"] for c in cands)
                groups.append({"cartes": cands, "via": via, "sure": sure and len(cands) == 1})

        for zone, src in ((text, "annonce"), (ocr_text, "photos")):
            for m in NUMBER.finditer(zone):
                p1, n, p2, tot = m.group(1), int(m.group(2)), m.group(3), int(m.group(4))
                if p2 and p1 != p2:
                    continue
                cands = self.by_number.get((p1, n, tot), [])
                named = [c for c in cands if any(_phrase(every, x) for x in c["norm_names"])]
                if not named:
                    named = [c for c in cands if self._set_mentioned(c["set"], text, title)]
                if named:
                    add(named, f"n° {m.group(0).strip()} lu dans {'le texte' if src == 'annonce' else 'les photos'}",
                        src == "annonce")
            for m in PROMO.finditer(zone):
                cands = self.promo.get((PROMO_SETS[m.group(1)], int(m.group(2))), [])
                named = [c for c in cands if any(_phrase(every, x) for x in c["norm_names"])]
                if named:
                    add(named, f"promo {m.group(0).strip().upper()}", src == "annonce")
        if groups:
            return groups
        # Sans numéro : nom de carte + extension citée
        for sid in [sid for sid in self.sets if self._set_mentioned(sid, text, title)]:
            cards = [c for k, lst in self.by_number.items() for c in lst
                     if c["set"] == sid and k[2] == self.sets[sid].get("official")]
            named = [c for c in cards if any(len(x) >= 4 and _phrase(text, x) for x in c["norm_names"])]
            if named:
                # garde le nom le plus long (Dracaufeu ex plutôt que Dracaufeu)
                best = max(len(x) for c in named for x in c["norm_names"] if _phrase(text, x))
                named = [c for c in named if any(len(x) == best and _phrase(text, x) for x in c["norm_names"])]
                add(named, f"nom + extension « {self.sets[sid]['names'][-1]} »", False)
        return groups

    # --- Prix ----------------------------------------------------------------------

    def card(self, card_id: str) -> dict[str, Any]:
        with self.lock:
            c = self.prices.get(card_id)
        if c and time.time() - c.get("at", 0) < PRICE_MAX_AGE and "avg1" in c.get("cm", {}):
            return c
        d: dict[str, Any] = {}
        for lang in ("fr", "en"):
            try:
                d = self._get(f"/{lang}/cards/{card_id}")
                if (d.get("pricing") or {}).get("cardmarket"):
                    break
            except Exception:  # noqa: BLE001
                continue
        cm = (d.get("pricing") or {}).get("cardmarket") or {}
        c = {"at": time.time(), "name": d.get("name"), "rarity": d.get("rarity"),
             "set": (d.get("set") or {}).get("name"), "official": ((d.get("set") or {}).get("cardCount") or {}).get("official"),
             "local_id": d.get("localId"), "cm": {k: cm.get(k) for k in
                                                  ("trend", "avg30", "avg7", "avg", "low", "trend-holo", "avg30-holo",
                                                   "avg7-holo", "avg-holo", "low-holo", "avg1", "avg1-holo",
                                                   "updated")}}
        with self.lock:
            self.prices[card_id] = c
            try:
                self.price_file.write_text(json.dumps(self.prices, ensure_ascii=False), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
        return c

    def value(self, card_id: str, reverse: bool | None = False) -> tuple[float | None, dict[str, Any]]:
        """Prix de référence prudent : le plus bas entre tendance, moyennes 30 et 7 jours et prix moyen de vente.
        Sur une carte peu échangée, une seule vente chère fait exploser la tendance (ex. 232 € pour une
        commune reverse vendue 15 à 25 €) : prendre le plus bas évite ces fausses pépites."""
        c = self.card(card_id)
        cm = c["cm"]
        base = ("trend", "avg30", "avg7", "avg")
        rev = tuple(f"{k}-holo" for k in base)
        if reverse is None:  # lot : on ne sait pas quelle carte est en reverse, on prend la version la moins chère
            vals = [cm.get(x) for x in base + rev if (cm.get(x) or 0) > 0]
            return (float(min(vals)) if vals else None), c
        for k in ((rev, base) if reverse else (base,)):
            vals = [cm.get(x) for x in k if (cm.get(x) or 0) > 0]
            if vals:
                return float(min(vals)), c
        return None, c
