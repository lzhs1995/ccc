"""Provider switch/import repair without credentials, foreign hooks or selection drift."""
import copy
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import configuration_guardian as g
import manage_install as m


class GuardianTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.config = self.home / '.claude/settings.json'
        self.doc = {'env': {'API_KEY': 'fixture-secret', 'BASE_URL': 'fixture-url'},
                    'model': 'fixture-model', 'permissions': {'allow': ['Read']},
                    'hooks': {'Stop': [{'matcher': 'keep', 'hooks': [
                        {'type': 'command', 'command': 'foreign-fixture-hook'}]}]}}

    def write(self, path, doc):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc))

    def db(self):
        path = self.home / '.cc-switch/cc-switch.db'
        path.parent.mkdir(parents=True)
        db = sqlite3.connect(path)
        db.execute('CREATE TABLE providers (id TEXT, app_type TEXT, settings_config TEXT, is_current INTEGER)')
        db.execute('CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)')
        db.execute('INSERT INTO providers VALUES (?,?,?,?)', ('a', 'claude', json.dumps(self.doc), 1))
        db.execute('INSERT INTO providers VALUES (?,?,?,?)', ('c', 'codex', '{"keep":"codex"}', 1))
        db.commit()
        return db

    def test_switch_erasing_live_hooks_is_repaired_idempotently(self):
        self.write(self.config, self.doc)
        self.assertTrue(g.reconcile_files(self.home, m, False)[0]['change_needed'])
        self.assertEqual(json.loads(self.config.read_text()), self.doc)
        for _ in range(3):
            self.write(self.config, self.doc)  # cc-switch replaced settings
            g.reconcile_files(self.home, m, True)
            result = json.loads(self.config.read_text())
            self.assertEqual(result['env'], self.doc['env'])
            self.assertEqual(g.foreign_hooks(result, m, self.home), self.doc['hooks'])
            commands = [h['command'] for group in result['hooks'].values() for entry in group for h in entry['hooks']]
            self.assertEqual(commands.count(m.command('cmux_executor_reask_stop_guard')), 1)
            self.assertFalse(any(x['change_needed'] for x in g.reconcile_files(self.home, m, True)))
        snapshots = list(g.state_root(self.home).rglob('*previous-hooks.json'))
        self.assertEqual(len(snapshots), 2)
        self.assertNotIn('fixture-secret', ''.join(p.read_text() for p in snapshots))

    def test_mixed_gate_and_advisory_profiles_upgrade_without_foreign_hook_loss(self):
        release = self.home / '.local/share/multi-agent-collaboration/releases/old/source/scripts'
        hook = 'cmux_consensus_stop_guard.py'
        owned = [f'python3 -B {release / hook}',
                 f'python3 -B {release}/cmux_workflow_advisory.py --hook {hook}']
        foreign = f'python3 /opt/foreign/scripts/cmux_workflow_advisory.py --hook {hook}'
        self.doc['hooks']['Stop'][0]['hooks'].extend(
            {'type': 'command', 'command': command} for command in owned + [foreign])
        expected_foreign = g.foreign_hooks(self.doc, m, self.home)
        db = self.db()
        self.write(self.config, self.doc)
        g.reconcile_files(self.home, m, True)
        self.assertEqual(g.reconcile_profiles(self.home, m, True)['changed'], 1)
        documents = [json.loads(self.config.read_text()), json.loads(
            db.execute("SELECT settings_config FROM providers WHERE id='a'").fetchone()[0])]
        for document in documents:
            self.assertEqual(g.foreign_hooks(document, m, self.home), expected_foreign)
            commands = [row['command'] for entries in document['hooks'].values()
                        for entry in entries for row in entry['hooks']]
            self.assertIn(foreign, commands)
            self.assertFalse(set(owned) & set(commands))
            for name in dict(m.GUARDS, **m.OPTIONAL_GUARDS):
                self.assertEqual(commands.count(m.command(name)), 1)
            self.assertEqual(document['env'], self.doc['env'])
        self.assertEqual(g.reconcile_profiles(self.home, m, True)['changed'], 0)
        self.assertFalse(any(row['change_needed'] for row in g.reconcile_files(self.home, m, True)))
        db.close()

    def test_imported_profiles_and_common_snippet_preserve_credentials_selection_and_foreign_hooks(self):
        db = self.db()
        common = m.transform({'env': {'COMMON': 'keep'}, 'hooks': self.doc['hooks']}, executor_reask=True)
        db.execute('INSERT INTO settings VALUES (?,?)', ('common_config_claude', json.dumps(common)))
        db.commit()
        g.reconcile_profiles(self.home, m, True)
        db.execute('INSERT INTO providers VALUES (?,?,?,?)', ('b', 'claude', json.dumps(self.doc), 0))
        db.commit()
        r = g.reconcile_profiles(self.home, m, True)
        self.assertEqual((r['profiles'], r['changed']), (2, 1))
        rows = db.execute('SELECT id, settings_config, is_current FROM providers ORDER BY id').fetchall()
        self.assertEqual([r[2] for r in rows], [1, 0, 1])
        self.assertEqual(rows[-1][1], '{"keep":"codex"}')
        for _, raw, _ in rows[:2]:
            parsed = json.loads(raw)
            self.assertEqual(parsed['env'], self.doc['env'])
            self.assertEqual(g.foreign_hooks(parsed, m, self.home), self.doc['hooks'])
        snippet = json.loads(db.execute('SELECT value FROM settings').fetchone()[0])
        self.assertEqual(snippet, {'env': {'COMMON': 'keep'}, 'hooks': self.doc['hooks']})
        self.assertEqual(g.reconcile_profiles(self.home, m, True)['changed'], 0)
        db.close()

    def test_bad_profile_rolls_back_without_logging_credentials(self):
        db = self.db()
        db.execute('INSERT INTO providers VALUES (?,?,?,?)', ('z', 'claude', 'broken secret', 0))
        db.commit()
        with self.assertRaises(ValueError):
            g.reconcile_profiles(self.home, m, True)
        self.assertEqual(json.loads(db.execute("SELECT settings_config FROM providers WHERE id='a'").fetchone()[0]), self.doc)
        db.close()

    def test_cas_and_symlink_refusal_preserve_concurrent_config(self):
        self.write(self.config, self.doc)
        with self.assertRaisesRegex(RuntimeError, 'concurrently'):
            g.atomic_json(self.config, {}, b'{}', compare=True)
        before = self.config.read_bytes()
        with patch.object(g.tempfile, 'mkstemp', wraps=g.tempfile.mkstemp) as make:
            original = g.os.fsync
            def concurrent(fd):
                original(fd)
                self.config.write_text('{"new_provider":"keep"}')
            with patch.object(g.os, 'fsync', side_effect=concurrent):
                with self.assertRaises(RuntimeError):
                    g.atomic_json(self.config, {}, before, compare=True)
            self.assertEqual(make.call_count, 1)
        self.assertEqual(json.loads(self.config.read_text()), {'new_provider': 'keep'})
        link = self.home / 'link.json'
        link.symlink_to(self.config)
        with self.assertRaises(ValueError):
            g.atomic_json(link, {})
        self.assertFalse(list(self.config.parent.glob('.guardian-*')))

    def test_one_launchd_job_watches_switches_and_has_no_terminal_arguments(self):
        spec = g.launchd_spec(self.home, '/fixture/source', sys.executable, '/fixture/CURRENT.json')
        self.assertEqual(spec['StartInterval'], 60)
        self.assertEqual(spec['ThrottleInterval'], 60)
        self.assertEqual(spec['Label'], g.LABEL)
        self.assertEqual(spec['WatchPaths'], [str(self.home / '.claude/settings.json'),
                                             str(self.home / '.codex/hooks.json'),
                                             '/fixture/CURRENT.json'])
        self.assertIn('reconcile', spec['ProgramArguments'])
        self.assertNotIn('send', spec['ProgramArguments'])

    def test_repeated_noop_planning_allows_independent_writer_commit(self):
        db = self.db()
        self.addCleanup(db.close)
        self.assertEqual(g.reconcile_profiles(self.home, m, True)['changed'], 1)
        database = self.home / '.cc-switch/cc-switch.db'
        connect, normalize = sqlite3.connect, g.normalized
        commits = []

        def while_planning(*args):
            # DELETE journal mode makes an unclosed reader block this commit.
            writer = connect(database, timeout=0)
            try:
                writer.execute('BEGIN IMMEDIATE')
                writer.execute("INSERT OR REPLACE INTO settings VALUES ('request-log', ?)",
                               (str(len(commits)),))
                writer.commit()
                commits.append(True)
            finally:
                writer.close()
            return normalize(*args)

        with patch.object(g, 'normalized', side_effect=while_planning), \
                patch.object(g.sqlite3, 'connect', wraps=connect) as opened:
            for _ in range(3):
                result = g.reconcile_profiles(self.home, m, True)
                self.assertEqual((result['status'], result['changed']), ('CHECKED', 0))
                self.assertFalse(result['write_committed'])
                self.assertFalse(result['timings']['write_transaction_started'])
            self.assertEqual(opened.call_count, 3)
            self.assertTrue(all(call.args[0].endswith('?mode=ro')
                                for call in opened.call_args_list))
        self.assertEqual(len(commits), 3)

    def test_later_profile_concurrent_edit_rolls_back_earlier_update(self):
        db = self.db()
        self.addCleanup(db.close)
        original = json.dumps(self.doc)
        db.execute('INSERT INTO providers VALUES (?,?,?,?)', ('b', 'claude', original, 0))
        db.commit()
        database = self.home / '.cc-switch/cc-switch.db'
        plan = g.profile_plan
        external = json.dumps(dict(self.doc, model='user-concurrent-model'))

        def edit_after_snapshot(*args):
            result = plan(*args)
            writer = sqlite3.connect(database, timeout=0)
            try:
                writer.execute("UPDATE providers SET settings_config=? WHERE id='b'", (external,))
                writer.commit()
            finally:
                writer.close()
            return result

        with patch.object(g, 'profile_plan', side_effect=edit_after_snapshot):
            result = g.reconcile_profiles(self.home, m, True)
        self.assertEqual(result['status'], 'CONCURRENT_EDIT')
        self.assertTrue(result['timings']['write_transaction_started'])
        self.assertFalse(result['write_committed'])
        self.assertEqual(db.execute("SELECT settings_config FROM providers WHERE id='a'").fetchone()[0], original)
        self.assertEqual(db.execute("SELECT settings_config FROM providers WHERE id='b'").fetchone()[0], external)

    def test_common_concurrent_edit_rolls_back_profile_updates(self):
        db = self.db()
        self.addCleanup(db.close)
        common = m.transform({'env': {'COMMON': 'keep'}, 'hooks': self.doc['hooks']}, executor_reask=True)
        db.execute('INSERT INTO settings VALUES (?,?)', ('common_config_claude', json.dumps(common)))
        db.commit()
        plan = g.profile_plan
        external = json.dumps({'env': {'COMMON': 'user-edit'}})

        def edit_common(*args):
            result = plan(*args)
            writer = sqlite3.connect(self.home / '.cc-switch/cc-switch.db', timeout=0)
            try:
                writer.execute("UPDATE settings SET value=? WHERE key='common_config_claude'", (external,))
                writer.commit()
            finally:
                writer.close()
            return result

        with patch.object(g, 'profile_plan', side_effect=edit_common):
            result = g.reconcile_profiles(self.home, m, True)
        self.assertEqual(result['status'], 'CONCURRENT_EDIT')
        self.assertEqual(result['changed'], 0)
        self.assertEqual(json.loads(db.execute("SELECT settings_config FROM providers WHERE id='a'").fetchone()[0]), self.doc)
        self.assertEqual(db.execute("SELECT value FROM settings WHERE key='common_config_claude'").fetchone()[0], external)

    def test_existing_writer_is_bounded_visible_and_releases_connection(self):
        db = self.db()
        self.addCleanup(db.close)
        db.execute('BEGIN IMMEDIATE')
        started = time.monotonic()
        result = g.reconcile_profiles(self.home, m, True)
        elapsed = time.monotonic() - started
        self.assertEqual(result['status'], 'LOCK_BUSY')
        self.assertFalse(result['timings']['write_transaction_started'])
        self.assertFalse(result['write_committed'])
        self.assertLess(elapsed, 2.0)
        db.rollback()
        self.assertEqual(g.reconcile_profiles(self.home, m, True)['changed'], 1)

    def test_snapshot_contention_propagates_to_status_and_cli_exit(self):
        db = self.db()
        self.addCleanup(db.close)
        db.execute('BEGIN EXCLUSIVE')
        out = io.StringIO()
        with patch.object(g, 'load_release', return_value=({'source': '/fixture/source'}, m)), \
                redirect_stdout(out):
            code = g.main(['reconcile', '--home', str(self.home), '--apply'])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.getvalue())['status'], 'LOCK_BUSY')
        status = json.loads((g.state_root(self.home) / 'configuration-guardian-v1/STATUS.json').read_text())
        self.assertEqual(status['profiles']['status'], 'LOCK_BUSY')
        self.assertEqual(status['input_operations'], 0)
        self.assertNotIn('fixture-secret', out.getvalue())
        db.rollback()
        db.execute('BEGIN EXCLUSIVE')
        db.rollback()

    def test_snapshot_budget_and_limits_leave_no_reader_lock(self):
        db = self.db()
        self.addCleanup(db.close)
        for setting, limit, expected in [
                ('PROFILE_BUDGET_SECONDS', 0, 'PROFILE_BUDGET_EXHAUSTED'),
                ('MAX_PROFILES', 0, 'PROFILE_SNAPSHOT_LIMIT'),
                ('MAX_PROFILE_SNAPSHOT_BYTES', 1, 'PROFILE_SNAPSHOT_LIMIT')]:
            with self.subTest(setting=setting), patch.object(g, setting, limit):
                result = g.reconcile_profiles(self.home, m, True)
                self.assertEqual(result['status'], expected)
                self.assertFalse(result['write_committed'])
                db.execute('BEGIN EXCLUSIVE')
                db.rollback()
        self.assertEqual(json.loads(db.execute("SELECT settings_config FROM providers WHERE id='a'").fetchone()[0]), self.doc)

    def test_expensive_write_budget_rolls_back_and_releases_writer(self):
        db = self.db()
        self.addCleanup(db.close)
        db.execute('INSERT INTO providers VALUES (?,?,?,?)', ('b', 'claude', json.dumps(self.doc), 0))
        db.execute("CREATE TRIGGER expensive_update AFTER UPDATE ON providers "
                   "WHEN new.id='a' BEGIN SELECT fixture_expire(); END")
        db.commit()
        real_clock, connect = time.monotonic, sqlite3.connect
        offset = [0]

        def expire():
            offset[0] += 2.0  # Simulate time spent by a SQLite trigger, without sleeping.
            return 0

        def connection(*args, **kwargs):
            result = connect(*args, **kwargs)
            result.create_function('fixture_expire', 0, expire)
            return result

        with patch.object(g.sqlite3, 'connect', side_effect=connection), \
                patch.object(g.time, 'monotonic', side_effect=lambda: real_clock() + offset[0]):
            result = g.reconcile_profiles(self.home, m, True)
        self.assertEqual(result['status'], 'PROFILE_BUDGET_EXHAUSTED')
        self.assertTrue(result['timings']['write_transaction_started'])
        self.assertFalse(result['write_committed'])
        self.assertEqual([json.loads(raw) for (raw,) in db.execute(
            "SELECT settings_config FROM providers WHERE app_type='claude'")], [self.doc, self.doc])
        db.execute('BEGIN EXCLUSIVE')
        db.rollback()

    def test_sqlite_exception_rolls_back_all_updates_and_releases_writer(self):
        db = self.db()
        self.addCleanup(db.close)
        db.execute('INSERT INTO providers VALUES (?,?,?,?)', ('b', 'claude', json.dumps(self.doc), 0))
        db.execute("CREATE TRIGGER fail_update BEFORE UPDATE ON providers WHEN new.id='b' "
                   "BEGIN SELECT RAISE(ABORT, 'fixture-secret'); END")
        db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            g.reconcile_profiles(self.home, m, True)
        self.assertEqual([json.loads(raw) for (raw,) in db.execute(
            "SELECT settings_config FROM providers WHERE app_type='claude'")], [self.doc, self.doc])
        db.execute('BEGIN EXCLUSIVE')
        db.rollback()

    def test_database_replacement_during_planning_is_not_written(self):
        db = self.db()
        db.close()
        database = self.home / '.cc-switch/cc-switch.db'
        plan = g.profile_plan

        def replace_database(*args):
            result = plan(*args)
            replacement = database.with_suffix('.replacement')
            shutil.copyfile(database, replacement)
            replacement.replace(database)
            return result

        with patch.object(g, 'profile_plan', side_effect=replace_database):
            result = g.reconcile_profiles(self.home, m, True)
        self.assertEqual(result['status'], 'CONCURRENT_EDIT')
        self.assertFalse(result['timings']['write_transaction_started'])
        reader = sqlite3.connect(database)
        try:
            self.assertEqual(json.loads(reader.execute(
                "SELECT settings_config FROM providers WHERE id='a'").fetchone()[0]), self.doc)
        finally:
            reader.close()

    def test_disable_all_hooks_is_visible_and_never_silently_changed(self):
        doc = dict(self.doc, disableAllHooks=True)
        self.write(self.config, doc)
        self.assertTrue(g.reconcile_files(self.home, m, True)[0]['hooks_disabled'])
        self.assertTrue(json.loads(self.config.read_text())['disableAllHooks'])

    def test_manifest_verifies_immutable_members_and_running_version(self):
        source = self.home / '.local/share/multi-agent-collaboration/releases/fixture/source'
        (source / 'scripts').mkdir(parents=True)
        files = {}
        for name in ('configuration_guardian.py', 'manage_install.py'):
            path = source / 'scripts' / name
            raw = (Path(g.__file__).parent / name).read_bytes()
            path.write_bytes(raw)
            files['scripts/' + name] = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
            path.chmod(0o444)
        manifest = source.parent / 'MANIFEST.json'
        self.write(manifest, {'files': files})
        current = self.home / 'CURRENT.json'
        self.write(current, {'source': str(source), 'python': sys.executable,
                             'manifest_sha256': hashlib.sha256(manifest.read_bytes()).hexdigest()})
        with patch.object(g, '__file__', str(source / 'scripts/configuration_guardian.py')):
            g.load_release(self.home, current)
            member = source / 'scripts/manage_install.py'
            member.chmod(0o644)
            with self.assertRaisesRegex(ValueError, 'writable'):
                g.load_release(self.home, current)
            member.write_text('changed')
            member.chmod(0o444)
            with self.assertRaisesRegex(ValueError, 'mismatch'):
                g.load_release(self.home, current)


if __name__ == '__main__':
    unittest.main()
