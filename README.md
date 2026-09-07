# convia-sanitize

Sanitizer des conversations IA exportées par `convia` sur `vps-etude`
(remote Google Drive `convia:`). C'est le composant qui décide ce qui sort du
corpus de conversations vers le Drive : **le dernier point où un secret peut
être arrêté avant publication**.

Ce dépôt est la **source canonique** du service `convia-sanitize.service`.
L'exécution reste sur `/opt/convia-sanitize/` (VPS) ; toute modification se fait
ici, est testée, puis déployée — jamais l'inverse. Le code a historiquement été
versionné dans `homelab-ops` (branche `convia-chain`, commit `04cbf807`) avant
d'en être retiré de l'arbre courant ; cet historique est conservé tel quel,
sans copie réintroduite ailleurs.

## Rôle

```
Google Drive « Conv IA » (convia:)
  → listing rclone (lsjson)                 (lecture, retry borné)
  → téléchargement des .md à traiter        (copyto)
  → sanitizer/redaction                     (sanitizer.py + redact.py)
  → sauvegarde pristine dans .raw/          (copyto, une seule fois par fichier)
  → réécriture du fichier assaini           (copyto)
  → publication vers le RAG Vault           (ExecStartPost, rclone copy)
```

Aucune donnée du corpus n'est passée à un shell : `subprocess` est appelé avec
une liste d'arguments, jamais avec `shell=True`.

## Composants

