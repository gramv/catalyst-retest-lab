"""Container entrypoint (package cloud): give the Railway volume to the runtime user, drop root.

    python -m catalyst_lab.cloud_entry trader|ops|jobs
    python -m catalyst_lab.cloud_entry provision initial|rotate-passwords
    python -m catalyst_lab.cloud_entry provision migrate --expect-current N --target M \\
        --backup <reference>
    python -m catalyst_lab.cloud_entry backup    # the owner, in the ops shell (railway ssh)

``backup`` (package cloud-migrate) is the owner's fresh backup before a guarded migration. The
owner runs it with ``railway ssh --service ops``, whose shell is root; it drops to the ops
service's own user first, so the backup it writes on the ops volume is owned like the daily ones.

Railway mounts a service volume owned by root, and documents that an image running as a
non-root user cannot write to it (https://docs.railway.com/volumes/reference). The container
therefore starts as root only for this module, which imports nothing but the standard library:
before it creates or changes anything it requires ``RAILWAY_VOLUME_MOUNT_PATH`` and a
``CATALYST_STATE_DIR`` equal to it or inside it, reached through real directories only (no
symlink); then it creates the state directory if needed, hands it (and every directory between
the volume and it) to the image's ``catalyst`` user (uid/gid 10001) with mode 0700,
drops every group and both ids for good, and ``exec``s the real component, which never runs as
root, with ``HOME`` set to that user's home (as su and gosu do). A process that is not root (a
local proof) skips straight to the ``exec`` with its environment unchanged. The component keeps
PID 1, so Railway's SIGTERM reaches it directly.

Why ``HOME`` matters: the container starts as root with ``HOME=/root`` (mode 0700). libpq 18
(psycopg's wheel; PostgreSQL 18's pg_dump) takes the home directory from ``HOME``, looks for a
client certificate under ``$HOME/.postgresql`` and fails every TLS connection (``sslmode=require``,
as every cloud DSN says) when that path cannot be searched: "could not open certificate file ...:
Permission denied". The runtime user's home in the image is ``/nonexistent``: libpq finds no
client or root certificate there, which ``sslmode=require`` accepts.
"""

import os
import pwd
import re
import stat
import sys

RUNTIME_UID = RUNTIME_GID = 10001
COMPONENTS = {
    "trader": ("catalyst_lab.cloud_runtime", ("trader",)),
    "ops": ("catalyst_lab.cloud_runtime", ("ops",)),
    # The nightly learning cron (package learning-app): no volume, so no state directory.
    "jobs": ("catalyst_lab.cloud_runtime", ("jobs",)),
    # The owner's fresh backup in the ops shell (package cloud-migrate). The running ops service
    # already prepared its state directory; this only drops root before it writes there.
    "backup": ("catalyst_lab.cloud_runtime", ("backup",)),
    "provision": ("catalyst_lab.cloud_provision", None),
}
PROVISION_MODES = ("initial", "rotate-passwords")
STATEFUL = ("trader", "ops")
# ``provision migrate`` takes exactly these options in this order (.railway/railway.ts writes
# them). The shapes repeat catalyst_lab.ledger_ops.BACKUP_REFERENCE: this module imports only
# the standard library (tests/test_cloud_migrate.py keeps the copies equal).
VERSION = re.compile(r"[1-9][0-9]{0,3}")
BACKUP_REFERENCE = re.compile(
    r"[0-9]{8}T[0-9]{6}Z\.[1-9][0-9]{0,3}\.[1-9][0-9]{0,18}\.[0-9a-f]{64}\.[0-9a-f]{64}")


def _migrate_arguments(rest):
    return (len(rest) == 7 and rest[0] == "migrate" and rest[1] == "--expect-current"
            and VERSION.fullmatch(rest[2]) and rest[3] == "--target"
            and VERSION.fullmatch(rest[4]) and rest[5] == "--backup"
            and BACKUP_REFERENCE.fullmatch(rest[6]))


def refuse(code):
    print(f"CLOUD_ENTRY_REFUSED: {code}", file=sys.stderr, flush=True)
    raise SystemExit(2)


def _absolute(value, code):
    if not value or not os.path.isabs(value) or "\x00" in value:
        refuse(code)
    return os.path.normpath(value)


