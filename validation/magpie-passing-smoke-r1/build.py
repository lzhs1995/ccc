"""Build new candidate/baseline artifacts; no installed binary is reused or replaced."""
import argparse
import json
import os
from pathlib import Path
import subprocess
from common import digest, gha_only, no_portable_selector, pin, require, run_guarded, save, verify_package, verify_source


def main():
    p = argparse.ArgumentParser()
    p.add_argument('variant', choices=['candidate', 'baseline'])
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--package-pin', required=True)
    a = p.parse_args()
    gha_only()
    root = Path(__file__).resolve().parent
    verify_package(root, a.package_pin)
    source = a.source.resolve(strict=True)
    output = a.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    m = json.loads((root / (a.variant + '-source.json')).read_text())
    result = {'status': 'NOT_STARTED', 'variant': a.variant, 'package_pin': a.package_pin,
              'source_manifest': pin(root / (a.variant + '-source.json')),
              'same_historical_binary_claimed': False, 'source_after_match': False}
    try:
        require(subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip() == m['base_commit'], 'wrong base commit')
        require(subprocess.check_output(['git', 'rev-parse', 'HEAD^{tree}'], cwd=source, text=True).strip() == m['base_tree'], 'wrong base tree')
        require(not subprocess.check_output(['git', 'status', '--porcelain'], cwd=source, text=True), 'base checkout is not clean')
        for flags in [['--check'], []]:
            subprocess.run(['git', 'apply', *flags, '--whitespace=error', str(root / (a.variant + '.patch'))], cwd=source, check=True)
        verify_source(source, m)
        env = os.environ.copy()
        env.update(GOTOOLCHAIN='local', GOWORK='off', GOENV='off', GOFLAGS='', GOEXPERIMENT='',
                   GOOS='darwin', GOARCH='arm64', CGO_ENABLED='1', GOMAXPROCS='2')
        for key, name in [('TMPDIR', 'tmp'), ('XDG_CONFIG_HOME', 'config'), ('XDG_CACHE_HOME', 'cache'), ('XDG_DATA_HOME', 'xdg-data')]:
            d = output / name
            d.mkdir()
            env[key] = str(d)
        capture = lambda argv: subprocess.check_output(argv, env=env, text=True).strip()
        require(capture(['go', 'env', 'GOVERSION']) == m['toolchain'], 'wrong Go toolchain')
        result['environment'] = {'go': m['toolchain'], 'target': 'darwin/arm64',
                                 'sdk': capture(['xcrun', '--show-sdk-version']),
                                 'clang': capture(['xcrun', 'clang', '--version']),
                                 'original_sdk': m['original_sdk']}
        env.update(SDKROOT=capture(['xcrun', '--show-sdk-path']), CC=capture(['xcrun', '--find', 'clang']),
                   CXX=capture(['xcrun', '--find', 'clang++']), CGO_CPPFLAGS='')
        for k in ['CGO_CFLAGS', 'CGO_CXXFLAGS', 'CGO_LDFLAGS']:
            env[k] = '-mmacosx-version-min=12.0'
        env['GOFLAGS'] = '-mod=readonly'
        run_guarded(['go', 'mod', 'download'], source, env, output / 'modules', 600)
        verify_source(source, m)
        env.update(GOPROXY='off', GOSUMDB='off', GOFLAGS='')
        binary = output / ('magpie-' + a.variant)
        version = '0.1.1319-gha-passing-' + a.variant + '-20261112-r1'
        argv = ['go', 'build', '-p=1', '-mod=readonly', '-tags=production', '-trimpath',
                '-buildvcs=false', '-ldflags=-s -w -X main.version=' + version, '-o', str(binary), '.']
        result['build'] = run_guarded(argv, source, env, output / 'compile', 1200)
        no_portable_selector(binary)
        result['binary'] = pin(binary)
        result['binary_name'] = binary.name
        result['version'] = version
        result['status'] = 'PASS'
    except Exception as error:
        result.update(status='FAIL', error=type(error).__name__ + ': ' + str(error))
    finally:
        try:
            verify_source(source, m)
            verify_package(root, a.package_pin)
            result['source_after_match'] = True
        except Exception as error:
            result.update(status='FAIL', after_error=str(error))
        save(output / 'BUILD.json', result)
    require(result['status'] == 'PASS', 'build failed; see preserved BUILD.json')


if __name__ == '__main__':
    main()
