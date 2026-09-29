"""The container entrypoint: volume handover and the permanent drop from root (package cloud).

This process is not root, so the root branch is exercised with injected functions only
(tests/test_cloud_tls.py runs a real provisioner process with the environment it produces).
"""

import os
import stat
import sys
from types import SimpleNamespace

import pytest

from catalyst_lab import cloud_entry


def test_components_map_to_fixed_argv_without_a_shell():
    assert cloud_entry.command(["trader"]) == (
        "trader", [sys.executable, "-m", "catalyst_lab.cloud_runtime", "trader"])
    assert cloud_entry.command(["ops"])[1][-1] == "ops"
    assert cloud_entry.command(["provision", "initial"])[1] == [
        sys.executable, "-m", "catalyst_lab.cloud_provision", "initial"]
    assert cloud_entry.command(["provision", "rotate-passwords"])[1][-1] == "rotate-passwords"


@pytest.mark.parametrize("argv", [[], ["web"], ["trader", "--port", "1"], ["provision"],
                                  ["provision", "drop-everything"], ["ops", "extra"]])
def test_anything_else_is_refused(argv, capsys):
    with pytest.raises(SystemExit) as exited:
        cloud_entry.command(argv)
    assert exited.value.code == 2
    assert capsys.readouterr().err.startswith("CLOUD_ENTRY_REFUSED: ")


def test_a_non_root_process_execs_the_component_directly(tmp_path, monkeypatch):
    if os.geteuid() == 0:
        pytest.skip("runs as root")
    calls = []
    environ = {"CATALYST_STATE_DIR": str(tmp_path / "never-created"), "HOME": str(tmp_path)}
    cloud_entry.main(["ops"], execve=lambda path, argv, env: calls.append((path, argv, env)),
                     environ=environ)
    assert calls == [(sys.executable, [sys.executable, "-m", "catalyst_lab.cloud_runtime",
                                       "ops"], environ)]
    assert not (tmp_path / "never-created").exists()


class RootEntry:
    """The root branch of ``main`` with the privileged calls recorded instead of made."""

    def __init__(self, monkeypatch, home="/nonexistent"):
        self.calls = []
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        monkeypatch.setattr(cloud_entry, "prepare_state_directory",
                            lambda path, volume: self.calls.append(("prepare", path, volume)))
        monkeypatch.setattr(cloud_entry, "drop_privileges",
                            lambda: self.calls.append(("drop",)))

        def getpwuid(uid):
            if home is None:
                raise KeyError(uid)
            self.calls.append(("getpwuid", uid))
            return SimpleNamespace(pw_dir=home)

        monkeypatch.setattr(cloud_entry.pwd, "getpwuid", getpwuid)

    def execve(self, path, argv, env):
        self.calls.append(("exec", path, argv, env))


def test_root_drops_then_execs_the_component_with_the_runtime_users_home(monkeypatch):
    entry = RootEntry(monkeypatch)
    environ = {"HOME": "/root", "CATALYST_STATE_DIR": "/data", "PORT": "8080",
               "RAILWAY_VOLUME_MOUNT_PATH": "/data"}
    cloud_entry.main(["trader"], execve=entry.execve, environ=environ)
    child = [sys.executable, "-m", "catalyst_lab.cloud_runtime", "trader"]
    assert entry.calls == [
        ("getpwuid", 10001), ("prepare", "/data", "/data"), ("drop",),
        ("exec", sys.executable, child,
         {**environ, "HOME": "/nonexistent"}),
    ]
    assert environ["HOME"] == "/root"  # The caller's mapping is not modified.
    # The provisioner has no volume: it is only dropped and given the same home.
    entry = RootEntry(monkeypatch)
    cloud_entry.main(["provision", "initial"], execve=entry.execve, environ={"HOME": "/root"})
    assert [call[0] for call in entry.calls] == ["getpwuid", "drop", "exec"]
    assert entry.calls[-1][3] == {"HOME": "/nonexistent"}


@pytest.mark.parametrize("home", [None, "", "relative", "/root"])
def test_root_refuses_before_the_drop_without_a_runtime_home(monkeypatch, capsys, home):
    entry = RootEntry(monkeypatch, home=home)
    with pytest.raises(SystemExit) as exited:
        cloud_entry.main(["ops"], execve=entry.execve,
                         environ={"HOME": "/root", "CATALYST_STATE_DIR": "/data"})
    assert exited.value.code == 2
    assert capsys.readouterr().err == "CLOUD_ENTRY_REFUSED: RUNTIME_USER_UNKNOWN\n"
    assert [call[0] for call in entry.calls] in ([], ["getpwuid"])


def handover(path, volume, chowned):
    return cloud_entry.prepare_state_directory(
        str(path), str(volume), uid=os.getuid() + 1, gid=os.getgid(),
        chown=lambda p, uid, gid: chowned.append(p))


