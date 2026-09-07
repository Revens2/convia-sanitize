# -*- coding: utf-8 -*-
"""Demande durable de publication (mission 5) cote SANITIZER.

Aucun appel reseau : `subprocess.run` est remplace par un double simulant un
remote Drive minimal. Ce que ces tests verrouillent :

  - a_traiter=0 sans deplacement -> AUCUNE demande, AUCUN publisher ;
  - reecriture reelle -> demande creee AVANT la premiere ecriture distante ;
  - conversation connue disparue (deplacee vers Traite par l'analyse) ->
    demande meme si a_traiter=0, une seule fois (purgee du manifeste) ;
  - --dry-run ne cree aucune demande et n'ecrit rien ;
  - un run ne pose jamais deux demandes identiques.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import runner  # noqa: E402
from sanitizer import VERSION  # noqa: E402

REMOTE_PREFIX = "convia:"


class FakeDrive:
    def __init__(self, files):
        # files: {relpath: {"size": int, "modtime": str, "content": str}}
        self.files = files
        self.writes = []


def _res(rc=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)


def install_fake(monkeypatch, drive, spool: Path, expect_request_before_write=True):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        assert cmd[0] == "rclone"
        op = cmd[1]
        if op == "lsjson":
            out = json.dumps([
                {"Path": rel, "Size": f["size"], "ModTime": f["modtime"]}
                for rel, f in sorted(drive.files.items())
            ])
            return _res(stdout=out)
        if op == "lsf":
            return _res(stdout="")
        if op == "copyto":
            src, dst = cmd[2], cmd[3]
            if dst.startswith(REMOTE_PREFIX):  # upload local -> remote
                if expect_request_before_write:
                    assert any(spool.iterdir()), (
                        "ecriture distante sans demande de publication prealable")
                rel = dst[len(REMOTE_PREFIX):]
                content = Path(src).read_text(encoding="utf-8", errors="replace")
                drive.files[rel] = {
                    "size": len(content.encode("utf-8")), "modtime": "m",
                    "content": content,
                }
                drive.writes.append(rel)
            else:  # download remote -> local
                rel = src[len(REMOTE_PREFIX):]
                p = Path(dst)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(drive.files[rel]["content"], encoding="utf-8")
            return _res()
        raise AssertionError("operation rclone inattendue: %s" % op)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    return calls


@pytest.fixture
def env(tmp_path, monkeypatch):
    state = tmp_path / "state"
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setenv("CONVIA_STATE", str(state))
    monkeypatch.setenv("CONVIA_PUBLISH_SPOOL", str(spool))
    monkeypatch.delenv("RCLONE_CONFIG", raising=False)
    return {"state": state, "spool": spool}


def _one_file(rel="Claude-CLI/2026-08-01_x.md", content="contenu brut\n"):
    return {rel: {"size": len(content.encode("utf-8")), "modtime": "2026-08-01T00:00:00Z",
                  "content": content}}


def _manifest(state: Path, entries: dict):
    state.mkdir(parents=True, exist_ok=True)
    data = {rel: {"size": f["size"], "modtime": f["modtime"], "version": VERSION}
            for rel, f in entries.items()}
    (state / "sanitize-manifest.json").write_text(
        json.dumps(data), encoding="utf-8")


def test_noop_aucune_demande(env, monkeypatch, capsys):
    """a_traiter=0 (tout a jour, rien deplace) -> 0 demande, 0 ecriture."""
    drive = FakeDrive(_one_file())
    _manifest(env["state"], drive.files)
    install_fake(monkeypatch, drive, env["spool"])
    monkeypatch.setattr(sys, "argv", ["runner.py"])
    assert runner._main() == 0
    assert list(env["spool"].iterdir()) == [], "no-op ne doit rien demander"
    assert drive.writes == []
    out = capsys.readouterr().out
    assert "a_traiter=0" in out


def test_reecriture_pose_une_demande(env, monkeypatch, capsys):
    """Fichier a reecrire -> exactement 1 demande, creee avant la 1ere ecriture."""
    drive = FakeDrive(_one_file(content="du texte a nettoyer  \n"))
    install_fake(monkeypatch, drive, env["spool"])
    monkeypatch.setattr(sys, "argv", ["runner.py"])
    assert runner._main() == 0
    reqs = list(env["spool"].iterdir())
    assert len(reqs) == 1, "une reecriture = une demande"
    assert "sanitize-rewrite" in reqs[0].read_text(encoding="utf-8")
    assert drive.writes, "le fichier a du etre reecrit"
    assert "a_traiter=1" in capsys.readouterr().out


def test_deux_fichiers_une_seule_demande(env, monkeypatch):
    files = _one_file("Claude-CLI/2026-08-01_a.md", "x\n")
    files.update(_one_file("Claude-CLI/2026-08-01_b.md", "y\n"))
    drive = FakeDrive(files)
    install_fake(monkeypatch, drive, env["spool"])
    monkeypatch.setattr(sys, "argv", ["runner.py"])
    assert runner._main() == 0
    assert len(list(env["spool"].iterdir())) == 1


def test_deplacement_externe_demande_meme_si_noop(env, monkeypatch, capsys):
    """Conversation connue disparue (analyse -> Traite) : demande external-move
    meme avec a_traiter=0, puis purge du manifeste (pas de re-demande au tick
    suivant)."""
    p1 = "Claude-CLI/2026-08-01_garde.md"
    p2 = "Claude-CLI/2026-08-01_partie_vers_traite.md"
    drive = FakeDrive(_one_file(p1, "ok\n"))  # p1 a jour : a_traiter sera 0
    man = {p1: {"size": drive.files[p1]["size"],
                "modtime": drive.files[p1]["modtime"], "version": VERSION},
           p2: {"size": 3, "modtime": "m", "version": VERSION}}
    (env["state"]).mkdir(parents=True, exist_ok=True)
    (env["state"] / "sanitize-manifest.json").write_text(
        json.dumps(man), encoding="utf-8")
    install_fake(monkeypatch, drive, env["spool"])
    monkeypatch.setattr(sys, "argv", ["runner.py"])
    assert runner._main() == 0
    reqs = list(env["spool"].iterdir())
    assert len(reqs) == 1
    assert "external-move" in reqs[0].read_text(encoding="utf-8")
    assert drive.writes == [], "no-op : aucune reecriture"
    # purge persistee : un second run ne re-demandera plus
    env["spool"].joinpath(reqs[0].name).unlink()
    assert runner._main() == 0
    assert list(env["spool"].iterdir()) == []


def test_dry_run_aucune_demande_aucune_ecriture(env, monkeypatch):
    drive = FakeDrive(_one_file(content="texte a nettoyer\n"))
    install_fake(monkeypatch, drive, env["spool"],
                 expect_request_before_write=False)
    monkeypatch.setattr(sys, "argv", ["runner.py", "--dry-run"])
    assert runner._main() == 0
    assert list(env["spool"].iterdir()) == []
    assert drive.writes == []
