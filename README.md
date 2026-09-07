# convia-sanitize

Sanitizer des conversations IA exportées par `convia` sur `vps-etude`
(remote Google Drive `convia:`), **+ publication RAG découplée**. C'est le
composant qui décide ce qui sort du corpus de conversations vers le Drive : le
dernier point où un secret peut être arrêté avant publication.

Ce dépôt est la **source canonique** des services `convia-sanitize.service` et
`convia-publish.service`. L'exécution reste sur `/opt/convia-sanitize/` (VPS) ;
toute modification se fait ici, est testée, puis déployée — jamais l'inverse.
Le code a historiquement été versionné dans `homelab-ops` (branche
`convia-chain`, commit `04cbf807`) avant d'en être retiré de l'arbre courant ;
cet historique est conservé tel quel, sans copie réintroduite ailleurs.

## Rôle

```
Google Drive « Conv IA » (convia:)
  → listing rclone (lsjson)                 (lecture, retry borné)
  → téléchargement des .md à traiter        (copyto)
  → sanitizer/redaction                     (sanitizer.py + redact.py)
  → sauvegarde pristine dans .raw/          (copyto, une seule fois par fichier)
  → réécriture du fichier assaini           (copyto)
  → SUCCÈS → OnSuccess= (déclenchement non bloquant)
       → convia-publish.service             (rclone copy → RAG, découplé)
```

Aucune donnée du corpus n'est passée à un shell : `subprocess` est appelé avec
une liste d'arguments, jamais avec `shell=True`.

## Composants

| Fichier | Rôle |
|---|---|
| `runner.py` | Sanitizer : polling, manifeste, boucle de traitement, retry borné des lectures, `--version` (commit déployé) |
| `publish.py` | Publisher RAG (unité `convia-publish.service`) : copies rclone racine + `Traité` → `convia-rag:ConvIA` |
| `sanitizer.py` | Transformation des conversations |
| `redact.py` | Redaction des secrets |
| `tests/` | Suite pytest (voir « Tests ») |
| `systemd/` | Unités/timer de référence + wrapper |
| `deploy/deploy.sh` | Déploiement reproductible (SHA tracé dans `.deployed-commit`) |
| `.github/workflows/ci.yml` | CI : gitleaks (arbre + historique) + pytest |

## Architecture systemd (depuis 2026-09-07)

```text
convia-sanitize.timer  (*:0/10)
   └─ convia-sanitize.service            python de sanitization, SE TERMINE VITE
        └─ mutation distante (réécriture / déplacement vers Traité)
             └─ demande durable → publish-requests/<run-id>
                  └─ convia-publish.path (DirectoryNotEmpty)
                       └─ convia-publish.service   (rclone copy racine + Traité → RAG)
```

