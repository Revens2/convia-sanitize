# -*- coding: utf-8 -*-
"""Tests du publisher RAG (publish.py) : copies decouplees du sanitizer.

Aucun appel reseau : `subprocess.run` est remplace par un double. Ce que ces
tests verrouillent :
  - la copie racine porte --fast-list et les exclusions (.raw/Traité/traiter) ;
  - la copie Traité n'a PAS --fast-list (benchmark 2026-09-07 : plus lent) ;
  - un echec de copie fait echouer l'unite (rc != 0) et journalise une erreur
    redactee et bornee (compatible notify-failure) ;
  - le dry-run ajoute --dry-run sans rien copier ;
  - l'analyse des stats rclone est tolerante.
"""
import os
import subprocess
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import publish  # noqa: E402

STATS_SAMPLE = (
    "Transferred:   \t  729.645 KiB / 729.645 KiB, 100%, 31.963 KiB/s, ETA 0s\n"
    "Checks:               705 / 705, 100%\n"
    "Transferred:            4 / 4, 100%\n"
    "Elapsed time:        32.7s\n"
)


def _res(rc=0, stderr=""):
    return types.SimpleNamespace(returncode=rc, stdout="", stderr=stderr)


def _install(monkeypatch, results):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return results.pop(0) if results else _res()

    monkeypatch.setattr(publish.subprocess, "run", fake_run)
    return calls


@pytest.fixture
def spool(tmp_path, monkeypatch):
    """Spool de demandes durables isole (env lu a chaque appel)."""
    d = tmp_path / "publish-requests"
    d.mkdir()
    monkeypatch.setenv("CONVIA_PUBLISH_SPOOL", str(d))
    return d


def _req(spool, name):
    (spool / name).write_text("2026-09-07T00:00:00Z test\n", encoding="utf-8")


# --- construction des commandes ---------------------------------------------
def test_copie_racine_fast_list_et_exclusions(monkeypatch):
    calls = _install(monkeypatch, [_res()])
    assert publish.run_copy("root", publish.ROOT_COPY, False) == 0
    cmd = calls[0]
    assert cmd[0] == "rclone" and cmd[1] == "copy"
    assert "--fast-list" in cmd
    assert "--exclude" in cmd and ".raw/**" in cmd
    assert "Trait*/**" in cmd and "traiter/**" in cmd
    assert publish.REMOTE in cmd and publish.DEST in cmd


def test_copie_traite_sans_fast_list(monkeypatch):
    calls = _install(monkeypatch, [_res()])
    assert publish.run_copy("traite", publish.TRAITE_COPY, False) == 0
    cmd = calls[0]
    assert "--fast-list" not in cmd, "benchmark: --fast-list est plus lent sur Traité"
    assert publish.REMOTE + "Trait\u00e9" in cmd


# --- dry-run -----------------------------------------------------------------
def test_dry_run_ajoute_le_flag(monkeypatch):
    calls = _install(monkeypatch, [_res()])
    assert publish.run_copy("root", publish.ROOT_COPY, True) == 0
    assert "--dry-run" in calls[0]


# --- echec = unite failed, jamais masque ------------------------------------
def test_echec_traite_rc_non_nul_et_log_redige(monkeypatch, capsys):
    token = "sk-" + "B" * 40
    single = ("googleapi: Error 429: echec avec %s et un tres long contexte "
              "inutilement verbeux " % token)
    err = single * 6
    _install(monkeypatch, [_res(rc=1, stderr=err)])
    rc = publish.run_copy("traite", publish.TRAITE_COPY, False)
    assert rc == 1
    out = capsys.readouterr().out
    assert token not in out, "le token ne doit jamais apparaitre"
    assert "[REDACTED:openai_api_key]" in out
    assert "publish copy=traite status=failed rc=1" in out
    for ln in out.splitlines():
        assert len(ln) < 400


