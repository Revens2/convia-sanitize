# systemd/ — unités de référence de `convia-sanitize` (+ `convia-publish`)

Copies fidèles des fichiers installés sur `vps-etude` (2026-09-07, mission 4).
Le dépôt est la source ; `/etc` est la copie d'exploitation.

| Fichier (dépôt) | Cible (VPS) | Rôle |
|---|---|---|
| `convia-sanitize.service` | `/etc/systemd/system/convia-sanitize.service` | Sanitizer (polling 10 min) |
| `convia-sanitize.service.d/10-verrou-non-bloquant.conf` | `/etc/systemd/system/convia-sanitize.service.d/` | flock `-E 0` : verrou occupé ≠ panne |
| `convia-sanitize.service.d/20-timeout.conf` | idem | `TimeoutStartSec=20min` (publication découplée) |
| `convia-sanitize.service.d/30-journal.conf` | idem | `SyslogIdentifier=convia-sanitize` |
| `convia-sanitize.service.d/40-publish-on-success.conf` | idem | `OnSuccess=convia-publish.service` (déclenchement non bloquant) |
| `convia-publish.service` | `/etc/systemd/system/convia-publish.service` | Publication RAG autonome (oneshot, jamais 2 copies concurrentes) |
| `convia-sanitize.timer` | `/etc/systemd/system/convia-sanitize.timer` | Cadence `*:0/10` + `RandomizedDelaySec=60` |
| `convia_sanitize.py` | `/usr/local/bin/convia_sanitize.py` | Wrapper d'exécution du runner |

Architecture (depuis 2026-09-07) :

```text
convia-sanitize.timer
   └─ convia-sanitize.service      (python : listing + sanitization)
        └─ succès → OnSuccess=
             └─ convia-publish.service   (rclone copy racine + Traité → RAG)
```

Le sanitizer n'a plus d'`ExecStartPost` : il se termine dès la sanitization
finie. La publication (copies rclone potentiellement longues) est l'affaire du
publisher, observable et faillible séparément.

Le code Python (`runner.py`, `sanitizer.py`, `redact.py`, `publish.py`) est
exécuté depuis `/opt/convia-sanitize/` (racine du dépôt déployée). Le
`.deployed-commit` écrit par le déploiement s'y trouve aussi (traçabilité,
voir `README.md` et `deploy/`).

Dépendances externes, **non versionnées ici** :

- `OnFailure=notify-failure@%n.service` (sanitizer ET publisher) : socle
  Telegram (`/usr/local/lib/telegram/notify_failure.sh`, config
  `/etc/default/telegram_notify`) ;
- credentials rclone : `/var/lib/convia/rclone.conf` (0600) ;
- remotes rclone : `convia:` (corpus Drive), `convia-rag:` (publication RAG).

Après modification d'une unité : `sudo systemctl daemon-reload` ; puis
`sudo systemctl restart convia-sanitize.timer` si la cadence change.
