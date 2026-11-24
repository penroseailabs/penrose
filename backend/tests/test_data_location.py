"""The data folder chosen at install: penrose/data-location.txt, read the same way on every platform."""
import sys

import pytest

from app import cli


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """A packaged app whose per-user app data folder is tmp_path, whatever platform the tests run on."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delenv("RELAY_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    folder = tmp_path / "Library" / "Application Support" if sys.platform == "darwin" else tmp_path
    pointer = folder / "penrose" / "data-location.txt"
    pointer.parent.mkdir(parents=True)
    return pointer


def absolute(tmp_path):
    return tmp_path / "Données" / "penrose"


def test_reads_what_the_windows_installer_writes(installed, tmp_path):
    installed.write_bytes(str(absolute(tmp_path)).encode("utf-16-le"))
    assert cli.chosen_data_dir() == absolute(tmp_path)


def test_reads_a_file_written_by_hand(installed, tmp_path):
    installed.write_text(f"{absolute(tmp_path)}\n", encoding="utf-8")
    assert cli.chosen_data_dir() == absolute(tmp_path)


def test_the_chosen_folder_comes_before_the_default(installed, tmp_path):
    installed.write_text(str(absolute(tmp_path)), encoding="utf-8")
    assert cli.data_dirs()[0] == absolute(tmp_path) / "data"


def test_the_environment_still_wins(installed, tmp_path, monkeypatch):
    installed.write_text(str(absolute(tmp_path)), encoding="utf-8")
    monkeypatch.setenv("RELAY_DATA_DIR", str(tmp_path / "other"))
    assert cli.data_dirs()[0] == tmp_path / "other" / "data"


@pytest.mark.parametrize("content", [b"", b"relative/folder", b"   \n"])
def test_an_unusable_file_means_the_default(installed, content):
    installed.write_bytes(content)
    assert cli.chosen_data_dir() is None


def test_no_file_means_the_default(installed):
    assert cli.chosen_data_dir() is None


def test_development_ignores_the_file(installed, tmp_path, monkeypatch):
    installed.write_text(str(absolute(tmp_path)), encoding="utf-8")
    monkeypatch.delattr(sys, "frozen")
    assert cli.chosen_data_dir() is None
