"""Pinned official RTK data extraction, only to the private GitHub fixture."""
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import tarfile
import urllib.request

import fixture_common as f

URL='https://github.com/rtk-ai/rtk/releases/download/v1.1.0/rtk-aarch64-apple-darwin.tar.gz'
ARCHIVE_BYTES=5929243
ARCHIVE_SHA='8da773b0954cf90b115ec95c4e4eb420f04c39de233c5db102246a6caf073ba7'
MEMBERS={
    'rtk':(12576320,'65629fb7c2f057cc1bb9c28dbe32a44e7acf2e196e1514104c350d7d1c6caab8'),
    'LICENSE':(10756,'33f694a31d9f9df3e292a9413a3cdb0113fa72698e2c719f657ff6b53165dcc4'),
    'LICENSE-ENGINE':(4273,'4e3d24d5ae066274a03b82b400b31ad057628c40911cd08aa5a19ff31787dc98')}


def admit(root):
    root=f.require_runner(root)
    f.need(os.environ.get('RUNNER_ENVIRONMENT')=='github-hosted','RTK is only staged on private GitHub runner')
    out=root/'rtk';out.mkdir(mode=0o700)
    archive=out/'original.tar.gz'
    with urllib.request.urlopen(URL,timeout=60) as source,archive.open('xb') as target:
        digest=hashlib.sha256();total=0
        while part:=source.read(min(1024**2,ARCHIVE_BYTES-total+1)):
            total+=len(part);f.need(total<=ARCHIVE_BYTES,'RTK archive exceeds original size')
            target.write(part);digest.update(part)
        target.flush();os.fsync(target.fileno())
    f.need(total==ARCHIVE_BYTES and digest.hexdigest()==ARCHIVE_SHA,'RTK original archive mismatch')
    with tarfile.open(archive,'r:gz') as tar:
        rows=tar.getmembers()
        f.need(len(rows)==len(MEMBERS) and {r.name for r in rows}==set(MEMBERS)
               and all(r.isfile() and not r.issym() and not r.islnk() for r in rows),'RTK original regular member set differs')
        for row in rows:
            size,wanted=MEMBERS[row.name]
            f.need(row.size==size,'RTK member size differs')
            with tar.extractfile(row) as stream:raw=stream.read(size+1)
            f.need(len(raw)==size and hashlib.sha256(raw).hexdigest()==wanted,'RTK original member differs')
            f.write_new(out/row.name,raw,0o500 if row.name=='rtk' else 0o400)
    binary=out/'rtk'
    with binary.open('rb') as stream:header=stream.read(8)
    f.need(struct.unpack('<II',header)==(0xFEEDFACF,0x0100000C),'RTK must be the original arm64 Mach-O')
    result=subprocess.run([str(binary),'--version'],check=True,capture_output=True,text=True,timeout=10)
    f.need(result.stdout.strip()=='rtk 1.1.0' and not result.stderr.strip(),'actual RTK version differs')
    f.save_once(out/'ADMISSION.json',dict(repository='rtk-ai/rtk',tag='v1.1.0',release=423567764,
        target_commit='753a3b0e939f2e4490fc24e3a1984010f40a103c',asset=675762802,
        archive_sha256=ARCHIVE_SHA,binary_sha256=MEMBERS['rtk'][1],actual_version=result.stdout.strip(),
        private_fixture_only=True,installation=False,retries=0))
    return binary