def state_path_on_volume(path, volume):
    """``(volume, [components below it])``, refused unless ``path`` is the volume or inside it.

    Lexical only; nothing is touched. ``prepare_state_directory`` checks every component on
    disk before it creates or changes one.
    """
    volume = _absolute(volume, "CLOUD_VOLUME_REQUIRED")
    path = _absolute(path, "CLOUD_STATE_DIR_REQUIRED")
    if volume == os.sep:
        refuse("CLOUD_VOLUME_REQUIRED")
    if path != volume and not path.startswith(volume + os.sep):
        refuse("CLOUD_STATE_DIR_NOT_ON_VOLUME")
    below = [] if path == volume else os.path.relpath(path, volume).split(os.sep)
    return volume, below


def prepare_state_directory(path, volume, *, uid=RUNTIME_UID, gid=RUNTIME_GID, chown=os.chown):
    """Give the state directory on the Railway volume (never recursively) to the runtime user.

    Refused before anything is created or chowned unless ``path`` is the volume
    (``RAILWAY_VOLUME_MOUNT_PATH``) or inside it; every existing component from the volume down
    must be a real directory, never a symlink, so root cannot be led off the volume. Missing
    components are created one at a time (0700); the state directory and every directory between
    the volume and it are given to the runtime user (0700). The volume itself is chowned only
    when it is the state directory.
    """
    volume, below = state_path_on_volume(path, volume)
    current, owned = volume, [] if below else [volume]
    for part in [None, *below]:
        if part is not None:
            current = os.path.join(current, part)
            owned.append(current)
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if part is None:
                refuse("CLOUD_VOLUME_UNAVAILABLE")
            try:
                os.mkdir(current, 0o700)
                info = os.lstat(current)
            except OSError:
                refuse("CLOUD_STATE_DIR_UNAVAILABLE")
        except OSError:
            refuse("CLOUD_STATE_DIR_UNAVAILABLE")
        if not stat.S_ISDIR(info.st_mode):
            refuse("CLOUD_VOLUME_UNAVAILABLE" if part is None
                   else "CLOUD_STATE_DIR_NOT_A_DIRECTORY")
    for directory in owned:
        try:
            info = os.lstat(directory)
            if not stat.S_ISDIR(info.st_mode):  # Changed since the walk above: fail closed.
                refuse("CLOUD_STATE_DIR_NOT_A_DIRECTORY")
            if info.st_uid != uid or info.st_gid != gid:
                chown(directory, uid, gid)
            os.chmod(directory, 0o700)
        except OSError:
            refuse("CLOUD_STATE_DIR_UNAVAILABLE")
    return current


def runtime_home(uid=RUNTIME_UID):
    """The runtime user's home from the passwd database (the component's ``HOME``)."""
    try:
        home = pwd.getpwuid(uid).pw_dir
    except KeyError:
        home = ""
    if not home or not os.path.isabs(home) or home == "/root":
        refuse("RUNTIME_USER_UNKNOWN")
    return home


def drop_privileges(*, uid=RUNTIME_UID, gid=RUNTIME_GID):
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    if os.getuid() == 0 or os.geteuid() == 0 or os.getgid() == 0 or os.getegid() == 0:
        refuse("PRIVILEGE_DROP_FAILED")
    try:  # Root must be unrecoverable after the drop.
        os.setuid(0)
    except PermissionError:
        return
    refuse("PRIVILEGE_DROP_FAILED")


def command(argv):
    if not argv or argv[0] not in COMPONENTS:
        refuse("CLOUD_COMPONENT_REQUIRED")
    component, rest = argv[0], list(argv[1:])
    module, fixed = COMPONENTS[component]
    if fixed is not None:
        if rest:
            refuse("CLOUD_COMPONENT_ARGUMENTS_UNEXPECTED")
        args = list(fixed)
    else:
        if not (len(rest) == 1 and rest[0] in PROVISION_MODES) and not _migrate_arguments(rest):
            refuse("CLOUD_PROVISION_MODE_REQUIRED")
        args = rest
    return component, [sys.executable, "-m", module, *args]


def main(argv=None, *, execve=os.execve, environ=None):
    environ = os.environ if environ is None else environ
    component, child = command(sys.argv[1:] if argv is None else argv)
    env = dict(environ)
    if os.geteuid() == 0:
        home = runtime_home()
        if component in STATEFUL:
            prepare_state_directory(environ.get("CATALYST_STATE_DIR", ""),
                                    environ.get("RAILWAY_VOLUME_MOUNT_PATH", ""))
        drop_privileges()
        env["HOME"] = home  # Never root's: see the module docstring.
    execve(child[0], child, env)


if __name__ == "__main__":
    main()
