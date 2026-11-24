# Pile Vinted personnelle

Le dossier `vinted-bot/` contient ce qu'il faut copier dans `C:\Users\<toi>\vinted-bot` :

| Fichier | Rôle |
|---|---|
| `docker-compose.yml` | vinted-bot (alertes) + l'analyste Claude |
| `analyst/` | Le service qui analyse chaque alerte avec Claude et envoie un verdict sur Telegram |
| `analyst/rules.json` | Le mode sans IA (`MODE=regles`) : modèles reconnus, prix de référence, mots suspects et défauts |
| `analyst/reference.md` | Les niches, prix de référence, grilles et règles de l'analyste. **À mettre à jour avec tes vraies ventes.** |
| `.env.example` | Modèle du fichier `.env`, où vont ta clé API et le jeton Telegram (jamais versionné) |

## Fonctionnement

```
vinted-bot ──webhook──► analyst ──photos + annonce──► Claude ──verdict──► Telegram (bot "Analyste")
```

- Chaque alerte de vinted-bot est complétée (description, toutes les photos, profil vendeur) puis analysée.
- Le programme recalcule lui-même la marge et applique la règle d'achat (≥ 8 € et ≥ 40 %) : un « ACHETER » sans marge est rétrogradé.
- Par défaut, seuls les verdicts ACHETER et OFFRE sont envoyés (`NOTIFY=deals`).
- Tu peux aussi coller un lien Vinted dans la conversation avec le bot analyste pour une analyse à la demande.
- Chaque verdict est noté dans `verdicts.csv` (volume `analyst-data`), avec le coût de l'analyse.

## Mise à jour des prix de référence

Modifie `analyst/reference.md`, puis :

```powershell
docker compose up -d --build analyst
```
