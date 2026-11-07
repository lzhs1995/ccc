"""Stage one pinned original artifact on a fresh GitHub Mac; no app launch.

The reviewed pin and exact saved API JSON bytes are local inputs. This script
never fetches/reformats metadata, retries a download, or extracts the app.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile

MAX_OUTER = 8 * 1024**3
MAX_APP = 3 * 1024**3
# Preserve the original fixture's 40 GiB admission floor after staging and
# anticipated inner archive extraction; do not consume it before main entry.
RESERVE = 40 * 1024**3
MAX_JSON = 32 * 1024**2
CHUNK = 1024**2


def need(value, reason):
    if not value:
        raise ValueError(reason)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_regular(path, limit=MAX_JSON):
    path = Path(path)
    need(path.is_absolute() and path == path.resolve(strict=True), 'input path is linked or relative')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        need(stat.S_ISREG(before.st_mode) and before.st_uid == os.geteuid()
             and before.st_nlink == 1 and not before.st_mode & 0o022
             and before.st_size <= limit, 'input is not a bounded owned regular file')
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    need(identity(before) == identity(after) == identity(path.lstat()) and len(data) <= limit,
         'input changed during complete read')
    return data


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            need(key not in result, 'duplicate JSON key')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs)


def pinned_bytes(path, wanted, limit=MAX_JSON):
    need(isinstance(wanted, str) and re.fullmatch('[0-9a-f]{64}', wanted), 'external digest is unfilled')
    raw = read_regular(path, limit)
    need(digest(raw) == wanted, 'reviewed input bytes changed')
    return raw


def load_admission(source, manifest_sha):
    """Authenticate the full decrypted source before importing its admission code."""
    source = Path(source)
    need(source.is_absolute() and source == source.resolve(strict=True), 'source root is linked')
    manifest = decode(pinned_bytes(source / 'PUBLIC-SOURCE-MANIFEST.json', manifest_sha))
    actual = {}
    for path in source.rglob('*'):
        need(not path.is_symlink(), 'linked source member')
        if path.is_file() and path != source / 'PUBLIC-SOURCE-MANIFEST.json':
            actual[path.relative_to(source).as_posix()] = digest(read_regular(path))
        else:
            need(path.is_dir() or path == source / 'PUBLIC-SOURCE-MANIFEST.json', 'special source member')
    need(actual == manifest['files'], 'complete source manifest differs')
    need(read_regular(source / 'stage_fixed_artifact.py') == read_regular(Path(__file__).resolve()),
         'published staging helper differs from sealed source')
    spec = importlib.util.spec_from_file_location('reviewed_fixed_app_admission', source / 'fixed_app_admission.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_api_join(pin, run, artifact):
    """No provenance is available yet: require all original API identity facts now."""
    need(run.get('repository', {}).get('full_name') == pin['build_repository']
         and run.get('id') == pin['build_run_id'] and run.get('run_attempt') == pin['build_run_attempt']
         and run.get('head_sha') == pin['build_preparation_commit']
         and run.get('status') == 'completed' and run.get('conclusion') == 'success',
         'original build run is not the pinned successful attempt')
    need(artifact.get('id') == pin['artifact_id'] and artifact.get('expired') is False
         and artifact.get('name') == f"nested-native-{pin['build_run_id']}-{pin['build_run_attempt']}"
         and artifact.get('digest') == 'sha256:' + pin['artifact_api_digest_sha256']
         and artifact.get('workflow_run', {}).get('id') == pin['build_run_id']
         and artifact.get('workflow_run', {}).get('head_sha') == pin['build_preparation_commit'],
         'original artifact identity differs')
    need(pin['artifact_api_digest_sha256'] == pin['artifact_download_zip_sha256'], 'API and archive digest pins disagree')
    size = artifact.get('size_in_bytes')
    need(type(size) is int and 0 < size <= MAX_OUTER, 'original artifact size is unavailable or exceeds budget')
    need(0 < pin['retained_app_zip_bytes'] <= MAX_APP, 'app archive exceeds staging budget')
    return size


def preflight(pin_path, pin_sha, metadata, admission):
    raw_pin = pinned_bytes(pin_path, pin_sha)
    pin = admission.require_pin(decode(raw_pin))
    metadata = Path(metadata)
    need(metadata.is_absolute() and metadata == metadata.resolve(strict=True), 'metadata root is linked')
    raw_run = pinned_bytes(metadata / 'BUILD-RUN.json', pin['build_run_json_sha256'])
    raw_artifact = pinned_bytes(metadata / 'ARTIFACT.json', pin['artifact_json_sha256'])
    run, artifact = decode(raw_run), decode(raw_artifact)
    outer_bytes = verify_api_join(pin, run, artifact)
    return {'pin': pin, 'run': run, 'artifact': artifact, 'outer_bytes': outer_bytes,
            'raw_run': raw_run, 'raw_artifact': raw_artifact, 'pin_sha256': pin_sha}


def capacity(directory, required):
    need(shutil.disk_usage(directory).free >= required, 'insufficient disposable-runner capacity')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def checked_blob_url(value):
    parsed = urllib.parse.urlsplit(value)
    need(parsed.scheme == 'https' and parsed.hostname is not None
         and parsed.hostname.endswith('.blob.core.windows.net') and parsed.port in (None, 443)
         and not parsed.username and not parsed.password and not parsed.fragment,
         'artifact redirect is not an admitted GitHub artifact blob endpoint')
    return value


def download_response(plan, opener=None):
    """One authenticated API request, then one unauthenticated signed-blob request."""
    token = os.environ.get('GH_TOKEN', '')
    need(bool(token), 'GitHub download token unavailable')
    opener = opener or urllib.request.build_opener(NoRedirect())
    pin = plan['pin']
    endpoint = f"https://api.github.com/repos/lzhs1995/ccc/actions/artifacts/{pin['artifact_id']}/zip"
    request = urllib.request.Request(endpoint, headers={
        'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'reviewed-fixed-artifact-stager'})
    try:
        response = opener.open(request, timeout=60)
    except urllib.error.HTTPError as error:
        try:
            need(error.code == 302, 'original artifact API did not return one download redirect')
            target = checked_blob_url(error.headers.get('Location', ''))
        finally:
            error.close()
    except (OSError, urllib.error.URLError):
        raise ValueError('original artifact API request failed') from None
    else:
        response.close()
        raise ValueError('original artifact API did not return one download redirect')
    # Never copy authentication headers to the signed blob endpoint or log its URL.
    try:
        response = opener.open(urllib.request.Request(target, headers={
            'User-Agent': 'reviewed-fixed-artifact-stager'}), timeout=60)
    except urllib.error.HTTPError as error:
        error.close()
        raise ValueError('original artifact blob request failed') from None
    except (OSError, urllib.error.URLError):
        raise ValueError('original artifact blob request failed') from None
    if response.status != 200:
        response.close()
        raise ValueError('artifact blob status differs')
    return response


def copy_bounded(source, destination, size, wanted):
    hashed = hashlib.sha256(); count = 0
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as output:
        while True:
            block = source.read(min(CHUNK, size - count + 1))
            if not block:
                break
            count += len(block)
            need(count <= size, 'download/member exceeds original byte count')
            output.write(block); hashed.update(block)
        output.flush(); os.fsync(output.fileno())
    need(count == size and hashed.hexdigest() == wanted, 'download/member bytes differ from original pin')


def write_once(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())


def stage_plan(plan, output, admission, *, download=download_response):
    output = Path(output)
    temporary = Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
    need(output.is_absolute() and output == output.resolve(strict=False)
         and output.parent.is_relative_to(temporary) and output != temporary
         and not output.exists() and not output.is_symlink(), 'fresh private output below runner temp required')
    need(output.parent.is_dir() and output.parent.stat().st_uid == os.geteuid(), 'output parent is not owned')
    pin = plan['pin']
    capacity(output.parent, plan['outer_bytes'] + pin['retained_app_zip_bytes'] + MAX_APP + RESERVE)
    output.mkdir(mode=0o700)
    write_once(output / 'BUILD-RUN.json', plan['raw_run'])
    write_once(output / 'ARTIFACT.json', plan['raw_artifact'])
    archive = output / 'artifact.zip'
    with download(plan) as response:
        length = response.headers.get('Content-Length')
        need(length is None or (length.isdecimal() and int(length) == plan['outer_bytes']),
             'HTTP content length differs from original artifact API')
        copy_bounded(response, archive, plan['outer_bytes'], pin['artifact_download_zip_sha256'])
    admission.validate_artifact_members(archive, pin)
    capacity(output, pin['retained_app_zip_bytes'] + MAX_APP + RESERVE)
    with zipfile.ZipFile(archive) as zipped:
        for name, destination, size, wanted in (
            (pin['artifact_members']['provenance'], 'PROVENANCE.json', None, pin['provenance_sha256']),
            (pin['artifact_members']['app_archive'], 'cmux-nested-fixed.zip', pin['retained_app_zip_bytes'], pin['retained_app_zip_sha256']),
        ):
            row = zipped.getinfo(name)
            size = row.file_size if size is None else size
            need(destination != 'PROVENANCE.json' or size <= MAX_JSON, 'provenance exceeds bound')
            with zipped.open(row) as stream:
                copy_bounded(stream, output / destination, size, wanted)
    provenance = decode(read_regular(output / 'PROVENANCE.json'))
    admission.validate_metadata(pin, plan['run'], plan['artifact'], provenance)
    admission.inspect_zip(output / 'cmux-nested-fixed.zip', pin['app_name'])
    receipt = {'schema': 'fixed-artifact-staging-v1', 'status': 'STAGED_AUTHENTICATED_ARCHIVES_NOT_LAUNCHED',
        'pin_sha256': plan['pin_sha256'], 'original_run': pin['build_run_id'],
        'original_attempt': pin['build_run_attempt'], 'artifact_id': pin['artifact_id'],
        'artifact_bytes': plan['outer_bytes'], 'artifact_sha256': pin['artifact_download_zip_sha256'],
        'api_requests': 1, 'blob_requests': 1, 'retries': 0, 'metadata_fetched': False,
        'app_extracted': False, 'app_launched': False, 'complete_nested_restore_verified': False}
    write_once(output / 'STAGING.json', (json.dumps(receipt, indent=2) + '\n').encode())
    return receipt


def run(args, *, loader=load_admission, download=download_response):
    need(sys.platform == 'darwin' and os.environ.get('GITHUB_ACTIONS') == 'true'
         and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted'
         and os.environ.get('GITHUB_REPOSITORY') == 'lzhs1995/ccc', 'only a disposable original GitHub Mac may stage')
    # Reject the pending template before source imports, API reads or networking.
    preliminary = decode(pinned_bytes(args.pin, args.pin_sha256))
    need(preliminary.get('status') == 'PINNED_ORIGINAL_BUILD', 'real fixed-app pin is not ready')
    admission = loader(args.source, args.source_manifest_sha256)
    plan = preflight(args.pin, args.pin_sha256, args.metadata, admission)
    if args.check_only:
        return {'status': 'LOCAL_PINS_AND_ORIGINAL_API_BYTES_VERIFIED_NO_NETWORK'}
    return stage_plan(plan, args.output, admission, download=download)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--source-manifest-sha256', required=True)
    parser.add_argument('--pin', type=Path, required=True)
    parser.add_argument('--pin-sha256', required=True)
    parser.add_argument('--metadata', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--check-only', action='store_true')
    print(json.dumps(run(parser.parse_args()), sort_keys=True))


if __name__ == '__main__':
    main()
