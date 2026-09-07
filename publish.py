"""Publisher ConvIA : copie Drive -> RAG (remote `convia-rag:ConvIA`).

Responsabilite UNIQUE, decouplee de convia-sanitize.service (commit 2026-09-07,
mission 4) : le sanitizer finit des que la sanitization est terminee ; la
publication vers le RAG est declenchee separement par
`convia-publish.service` (OnSuccess= du sanitizer) et ne le bloque jamais.

Copies realisees (jamais `sync`, aucune suppression distante automatique) :

  1. racine `convia:` (fichiers assainis) -> `convia-rag:ConvIA`, hors
     `.raw/`, `Trait*/`, `traiter/` ; `--fast-list` mesure 2x plus rapide sur
     cet arbre a exclusions profondes (benchmark 2026-09-07 : dry-run 15.5 s ->
     7.2 s). Les autres copies gardent le comportement de reference.
  2. `convia:Traité` -> `convia-rag:ConvIA` (conversations deplacees par le
     moteur d'analyse) ; `--fast-list` n'apporte pas de gain mesure sur cet
     arbre plat (44 s vs 35 s) -> non applique.

Les ECRITURES rclone ne sont pas rejouees au niveau applicatif (une copie
partielle rejouee peut dupliquer ; rclone gere ses propres retries internes) :
un echec fait echouer l'unite -> OnFailure notifie Telegram.

Journalisation : une ligne compacte par copie + une ligne de synthese. Pas de
contenu de conversation, pas de token, pas de chemins inutiles.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

try:  # meme repertoire que redact.py en production (/opt/convia-sanitize)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from redact import SECRET_PATTERNS, marker as _redact_marker  # noqa: E402
except Exception:  # pragma: no cover - import standalone improbable
    SECRET_PATTERNS = []
    _redact_marker = lambda fam: "[REDACTED:%s]" % fam

REMOTE = os.environ.get("CONVIA_REMOTE", "convia:")
DEST = os.environ.get("CONVIA_RAG_REMOTE", "convia-rag:ConvIA")
ROOT_EXCLUDES = [
    "--exclude", ".raw/**",
    "--exclude", "Trait*/**",
    "--exclude", "traiter/**",
]
_TRANSFERS = ["--transfers", "4", "--checkers", "8"]
# Stats capturees (pipe -> journal une ligne), pas de flot dans journald.
# L'analyse de ces lignes est best-effort : si rclone ne les emet pas (sortie
# non-interactive), la synthese ne porte que la duree mesuree par Python.
_STATS = ["--stats", "2s"]
_MAX_ERR = 300

# Copie 1 : racine assainie -> RAG. --fast-list mesure 2x plus rapide (arbre a
# exclusions profondes) : listing source recursive en ~7 s au lieu de ~15 s.
ROOT_COPY = ["copy", REMOTE, DEST, "--fast-list", *ROOT_EXCLUDES]
# Copie 2 : Traité -> RAG (convergence analyse). Reference, sans --fast-list.
TRAITE_COPY = ["copy", REMOTE + "Trait\u00e9", DEST]


def rclone_base() -> list[str]:
    cfg = os.environ.get("RCLONE_CONFIG", "")
    return ["rclone", "--config", cfg] if cfg else ["rclone"]


def _scrub(text: str) -> str:
    for family, rx in SECRET_PATTERNS:
        text = rx.sub(_redact_marker(family), text)
    return text


def parse_stats(stderr: str) -> dict:
    """Extrait du flux de stats rclone : fichiers transferes, octets, duree.

    Best-effort : rclone n'emettra un bloc a 100%% que si un tick tombe
    exactement a la fin (sinon `files=0` sur les runs longs, cf. runs contraints
    du 2026-09-07). On retient donc le MEILLEUR couple done/total observe au
    lieu de n'accepter que les blocs a 100%%. `files` = done max, `total` = le
    total du meme bloc. Le flux `--stats 2s` va sur stderr (PIPE) : jamais dans
    journald, seule la synthese Python d'une ligne y parvient.
    """
    out = {"files": 0, "total": 0, "bytes_raw": "", "elapsed": 0.0}
    if not stderr:
        return out
    done_max = total_max = 0
    for m in re.finditer(
            r"^Transferred:\s+(\d+) / (\d+), \d+%", stderr, re.MULTILINE):
        done, total = int(m.group(1)), int(m.group(2))
        if done > done_max:
            done_max, total_max = done, total
    trans_size = re.findall(
        r"^Transferred:\s+([0-9.]+ [A-Za-z]+) / [0-9.]+ [A-Za-z]+, \d+%",
        stderr, re.MULTILINE)
    elapsed = re.findall(r"^Elapsed time:\s+([0-9.]+)s$", stderr, re.MULTILINE)
    if done_max:
        out["files"], out["total"] = done_max, total_max
    if trans_size:
        out["bytes_raw"] = trans_size[-1]
    if elapsed:
        out["elapsed"] = float(elapsed[-1])
    return out


def _log(copy_name: str, kind: str, exc=None, stats=None, duration=None):
    if kind == "failed":
        err = ""
        if exc is not None:
            err = _scrub(exc.stderr or str(exc))
            err = re.sub(r"\s+", " ", err).strip()[:_MAX_ERR]
        print("publish copy=%s status=failed rc=%d err=%s" % (
            copy_name, exc.returncode if exc is not None else "?",
            err), flush=True)
        return
    dur = "%.1fs" % (duration or 0.0)
    files = stats.get("files", 0) if stats else 0
    total = stats.get("total", 0) if stats else 0
    label = "%d/%d" % (files, total) if total else str(files)
    print("publish copy=%s status=ok files=%s duration=%s" % (
        copy_name, label, dur), flush=True)


def run_copy(name: str, args: list[str], dry_run: bool) -> int:
    cmd = rclone_base() + list(args)
    if dry_run:
        cmd.append("--dry-run")
    cmd += _TRANSFERS + _STATS
    t0 = time.monotonic()
    try:
        res = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            stdin=subprocess.DEVNULL)
    except OSError as exc:  # rclone introuvable : panne franche
        print("publish copy=%s status=failed rc=127 err=%s" % (
            name, str(exc)[:_MAX_ERR]), flush=True)
        return 1
    duration = time.monotonic() - t0
    if res.returncode != 0:
        err = _scrub(res.stderr or "")
        err = re.sub(r"\s+", " ", err).strip()[:_MAX_ERR]
        print("publish copy=%s status=failed rc=%d err=%s" % (
            name, res.returncode, err), flush=True)
        return res.returncode
    stats = parse_stats(res.stderr)
    _log(name, "ok", stats=stats, duration=duration)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Publication ConvIA vers le RAG")
    ap.add_argument("--dry-run", action="store_true",
                    help="aucune copie (validation/benchmark)")
    args = ap.parse_args()
    t0 = time.monotonic()
    # Copie 1 puis copie 2 ; on s'arrete a la premiere erreur (alerte utile,
    # pas de demi-publication silencieuse).
    rc = run_copy("root", ROOT_COPY, args.dry_run)
    if rc == 0:
        rc = run_copy("traite", TRAITE_COPY, args.dry_run)
    print("publish copies=2 status=%s duration=%.1fs" % (
        "success" if rc == 0 else "failed", time.monotonic() - t0), flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
