"""Tests for the CLI (install-skill path; decisions are covered elsewhere)."""

from __future__ import annotations

from pathlib import Path

from neohorse_kit.cli import install_skill, main


def test_install_skill_copies_to_target(tmp_path: Path):
    dest = install_skill(tmp_path)
    assert dest == tmp_path / "neohorse-kit" / "SKILL.md"
    text = dest.read_text(encoding="utf-8")
    assert text.startswith("---")
    assert "name: neohorse-kit" in text
    assert "description:" in text
    # idempotent
    assert install_skill(tmp_path) == dest


def test_cli_install_skill_flag(tmp_path: Path, capsys):
    code = main(["--install-skill", "--skill-dir", str(tmp_path)])
    assert code == 0
    assert "skill installed" in capsys.readouterr().out
    assert (tmp_path / "neohorse-kit" / "SKILL.md").exists()


def test_skill_file_ships_in_package():
    from importlib import resources

    src = resources.files("neohorse_kit") / "skills" / "neohorse-kit" / "SKILL.md"
    text = src.read_text(encoding="utf-8")
    assert "noul" in text and "NEOHORSE_API_KEYS" in text
