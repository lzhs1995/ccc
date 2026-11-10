"""Fetch exactly this run's native artifact once, then authenticate every member."""
from pathlib import Path
import argparse
import json
import os
import re
import shutil
import zipfile
import contract as c
import artifact_io as io
import evidence_native as evidence

LIMIT=64*1024**2

def digest_value(value):
    c.need(isinstance(value,str), 'artifact digest missing')
    value=value.removeprefix('sha256:')
    c.need(re.fullmatch('[0-9a-f]{64}',value), 'invalid needs artifact digest')
    return value

def validate_metadata(run, artifact, identity, artifact_id, wanted_digest):
    c.need(c.same([run.get('id'),run.get('run_attempt'),run.get('head_sha')],[identity['run_id'],identity['run_attempt'],identity['head_sha']]), 'run attempt/head differs')
    c.need(run.get('repository',{}).get('full_name')==identity['repository'] and run.get('head_repository',{}).get('full_name')==identity['repository'], 'foreign run repository')
    c.need(type(artifact.get('id')) is int and artifact['id']==artifact_id and artifact.get('name')==f"cmux-natural-native-{identity['run_id']}-{identity['run_attempt']}", 'wrong native artifact ID/name')
    c.need(artifact.get('expired') is False, 'native artifact expired/unknown')
    size=artifact.get('size_in_bytes')
    c.need(type(size) is int and 0<size<=LIMIT, 'native ZIP outside 64 MiB envelope')
    c.need(artifact.get('digest')=='sha256:'+wanted_digest,'API digest differs from native needs output')
    source=artifact.get('workflow_run',{})
    c.need(c.same([source.get('id'),source.get('head_sha')],[identity['run_id'],identity['head_sha']]), 'artifact source run/head differs')
    return size

def download(artifact_id, artifact_digest, output):
    identity=c.run_identity()
    c.need(type(artifact_id) is int and artifact_id>0,'artifact ID required')
    wanted=digest_value(artifact_digest)
    value=c.admit(evidence.ROOT)
    output=Path(output).absolute()
    temp=Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
    c.need(output.parent.resolve(strict=True).is_relative_to(temp) and not output.exists() and not output.is_symlink(),'fresh runner output required')
    transport=output.with_name(output.name+'-download')
    transport.mkdir(mode=0o700,exist_ok=False)
    c.save(transport/'REQUEST.json',{'run':identity,'artifact_id':artifact_id,'needs_digest':'sha256:'+wanted,'output':str(output),'retries':0})
    repo=identity['repository']
    run_path=io.fetch(transport,'run-metadata',f"repos/{repo}/actions/runs/{identity['run_id']}/attempts/{identity['run_attempt']}",maximum=1024**2,timeout=120)
    metadata=io.fetch(transport,'artifact-metadata',f'repos/{repo}/actions/artifacts/{artifact_id}',maximum=1024**2,timeout=120)
    size=validate_metadata(c.read(run_path),c.read(metadata),identity,artifact_id,wanted)
    archive=io.fetch(transport,'archive',f'repos/{repo}/actions/artifacts/{artifact_id}/zip',maximum=LIMIT,exact=size,sha256=wanted,timeout=600)
    output.mkdir(mode=0o700)
    with zipfile.ZipFile(archive) as bundle:
        rows=io.regular_archive(bundle,max_bytes=LIMIT,max_files=512)
        for row in rows:
            target=output/row.filename
            if row.is_dir():
                target.mkdir(mode=0o700,parents=True,exist_ok=True)
            else:
                target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                with bundle.open(row) as source,target.open('xb') as dest:
                    shutil.copyfileobj(source,dest,1024**2)
    verified=evidence.verify(output,expected_run=identity,expected_source=value['source'],expected_contract_sha256=c.digest(evidence.ROOT/'contract.json'))
    c.save(transport/'VERIFIED.json',{'passed':True,'run':identity,'artifact_id':artifact_id,'zip':c.pin(archive),'metadata':c.pin(metadata),'native_verification':verified})
    return {'passed':True,'evidence':str(output),'transport':str(transport),'artifact_id':artifact_id,'artifact_digest':'sha256:'+wanted}

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--artifact-id',type=int,required=True)
    p.add_argument('--artifact-digest',required=True)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    print(json.dumps(download(args.artifact_id,args.artifact_digest,args.output),indent=2))

if __name__=='__main__':main()
