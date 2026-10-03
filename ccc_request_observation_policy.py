"""Shared opt-in validation for native observations (no credential reads)."""
from pathlib import Path

def request_observation_directory_matches(env: dict, directory: Path) -> bool:
    """Bind opt-in to the writer's home, never the inspector's global config."""
    import os
    import stat
    explicit = env.get("CODEX_CREDENTIAL_OBSERVATIONS_DIR")
    if explicit is not None:
        return explicit == str(directory)
    home = env.get("CODEX_HOME")
    if home is None:
        base = env.get("HOME")
        if not base:
            return False
        home = str(Path(base) / ".codex")
    home = Path(home)
    def identity(s):
        return (s.st_dev, s.st_ino, s.st_mode, s.st_uid,
                s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    try:
        hs = home.lstat()
        ds = directory.lstat()
        if (not home.is_absolute() or directory != home / "credential-observations"
                or not stat.S_ISDIR(hs.st_mode) or not stat.S_ISDIR(ds.st_mode)
                or hs.st_uid != os.getuid() or ds.st_uid != hs.st_uid
                or ds.st_mode & 0o077):
            return False
        marker = directory / "enabled-v1"
        fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != ds.st_uid
                    or before.st_mode & 0o077 or before.st_size != 27):
                return False
            raw = stream.read(28)
            after = os.fstat(stream.fileno())
        return (raw == b"ccc-request-credentials-v1\n"
                and identity(before) == identity(after) == identity(marker.lstat())
                # Other sessions publish requests and update files under HOME.
                # Their directory timestamps/sizes are not identity changes.
                # The opt-in file itself still requires the complete identity.
                and identity(ds)[:4] == identity(directory.lstat())[:4]
                and identity(hs)[:4] == identity(home.lstat())[:4])
    except (OSError, ValueError):
        return False

