"""Orchestrateur du sanitizer : polling rclone + manifeste + reecriture en place.

`gdrive:` est une API distante : inotify et les unites .path ne la voient pas.
La detection d'update se fait donc par polling (Size + ModTime) contre
/var/lib/convia/sanitize-manifest.json.

Aucune donnee du corpus n'est passee a un shell : subprocess est appele avec
une liste d'arguments, jamais avec shell=True.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sanitizer import VERSION, frontmatter_version, sanitize_with_stats  # noqa: E402

REMOTE = os.environ.get("CONVIA_REMOTE", "convia:")
STATE_DIR = Path(os.environ.get("CONVIA_STATE", "/var/lib/convia"))
MANIFEST = STATE_DIR / "sanitize-manifest.json"
BACKUP_PREFIX = ".raw"
EXCLUDES = ["--exclude", "Trait*/**", "--exclude", ".raw/**", "--exclude", "traiter/**"]
RCLONE = ["rclone", "--config", os.environ.get("RCLONE_CONFIG", "")]

# Traçabilité : le SHA deploye est ECRIT par le mécanisme de déploiement
# (deploy/deploy.sh) dans `.deployed-commit` a cote de ce fichier. Le runtime
# ne l'invente jamais : sans fichier, il affiche "unknown". Le `.deployed-at`
# (ISO) est une information secondaire, le SHA est l'identité canonique.
_DEPLOYED = Path(__file__).resolve().parent


def deployed_commit() -> str:
    try:
        return _DEPLOYED.joinpath(".deployed-commit").read_text(
            encoding="utf-8").strip().splitlines()[0][:40]
    except OSError:
        return "unknown"


def deployed_at() -> str:
    try:
        return _DEPLOYED.joinpath(".deployed-at").read_text(
            encoding="utf-8").strip().splitlines()[0]
    except OSError:
        return "unknown"


from redact import SECRET_PATTERNS, marker as _redact_marker  # noqa: E402

# ---------------------------------------------------------------------------
# Robustesse des lectures distantes (mesures du 2026-09-07)
# ---------------------------------------------------------------------------
# Deux echecs transitoires de `rclone lsjson` (11:20:52Z et 13:18:50Z) ont fait
# echouer le service et declenche une FAUSSE alerte Telegram a chaque fois : le
# tick suivant (2 min plus tard) reussissait. rclone v1.60 retente deja en
# interne (--retries 3, --low-level-retries 10) MAIS sans delai entre les
# tentatives (--retries-sleep 0) : une limitation temporaire cote Google survit
# a cette salve et ne retombe qu'apres quelques secondes a quelques minutes.
#
# Ce retry Python est volontairement BORNE (3 tentatives, backoff 10 s puis
# 60 s) et reserve aux LISTINGS (lectures, ~2 s en nominal) : `lsjson` de
# list_remote() et `lsf` de list_backups(). Les ECRITURES (`copyto`) n'en
# beneficient PAS : rejouer une ecriture sans preuve de necessite peut dupliquer
# ou ecraser, et rclone gere deja ses propres retries internes.
#
# Une panne PERSISTANTE sort toujours en erreur (jamais d'exit 0 masque) :
# l'unite echoue, OnFailure notifie Telegram, et le journal contient desormais
# l'erreur reelle (redactee, bornee) au lieu d'un simple code retour.
READ_ATTEMPTS = 3
READ_BACKOFF = (10, 60)  # secondes entre les tentatives de lecture

# Classification simple et fail-safe : seule une erreur clairement PERMANENTE
# coupe court au retry. Tout le reste (transient, inconnu) est reessaye dans la
# limite du budget, puis echoue si le budget est epuise.
_TRANSIENT_RE = re.compile(
    r"(?i)(429|5\d\d|too many requests|rate.?limit|throttl|quota|timeout|"
    r"timed out|temporar|reset by peer|connection (?:refused|reset)|eof|"
    r"interrupted|unavailable|retr(?:y|ies))")
_PERMANENT_RE = re.compile(
    r"(?i)(not found|no such file|didn't find|invalid (?:config|argument|"
    r"characters|remote)|unknown flag|401|403|404|permission denied|"
    r"access.?denied|config file)")


def classify_rclone_error(stderr: str) -> str:
    """transient | permanent | unknown. Un message contenant a la fois un
    code HTTP et un motif (`429 User Rate Limit`) est TRANSIENT."""
    if not stderr:
        return "unknown"
    if _TRANSIENT_RE.search(stderr):
        return "transient"
    if _PERMANENT_RE.search(stderr):
        return "permanent"
    return "unknown"


def _rclone_error_text(stderr: str) -> str:
    """Extrait d'erreur exploitable pour journald/Telegram.

    Redaction des jetons connus (memes motifs que le sanitizer), une seule
    ligne, bornee a 300 caracteres. Jamais le stdout du corpus, jamais la
    commande complete (les arguments contiennent des chemins de conversation).
    """
    if not stderr:
        return ""
    text = stderr
    for family, rx in SECRET_PATTERNS:
        text = rx.sub(_redact_marker(family), text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:300]


def _retry_delay(attempt: int) -> float:
    if not READ_BACKOFF:
        return 0.0
    return READ_BACKOFF[min(attempt, len(READ_BACKOFF)) - 1]


def rclone(*args: str, capture: bool = False, retries: int = 1) -> str:
    cmd = [a for a in RCLONE if a] + list(args)
    if not os.environ.get("RCLONE_CONFIG"):
        cmd = ["rclone"] + list(args)
    op = args[0] if args else "rclone"
    attempts = max(1, retries)
    for attempt in range(1, attempts + 1):
        try:
            res = subprocess.run(
                cmd, check=True, stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL,
            )
            return res.stdout if capture else ""
        except subprocess.CalledProcessError as exc:
            kind = classify_rclone_error(exc.stderr)
            print("rclone op=%s attempt=%d/%d rc=%d class=%s err=%s" % (
                op, attempt, attempts, exc.returncode, kind,
                _rclone_error_text(exc.stderr)), flush=True)
            if attempt < attempts and kind != "permanent":
                time.sleep(_retry_delay(attempt))
                continue
            raise
    raise RuntimeError("unreachable")  # pragma: no cover


def list_backups() -> set:
    """Sauvegardes deja presentes dans .raw/.

    Indispensable depuis la v2 : un repassage du corpus re-televerse le fichier
    COURANT (deja assaini en v1). Sans ce garde-fou, `copyto` ecraserait
    l'original pristine par sa propre version assainie, et la sauvegarde
    perdrait tout interet. Une sauvegarde est donc ecrite UNE SEULE FOIS, a la
    premiere reecriture d'un fichier donne.
    """
    try:
        out = rclone("lsf", "-R", "--files-only", REMOTE + BACKUP_PREFIX,
                     capture=True, retries=READ_ATTEMPTS)
    except subprocess.CalledProcessError:
        return set()
    return {line for line in out.splitlines() if line}


def list_remote() -> list[dict]:
    out = rclone("lsjson", "-R", "--files-only", REMOTE, *EXCLUDES,
                 capture=True, retries=READ_ATTEMPTS)
    return [f for f in json.loads(out) if f["Path"].endswith(".md")]


def load_manifest() -> dict:
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_manifest(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, MANIFEST)


def needs_work(entry: dict, man: dict) -> bool:
    rec = man.get(entry["Path"])
    if not rec:
        return True
    return (
        rec.get("version") != VERSION
        or rec.get("size") != entry["Size"]
        or rec.get("modtime") != entry["ModTime"]
    )


def _main() -> int:
    ap = argparse.ArgumentParser(description="Sanitizer des conversations IA")
    ap.add_argument("--dry-run", action="store_true", help="aucune ecriture sur le Drive")
    ap.add_argument("--all", action="store_true", help="ignorer le manifeste")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--local", type=Path, help="travailler sur un repertoire local (mesure)")
    ap.add_argument("--version", action="store_true",
                    help="afficher la version et le commit deploye")
    args = ap.parse_args()

    if args.version:
        print("convia-sanitize sanitizer_v%d git=%s deployed=%s" % (
            VERSION, deployed_commit(), deployed_at()))
        return 0

    if args.local:
        return measure_local(args.local)

    man = {} if args.all else load_manifest()
    entries = list_remote()
    todo = [e for e in entries if args.all or needs_work(e, man)]
    if args.limit:
        todo = todo[: args.limit]
    print("corpus=%d a_traiter=%d" % (len(entries), len(todo)), flush=True)
    if not todo:
        return 0

    total_before = total_after = 0
    agg = {}
    backups = list_backups() if not args.dry_run else set()
    print("sauvegardes .raw deja presentes=%d" % len(backups), flush=True)
    with tempfile.TemporaryDirectory(prefix="convia-", dir=os.environ.get("TMPDIR", "/tmp")) as td:
        tdp = Path(td)
        for entry in todo:
            rel = entry["Path"]
            src = tdp / "in" / rel
            src.parent.mkdir(parents=True, exist_ok=True)
            rclone("copyto", REMOTE + rel, str(src))
            raw = src.read_text(encoding="utf-8", errors="replace")
            total_before += len(raw.encode("utf-8"))
            if frontmatter_version(raw) == VERSION:
                total_after += len(raw.encode("utf-8"))
                man[rel] = {"size": entry["Size"], "modtime": entry["ModTime"],
                            "version": VERSION}
                continue
            clean, stats = sanitize_with_stats(raw)
            for k, v in stats.items():
                agg[k] = agg.get(k, 0) + v
            data = clean.encode("utf-8")
            total_after += len(data)
            if args.dry_run:
                continue
            # 1. sauvegarde de l'original AVANT toute reecriture, UNE SEULE
            #    FOIS. Si .raw/<rel> existe deja, il contient l'original
            #    pristine : l'ecraser avec le contenu courant (deja
            #    assaini en v1) le detruirait definitivement.
            if rel not in backups:
                rclone("copyto", str(src), "%s%s/%s" % (REMOTE, BACKUP_PREFIX, rel))
                backups.add(rel)
            # 2. reecriture : ecriture a cote puis remplacement (rclone cree une
            #    nouvelle revision cote Drive, jamais de troncature en place)
            dst = tdp / "out" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
            rclone("copyto", str(dst), REMOTE + rel)
            man[rel] = {"size": len(data), "modtime": None, "version": VERSION}

    if not args.dry_run:
        for e in list_remote():
            rec = man.get(e["Path"])
            if not rec or rec.get("version") != VERSION:
                continue
            expected = rec.get("size")
            if expected is not None and expected != e["Size"]:
                # Le fichier a ete reecrit par l'export PENDANT le run : ce qui
                # est sur le Drive n'est pas ce que nous avons assaini. Ne pas
                # le tamponner, sinon il ne serait plus jamais repris.
                del man[e["Path"]]
                print("reprise requise (modifie pendant le run): %s" % e["Path"],
                      flush=True)
                continue
            man[e["Path"]] = {"size": e["Size"], "modtime": e["ModTime"],
                              "version": VERSION}
        save_manifest(man)

    report(total_before, total_after, agg)
    return 0


def measure_local(root: Path) -> int:
    total_before = total_after = 0
    agg: dict = {}
    files = sorted(p for p in root.rglob("*.md")
                   if ".raw" not in p.parts and not any(
                       part.lower().startswith("trait") for part in p.parts))
    for p in files:
        raw = p.read_text(encoding="utf-8", errors="replace")
        total_before += len(raw.encode("utf-8"))
        clean, stats = sanitize_with_stats(raw)
        for k, v in stats.items():
            agg[k] = agg.get(k, 0) + v
        total_after += len(clean.encode("utf-8"))
    print("fichiers=%d" % len(files))
    report(total_before, total_after, agg)
    return 0


def report(before: int, after: int, agg: dict) -> None:
    pct = (1 - after / before) * 100 if before else 0.0
    print("avant=%d octets" % before)
    print("apres=%d octets" % after)
    print("reduction=%.1f %%" % pct)
    for k in ("tool_result", "thinking", "reminders", "cosmetic"):
        v = agg.get(k, 0)
        print("  %-12s %12d o  (%.1f %% du corpus)" % (k, v, 100 * v / before if before else 0))
    # Redactions de secrets : un COMPTE d occurrences, pas un volume d octets.
    red = {k[len("redacted_"):]: v for k, v in sorted(agg.items())
           if k.startswith("redacted_")}
    print("redactions=%d" % sum(red.values()))
    for k, v in red.items():
        print("  %-24s %6d occurrence(s)" % (k, v))


def main() -> int:
    """Point d'entree : mesure la duree totale et journalise une ligne compacte.

    Une panne reste une panne : l'exception remonte (unite FAILED -> OnFailure
    -> Telegram) apres une ligne `sanitize status=failed`.
    """
    t0 = time.monotonic()
    try:
        rc = _main()
    except Exception:
        print("sanitize status=failed duration=%.1fs" % (time.monotonic() - t0),
              flush=True)
        raise
    print("sanitize status=success duration=%.1fs rc=%d" % (
        time.monotonic() - t0, rc), flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
