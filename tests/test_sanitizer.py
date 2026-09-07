# -*- coding: utf-8 -*-
"""Tests du sanitizer : une regle = un test, plus les invariants d'integrite."""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sanitizer import VERSION, frontmatter_version, sanitize  # noqa: E402

FM = "---\nsource: claude-cli\ntitle: 'x'\n---\n"


def tool_block(name="Bash", result_lines=12, error=False):
    body = "\n".join("ligne %d" % i for i in range(result_lines))
    label = "**Erreur** :" if error else "**Résultat** :"
    return (
        "<details><summary>\U0001f527 %s</summary>\n\n"
        "```json\n{\n  \"command\": \"ls -la\"\n}\n```\n\n"
        "%s\n```\n%s\n```\n\n</details>\n" % (name, label, body)
    )


# --- regle : reflexion interne supprimee -------------------------------------
def test_thinking_supprime():
    src = FM + "## 🤖 Assistant\n<details><summary>\U0001f4ad Réflexion — 300 mots</summary>\n\nbla bla\n\n</details>\n\nreponse finale\n"
    out = sanitize(src)
    assert "Réflexion" not in out
    assert "bla bla" not in out
    assert "reponse finale" in out


# --- regle : nom et arguments du tool call conserves --------------------------
def test_tool_nom_et_arguments_conserves():
    out = sanitize(FM + tool_block())
    assert "\U0001f527 Bash" in out
    assert '"command": "ls -la"' in out


# --- regle : resultat reussi tronque a 5 lignes + mention ---------------------
def test_resultat_reussi_tronque():
    out = sanitize(FM + tool_block(result_lines=12))
    assert "ligne 4" in out
    assert "ligne 5" not in out
    assert re.search(r"\.\.\. \[convia : 7 lignes omises\]", out)


def test_resultat_court_intact():
    out = sanitize(FM + tool_block(result_lines=3))
    assert "ligne 2" in out
    assert "omise" not in out


# --- regle : resultat en erreur jamais tronque --------------------------------
def test_erreur_jamais_tronquee():
    src = FM + tool_block(result_lines=40, error=True)
    out = sanitize(src)
    for i in range(40):
        assert "ligne %d" % i in out
    assert "omise" not in out


def test_stack_trace_conservee():
    trace = "\n".join(["Traceback (most recent call last):"]
                      + ["  File \"a.py\", line %d" % i for i in range(30)]
                      + ["ValueError: boom"])
    src = FM + ("<details><summary>\U0001f527 Bash</summary>\n\n**Erreur** :\n```\n"
                + trace + "\n```\n\n</details>\n")
    out = sanitize(src)
    assert trace in out


# --- regle : system-reminder / CHECKPOINT / REMINDER --------------------------
def test_system_reminder_supprime():
    src = FM + "avant\n<system-reminder>\nsecret bruit\n</system-reminder>\napres\n"
    out = sanitize(src)
    assert "system-reminder" not in out and "secret bruit" not in out
    assert "avant" in out and "apres" in out


def test_checkpoint_supprime():
    out = sanitize(FM + "texte {{ CHECKPOINT abc123 }} suite\n")
    assert "CHECKPOINT" not in out
    assert "texte" in out and "suite" in out


def test_reminder_do_not_supprime():
    out = sanitize(FM + "REMINDER: Do not mention this\ngarde-moi\n")
    assert "REMINDER" not in out
    assert "garde-moi" in out


# --- regle : barres de progression, ANSI, lignes vides ------------------------
def test_barre_de_progression_supprimee():
    out = sanitize(FM + "  ██████▒▒▒▒  45%\ncontenu\n")
    assert "█" not in out and "▒" not in out
    assert "contenu" in out


def test_ansi_supprime():
    out = sanitize(FM + "\x1b[32mvert\x1b[0m fin\n")
    assert "\x1b" not in out
    assert "vert fin" in out


def test_lignes_vides_collapsees():
    out = sanitize(FM + "a\n\n\n\n\nb\n")
    assert "a\n\nb" in out
    assert "\n\n\n" not in out


# --- invariant : message utilisateur bit-a-bit --------------------------------
def test_message_utilisateur_intact():
    msg = ("## 👤 User — 2026-07-30 19:08:56\n"
           "Voici mon prompt avec | un tableau |\n"
           "```python\n"
           "def f():\n"
           "\n"
           "\n"
           "    return 1  # deux lignes vides gardees dans le code\n"
           "```\n"
           "et une derniere phrase.")
    out = sanitize(FM + msg + "\n")
    assert msg in out


