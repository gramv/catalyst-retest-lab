"""Keys live in a secret manager or transient environment, never config or receipts."""

import ctypes
import os
import stat
import sys
from pathlib import Path


class CredentialUnavailable(RuntimeError):
    pass


def _local_env_key():
    path = Path(os.environ.get("TYPESAFE_ENV_FILE", ".env"))
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False, None
    except OSError:
        raise CredentialUnavailable("LOCAL_CREDENTIAL_FILE_UNAVAILABLE") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise CredentialUnavailable("LOCAL_CREDENTIAL_FILE_REQUIRES_MODE_0600")
        if info.st_uid != os.getuid() or info.st_size > 65536:
            raise CredentialUnavailable("LOCAL_CREDENTIAL_FILE_UNAVAILABLE")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = None
            lines = stream.read().splitlines()
        found = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, separator, value = line.partition("=")
            if name.strip() != "TYPESAFE_API_KEY":
                continue
            if not separator:
                raise CredentialUnavailable("MISSING_TYPESAFE_CREDENTIAL")
            value = value.strip()
            if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
                value = value[1:-1]
            found.append(value)
        if len(found) != 1 or not found[0] or any(c.isspace() for c in found[0]):
            raise CredentialUnavailable("MISSING_TYPESAFE_CREDENTIAL")
        return True, found[0]
    except UnicodeError:
        raise CredentialUnavailable("LOCAL_CREDENTIAL_FILE_INVALID") from None
    finally:
        if fd is not None:
            os.close(fd)


def typesafe_key():
    production = os.environ.get("CATALYST_ENVIRONMENT") == "railway" or bool(
        os.environ.get("RAILWAY_ENVIRONMENT_ID")
    )
    if not production:
        present, key = _local_env_key()
        if present:
            return key
    key = os.environ.get("TYPESAFE_API_KEY")
    if key and not any(c.isspace() for c in key):
        return key
    if production or sys.platform != "darwin":
        raise CredentialUnavailable("MISSING_TYPESAFE_CREDENTIAL")
    security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    # A background worker must fail closed instead of waiting on a desktop permission dialog.
    interaction = security.SecKeychainSetUserInteractionAllowed
    interaction.argtypes = [ctypes.c_bool]
    interaction.restype = ctypes.c_int32
    if interaction(False) != 0:
        raise CredentialUnavailable("TYPESAFE_CREDENTIAL_UNAVAILABLE")
    find = security.SecKeychainFindGenericPassword
    find.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    find.restype = ctypes.c_int32
    service, account = b"catalyst-retest-lab.typesafe", b"jev-review"
    size, data = ctypes.c_uint32(), ctypes.c_void_p()
    status = find(
        None,
        len(service),
        service,
        len(account),
        account,
        ctypes.byref(size),
        ctypes.byref(data),
        None,
    )
    if status != 0:
        raise CredentialUnavailable("TYPESAFE_CREDENTIAL_UNAVAILABLE")
    try:
        return ctypes.string_at(data, size.value).decode()
    finally:
        free = security.SecKeychainItemFreeContent
        free.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        free.restype = ctypes.c_int32
        free(None, data)
