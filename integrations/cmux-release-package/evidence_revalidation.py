"""Export this distinct validation attempt, retaining the original source failure."""
import argparse
import os
from pathlib import Path
import shutil
import stat

import source_contract as shared
import retained_release as retained
import evidence_release as original
import ci_revalidate

need, save, digest = shared.need, shared.save, shared.digest


def candidates(root):
    files = set(original.evidence_candidates(root))
    source = root / 'original-evidence'
    if source.is_dir():
        files.update(p for p in source.rglob('*') if p.is_file())
    downloads = root / 'downloads'
    if downloads.is_dir():
        files.update(p for p in downloads.rglob('*') if p.is_file()
                     and (p.suffix == '.json' or p.name == 'stderr'))
    need(len(files) <= original.MAX_FILES, 'validation evidence inventory exceeds limit')
    for path in files:
        info = path.lstat()
        need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and path.resolve(strict=True) == path
             and path.is_relative_to(root) and info.st_size <= original.MAX_STREAM_BYTES,
             'unsafe or oversized validation evidence')
        need(not path.name.endswith('.zip'), 'App archives cannot enter small validation evidence')
    return sorted(files)


def export(root, output):
    shared.original_admission().runner_only()
    selection, current = ci_revalidate.context()
    root = Path(root).resolve(strict=True)
    need(root.is_relative_to(Path(os.environ['RUNNER_TEMP']).resolve(strict=True)), 'foreign validation attempt')
    output = shared.fresh_runner_output(output)
    try:
        context = shared.read_json(root / 'RUN-CONTEXT.json')
        need(context['source_run'] == selection['source_run'] and context['validation_run'] == current,
             'exported attempt has foreign source or validation context')
        rows = []
        for path in candidates(root):
            rows.append(original.export_member(path, root, output))
            need(sum(row['bytes'] for row in rows) <= original.MAX_EVIDENCE_BYTES - 2 * 1024**2,
                 'validation evidence exceeds bounded export')
        total = original.verify_exported(output, rows)
        source = output / 'source-pins'; source.mkdir(mode=0o700)
        for name in ('SOURCE-MANIFEST.json', 'FROZEN.json', 'RETAINED-SOURCE.json'):
            shutil.copyfile(shared.ROOT / name, source / name)
        save(source / 'IMPLEMENTATION-PINS.json', [{'file': path.name, 'sha256': digest(path),
             'bytes': path.stat().st_size} for path in sorted(shared.ROOT.glob('*.py'))])
        result_path = root / 'RESULT.json'
        result = shared.read_json(result_path) if result_path.is_file() else None
        if result is not None:
            need(result.get('passed') is True and result.get('source_run') == selection['source_run']
                 and result.get('validation_run') == current and result.get('new_builds') == 0,
                 'successful validation result context differs')
        exported = {'schema': 'cmux-retained-release-evidence-export-v1', 'passed': True,
            'source_run': selection['source_run'], 'validation_run': current, 'run': current,
            'source_run_conclusion': 'failure', 'original_source_failure_retained':
                (output / 'original-evidence/FAILURE.json').is_file(),
            'source_members': rows, 'evidence_bytes_without_export_manifest': total,
            'max_evidence_bytes': original.MAX_EVIDENCE_BYTES,
            'new_validation_result_sha256': digest(result_path) if result else None,
            'new_validation_acceptance_observed': result is not None,
            'source_app_artifact': selection['app_artifact'], 'source_inner_archive': selection['inner_archive'],
            'full_app_in_evidence': False, 'original_command_streams_lossless': True,
            'source_and_validation_contexts_distinct': True, 'new_builds': 0,
            'final_external_validation_run_check_required': True, 'business_adoption_ready': False}
        save(output / 'EXPORT.json', exported)
        inventory = [{'path': p.relative_to(output).as_posix(), 'sha256': digest(p), 'bytes': p.stat().st_size}
                     for p in sorted(output.rglob('*')) if p.is_file()]
        save(output / 'EVIDENCE-MANIFEST.json', {'files': inventory, 'source_run': selection['source_run'],
            'validation_run': current, 'run': current,
            'scope': 'Every exported file except this manifest and its SHA receipt'})
        save(output / 'EVIDENCE-MANIFEST-SHA256.json', {'sha256': digest(output / 'EVIDENCE-MANIFEST.json')})
        need(sum(p.stat().st_size for p in output.rglob('*') if p.is_file()) <= original.MAX_EVIDENCE_BYTES,
             'final validation evidence exceeds bound')
        return exported
    except BaseException as error:
        save(output / 'EXPORT-UNKNOWN.json', {'passed': False, 'complete': False, 'error': repr(error),
            'source_run': selection['source_run'], 'validation_run': current, 'automatic_retry': False})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--attempt', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    export(args.attempt, args.output)


if __name__ == '__main__':
    main()
