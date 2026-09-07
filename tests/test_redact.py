"""Tests de la passe de redaction des secrets.

Aucun secret reel n'apparait ici : les valeurs sont fabriquees pour avoir la FORME
d'un secret (prefixe + longueur), jamais la valeur d'une cle existante.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from redact import count, marker, redact  # noqa: E402
from sanitizer import VERSION, sanitize  # noqa: E402

B64 = "ZZZZzzzz0000111122223333444455556666777788889999aaaabbbbccccddd"

CASES = [
    ("google_api_key", "AQ." + "Xy9_" * 10),
    ("google_api_key", "AIza" + "B" * 35),
    ("google_oauth_token", "ya29." + "c" * 40),
    ("anthropic_api_key", "sk-ant-" + "d" * 40),
    ("openai_api_key", "sk-" + "e" * 40),
    ("github_token", "ghp_" + "f" * 36),
    ("github_token", "gho_" + "g" * 36),
    ("aws_access_key_id", "AKIA" + "H" * 16),
    ("slack_token", "xoxb-" + "1234567890-abcdefghij"),
    ("telegram_bot_token", "123456789:AA" + "k" * 33),
]


@pytest.mark.parametrize("family,secret", CASES)
def test_chaque_famille_est_redigee(family, secret):
    src = "avant %s apres" % secret
    out = redact(src)
    assert secret not in out, "le secret survit pour %s" % family
    assert marker(family) in out
    assert out == "avant %s apres" % marker(family), "structure alteree"


def test_ordre_anthropic_avant_openai():
    """sk-ant- est un cas particulier de sk- : il doit garder sa propre etiquette."""
    secret = "sk-ant-" + "z" * 40
    out = redact(secret)
    assert out == marker("anthropic_api_key")


def test_bloc_pem_reel_est_redige():
    src = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        + B64 + "\n" + B64 + "\n" + B64 + "\n"
        + "-----END OPENSSH PRIVATE KEY-----"
    )
    out = redact(src)
    assert out == marker("private_key_pem")
    assert B64 not in out


def test_placeholder_pem_n_est_PAS_redige():
    """Le corpus contient des placeholders de documentation : en-tete PEM sans
    corps base64. Les rediger serait un faux positif."""
    for body in ("...", "<votre cle privee>", "YOUR_KEY_HERE", "[snip]"):
        src = ("-----BEGIN PRIVATE KEY-----\n%s\n-----END PRIVATE KEY-----" % body)
        assert redact(src) == src, "faux positif sur le placeholder %r" % body


def test_prose_mentionnant_une_cle_privee_intacte():
    src = "Le fichier commence par -----BEGIN PRIVATE KEY----- puis le corps."
    assert redact(src) == src


def test_idempotence_sur_contenu_deja_redige():
    src = "cle: %s et %s" % (CASES[0][1], CASES[5][1])
    once = redact(src)
    assert redact(once) == once
    assert redact(redact(once)) == once


def test_marqueur_ne_redeclenche_aucun_motif():
    for family, _ in CASES:
        assert count(marker(family)) == {}


def test_statistiques_par_famille():
    stats = {}
    redact("%s %s %s" % (CASES[0][1], CASES[1][1], CASES[7][1]), stats)
    assert stats["redacted_google_api_key"] == 2
    assert stats["redacted_aws_access_key_id"] == 1


def test_texte_sans_secret_inchange():
    src = "Une phrase normale avec sk et AQ mais rien de sensible.\nAIza trop court."
    assert redact(src) == src


def test_redaction_appliquee_aux_zones_protegees_par_le_sanitizer():
    """Exception assumee : un prompt utilisateur est normalement conserve
    bit-a-bit, mais un secret doit disparaitre meme la."""
    secret = CASES[0][1]
    src = (
        "---\ntitle: essai\n---\n\n"
        "## \U0001f464 User\n\n"
        "Utilise la cle %s pour appeler l'API.\n" % secret
    )
    out = sanitize(src)
    assert secret not in out
    assert marker("google_api_key") in out
    assert "pour appeler l'API." in out, "le reste du prompt doit survivre"


def test_version_incrementee_pour_forcer_le_repassage():
    """Le marqueur convia_sanitized: <version> fait sauter les fichiers deja
    traites. Sans incrementation, le corpus ne serait jamais redige."""
    assert VERSION >= 2

def test_identifiant_hyphene_nest_pas_une_cle_openai():
    """`sk-...` avec des tirets est un nom de service, pas un secret.
    Cas reel : `sk-notification-service` apparait dans le corpus."""
    for ident in ("sk-notification-service-production",
                  "sk-learn-model-training-pipeline",
                  "sk-a-b-c-d-e-f-g-h-i-j-k-l-m-n-o-p"):
        assert redact(ident) == ident, "faux positif sur %r" % ident


def test_cle_openai_projet_est_redigee():
    secret = "sk-proj-" + "A1b2C3d4E5f6G7h8I9j0"
    assert redact(secret) == marker("openai_api_key")
