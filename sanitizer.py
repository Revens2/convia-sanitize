"""Sanitizer de conversations IA exportees par `convia`.

Regles (plan.md 3bis) :
  - blocs <details><summary>1f4ad Reflexion ...</summary> : supprimes
  - blocs <details><summary>1f527 <outil></summary> : nom + arguments conserves
    integralement ; resultat en ERREUR conserve integralement ; resultat REUSSI
    tronque a TRUNCATE_LINES lignes avec mention du nombre de lignes omises
  - <system-reminder>...</system-reminder>, {{ CHECKPOINT ... }},
    lignes "REMINDER: Do not ...", barres de progression, sequences ANSI : supprimes
  - lignes vides consecutives reduites a une

Invariants :
  - le frontmatter YAML reste parsable et intact (un seul champ ajoute :
    convia_sanitized)
  - les messages utilisateur ne sont jamais alteres
  - toute cloture de bloc de code est preservee ou reemise avec une longueur de
    fence superieure a tout backtick contenu dans le texte conserve
  - sanitize(sanitize(x)) == sanitize(x)

Le corpus est une ENTREE HOSTILE : aucun eval/exec/interpolation shell ici.
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

# v2 (2026-08-13) : ajout de la passe de redaction des secrets (redact.py).
# L increment invalide le marqueur convia_sanitized: 1 et force un repassage
# complet du corpus - c est voulu, sans quoi les secrets deja presents
# resteraient en clair.
VERSION = 2
from redact import redact  # noqa: E402

MARKER_KEY = "convia_sanitized"
TRUNCATE_LINES = 5

_OMIT_RE = re.compile(r"^\.\.\. \[convia : (\d+) lignes? omises?\]$")
# Caracteres de controle C0, tabulation et saut de ligne exceptes. Un NUL fait
# echouer execve en aval (AGY est appele en sous-processus) et aucun de ces
# octets ne porte d'information utile a l'analyse d'une conversation.
_C0_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ANSI_RE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_CHECKPOINT_RE = re.compile(r"\{\{\s*CHECKPOINT.*?\}\}", re.DOTALL)
_REMINDER_LINE_RE = re.compile(r"^\s*REMINDER:\s*Do not\b")
_PROGRESS_RE = re.compile(r"[█▓▒░]")
_SPINNER_RE = re.compile(r"^\s*[-\\|/]\s*$")
_FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")
_DETAILS_OPEN_RE = re.compile(r"<details\b", re.IGNORECASE)
_DETAILS_CLOSE_RE = re.compile(r"</details\s*>", re.IGNORECASE)
_SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.IGNORECASE | re.DOTALL)
_RESULT_RE = re.compile(r"^\*\*Résultat\*\*\s*:?\s*$")
_ERROR_RE = re.compile(r"^\*\*Erreur\*\*\s*:?\s*$")
_SR_OPEN = "<system-reminder>"
_SR_CLOSE = "</system-reminder>"
_SR_INLINE_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_MARKER_FM_RE = re.compile(r"^%s\s*:" % MARKER_KEY)

THINKING_MARK = "\U0001f4ad"  # 1f4ad
TOOL_MARK = "\U0001f527"  # 1f527


def _new_stats() -> Dict[str, int]:
    return {
        "thinking": 0,
        "tool_result": 0,
        "reminders": 0,
        "cosmetic": 0,
    }


def frontmatter_version(text: str) -> int | None:
    """Version du sanitizer inscrite dans le frontmatter, ou None."""
    fm, _, _ = _split_frontmatter(text.splitlines())
    for line in fm:
        if _MARKER_FM_RE.match(line):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def _split_frontmatter(lines: List[str]) -> Tuple[List[str], List[str], bool]:
    """Retourne (lignes_interieures_du_frontmatter, corps, present)."""
    if not lines or lines[0].strip() != "---":
        return [], lines, False
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            return lines[1:i], lines[i + 1 :], True
    return [], lines, False


def _render_frontmatter(inner: List[str]) -> List[str]:
    out = [ln for ln in inner if not _MARKER_FM_RE.match(ln)]
    out.append("%s: %d" % (MARKER_KEY, VERSION))
    return ["---"] + out + ["---"]


def _max_backtick_run(lines: List[str]) -> int:
    best = 0
    for ln in lines:
        for m in re.finditer(r"`+", ln):
            best = max(best, len(m.group(0)))
    return best


def _find_details_end(lines: List[str], start: int) -> int:
    """Index de la ligne fermant le <details> ouvert en `start` (inclus)."""
    depth = 0
    for i in range(start, len(lines)):
        if _DETAILS_OPEN_RE.match(lines[i]):
            depth += 1
        if _DETAILS_CLOSE_RE.match(lines[i]):
            depth -= 1
        if depth <= 0:
            return i
    return len(lines) - 1  # non ferme : on prend tout, la fermeture sera reemise


def _clean_inline(line: str, stats: Dict[str, int]) -> str | None:
    """Nettoyage cosmetique d'une ligne hors bloc de code. None = ligne supprimee."""
    original = line
    if "\r" in line:
        line = line.split("\r")[-1]
    line = _ANSI_RE.sub("", line)
    line = _C0_RE.sub("", line)
    line = _CHECKPOINT_RE.sub("", line)
    if _REMINDER_LINE_RE.match(line):
        stats["reminders"] += len(original) + 1
        return None
    if _PROGRESS_RE.search(line) or (_SPINNER_RE.match(line) and line.strip()):
        stats["cosmetic"] += len(original) + 1
        return None
    if line != original:
        stats["cosmetic"] += len(original) - len(line)
    return line


