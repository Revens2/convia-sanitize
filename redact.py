"""Redaction des secrets dans les conversations, en amont de toute sortie.

POURQUOI CETTE PASSE EXISTE
Le corpus contient de vrais secrets : les agents manipulent des cles, et les
transcriptions les recopient. Mesure du 2026-08-13 : deux cles AI Studio completes
(53 caracteres) dans deux fichiers distincts de `Claude-CLI/`. Sans cette passe, le
job d'analyse enverrait ces cles a l'API Gemini, puis les recopierait dans
`Obsidian Vault/raw/assets` pour ingestion RAG.

PORTEE - EXCEPTION ASSUMEE AU PERIMETRE DE CONSERVATION
Cette redaction s'applique a TOUT le contenu, y compris aux zones que le sanitizer
protege par ailleurs : frontmatter, messages utilisateur, arguments de tool calls.
C'est la seule exception au principe "ne jamais alterer un prompt utilisateur",
et elle prime sur lui. Un secret ne doit exister nulle part ailleurs que dans
`.raw/`, dont c'est precisement le role d'etre fidele a l'original.

IRREVERSIBILITE
Le remplacement est un marqueur fixe, sans longueur ni empreinte du secret :
on ne doit rien pouvoir reconstruire depuis le fichier redige.
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

# L'ordre compte : un motif plus specifique doit passer AVANT un motif plus large.
# `sk-ant-...` serait sinon avale par le `sk-...` generique et mal etiquete.
SECRET_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    # --- bloc PEM : en-tete, CORPS base64, pied. Le corps est obligatoire, sinon
    # on re-declenche sur les placeholders de documentation du type
    #   -----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----
    # qui sont de la prose, pas des secrets.
    ("private_key_pem", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
        r"[ \t]*\r?\n"
        r"(?:[A-Za-z0-9+/=]{16,}[ \t]*\r?\n){2,}"
        r"-----END [A-Z ]*PRIVATE KEY-----")),
    # --- Google
    ("google_api_key", re.compile(r"\bAQ\.[A-Za-z0-9_-]{30,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),
    ("google_oauth_token", re.compile(r"\bya29\.[A-Za-z0-9_-]{20,}")),
    # --- Anthropic AVANT OpenAI : `sk-ant-` est un cas particulier de `sk-`
    ("anthropic_api_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    # Pas de tiret ni de souligne dans le corps : "sk-notification-service-prod"
    # est un identifiant ordinaire, pas une cle. Cas reel rencontre dans le corpus.
    ("openai_api_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{20,}")),
    # --- divers
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("aws_access_key_id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("telegram_bot_token", re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}")),
]

STAT_PREFIX = "redacted_"


def marker(family: str) -> str:
    return "[REDACTED:%s]" % family


# Un marquage deja pose ne doit jamais etre re-marque : la fonction est idempotente
# parce qu'aucun `marker()` ne peut satisfaire l'un des motifs ci-dessus (aucun ne
# contient `AQ.`, `AIza`, `sk-`, `gh?_`, `AKIA`, `xox`, ni de suite chiffres+`:AA`).
_MARKER_RE = re.compile(r"\[REDACTED:[a-z_]+\]")


# =====================================================================
# ETAGE 2 - SECRETS GENERIQUES (ajoute le 2026-08-22)
# =====================================================================
# L'etage 1 ci-dessus ne reconnait que des jetons de fournisseur a forte entropie
# (`AIza`, `sk-`, `ghp_`, `AKIA`...). Il est aveugle aux MOTS DE PASSE, qui n'ont
# aucune forme distinctive. Mesure du 2026-08-22 : un lot de 6 conversations
# contenant un mot de passe en clair est ressorti avec `redactions=0`.
#
# LE PROBLEME N'EST PAS LA DETECTION, C'EST LE FAUX POSITIF
# ---------------------------------------------------------
# Releve sur le vault (5 566 notes, 990 849 lignes) : 1 955 occurrences du
# mot-cle `mot de passe|mdp|password|passwd`. L'ecrasante majorite est de la
# PROSE ("Change ton mot de passe : Choisis un mot de passe fort") ou du CODE
# ("password: z.string().min(8)", "credentials['password']"). Un motif
# `password\s*[:=]\s*\S+` caviarderait des centaines de passages legitimes.
# Un sanitizer qui mutile le texte normal est desactive par son proprietaire
# dans la semaine : le taux de faux positifs prime sur la couverture.
#
# La discrimination porte donc sur la VALEUR, pas sur le mot-cle :
#   - la prose francaise/anglaise est faite de LETTRES SEULES
#     -> on exige un chiffre ou un symbole de mot de passe ;
#   - le code porte de la ponctuation de langage (`(`, `[`, `'`, `"`, `.`)
#     -> on rejette ces formes, y compris l'identifiant pointe `z.string` ;
#   - les references de variable (`$VAR`, `${VAR}`, `%VAR%`) ne sont pas des
#     secrets -> rejetees.
#
# Les formes COMMANDE (`sshpass -p`, `echo ... | sudo -S`, `-pw`) n'ont, elles,
# pratiquement pas de faux positif : la valeur y est un argument, jamais de la
# prose. Elles sont donc validees plus largement.
#
# AUCUNE VALEUR DE SECRET N'APPARAIT DANS CE FICHIER : uniquement des motifs.

# Ponctuation de langage de programmation. Sa presence signale du code, pas un
# mot de passe : aucun mot de passe realiste ne contient une parenthese ou un
# guillemet non echappe dans une transcription.
_PONCT_CODE = set("()[]{}<>'\"`;,")

# Symboles courants dans un mot de passe. `.` en est volontairement ABSENT :
# c'est le separateur de l'identifiant pointe (`z.string`, `os.environ`), la
# premiere source de faux positifs du corpus.
_SYMBOLES_MDP = set("!@#$%^&*_+=?~-/\\|:")

# Valeurs qui NOMMENT un secret sans en etre un.
_PLACEHOLDERS = {
    "password", "passwd", "motdepasse", "mot_de_passe", "changeme",
    "yourpassword", "votre_mot_de_passe", "secret", "xxxxxx", "******",
    "none", "null", "vide", "empty", "todo", "redacted",
}


def _est_reference_variable(v: str) -> bool:
    return v.startswith(("$", "%", "{", "<")) or v.endswith(("%", "}", ">"))


def _est_chemin(v: str) -> bool:
    """`/srv/docs`, `C:/Users/...`, `./rel/at.if` : un chemin n'est pas un secret.

    Mesure du 2026-08-22 : la ligne de journal
    `sudo: juliann : PWD=/srv/docs ; USER=root` etait le seul faux positif du
    corpus. `PWD` y designe le repertoire courant.
    """
    return v.startswith(("/", "./", "../", "~/")) or "/" in v.strip("/")


def _est_identifiant_pointe(v: str) -> bool:
    """`z.string`, `os.environ`, `mt5.login` : du code, pas un secret."""
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+", v))


def _valeur_plausible(v: str, *, min_len: int, exiger_complexite: bool) -> bool:
    """Une valeur candidate est-elle credible comme secret ?"""
    if not v or len(v) < min_len or len(v) > 128:
        return False
    if v.startswith("[REDACTED:"):
        return False
    if v.strip(".,;:!?").lower() in _PLACEHOLDERS:
        return False
    if _est_reference_variable(v) or _est_identifiant_pointe(v):
        return False
    if _est_chemin(v):
        return False
    if any(c in _PONCT_CODE for c in v):
        return False
    if not exiger_complexite:
        return True
    # Le discriminant central : la prose n'a ni chiffre ni symbole de mot de passe.
    return any(c.isdigit() for c in v) or any(c in _SYMBOLES_MDP for c in v)


def _valide_mdp(v: str) -> bool:
    # min_len=4, pas 6. Mesure du 2026-09-06 : le mot de passe SSH d un VPS,
    # quatre chiffres, est ressorti EN CLAIR de la sanitization et se trouve
    # aujourd hui dans huit fichiers raw/assets/ConvIA deja publies sur Drive.
    # Le mot-cle "mot de passe :" est deja un qualificatif fort ; la longueur
    # n a pas a faire un second tri. La complexite reste exigee, donc
    # "mot de passe : fort" n est toujours pas caviarde.
    return _valeur_plausible(v, min_len=4, exiger_complexite=True)


def _valide_commande(v: str) -> bool:
    # Forme commande : la valeur est un argument, le contexte suffit a qualifier.
    # On reste tolerant sur la complexite mais on refuse les references et le code.
    return _valeur_plausible(v.strip("'\""), min_len=4, exiger_complexite=False)


def _valide_bearer(v: str) -> bool:
    # Un vrai jeton porteur est long. Les 32 occurrences de 14 a 19 caracteres
    # relevees dans le vault sont des noms de variable, pas des jetons.
    return _valeur_plausible(v, min_len=20, exiger_complexite=False)


# (famille, motif avec groupe nomme `v`, validateur)
# Le groupe `v` est le SEUL fragment remplace : le contexte reste lisible, ce qui
# preserve la valeur documentaire de la conversation.
SECRET_RULES = [
    ("password_commande", re.compile(
        r"(?i)\bsshpass\s+-p\s*(?P<v>[^\s]{4,128})"), _valide_commande),
    ("password_commande", re.compile(
        r"(?i)\b(?:plink|psftp|pscp)\b[^\n]{0,120}?\s-pw\s+(?P<v>[^\s]{4,128})"),
     _valide_commande),
    ("password_commande", re.compile(
        r"(?i)\becho\s+(?P<v>[^\s|]{4,128})\s*\|\s*sudo\s+-S\b"), _valide_commande),
    ("password_kv", re.compile(
        r"(?i)\b(?:mot\s+de\s+passe|mdp|password|passwd)\b"
        # Qualificatif optionnel entre le mot-cle et le separateur :
        # "Mot de passe SSH :", "password root =". Borne a 2 jetons courts --
        # au-dela on est dans de la prose, pas dans une etiquette de champ.
        r"(?:[^\S\n]{1,2}[A-Za-z0-9_.\-]{1,12}){0,2}"
        r"[^\S\n]{0,4}[:=][^\S\n]{0,4}(?P<v>[^\s]{4,128}?)(?=[)\].,;!?]*(?:\s|$))"), _valide_mdp),
    ("password_kv", re.compile(
        r"(?i)\b[\w.+-]+@[\w.\-]+\s+(?:mdp|mot\s+de\s+passe|pw)\b"
        r"[^\S\n]{0,4}:?[^\S\n]{0,4}(?P<v>[^\s]{4,128}?)(?=[)\].,;!?]*(?:\s|$))"), _valide_mdp),
    ("bearer_token", re.compile(
        r"(?i)\bAuthorization\s*:\s*Bearer\s+(?P<v>[^\s]{20,256})"), _valide_bearer),
]


def _appliquer_regles(text, stats):
    """Applique l'etage 2. Ne remplace que le groupe `v`, apres validation."""
    for famille, rx, valide in SECRET_RULES:
        jeton = marker(famille)
        compteur = [0]

        def _sub(m, _f=famille, _j=jeton, _val=valide, _c=compteur):
            v = m.group("v")
            if not _val(v):
                return m.group(0)          # laisse le texte intact
            _c[0] += 1
            entier = m.group(0)
            d0 = m.start()
            debut, fin = m.span("v")
            return entier[: debut - d0] + _j + entier[fin - d0 :]

        text = rx.sub(_sub, text)
        if compteur[0]:
            cle = STAT_PREFIX + famille
            stats[cle] = stats.get(cle, 0) + compteur[0]
    return text


def redact(text: str, stats: Dict[str, int] | None = None) -> str:
    """Remplace tout secret reconnu par un marqueur stable et non reversible."""
    if stats is None:
        stats = {}
    for family, rx in SECRET_PATTERNS:
        repl = marker(family)
        text, n = rx.subn(repl, text)
        if n:
            key = STAT_PREFIX + family
            stats[key] = stats.get(key, 0) + n
    # Etage 2 APRES l'etage 1 : un jeton deja marque ne peut plus etre repris par
    # une regle generique, puisque `[REDACTED:...]` est refuse par la validation.
    text = _appliquer_regles(text, stats)
    return text


def count(text: str) -> Dict[str, int]:
    """Comptage sans modification, pour l'audit."""
    out: Dict[str, int] = {}
    scratch = text
    for family, rx in SECRET_PATTERNS:
        scratch, n = rx.subn(marker(family), scratch)
        if n:
            out[family] = out.get(family, 0) + n
    brut: Dict[str, int] = {}
    _appliquer_regles(scratch, brut)
    for cle, n in brut.items():
        famille = cle[len(STAT_PREFIX):] if cle.startswith(STAT_PREFIX) else cle
        out[famille] = out.get(famille, 0) + n
    return out
