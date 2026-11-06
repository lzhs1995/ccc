"""Draft admission and typed result checks; does not run builds or tests itself."""
from pathlib import Path
import hashlib
import importlib.util
import json
import re
import sys

def contract(root):
    return json.loads((root / 'contract.json').read_text())

def admit(root):
    value = contract(root)
    if value['upstream_commit'] != '2e109a1739d320fe36220afb3e7775b59c1acd47':
        raise ValueError('upstream source changed')
    for key, name in [('tests_patch_sha256', 'tests.patch'), ('fix_patch_sha256', 'fix.patch')]:
        wanted = value.get(key)
        if not isinstance(wanted, str) or not re.fullmatch('[0-9a-f]{64}', wanted):
            raise ValueError('reviewed patch hash missing: ' + key)
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != wanted:
            raise ValueError('patch hash mismatch: ' + name)
    if value.get('selector') != 'cmuxTests/HerdrStartupRestoreRegressionTests':
        raise ValueError('reviewed regression suite selector is required')
    for key in ['expected_red_test_ids', 'required_green_test_ids', 'expected_red_assertion_text',
                'boundary_test_ids', 'existing_binding_test_ids', 'package_test_display_names']:
        if not isinstance(value.get(key), list) or not value[key] or not all(isinstance(x, str) and x for x in value[key]):
            raise ValueError('reviewed expected behavior missing: ' + key)
        if len(set(value[key])) != len(value[key]):
            raise ValueError('duplicate contract entries: ' + key)
    suite_prefix = value['selector'].removeprefix('cmuxTests/') + '/'
    if not all(test.startswith(suite_prefix) for test in value['required_green_test_ids']):
        raise ValueError('green test identifiers must belong to the selected suite')
    if not set(value['expected_red_test_ids']).issubset(value['required_green_test_ids']):
        raise ValueError('every red regression must also pass green')
    for phase in ('boundary', 'existing-binding'):
        selector, expected = phase_contract(value, phase)
        prefix = selector.removeprefix('cmuxTests/') + '/'
        if not all(test.startswith(prefix) for test in expected):
            raise ValueError('test identifier outside ' + phase + ' suite')
    original_ids = {
        'HerdrResumeBindingTests/testNamedSessionAttachProducesAutomaticProcessDetectedBinding()',
        'HerdrResumeBindingTests/testDefaultSessionAttachBindsWithoutCheckpoint()',
        'HerdrResumeBindingTests/testClientsInsideAHerdrPaneYieldNoBinding()',
        'HerdrResumeBindingTests/testProcessScanPrefersTheSurfaceClientOverAProcessInsideAHerdrPane()',
    }
    if set(value['existing_binding_test_ids']) != original_ids:
        raise ValueError('all four migrated original binding cases are required')
    package_display_names = {
        'Restore admission composes with relaunch spawn pacing',
        'Explicit input cancels a deferred agent resume before admission',
        'Cancelling deferred admission drops the resume command',
        'Expired restore input stays cancelled across topology commit',
        'Cancellation after admission removes unsent prompt input',
        'A stale cancellation cannot suppress replacement runtime input',
    }
    if set(value['package_test_display_names']) != package_display_names:
        raise ValueError('all six package policy cases are required')
    return value

def phase_contract(value, phase):
    if phase in ('red', 'green'):
        return value['selector'], value['required_green_test_ids']
    if phase == 'boundary':
        return 'cmuxTests/HerdrStartupRestoreBoundaryTests', value['boundary_test_ids']
    if phase == 'existing-binding':
        return 'cmuxTests/HerdrResumeBindingTests', value['existing_binding_test_ids']
    raise ValueError('unknown phase')

