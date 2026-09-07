# -*- coding: utf-8 -*-
"""Traçabilité : `runner.py --version` affiche le SHA réellement déployé, lu
depuis `.deployed-commit` écrit par le déploiement (jamais inventé)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import runner  # noqa: E402


def _run_version(monkeypatch, capsys, tmp_path):
    (tmp_path / ".deployed-commit").write_text(
        "0123456789abcdef0123456789abcdef01234567\n", encoding="utf-8")
    (tmp_path / ".deployed-at").write_text("2026-09-07T15:00:00Z\n",
                                           encoding="utf-8")
    monkeypatch.setattr(runner, "_DEPLOYED", tmp_path)
    monkeypatch.setattr(sys, "argv", ["runner.py", "--version"])
    rc = runner.main()
    out = capsys.readouterr().out
    return rc, out


def test_version_affiche_le_commit_deploye(monkeypatch, capsys, tmp_path):
    rc, out = _run_version(monkeypatch, capsys, tmp_path)
    assert rc == 0
    assert "0123456789abcdef0123456789abcdef01234567" in out
    assert "deployed=2026-09-07T15:00:00Z" in out
    assert "sanitizer_v" in out


def test_version_sans_fichier_affiche_unknown(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(runner, "_DEPLOYED", tmp_path)  # dossier vide
    monkeypatch.setattr(sys, "argv", ["runner.py", "--version"])
    rc = runner.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "git=unknown" in out


def test_deployed_commit_tronque_a_40(monkeypatch, tmp_path):
    (tmp_path / ".deployed-commit").write_text(
        "a" * 80 + "\n", encoding="utf-8")
    monkeypatch.setattr(runner, "_DEPLOYED", tmp_path)
    assert runner.deployed_commit() == "a" * 40
