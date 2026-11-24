# vinted-photos : analyser une annonce Vinted dans Claude Desktop, photos comprises

Colle un lien Vinted dans Claude Desktop : Claude appelle l'outil `voir_annonce`, reçoit la description
et toutes les photos, et les analyse. Ça passe par ton abonnement Claude, sans clé API.

Bloc à ajouter dans `mcpServers` de `claude_desktop_config.json` (adapter le nom d'utilisateur) :

```json
"vinted-photos": {
  "command": "C:\\Users\\Utilisateur\\.local\\bin\\uv.exe",
  "args": ["run", "--with", "vinted-search-mcp==0.2.1", "--with", "pillow",
           "C:\\Users\\Utilisateur\\vinted-bot\\claude-desktop\\vinted_photos_mcp.py"]
}
```
