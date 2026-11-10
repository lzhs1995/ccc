"""Exact original Darwin environment parser."""
import ctypes

def parse_arguments(data):
    """Darwin appends Apple startup metadata after the environment terminator."""
    argc = ctypes.c_int.from_buffer_copy(data[:4]).value
    if not 1 <= argc <= 65536:
        raise ValueError("invalid native argv")
    offset = data.index(b"\0", 4) + 1
    while offset < len(data) and data[offset] == 0:
        offset += 1
    argv = []
    for _ in range(argc):
        end = data.index(b"\0", offset)
        argv.append(data[offset:end].decode("utf-8"))
        offset = end + 1
    env = {}
    for entry in data[offset:].split(b"\0"):
        if not entry:
            break
        key, sep, value = entry.partition(b"=")
        if not sep:
            continue
        key, value = key.decode("utf-8"), value.decode("utf-8")
        if key in env:
            raise ValueError("ambiguous process environment")
        env[key] = value
    return argv, env

