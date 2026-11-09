"""Exact selected original r28 artifact/host functions; no Herdr/r9 retests."""
from __future__ import annotations
import base64
from collections import Counter
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
import fixture_common as f
import fixture_common as h

HERE = Path(__file__).resolve().parent / "fixture"
FLOOR = 40 * 1024**3
MAX_DOWNLOAD = 2 * 1024**3
MAX_ARCHIVE = 2 * 1024**3

def need(ok, why):
    if not ok:
        raise RuntimeError(why)



def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()



def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()



def private_file(path):
    path = Path(path)
    info = path.lstat()
    need(path.is_absolute() and stat.S_ISREG(info.st_mode) and not path.is_symlink(), 'absolute regular file required')
    need(info.st_uid == os.getuid() and not info.st_mode & 0o022, 'file ownership/writability invalid')
    return path



def validate_zip(path, item):
    path = private_file(path)
    need(0 < path.stat().st_size <= MAX_ARCHIVE and path.stat().st_size == item['size_in_bytes']
         and sha(path) == item['digest'].split(':')[1], 'archive digest/size mismatch')
    total, seen = 0, set()
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            pure = PurePosixPath(member.filename)
            need(bool(pure.parts) and not pure.is_absolute() and '..' not in pure.parts and '\\' not in member.filename and pure.as_posix() not in seen,
                 'unsafe/duplicate archive member')
            need(not stat.S_ISLNK(member.external_attr >> 16), 'symlink in artifact')
            need(not member.flag_bits & 1, 'encrypted archive member')
            total += member.file_size
            seen.add(pure.as_posix())
        need(0 < total <= MAX_ARCHIVE, 'artifact exceeds private preparation size budget')
    return total



def extract_zip(path, destination):
    destination.mkdir(mode=0o700)
    with zipfile.ZipFile(path) as archive:
        for member in archive.infolist():
            output = destination / member.filename
            if member.is_dir():
                output.mkdir(parents=True, exist_ok=True, mode=0o700)
            else:
                output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with archive.open(member) as source, output.open('xb') as target:
                    shutil.copyfileobj(source, target)
                output.chmod(0o600)



def junit(path, required=None):
    root = ET.parse(path).getroot()
    rows = root.findall('.//testcase')
    need(bool(rows) and not any(x.find(tag) is not None for x in rows for tag in ('failure', 'error', 'skipped', 'rerunFailure', 'flakyFailure')),
         'JUnit failed, skipped or retried case')
    need(all(int(n.get(key, '0')) == 0 for n in [root, *root.findall('.//testsuite')]
             for key in ('failures', 'errors', 'skipped', 'disabled')), 'JUnit has non-pass totals')
    names = [x.attrib['name'] for x in rows]
    if required is not None:
        need(Counter(names) == Counter(x['name'] for x in required), 'required fresh cases differ')
        for case in rows:
            possible = [x for x in required if x['name'] == case.attrib['name']]
            need(any(case.get('classname') in (r['package'], r['package'] + '::' + r['binary']) for r in possible),
                 'required case comes from wrong package/binary')
    return {'cases': len(rows), 'names_sha256': hashlib.sha256(canonical(sorted(names))).hexdigest(), 'junit_sha256': sha(path)}



def save(path,value):return h.save_once(path,value)



def gh_json(endpoint):
    result=subprocess.run(["gh","api",endpoint],check=True,capture_output=True,timeout=45)
    return json.loads(result.stdout)



def capacity(root,additional):
    st=os.statvfs(root)
    need(st.f_bavail*st.f_frsize>=FLOOR+additional,"runner capacity would cross 40 GiB reserve")



def current_runner():
    return {"repository":os.environ["GITHUB_REPOSITORY"],"run_id":os.environ["GITHUB_RUN_ID"],
            "run_attempt":os.environ["GITHUB_RUN_ATTEMPT"],"head_sha":os.environ["GITHUB_SHA"]}



def download_artifact(root,row,path):
    need(type(row.get("id"))is int and type(row.get("size_in_bytes"))is int
         and 0<row["size_in_bytes"]<=MAX_DOWNLOAD and not row.get("expired",True)
         and re.fullmatch(r"sha256:[0-9a-f]{64}",row.get("digest","")),"invalid artifact metadata")
    capacity(root,row["size_in_bytes"])
    with path.open("xb") as stream:
        subprocess.run(["gh","api","repos/lzhs1995/ccc/actions/artifacts/"+str(row["id"])+"/zip"],
                       stdout=stream,check=True,timeout=240)
        stream.flush();os.fsync(stream.fileno())
    need(path.stat().st_size==row["size_in_bytes"] and sha(path)==row["digest"].split(":")[1],
         "downloaded GitHub artifact digest/size differs")



def official_host(root,out):
    contract=json.loads(f.strict_file(HERE/"OFFICIAL-HOST-CONTRACT.json"))
    with urllib.request.urlopen(contract["metadata_url"],timeout=30) as response:
        raw=response.read(1024*1024+1)
    need(len(raw)<=1024*1024,"npm metadata exceeds bound")
    metadata=json.loads(raw)
    need(metadata.get("name")==contract["package"] and metadata.get("version")==contract["version"],"official package version differs")
    need(all(metadata["dist"].get(key)==contract[key] for key in ("tarball","integrity","shasum")),
         "official package digest/source differs")
    capacity(root,700*1024**2)
    archive=out/"official-host.tgz";digest=hashlib.sha512();old=hashlib.sha1();total=0
    with urllib.request.urlopen(contract["tarball"],timeout=120) as response,archive.open("xb") as stream:
        while block:=response.read(1024*1024):
            total+=len(block);need(total<=512*1024**2,"official archive exceeds bound")
            digest.update(block);old.update(block);stream.write(block)
        stream.flush();os.fsync(stream.fileno())
    need("sha512-"+base64.b64encode(digest.digest()).decode()==contract["integrity"]
         and old.hexdigest()==contract["shasum"],"full official npm archive integrity differs")
    host=out/"codex-code-mode-host"
    with tarfile.open(archive,"r:gz") as tar:
        members=[v for v in tar.getmembers() if v.name==contract["tar_member"]]
        need(len(members)==1 and members[0].isfile() and members[0].size==contract["bytes"],"official host member differs")
        with tar.extractfile(members[0]) as source,host.open("xb") as target:
            shutil.copyfileobj(source,target)
    need(sha(host)==contract["sha256"] and host.stat().st_size==contract["bytes"],"official host bytes differ")
    host.chmod(0o500)
    save(out/"OFFICIAL-HOST-PROOF.json",{"schema":"actual-official-host-download-v1","passed":True,
         "contract_sha256":sha(HERE/"OFFICIAL-HOST-CONTRACT.json"),"npm_metadata":metadata,
         "archive_sha256":sha(archive),"archive_sha512_integrity":contract["integrity"],"host_sha256":sha(host),
         "host_bytes":host.stat().st_size,"local_installed_package_reused":False})
    return host

