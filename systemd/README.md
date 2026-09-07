# systemd/ — unités de référence de `convia-sanitize`

Copies fidèles des fichiers installés sur `vps-etude` (2026-09-07). Elles
décrivent le déploiement cible ; le dépôt est la source, `/etc` la copie
d'exploitation.

| Fichier (dépôt) | Cible (VPS) |
|---|---|
| `convia-sanitize.service` | `/etc/systemd/system/convia-sanitize.service` |
| `convia-sanitize.service.d/*.conf` | `/etc/systemd/system/convia-sanitize.service.d/` |
| `convia-sanitize.timer` | `/etc/systemd/system/convia-sanitize.timer` |
| `convia_sanitize.py` | `/usr/local/bin/convia_sanitize.py` |

Le code Python (`runner.py`, `sanitizer.py`, `redact.py`) est exécuté depuis
`/opt/convia-sanitize/` (racine du dépôt déployée).

Dépendances externes, **non versionnées ici** :

- `OnFailure=notify-failure@%n.service` : socle Telegram
  (`/usr/local/lib/telegram/notify_failure.sh`, config `/etc/default/telegram_notify`) ;
- credentials rclone : `/var/lib/convia/rclone.conf` (0600) ;
- remotes rclone : `convia:` (corpus Drive) et `convia-rag:` (publication RAG).

Après modification d'une unité : `sudo systemctl daemon-reload`, puis
`sudo systemctl restart convia-sanitize.timer` si nécessaire.
