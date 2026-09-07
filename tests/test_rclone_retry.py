# -*- coding: utf-8 -*-
"""Tests du retry borne des lectures rclone (runner.py).

Incident du 2026-09-07 : `rclone lsjson` a echoue transitoirement a deux
reprises (11:20:52Z, 13:18:50Z) et chaque echec a declenche une FAUSSE alerte
Telegram, alors que le tick suivant reussissait. Le correctif :

  - reessaie les LISTINGS (lsjson, lsf) avec un budget borne (3 tentatives,
    backoff 10 s puis 60 s) et journalise chaque echec de facon compacte et
    redactee ;
  - ne reessaie PAS les ECRITURES (`copyto`) : une seule tentative, echec reel
    propage ;
  - une erreur clairement PERMANENTE ne consomme pas le budget de retry ;
  - un echec definitif reste un echec (exception propagee -> sortie != 0).

Aucun appel reseau : `subprocess.run` est remplace par un double.
"""
import os
import subprocess
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import runner  # noqa: E402

TRANSIENT_ERR = "googleapi: Error 429: User Rate Limit Exceeded, userRateLimitExceeded"


def _ok(stdout="", stderr=""):
    return types.SimpleNamespace(stdout=stdout, stderr=stderr, returncode=0)


def _install(monkeypatch, script):
    """Remplace subprocess.run par un double qui rejoue `script` (liste de
    dicts: rc / stdout / stderr), puis epuise en succes vide."""
    calls = []
    sleeps = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        step = script.pop(0) if script else {}
        if step.get("rc"):
            raise subprocess.CalledProcessError(step["rc"], cmd,
                                                stderr=step.get("stderr", ""))
        return _ok(step.get("stdout", ""), step.get("stderr", ""))

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    monkeypatch.setattr(runner.time, "sleep", sleeps.append)
    return calls, sleeps


# --- lecture reussie ---------------------------------------------------------
def test_lsjson_succes_une_seule_tentative(monkeypatch):
    stdout = '[{"Path": "dir/a.md", "Size": 10, "ModTime": "t"}]'
    calls, sleeps = _install(monkeypatch, [{"rc": 0, "stdout": stdout}])
    out = runner.list_remote()
    assert out == [{"Path": "dir/a.md", "Size": 10, "ModTime": "t"}]
    assert len(calls) == 1
    assert calls[0][0] == "rclone" and "lsjson" in calls[0]
    assert sleeps == []


# --- erreur transitoire puis succes ------------------------------------------
def test_lsjson_transitoire_puis_succes(monkeypatch):
    calls, sleeps = _install(monkeypatch, [
        {"rc": 1, "stderr": TRANSIENT_ERR},
        {"rc": 0, "stdout": "[]"},
    ])
    assert runner.list_remote() == []
    assert len(calls) == 2
    assert sleeps == [10]


def test_lsjson_deux_echecs_puis_succes(monkeypatch):
    calls, sleeps = _install(monkeypatch, [
        {"rc": 1, "stderr": TRANSIENT_ERR},
        {"rc": 1, "stderr": TRANSIENT_ERR},
        {"rc": 0, "stdout": "[]"},
    ])
    assert runner.list_remote() == []
    assert len(calls) == 3
    assert sleeps == [10, 60]


# --- echec definitif : exception propagee, jamais d'exit 0 masque -------------
def test_lsjson_echec_definitif_propage(monkeypatch, capsys):
    calls, sleeps = _install(monkeypatch, [
        {"rc": 1, "stderr": TRANSIENT_ERR},
        {"rc": 1, "stderr": TRANSIENT_ERR},
        {"rc": 1, "stderr": TRANSIENT_ERR},
    ])
    with pytest.raises(subprocess.CalledProcessError):
        runner.list_remote()
    assert len(calls) == 3
    assert sleeps == [10, 60]
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.startswith("rclone op=lsjson")]
    assert len(lines) == 3
    assert "attempt=3/3" in lines[-1]