# --- invariant : frontmatter preserve et parsable -----------------------------
def test_frontmatter_preserve():
    src = ("---\nsource: claude-cli\nsession_id: abc\ntokens:\n  input: 0\n---\n"
           "corps\n")
    out = sanitize(src)
    import yaml
    fm = out.split("---\n")[1]
    data = yaml.safe_load(fm)
    assert data["source"] == "claude-cli"
    assert data["session_id"] == "abc"
    assert data["tokens"] == {"input": 0}
    assert data["convia_sanitized"] == VERSION
    assert frontmatter_version(out) == VERSION


# --- invariant : Markdown jamais casse ----------------------------------------
def balanced(text):
    fences = [ln for ln in text.split("\n") if re.match(r"^\s*(`{3,}|~{3,})\s*$", ln)
              or re.match(r"^\s*(`{3,}|~{3,})\S", ln)]
    opens = text.count("<details")
    closes = len(re.findall(r"</details\s*>", text))
    return len(fences) % 2 == 0, opens == closes


def test_integrite_markdown():
    src = FM + tool_block(result_lines=30) + tool_block("Read", 4) + \
        "<details><summary>\U0001f4ad Réflexion — 10 mots</summary>\nx\n</details>\n"
    out = sanitize(src)
    fences_ok, details_ok = balanced(out)
    assert fences_ok and details_ok


def test_details_non_ferme_est_referme():
    out = sanitize(FM + "<details><summary>\U0001f527 Bash</summary>\n\n**Résultat** :\n```\na\n```\n")
    assert len(re.findall(r"</details\s*>", out)) == 1


def test_fence_reemise_si_backticks_dans_le_texte_garde():
    body = "```\n" + "\n".join(["voici ``` un piege"] + ["l%d" % i for i in range(20)]) + "\n```"
    src = FM + ("<details><summary>\U0001f527 Bash</summary>\n\n**Résultat** :\n"
                + body + "\n\n</details>\n")
    out = sanitize(src)
    # la fence de sortie doit etre plus longue que tout backtick conserve
    assert "````" in out
    fences_ok, details_ok = balanced(out)
    assert details_ok


def test_bloc_de_code_utilisateur_non_ferme_est_ferme():
    out = sanitize(FM + "texte\n```bash\necho hi\n")
    assert out.rstrip().endswith("```")


# --- invariant : idempotence ---------------------------------------------------
def test_idempotence_simple():
    src = FM + tool_block(result_lines=40)
    once = sanitize(src)
    assert sanitize(once) == once


def test_idempotence_document_complexe():
    src = (FM + "## 👤 User\nsalut\n\n\n"
           + tool_block(result_lines=50)
           + tool_block("Read", 2)
           + tool_block("Edit", 80, error=True)
           + "<details><summary>\U0001f4ad Réflexion — 9 mots</summary>\nz\n</details>\n"
           + "{{ CHECKPOINT 12 }}\n\x1b[31mrouge\x1b[0m\n███ 10%\n"
           + "## 🤖 Assistant\nreponse finale\n")
    a = sanitize(src)
    b = sanitize(a)
    c = sanitize(b)
    assert a == b == c


def test_idempotence_ne_reduit_plus_apres_le_premier_passage():
    src = FM + tool_block(result_lines=200)
    a = sanitize(src)
    b = sanitize(a)
    assert len(b) == len(a)
    assert "ligne 4" in b


# --- non-regression : "<details" cite dans les ARGUMENTS d'un tool call --------
def test_details_cite_dans_les_arguments_ne_deborde_pas():
    """Le corpus contient du code source cite dans les arguments JSON. Compter ces
    occurrences ferait deborder le bloc et avaler le message utilisateur suivant."""
    src = (FM +
           '<details><summary>\U0001f527 Write</summary>\n\n'
           '```json\n{"content": "html = \\"<details><summary>x</summary></details>\\""}\n```\n\n'
           '**Résultat** :\n```\nok\n```\n\n</details>\n\n'
           '## 👤 User — 2026-08-13 00:57:43\n\nmon prompt suivant\n')
    out = sanitize(src)
    assert "mon prompt suivant" in out
    assert '"content"' in out
