"""relay_server.py: which content the installed app's Python runs, and the check an update must pass."""

import json
import sys
import zipfile

import pytest
import relay_server


def content(folder, shell="aaaa"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "content.json").write_text(json.dumps({"build": "20260101000000", "shell": shell}))
    return folder


def active(data, folder):
    (data / "updates").mkdir(parents=True, exist_ok=True)
    (data / "updates" / "active.json").write_text(json.dumps({"dir": str(folder)}))


def test_the_app_names_the_content(tmp_path):
    assert relay_server.content_root({"RELAY_CONTENT": str(tmp_path)}, tmp_path / "bundled") == tmp_path


def test_a_program_started_without_the_app_runs_what_the_app_last_used(tmp_path):
    bundled, update = content(tmp_path / "bundled"), content(tmp_path / "update")
    active(tmp_path / "data", update)
    environ = {"RELAY_CLI_FILE": str(tmp_path / "data" / "data" / "cli.json")}
    assert relay_server.content_root(environ, bundled) == update


def test_content_made_for_another_installer_is_left_alone(tmp_path):
    bundled, update = content(tmp_path / "bundled", "bbbb"), content(tmp_path / "update")
    active(tmp_path / "data", update)
    assert relay_server.content_root({"RELAY_DATA_DIR": str(tmp_path / "data")}, bundled) == bundled


@pytest.fixture
def isolated_imports():
    """check_content imports a package called `app`: keep it away from the real one."""
    path = list(sys.path)
    held = {name: sys.modules.pop(name) for name in list(sys.modules) if name == "app" or name.startswith("app.")}
    yield
    for name in [name for name in sys.modules if name == "app" or name.startswith("app.")]:
        del sys.modules[name]
    sys.modules.update(held)
    sys.path[:] = path


def packed(tmp_path, modules):
    archive = tmp_path / "update.pack"
    with zipfile.ZipFile(archive, "w") as z:
        for name, text in {"__init__.py": "", "desktop.py": "", **modules}.items():
            z.writestr(f"backend/app/{name}", text)
    return archive


def test_an_update_that_loads_passes(tmp_path, isolated_imports):
    archive = packed(tmp_path, {"ok.py": "import json\n"})
    assert relay_server.check_content(tmp_path / "new", archive) == 0


def test_an_update_that_needs_a_missing_library_is_refused(tmp_path, isolated_imports, capsys):
    archive = packed(tmp_path, {"needs.py": "import a_library_that_is_not_here\n"})
    assert relay_server.check_content(tmp_path / "new", archive) == 1
    assert "a_library_that_is_not_here" in capsys.readouterr().err


def test_an_empty_update_is_refused(tmp_path, isolated_imports):
    assert relay_server.check_content(tmp_path / "nothing") == 1
