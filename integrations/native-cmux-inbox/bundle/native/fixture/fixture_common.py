"""Pure function excerpts from the original r28 fixture; no service/controller imports."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import sys
import uuid

MARKER = b"ccc-native-compaction-recovery-v1\n"


def contained(root, value):
    path = Path(value)
    if not path.is_absolute() or path != path.resolve(strict=True) or not path.is_relative_to(root):
        raise ValueError("path escaped canonical private fixture root")
    return path


def require_runner(root):
    root = Path(root)
    if (sys.platform != "darwin" or os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("GITHUB_REPOSITORY") != "lzhs1995/ccc"
            or not os.environ.get("GITHUB_RUN_ID") or not os.environ.get("GITHUB_RUN_ATTEMPT")):
        raise ValueError("only the isolated GitHub macOS fixture may execute")
    temporary = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
    contained(temporary, root)
    if root == temporary or root.stat().st_uid != os.geteuid() or root.stat().st_mode & 0o077:
        raise ValueError("a distinct private 0700 fixture root is required")
    return root


def save_once(path, value):
    data = json.dumps(value, sort_keys=True, indent=2).encode() + b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())
    fd = os.open(Path(path).parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return hashlib.sha256(data).hexdigest()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def strict_file(path):
    path = Path(path)
    if path != path.resolve(strict=True):
        raise ValueError("linked input file")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or before.st_nlink != 1 or before.st_mode & 0o022):
            raise ValueError("unsafe input member")
        data = stream.read()
        after = os.fstat(stream.fileno())
    identity = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    if identity(before) != identity(after) or identity(after) != identity(path.lstat()):
        raise ValueError("input changed during complete read")
    return data


def pinned(path, expected):
    if not isinstance(expected, str) or len(expected) != 64:
        raise ValueError("unfilled admission/source pin; no fixture may run")
    data = strict_file(path)
    if sha(data) != expected:
        raise ValueError("exact input pin mismatch: " + str(path))
    return data


def write_new(path, raw, mode=0o400):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def prepare_native_home(root, native_home):
    """Pre-TUI explicit opt-in only; never creates a publisher/capability."""
    root = require_runner(root)
    home = Path(native_home)
    contained(root, home.parent)
    home.mkdir(mode=0o700, exist_ok=False)
    target = home / "credential-observations/compaction-recovery"
    target.mkdir(parents=True, mode=0o700)
    (home / "credential-observations").chmod(0o700)
    for name in ("intents", "enrollments", "grants"):
        (target / name).mkdir(mode=0o700)
    write_new(target / "enabled-v1", MARKER, 0o600)
    return home


def need(value, message):
    if not value: raise ValueError(message)


def items(body, kind):
    return [item for item in body.get("input", []) if isinstance(item,dict) and item.get("type")==kind]


def tool_entries(body):
    entries=[];specs=list(body.get("tools",[]))
    for item in body.get("input",[]):
        if isinstance(item,dict) and item.get("type")=="additional_tools":
            specs.extend(item.get("tools",[]))
    def walk(values,namespace=None):
        for value in values:
            if not isinstance(value,dict):continue
            if value.get("type")=="namespace":walk(value.get("tools",[]),value.get("name"))
            else:entries.append((namespace,value))
    walk(specs)
    return entries


def tool(body, call_id, marker, project):
    entries=tool_entries(body)
    args={"cmd":"/usr/bin/printf "+shlex.quote(marker),"workdir":str(project),"login":False,"max_output_tokens":100}
    direct=[(ns,item) for ns,item in entries if item.get("type")=="function" and item.get("name")=="exec_command"]
    if direct:
        need(len(direct)==1,"ambiguous actual exec_command tool")
        item={"id":"fc_"+call_id,"type":"function_call","name":"exec_command","call_id":call_id,
              "arguments":json.dumps(args),"status":"completed"}
        if direct[0][0]:item["namespace"]=direct[0][0]
        return item
    wrapped=[item for ns,item in entries if ns=="functions" and item.get("type")=="custom"
             and item.get("name")=="exec" and "exec_command" in item.get("description","")]
    need(len(wrapped)==1,"no actual advertised executable route")
    return {"id":"ctc_"+call_id,"type":"custom_tool_call","namespace":"functions","name":"exec",
            "call_id":call_id,"input":"text(await tools.exec_command("+json.dumps(args)+"));","status":"completed"}


def completed_events(item, usage):
    need(type(usage)is int and usage>=2,"complete real fixture usage required")
    response={"id":"resp_"+uuid.uuid4().hex,"object":"response","status":"in_progress","output":[]}
    return [{"type":"response.created","response":response},
            {"type":"response.output_item.added","output_index":0,"item":dict(item,status="in_progress")},
            {"type":"response.output_item.done","output_index":0,"item":item},
            {"type":"response.completed","response":dict(response,status="completed",output=[item],
                usage={"input_tokens":usage-1,"output_tokens":1,"total_tokens":usage})}]


def sse(events):
    return "".join("event: "+event["type"]+"\ndata: "+json.dumps(event)+"\n\n" for event in events).encode()