def test_root_hands_only_the_state_directory_on_the_volume_to_the_runtime_user(tmp_path):
    volume = tmp_path / "data"
    volume.mkdir()
    chowned = []
    assert handover(volume, volume, chowned) == str(volume)  # CATALYST_STATE_DIR = the volume.
    assert chowned == [str(volume)] and stat.S_IMODE(volume.stat().st_mode) == 0o700
    # Inside the volume: the missing directories are created and given away, never the volume.
    chowned = []
    nested = volume / "state" / "trader"
    assert handover(nested, volume, chowned) == str(nested)
    assert chowned == [str(volume / "state"), str(nested)]
    assert all(stat.S_IMODE(os.stat(p).st_mode) == 0o700 for p in chowned)


@pytest.mark.parametrize("case", ["no_volume", "outside", "sibling_prefix", "dot_dot", "root",
                                  "relative", "volume_missing", "symlink_out", "file"])
def test_the_state_directory_is_refused_before_anything_is_created_or_chowned(
        tmp_path, capsys, case):
    volume, outside = tmp_path / "data", tmp_path / "etc"
    volume.mkdir()
    outside.mkdir()
    state, expected = volume / "state", None
    if case == "no_volume":
        volume_arg, expected = "", "CLOUD_VOLUME_REQUIRED"
    else:
        volume_arg = str(volume)
    if case == "outside":
        state, expected = outside / "state", "CLOUD_STATE_DIR_NOT_ON_VOLUME"
    elif case == "sibling_prefix":
        state, expected = tmp_path / "data2", "CLOUD_STATE_DIR_NOT_ON_VOLUME"
    elif case == "dot_dot":
        state, expected = f"{volume}/../etc/state", "CLOUD_STATE_DIR_NOT_ON_VOLUME"
    elif case == "root":
        volume_arg, expected = "/", "CLOUD_VOLUME_REQUIRED"
    elif case == "relative":
        state, expected = "data/state", "CLOUD_STATE_DIR_REQUIRED"
    elif case == "volume_missing":
        volume_arg, state = str(tmp_path / "unmounted"), tmp_path / "unmounted" / "state"
        expected = "CLOUD_VOLUME_UNAVAILABLE"
    elif case == "symlink_out":  # A link planted on the volume must not lead root off it.
        (volume / "link").symlink_to(outside)
        state, expected = volume / "link" / "state", "CLOUD_STATE_DIR_NOT_A_DIRECTORY"
    elif case == "file":
        (volume / "state").write_text("x")
        expected = "CLOUD_STATE_DIR_NOT_A_DIRECTORY"
    chowned = []
    with pytest.raises(SystemExit) as exited:
        handover(state, volume_arg, chowned)
    assert exited.value.code == 2
    assert capsys.readouterr().err == f"CLOUD_ENTRY_REFUSED: {expected}\n"
    assert chowned == [] and list(outside.iterdir()) == []  # Nothing created off the volume.
    assert not (tmp_path / "unmounted").exists() and not (tmp_path / "data2").exists()


def test_the_root_branch_refuses_without_the_volume_before_the_drop(tmp_path, monkeypatch,
                                                                    capsys):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(cloud_entry.pwd, "getpwuid",
                        lambda uid: SimpleNamespace(pw_dir="/nonexistent"))
    monkeypatch.setattr(cloud_entry, "drop_privileges",
                        lambda: pytest.fail("never dropped (nor exec'd) without the volume"))
    state = tmp_path / "data"
    for component in ("trader", "ops"):
        with pytest.raises(SystemExit) as exited:
            cloud_entry.main([component], execve=lambda *a: pytest.fail("never exec'd"),
                             environ={"CATALYST_STATE_DIR": str(state)})
        assert exited.value.code == 2 and not state.exists()
    assert capsys.readouterr().err.count("CLOUD_ENTRY_REFUSED: CLOUD_VOLUME_REQUIRED") == 2


def test_the_drop_is_permanent_or_the_entrypoint_refuses(monkeypatch):
    ids = {"uid": 0, "gid": 0}
    calls = []

    def setuid(value):
        if ids["uid"] != 0:
            raise PermissionError
        ids["uid"] = value
        calls.append(("setuid", value))

    monkeypatch.setattr(os, "setgroups", lambda groups: calls.append(("setgroups", groups)))
    monkeypatch.setattr(os, "setgid", lambda gid: (ids.update(gid=gid), calls.append(
        ("setgid", gid))))
    monkeypatch.setattr(os, "setuid", setuid)
    for name in ("getuid", "geteuid"):
        monkeypatch.setattr(os, name, lambda: ids["uid"])
    for name in ("getgid", "getegid"):
        monkeypatch.setattr(os, name, lambda: ids["gid"])
    cloud_entry.drop_privileges()
    assert calls == [("setgroups", []), ("setgid", 10001), ("setuid", 10001)]
    # A platform where root could come back must refuse instead of continuing.
    ids.update(uid=0, gid=0)
    monkeypatch.setattr(os, "setuid", lambda value: ids.update(uid=value))
    with pytest.raises(SystemExit):
        cloud_entry.drop_privileges()


def test_the_entrypoint_imports_only_the_standard_library():
    source = open(cloud_entry.__file__).read()
    imports = [line.split()[1] for line in source.splitlines()
               if line.startswith(("import ", "from "))]
    assert imports == ["os", "pwd", "re", "stat", "sys"]  # re: the migration's argument shapes.