- `convia-sanitize.service` : **n'a plus d'`ExecStartPost` ni d'`OnSuccess`**
  (mission 5). Sa durée = durée du python. Il ne déclenche le publisher QUE
  quand une mutation distante a eu lieu : il pose une **demande durable** dans
  `/var/lib/convia/publish-requests/` (a) avant la première réécriture d'un
  run, ou (b) quand une conversation connue disparaît du scope (déplacée vers
  `Traité` par le moteur d'analyse) — même si `a_traiter=0`.
- **Un run sans mutation ne publie jamais** : `a_traiter=0` sans déplacement →
  spool vide → `convia-publish.service` ne démarre pas (les checks rclone
  peuvent coûter plusieurs minutes même pour zéro fichier, mesuré le
  2026-09-07 : 7 s à 6 min sur état identique).
- `convia-publish.service` : oneshot autonome (`User=convia`), responsable
  unique de la publication. Instance unique systemd → **jamais deux copies
  concurrentes**. Au démarrage il photographie les demandes présentes ; en cas
  de succès il supprime **exactement ce snapshot** (une demande posée pendant
  le publish reste pour la passe suivante — pas de perte d'événement) ; en cas
  d'échec il ne supprime rien (retentative à la passe suivante). Spool vide →
  `status=noop`, succès, zéro appel rclone. `OnFailure=notify-failure@%n` :
  une panne de publication fait échouer LE PUBLISHER, pas le sanitizer.

Détail des unités et cibles : `systemd/README.md`.

## État runtime

- `CONVIA_STATE` : `/var/lib/convia` — manifestes `sanitize-manifest.json`,
  `convia.lock` (partagé avec le moteur d'analyse).
- `RCLONE_CONFIG` : `/var/lib/convia/rclone.conf` (0600, **hors Git**).
- Remotes : `convia:` (corpus), `convia-rag:` (destination RAG).

## Principe du manifeste

Le Drive est une API distante : ni inotify ni unités `.path` ne la voient.
La détection d'update se fait par polling (`Size` + `ModTime`) contre le
manifeste. Une entrée a besoin de travail si elle est absente du manifeste ou
si `version` / `size` / `modtime` diffèrent. Le manifeste n'est écrit qu'après
un run complet et vérifié (second listing) : un fichier modifié **pendant** le
run n'est pas tamponné comme traité, il repartira au prochain tick.

La sauvegarde `convia:.raw/<rel>` reçoit l'original **une seule fois**, avant
toute réécriture. Ne jamais la détruire : c'est la seule trace fidèle.

## Politique de retry (lectures uniquement)

Constat du 2026-09-07 : deux échecs transitoires de `rclone lsjson`
(11:20:52Z, 13:18:50Z) ont fait échouer le service et déclenché de **fausses
alertes Telegram**, le tick suivant (2 min plus tard) réussissant. rclone
retente déjà en interne (`--retries 3`, `--low-level-retries 10`) mais **sans
délai** (`--retries-sleep 0`) : une limitation temporaire Google survit à cette
salve.

- **Lectures de listing** (`lsjson`, `lsf`) : 3 tentatives au maximum, backoff
  10 s puis 60 s, journalisées.
- **Écritures** (`copyto`, `copy`) : aucune retentative applicative — rejouer
  une écriture sans preuve peut dupliquer ou écraser (le publisher s'appuie sur
  les retries internes de rclone + alerte `OnFailure` en cas d'échec).
- **Classification** simple : `transient` / `permanent` / `unknown`. Seule une
  erreur clairement permanente coupe court au retry.
- **Jamais d'exit 0 masqué** : si toutes les tentatives échouent, l'exception
  remonte, l'unité échoue et `OnFailure` notifie.

## Publication RAG (découplée)

`publish.py` réalise deux copies (`copy`, jamais `sync`, aucune suppression
distante) et journalise une ligne compacte par copie + une synthèse :

```
publish copy=root   status=ok files=… duration=…
publish copy=traite status=ok files=… duration=…
publish copies=2 status=success duration=…
```

Optimisation appliquée **sur benchmark** (2026-09-07) : `--fast-list` sur la
copie racine (arbre à exclusions profondes : 15.5 s → 7.2 s en dry-run) ;
volontairement PAS sur la copie `Traité` (plus lent : 44 s vs 35 s). Les stats
rclone sont capturées dans un pipe, jamais déversées dans journald.

## Observabilité

Chaque échec rclone (sanitizer ou publisher) produit une ligne de journal
unique, compacte et redactée :

```
rclone op=lsjson attempt=2/3 rc=1 class=transient err="googleapi: Error 429: User Rate Limit Exceeded..."
publish copy=traite status=failed rc=1 err="…"
```

- bornée à 300 caractères, une seule ligne ;
- jetons remplacés par `[REDACTED:<famille>]` (motifs de `redact.py`) ;
- jamais le stdout du corpus, jamais la commande complète ;
- sûres par conception pour `notify_failure` (dernières lignes journal →
  Telegram).

## Traçabilité du commit déployé

Le déploiement écrit le SHA réel à côté du code ; le runtime ne l'invente
jamais :

```text
/opt/convia-sanitize/.deployed-commit   # SHA git complet (identité canonique)
/opt/convia-sanitize/.deployed-at       # ISO 8601 (information secondaire)
```

Vérifiable à tout instant :

```bash
python3 /opt/convia-sanitize/runner.py --version
# convia-sanitize sanitizer_v2 git=<sha> deployed=<iso>
```

## Tests

```bash
python3 -m pytest            # depuis la racine du dépôt
```

Dépendances : `pytest`, `pyyaml` (un seul test lit le frontmatter).

- `test_sanitizer.py` — règles + invariants (frontmatter, messages bit-à-bit,
  idempotence, Markdown jamais cassé).
- `test_redact.py`, `test_redact_mdp_court.py` — redaction (et régression mdp
  court).
- `test_rclone_retry.py` — budget de retry/classification/journal redacté.
- `test_publish.py` — publisher : commandes construites (fast-list racine
  seulement), dry-run, échec = rc≠0 + erreur redactée, stats best-effort.
- `test_version.py` — `--version` lit `.deployed-commit`, fallback `unknown`.

`test_analyse_budget.py` (moteur d'analyse) n'appartient pas à ce dépôt.

## Déploiement / rollback

### Code + traçabilité (mécanisme canonique)

```bash
bash deploy/deploy.sh [host]     # host par défaut : vps-etude-nb
```

Le script, depuis un working tree propre : tests → staging → backup ciblé
(`/var/backups/convia-sanitize/<ts>/`, rétention 2) → installation atomique
(`install -o convia -g convia -m 0644`) → `.deployed-commit`/`.deployed-at` →
`py_compile` → `--version` affiché. Il ne déploie jamais `.git`, tests, caches,
secrets ni archives.

### Unités systemd (rare)

Copier les fichiers `systemd/*` vers `/etc/systemd/system/` (mêmes noms) puis :

```bash
sudo systemctl daemon-reload
sudo systemctl start convia-publish.service   # validation ponctuelle
```

### Rollback

- Backup complet du déploiement précédent :
  `/root/convia-sanitize-rollback-<date>/opt-convia-sanitize.tar.gz` (créé
  avant chaque mission) et `/var/backups/convia-sanitize/<ts>/` (automatique).
- Restauration : `sudo tar xzf … -C /opt` (convia-sanitize-rollback-*) ou
  recopie du backup `/var/backups`.
- Ne pas toucher à `/var/lib/convia` ni au remote `convia:` : le code est
  remplaçable, le corpus ne l'est pas.