def verdict(root, phase, checkout, evidence):
    value = admit(root)
    selector, required_ids = phase_contract(value, phase)
    path = checkout / 'scripts/ci/app_host_result_accounting.py'
    spec = importlib.util.spec_from_file_location('nested_upstream_accounting', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    results = module.parse_xcresult_tests(json.loads((evidence / 'tests.json').read_text()))
    status = int((evidence / 'exit-code.txt').read_text())
    log = (evidence / 'execution.log').read_text()
    complete, reason = module.run_is_complete(log)
    if not complete or 'Post-test timed out after' in log:
        raise ValueError('incomplete native run: ' + reason)
    if not results:
        raise ValueError('no typed test executions')
    if set(results) != set(required_ids):
        raise ValueError('actual test inventory differs from reviewed contract')
    if phase == 'red':
        if status != 65:
            raise ValueError('red must be xcode test assertion failure, not setup/timeout')
        wanted = value['expected_red_test_ids']
        if not all(results.get(test) == 'Failed' for test in wanted):
            raise ValueError('expected regression cases did not fail')
        unexpected = {k:v for k,v in results.items() if v not in ('Passed', 'Failed') or (v == 'Failed' and k not in wanted)}
        if unexpected:
            raise ValueError('unclassified red results: ' + repr(unexpected))
        if not all(text in log for text in value['expected_red_assertion_text']):
            raise ValueError('expected behavior assertions absent from red log')
    elif phase in ('green', 'boundary', 'existing-binding'):
        if status != 0 or any(result != 'Passed' for result in results.values()):
            raise ValueError('green requires every executed test passed')
        if not all(results.get(test) == 'Passed' for test in required_ids):
            raise ValueError('required regressions not executed and passed')
    else:
        raise ValueError('unknown phase')
    receipt = {'phase': phase, 'exit_code': status, 'selector': selector,
               'executed_test_count': len(results), 'results': results,
               'tests_patch_sha256': value['tests_patch_sha256'],
               'fix_patch_sha256': value['fix_patch_sha256']}
    (evidence / 'VERDICT.json').write_text(json.dumps(receipt, indent=2) + '\n')

def package_verdict(root, checkout, evidence):
    value = admit(root)
    path = checkout / 'scripts/ci/require_swift_test_execution.py'
    spec = importlib.util.spec_from_file_location('nested_swift_execution', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    log = (evidence / 'execution.log').read_text()
    clean_log = module.ANSI.sub('', log)
    wrapper_log = (evidence / 'wrapper.log').read_text()
    if any(marker in log + wrapper_log for marker in (
        'Exited with unexpected signal code', 'Restarting after unexpected exit',
        'Post-test timed out after', 'Idle timed out after',
    )):
        raise ValueError('package run interrupted')
    status = int((evidence / 'exit-code.txt').read_text())
    count = module.execution_count(log)
    if count != len(value['package_test_display_names']):
        raise ValueError('unexpected package executed count')
    # Every @Test in this exact suite has an explicit human display name.
    # Swift Testing reports that quoted name instead of the Swift function ID.
    passed = re.findall(r'^\s*✔ Test "([^"\n]+)" passed after .+\.$', clean_log, re.M)
    if len(passed) != len(set(passed)) or set(passed) != set(value['package_test_display_names']):
        raise ValueError('package case completion evidence differs from contract')
    if not re.search(r'^\s*✔ Suite "?TerminalSurfaceStartupRestorePolicyTests"? passed after .+\.$', clean_log, re.M):
        raise ValueError('package suite completion evidence missing')
    # Same narrow exception as exact upstream package-test-lane.sh CmuxTerminal
    # branch. Preserve original exit 1; never tolerate a failed/partial test run.
    error_lines = [line for line in clean_log.splitlines()
                   if re.search(r'(^|[^a-zA-Z])error:', line)
                   and not re.match(r'\s*warning:|.*:\d+:\d+:\s+warning:', line)]
    cosmetic = status == 1 and bool(error_lines) and all('unexpected binary' in line for line in error_lines)
    if status != 0 and not cosmetic:
        raise ValueError('package command failed outside official cosmetic diagnostic exception')
    receipt = {'phase': 'package', 'exit_code': status, 'executed_test_count': count,
               'package': 'Packages/macOS/CmuxTerminal', 'filter': 'TerminalSurfaceStartupRestorePolicyTests',
               'passed_display_names': passed, 'cosmetic_ghostty_binary_diagnostic': cosmetic,
               'tests_patch_sha256': value['tests_patch_sha256'], 'fix_patch_sha256': value['fix_patch_sha256']}
    (evidence / 'VERDICT.json').write_text(json.dumps(receipt, indent=2) + '\n')

if __name__ == '__main__':
    operation, directory, *args = sys.argv[1:]
    root = Path(directory)
    if operation == 'admit':
        admit(root)
    elif operation == 'selector':
        print(phase_contract(admit(root), args[0])[0])
    elif operation == 'verdict':
        verdict(root, args[0], Path(args[1]), Path(args[2]))
    elif operation == 'package-verdict':
        package_verdict(root, Path(args[0]), Path(args[1]))
    else:
        raise SystemExit('unknown operation')
