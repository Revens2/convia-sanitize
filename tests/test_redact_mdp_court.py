"""Regression : un mot de passe court, en fin de phrase, doit etre caviarde.

Mesure du 2026-09-06. Le mot de passe SSH d un VPS -- quatre chiffres -- est
ressorti EN CLAIR de la sanitization et se trouve aujourd hui dans huit fichiers
`raw/assets/ConvIA/**` deja publies sur Google Drive. Deux causes cumulees :

  1. `_valide_mdp` exigeait `min_len=6`. Le mot-cle « mot de passe : » est
     pourtant deja un qualificatif fort ; la longueur faisait un second tri qui
     ne servait qu a laisser passer les mots de passe faibles -- exactement ceux
     qu on veut le plus cacher.
  2. le groupe capture allait jusqu au prochain espace, donc `4051).` avec la
     parenthese fermante de la phrase. Cette parenthese est dans `_PONCT_CODE`,
     donc la valeur etait rejetee comme « du code ».

Les deux cas negatifs verifient qu on n a pas achete cette detection au prix de
faux positifs sur la prose et le code -- c est ce compromis, pas la detection
seule, qui rend la regle tenable.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import redact  # noqa: E402


def _red(texte):
    r = redact.redact(texte)
    return r[0] if isinstance(r, tuple) else r


def test_mdp_quatre_chiffres_en_fin_de_phrase():
    out = _red("Acces SSH : Utilisateur oui (Mot de passe : 4051).")
    assert "4051" not in out
    assert "[REDACTED:password_kv]" in out
    # Le contexte reste lisible : seule la valeur part.
    assert out.endswith(").")


def test_mdp_quatre_chiffres_apres_identite():
    out = _red("oui@10.0.0.9 mdp 4051")
    assert "4051" not in out


def test_prose_sans_valeur_non_caviardee():
    for texte in ("Choisis un mot de passe fort",
                  "changement de mot de passe : route backend securisee"):
        assert "[REDACTED" not in _red(texte), texte


def test_code_non_caviarde():
    assert "[REDACTED" not in _red("password: z.string().min(8)")


def test_idempotent():
    une = _red("Mot de passe SSH : 4051")
    assert _red(une) == une