def test_main_spool_vide_noop(spool, monkeypatch, capsys):
    """Gate mission 5 : spool vide -> success sans AUCUN appel rclone."""
    def boom(cmd, **kwargs):
        raise AssertionError("rclone ne doit pas etre appele sur spool vide: %s" % cmd)
    monkeypatch.setattr(publish.subprocess, "run", boom)
    monkeypatch.setattr(sys, "argv", ["publish.py"])
    assert publish.main() == 0
    out = capsys.readouterr().out
    assert "requests=0" in out and "status=noop" in out


def test_main_s_arrete_a_la_premiere_erreur(spool, monkeypatch, capsys):
    _req(spool, "req-a")
    _install(monkeypatch, [_res(), _res(rc=1, stderr="connection reset by peer")])
    monkeypatch.setattr(sys, "argv", ["publish.py"])
    assert publish.main() == 1
    out = capsys.readouterr().out
    assert "status=failed" in out
    assert "removed=0" in out and "remaining=1" in out
    # echec : la demande est CONSERVEE (pas de perte), retentative a la passe suivante
    assert (spool / "req-a").is_file()


def test_main_succes(spool, monkeypatch, capsys):
    _req(spool, "req-a")
    _req(spool, "req-b")
    _install(monkeypatch, [_res(stderr=STATS_SAMPLE), _res(stderr=STATS_SAMPLE)])
    monkeypatch.setattr(sys, "argv", ["publish.py"])
    assert publish.main() == 0
    out = capsys.readouterr().out
    assert "publish copy=root status=ok files=4/4" in out
    assert "publish copy=traite status=ok files=4/4" in out
    assert "removed=2" in out and "remaining=0" in out
    assert not (spool / "req-a").exists()
    assert not (spool / "req-b").exists()


def test_demande_creee_pendant_run_reste(spool, monkeypatch, capsys):
    """Anti-lost-wakeup : une demande posee PENDANT le publish n'est pas
    consommee (le publisher ne supprime que son snapshot de demarrage)."""
    _req(spool, "req-a")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        (spool / "req-pendant-run").write_text("x\n", encoding="utf-8")
        return _res()
    monkeypatch.setattr(publish.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["publish.py"])
    assert publish.main() == 0
    assert not (spool / "req-a").exists(), "snapshot consomme"
    assert (spool / "req-pendant-run").is_file(), "demande tardive conservee"
    out = capsys.readouterr().out
    assert "removed=1" in out and "remaining=1" in out


def test_dry_run_ne_consomme_pas(spool, monkeypatch, capsys):
    """Un dry-run ne copie rien et ne consomme AUCUNE demande."""
    _req(spool, "req-a")
    _install(monkeypatch, [_res(stderr=STATS_SAMPLE), _res(stderr=STATS_SAMPLE)])
    monkeypatch.setattr(sys, "argv", ["publish.py", "--dry-run"])
    assert publish.main() == 0
    assert (spool / "req-a").is_file()
    out = capsys.readouterr().out
    assert "remaining=1" in out


# --- analyse des stats (best-effort) ----------------------------------------
def test_parse_stats():
    s = publish.parse_stats(STATS_SAMPLE)
    assert s["files"] == 4
    assert s["total"] == 4
    assert s["elapsed"] == 32.7
    assert publish.parse_stats("")["files"] == 0


def test_parse_stats_bloc_partiel_seulement(monkeypatch, capsys):
    """Pas de bloc a 100%% (run coupe en plein transfert) : on garde le
    meilleur couple vu au lieu d'afficher files=0 (bug observe 2026-09-07 sur
    les runs longs)."""
    partial = (
        "Transferred:   \t  12.4 MiB / 12.4 MiB, 100%, 0 B/s, ETA -\n"
        "Checks:               119 / 119, 100%\n"
        "Transferred:           42 / 83, 51%\n"
        "Elapsed time:      1m29.3s\n"
    )
    s = publish.parse_stats(partial)
    assert s["files"] == 42
    assert s["total"] == 83
    assert s["bytes_raw"] == "12.4 MiB"
    _install(monkeypatch, [_res(stderr=partial)])
    assert publish.run_copy("root", publish.ROOT_COPY, False) == 0
    out = capsys.readouterr().out
    assert "files=42/83" in out
    assert "12.4 MiB" not in out, "la taille brute reste hors journal"