# --- ecritures : pas de retry applicatif -------------------------------------
def test_copyto_n_est_pas_retente(monkeypatch):
    calls, sleeps = _install(monkeypatch, [
        {"rc": 1, "stderr": TRANSIENT_ERR},
        {"rc": 0},
    ])
    with pytest.raises(subprocess.CalledProcessError):
        runner.rclone("copyto", "src", "convia:dest.md")
    assert len(calls) == 1, "une ecriture ne doit pas etre rejouee"
    assert sleeps == []


# --- erreur permanente : pas de boucle de retry inutile -----------------------
def test_erreur_permanente_une_seule_tentative(monkeypatch):
    err = "googleapi: Error 404: directory not found"
    calls, sleeps = _install(monkeypatch, [{"rc": 1, "stderr": err}])
    with pytest.raises(subprocess.CalledProcessError):
        runner.list_remote()
    assert len(calls) == 1
    assert sleeps == []


# --- lsf (backups) : meme budget, semantique preservee ------------------------
def test_lsf_succes_retourne_les_chemins(monkeypatch):
    calls, _ = _install(monkeypatch, [{"rc": 0, "stdout": "a.md\nb/c.md\n"}])
    assert runner.list_backups() == {"a.md", "b/c.md"}
    assert len(calls) == 1
    assert "lsf" in calls[0]


def test_lsf_echec_apres_budget_retourne_vide_comme_avant(monkeypatch):
    """Semantique d'origine preservee : si le listing .raw finit par echouer
    meme apres retries, list_backups() rend un ensemble vide (cas premier run
    sans dossier .raw). Les tentatives sont loguees par le wrapper."""
    calls, sleeps = _install(monkeypatch, [
        {"rc": 1, "stderr": TRANSIENT_ERR},
        {"rc": 1, "stderr": TRANSIENT_ERR},
        {"rc": 1, "stderr": TRANSIENT_ERR},
    ])
    assert runner.list_backups() == set()
    assert len(calls) == 3
    assert sleeps == [10, 60]


# --- classification -----------------------------------------------------------
def test_classification():
    assert runner.classify_rclone_error("... Error 429 ...") == "transient"
    assert runner.classify_rclone_error("http 500 internal error") == "transient"
    assert runner.classify_rclone_error(
        "Error 403: User Rate Limit Exceeded") == "transient"
    assert runner.classify_rclone_error(
        "connection reset by peer") == "transient"
    assert runner.classify_rclone_error("request timed out") == "transient"
    assert runner.classify_rclone_error(
        "googleapi: Error 404: directory not found") == "permanent"
    assert runner.classify_rclone_error(
        "didn't find section in config file") == "permanent"
    assert runner.classify_rclone_error("unknown flag: --bogus") == "permanent"
    assert runner.classify_rclone_error("") == "unknown"
    assert runner.classify_rclone_error("message quelconque") == "unknown"


# --- redaction et bornage du journal -----------------------------------------
def test_log_redige_secret_et_borne(monkeypatch, capsys):
    token = "sk-" + "A" * 40  # forme de cle, jamais une vraie valeur
    single = ("googleapi: Error 429: usage avec le jeton %s puis fin "
              "d'une tres longue ligne inutilement verbeuse " % token)
    err = single * 8  # long stderr: verifie aussi le bornage a 300 caracteres
    calls, _ = _install(monkeypatch, [
        {"rc": 1, "stderr": err},
        {"rc": 1, "stderr": err},
        {"rc": 1, "stderr": err},
    ])
    with pytest.raises(subprocess.CalledProcessError):
        runner.list_remote()
    out = capsys.readouterr().out
    assert token not in out, "le jeton ne doit jamais apparaitre dans le journal"
    assert "429" in out, "la nature technique de l'erreur doit survivre"
    assert "[REDACTED:openai_api_key]" in out
    for ln in out.splitlines():
        assert len(ln) < 400, "chaque ligne de journal doit rester bornee"


# --- backoff vide = pas d'attente ---------------------------------------------
def test_backoff_vide_ne_attend_pas(monkeypatch):
    calls, sleeps = _install(monkeypatch, [
        {"rc": 1, "stderr": TRANSIENT_ERR},
        {"rc": 0, "stdout": "[]"},
    ])
    monkeypatch.setattr(runner, "READ_BACKOFF", ())
    assert runner.list_remote() == []
    assert len(calls) == 2
    assert sleeps == [0]