| Fichier | Rôle |
|---|---|
| `runner.py` | Point d'entrée appelé par le service : polling, manifeste, boucle de traitement, retry borné des lectures |
| `sanitizer.py` | Transformation des conversations (suppression réflexions/reminders, troncature des résultats, invariants d'intégrité) |
| `redact.py` | Redaction des secrets (jetons fournisseur, mots de passe) — seule exception au périmètre de conservation |
| `tests/` | Suite pytest (voir « Tests ») |
| `systemd/` | Unités/timer systemd de référence + wrapper `/usr/local/bin/convia_sanitize.py` |
| `.github/workflows/ci.yml` | CI : scan secrets (gitleaks, arbre + historique) + tests Python |

## État runtime

- `CONVIA_STATE` : `/var/lib/convia` — manifeste `sanitize-manifest.json` +
  verrou `convia.lock` (partagé avec `convia-analyse.service`).
- `RCLONE_CONFIG` : `/var/lib/convia/rclone.conf` (0600, **hors Git**).
- `CONVIA_REMOTE` : `convia:` (Drive « Conv IA »).

## Principe du manifeste

Le Drive est une API distante : ni inotify ni unités `.path` ne la voient.
La détection d'update se fait par polling (`Size` + `ModTime`) contre le
manifeste. Une entrée a besoin de travail si elle est absente du manifeste ou
si `version` / `size` / `modtime` diffèrent. Le manifeste n'est écrit qu'après
un run complet et vérifié (second listing) : un fichier modifié **pendant** le
run n'est pas tamponné comme traité, il repartira au prochain tick.

La sauvegarde `convia:.raw/<rel>` reçoit l'original **une seule fois**, avant
toute réécriture. Ne jamais la détruire : c'est la seule trace fidèle.

## Systemd

`convia-sanitize.timer` déclenche `convia-sanitize.service` toutes les 10 min
(`OnCalendar=*:0/10`, `RandomizedDelaySec=60`). Type `oneshot`, utilisateur
`convia`, sandboxé (le corpus est une entrée hostile). `OnFailure=notify-failure@%n.service`
notifie Telegram en cas d'échec — les unités de notification vivent hors de ce
dépôt (socle `/usr/local/lib/telegram`, `notify_failure.sh`).

## Politique de retry (lectures uniquement)

Constat du 2026-09-07 : deux échecs transitoires de `rclone lsjson`
(11:20:52Z, 13:18:50Z) ont fait échouer le service et déclenché de **fausses
alertes Telegram**, le tick suivant (2 min plus tard) réussissant. rclone
retente déjà en interne (`--retries 3`, `--low-level-retries 10`) mais **sans
délai** (`--retries-sleep 0`) : une limitation temporaire Google survit à cette
salve.

- **Lectures de listing** (`lsjson`, `lsf`) : 3 tentatives au maximum, backoff
  10 s puis 60 s, journalisées.
- **Écritures** (`copyto`) : aucune retentative applicative — rejouer une
  écriture sans preuve peut dupliquer ou écraser.
- **Classification** simple : `transient` / `permanent` / `unknown`. Seule une
  erreur clairement permanente (remote/config introuvable, 401/403/404...) coupe
  court au retry. Tout le reste est réessayé dans la limite du budget.
- **Jamais d'exit 0 masqué** : si toutes les tentatives échouent, l'exception
  remonte, l'unité échoue et `OnFailure` notifie. Une vraie panne persistante
  reste visible et diagnostiquée.

## Observabilité

Chaque échec rclone produit une ligne de journal unique, compacte et redactée :

```
rclone op=lsjson attempt=2/3 rc=1 class=transient err="googleapi: Error 429: User Rate Limit Exceeded..."
```

- bornée à 300 caractères, une seule ligne ;
- les jetons connus sont remplacés par `[REDACTED:<famille>]` (mêmes motifs que
  `redact.py`) ;
- jamais le stdout du corpus, jamais la commande complète (les arguments
  contiennent des chemins de conversation) ;
- ces lignes peuvent transiter vers Telegram via `notify_failure` (qui lit les
  dernières lignes du journal) : elles sont donc sûres par conception.

## Tests

```bash
python3 -m pytest            # depuis la racine du dépôt
```

Dépendances de test : `pytest`, `pyyaml` (un seul test lit le frontmatter).

- `test_sanitizer.py` — règles de transformation + invariants (frontmatter,
  messages utilisateur bit-à-bit, idempotence, Markdown jamais cassé).
- `test_redact.py`, `test_redact_mdp_court.py` — redaction des secrets et
  régression du mot de passe court (2026-09-06).
- `test_rclone_retry.py` — budget de retry, classification, non-retry des
  écritures, redaction/bornage du journal (stub `subprocess.run`, aucun appel
  réseau).

`test_analyse_budget.py` (moteur d'analyse ChatGPT) n'appartient pas à ce dépôt :
il vit avec le moteur d'analyse, hors périmètre du sanitizer.

## Déploiement / rollback (VPS `vps-etude`)

Cibles (voir `systemd/`) :

| Source (dépôt) | Cible |
|---|---|
| `runner.py`, `sanitizer.py`, `redact.py`, `tests/` | `/opt/convia-sanitize/` (propriétaire `convia:convia`, 0644) |
| `systemd/convia_sanitize.py` | `/usr/local/bin/convia_sanitize.py` |
| `systemd/convia-sanitize.service` (+ `.service.d/*`) | `/etc/systemd/system/` |
| `systemd/convia-sanitize.timer` | `/etc/systemd/system/` |

Déploiement d'une nouvelle version du code :

```bash
# 1. sauvegarde (rollback immédiat, sans toucher au corpus)
ssh vps-etude-nb "sudo tar czf /root/convia-sanitize-rollback-\$(date +%Y%m%d-%H%M%S).tar.gz -C /opt convia-sanitize"
# 2. copie du code (propriétaire conservé)
scp runner.py sanitizer.py redact.py vps-etude-nb:/tmp/
ssh vps-etude-nb "sudo install -o convia -g convia -m 0644 /tmp/{runner,sanitizer,redact}.py /opt/convia-sanitize/"
# 3. vérification syntaxe + run manuel (même utilisateur que systemd)
ssh vps-etude-nb "sudo -u convia python3 -m py_compile /opt/convia-sanitize/runner.py"
ssh vps-etude-nb "sudo systemctl start convia-sanitize.service"   # run réel (flock)
```

Rollback :

```bash
ssh vps-etude-nb "sudo tar xzf /root/convia-sanitize-rollback-<TS>.tar.gz -C /opt"
```

Ne pas toucher à `/var/lib/convia` (état) ni au remote `convia:` pendant un
rollback : le code est remplaçable, le corpus ne l'est pas.