def _truncate_result(block: List[str], stats: Dict[str, int]) -> List[str]:
    """Tronque le resultat d'un tool call reussi. Idempotent."""
    idx = None
    for i, ln in enumerate(block):
        if _ERROR_RE.match(ln.strip()):
            return block  # erreur : jamais tronquee
        if _RESULT_RE.match(ln.strip()) and idx is None:
            idx = i
    if idx is None:
        return block

    # ouverture du bloc de code du resultat
    open_i = None
    for i in range(idx + 1, len(block)):
        if block[i].strip() == "":
            continue
        m = _FENCE_RE.match(block[i])
        if m:
            open_i = i
            fence = m.group(2)
        break
    if open_i is None:
        return block

    # fermeture : derniere ligne du bloc constituee d'une fence >= a l'ouverture
    close_i = None
    for i in range(len(block) - 1, open_i, -1):
        s = block[i].strip()
        if s and set(s) == {fence[0]} and len(s) >= len(fence):
            close_i = i
            break
    if close_i is None:
        return block

    content = block[open_i + 1 : close_i]
    if content and _OMIT_RE.match(content[-1].strip()):
        return block  # deja tronque : idempotence
    if len(content) <= TRUNCATE_LINES:
        return block

    kept = content[:TRUNCATE_LINES]
    omitted = len(content) - TRUNCATE_LINES
    marker = "... [convia : %d ligne%s omise%s]" % (
        omitted,
        "s" if omitted > 1 else "",
        "s" if omitted > 1 else "",
    )
    body = kept + [marker]
    width = max(3, _max_backtick_run(body) + 1)
    new_fence = "`" * width
    stats["tool_result"] += sum(len(x) + 1 for x in content[TRUNCATE_LINES:]) - (
        len(marker) + 1
    )
    return block[:open_i] + [new_fence] + body + [new_fence] + block[close_i + 1 :]


def _process_details(block: List[str], stats: Dict[str, int]) -> List[str]:
    joined = "\n".join(block)
    m = _SUMMARY_RE.search(joined)
    summary = m.group(1) if m else ""
    if THINKING_MARK in summary:
        stats["thinking"] += len(joined) + 1
        return []
    if not _DETAILS_CLOSE_RE.match(block[-1] if block else ""):
        block = block + ["</details>"]  # integrite : toujours refermer
    return _truncate_result(block, stats)


def sanitize(text: str, stats: Dict[str, int] | None = None) -> str:
    if stats is None:
        stats = _new_stats()
    # 0. REDACTION DES SECRETS - avant tout le reste, sur le texte integral.
    #    Seule exception au perimetre de conservation : elle s applique aussi aux
    #    prompts utilisateur et aux arguments de tool calls. Un secret ne survit
    #    que dans .raw/, dont c est le role.
    text = redact(text, stats)
    newline = "\r\n" if "\r\n" in text[:4096] else "\n"
    trailing = text.endswith(("\n", "\r"))
    lines = text.splitlines()

    fm_inner, body, has_fm = _split_frontmatter(lines)

    # 1. suppression des regions <system-reminder> (multi-lignes)
    joined = "\n".join(body)
    if _SR_OPEN in joined:
        before = len(joined)
        joined = _SR_INLINE_RE.sub("", joined)
        # region non fermee : on coupe jusqu'a la fin de ligne
        stats["reminders"] += before - len(joined)
        body = joined.split("\n")

    out: List[str] = []
    i = 0
    fence: str | None = None
    skip_sr = False
    n = len(body)
    while i < n:
        line = body[i]

        if skip_sr:
            if _SR_CLOSE in line:
                skip_sr = False
            stats["reminders"] += len(line) + 1
            i += 1
            continue

        if fence is not None:  # bloc de code de premier niveau : verbatim
            out.append(line)
            m = _FENCE_RE.match(line)
            if m and m.group(3).strip() == "" and m.group(2)[0] == fence[0] and len(
                m.group(2)
            ) >= len(fence):
                fence = None
            i += 1
            continue

        m = _FENCE_RE.match(line)
        if m:
            fence = m.group(2)
            out.append(line)
            i += 1
            continue

        if _DETAILS_OPEN_RE.match(line):
            end = _find_details_end(body, i)
            out.extend(_process_details(body[i : end + 1], stats))
            i = end + 1
            continue

        if _SR_OPEN in line:
            skip_sr = _SR_CLOSE not in line
            stats["reminders"] += len(line) + 1
            i += 1
            continue

        cleaned = _clean_inline(line, stats)
        if cleaned is None:
            i += 1
            continue
        out.append(cleaned)
        i += 1

    if fence is not None:  # bloc de code jamais referme : on le referme
        out.append(fence)

    # 2. lignes vides consecutives -> une seule (hors blocs de code)
    collapsed: List[str] = []
    fence = None
    for line in out:
        m = _FENCE_RE.match(line)
        if fence is None and m:
            fence = m.group(2)
        elif fence is not None and m and m.group(2)[0] == fence[0] and len(
            m.group(2)
        ) >= len(fence) and m.group(3).strip() == "":
            fence = None
        if fence is None and line.strip() == "":
            if collapsed and collapsed[-1].strip() == "":
                stats["cosmetic"] += len(line) + 1
                continue
            # ligne conservee telle quelle : une ligne d'espaces d'un message
            # utilisateur ne doit pas etre reecrite (integrite bit-a-bit)
            collapsed.append(line)
            continue
        collapsed.append(line)
    while collapsed and collapsed[-1].strip() == "":
        collapsed.pop()

    head = _render_frontmatter(fm_inner) if has_fm else _render_frontmatter([])
    result = newline.join(head + collapsed)
    if trailing or True:
        result += newline
    return result


def sanitize_with_stats(text: str) -> Tuple[str, Dict[str, int]]:
    stats = _new_stats()
    return sanitize(text, stats), stats
