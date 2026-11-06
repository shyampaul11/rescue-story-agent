"""Private workspace persistence and explicit attributed review; temporary roots only."""
import copy
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from content_models import DRAFT_KEYS, GeneratedDrafts

try:
    store = importlib.import_module('editorial_store')
except ModuleNotFoundError:
    store = None


SCOPE = 'a' * 20


def workspace():
    bundle = {'perspective': 'third_person'}
    for key in DRAFT_KEYS:
        bundle[key] = {'text': 'Azra may need care.\n\nAsk the rescuer.',
                       'claims': [], 'review_notes': []}
    return {'bundle': GeneratedDrafts.model_validate(bundle), 'animal_name': 'Azra',
            'final': {'nl': 'Mijn eigen tekst', 'en': 'My own text'},
            'facebook': {'nl': 'Post', 'en': ''},
            'facebook_baseline': {'nl': '', 'en': ''}, 'undo': {'final_en': ['Before']},
            'usage': {'input_tokens': 10, 'output_tokens': 12, 'total_tokens': 22},
            'profile_snapshot': {'identity': {'animal_name': 'Azra'},
                                 'unknown_information': ['Current care is uncertain.']},
            'assessment': {'version': 1, 'drafts': {}, 'questions': [
                {'id': 'care-question', 'topic': 'treatment', 'question': 'Which care?', 'priority': 1}]},
            'editor_assessments': {'final_en': {'status': 'yellow', 'findings': []}},
            'pending_response': {'candidate': bundle, 'usage': {'input_tokens': 4},
                                 'perspective': 'third_person', 'received_at': '2026-11-05'},
            'outdated': True}


class EditorialStoreTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(store, 'The editorial workspace store must exist')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'editorial'
        self.root.mkdir()

    def test_migration_retires_anonymous_snapshots_before_named_deletion(self):
        source, target, other_scope = SCOPE, 'b' * 20, 'c' * 20
        anonymous = {'bundle': None, 'final': {'nl': '', 'en': 'Anonymous volunteer text'},
                     'undo': {'final_en': ['Earlier volunteer text']}}
        store.save_record(source, anonymous, root=self.root)
        anonymous['final']['en'] = 'Latest anonymous text'
        store.save_record(source, anonymous, root=self.root)
        stale = copy.deepcopy(anonymous)
        desired = copy.deepcopy(anonymous)
        desired['animal_name'] = 'Azra'
        before = copy.deepcopy(desired)
        store.save_record(other_scope, workspace(), root=self.root)
        result = store.migrate_record(source, target, desired, root=self.root)
        migrated = result['record']
        self.assertIsNone(result['migration_warning'])
        self.assertEqual(desired, before)
        self.assertEqual(migrated['scope'], target)
        self.assertEqual(migrated['animal_name'], 'Azra')
        self.assertNotEqual(migrated['storage_session_id'], desired['storage_session_id'])
        self.assertEqual(migrated['undo'], desired['undo'])
        self.assertEqual(store.load_record(target, root=self.root)['final']['en'], 'Latest anonymous text')
        self.assertIsNone(store.load_record(source, root=self.root))
        self.assertFalse((self.root / source).exists())
        with self.assertRaises(store.WorkspaceDeletedError):
            store.save_record(source, stale, root=self.root)
        store.delete_record(target, root=self.root)
        self.assertIsNone(store.load_record(target, root=self.root))
        self.assertIsNone(store.load_record(source, root=self.root))
        fresh = {'final': {'en': 'New unrelated anonymous work'},
                 'storage_deletion_token': result['source_deletion_token']}
        store.save_record(source, fresh, root=self.root)
        store.delete_record(target, root=self.root)
        self.assertEqual(store.load_record(source, root=self.root)['final']['en'],
                         'New unrelated anonymous work')
        self.assertEqual(store.load_record(other_scope, root=self.root)['final']['en'], 'My own text')

    def test_migration_refuses_existing_target_without_retiring_source(self):
        anonymous = {'final': {'en': 'Unassigned text'}, 'bundle': None}
        store.save_record(SCOPE, anonymous, root=self.root)
        store.save_record('b' * 20, workspace(), root=self.root)
        with self.assertRaises(ValueError):
            store.migrate_record(SCOPE, 'b' * 20, anonymous, root=self.root)
        self.assertEqual(store.deletion_token(SCOPE, root=self.root), '')
        self.assertEqual(store.load_record(SCOPE, root=self.root)['final']['en'], 'Unassigned text')
        self.assertEqual(store.load_record('b' * 20, root=self.root)['final']['en'], 'My own text')

    def test_migration_refuses_generated_or_stale_source_and_invalid_scopes(self):
        for field, value in (('bundle', workspace()['bundle']), ('history', [{'bundle': {}}]),
                             ('pending_response', {'candidate': {}})):
            with self.subTest(field=field), self.assertRaises(ValueError):
                store.migrate_record(SCOPE, 'b' * 20, {field: value}, root=self.root)
        for source, target in (('../outside', SCOPE), (SCOPE, '../outside'), (SCOPE, SCOPE)):
            with self.subTest(source=source, target=target), self.assertRaises(ValueError):
                store.migrate_record(source, target, {'bundle': None}, root=self.root)
        stale = {'bundle': None, 'final': {'en': 'Old unassigned text'}}
        store.save_record(SCOPE, stale, root=self.root)
        store.delete_record(SCOPE, root=self.root)
        with self.assertRaises(store.WorkspaceDeletedError):
            store.migrate_record(SCOPE, 'b' * 20, stale, root=self.root)
        self.assertIsNone(store.load_record('b' * 20, root=self.root))

    def test_migration_save_failure_preserves_anonymous_data(self):
        anonymous = {'bundle': None, 'final': {'en': 'Keep source on failure'}}
        store.save_record(SCOPE, anonymous, root=self.root)
        with patch('editorial_store.os.replace', side_effect=OSError('Cannot save target')):
            with self.assertRaises(OSError):
                store.migrate_record(SCOPE, 'b' * 20, anonymous, root=self.root)
        self.assertEqual(store.load_record(SCOPE, root=self.root)['final']['en'], 'Keep source on failure')
        self.assertEqual(store.deletion_token(SCOPE, root=self.root), '')
        self.assertIsNone(store.load_record('b' * 20, root=self.root))

    def test_migration_cleanup_failure_reports_saved_record_and_retires_source(self):
        anonymous = {'bundle': None, 'final': {'en': 'Saved despite cleanup failure'}}
        store.save_record(SCOPE, anonymous, root=self.root)
        original_unlink = os.unlink
        def fail_source_removal(path, *args, **kwargs):
            if Path(path).parent == self.root / SCOPE:
                raise PermissionError('Cannot remove old anonymous snapshot')
            return original_unlink(path, *args, **kwargs)
        with patch('editorial_store.os.unlink', side_effect=fail_source_removal):
            result = store.migrate_record(SCOPE, 'b' * 20, anonymous, root=self.root)
        self.assertTrue(result['migration_warning'])
        self.assertEqual(result['record']['final']['en'], 'Saved despite cleanup failure')
        self.assertEqual(store.load_record('b' * 20, root=self.root)['final']['en'],
                         'Saved despite cleanup failure')
        self.assertIsNone(store.load_record(SCOPE, root=self.root))
        with self.assertRaises(store.WorkspaceDeletedError):
            store.save_record(SCOPE, anonymous, root=self.root)

    def test_migration_marker_failure_preserves_both_recoverable_workspaces(self):
        anonymous = {'bundle': None, 'final': {'en': 'Recoverable volunteer text'}}
        store.save_record(SCOPE, anonymous, root=self.root)
        original_replace = os.replace
        def fail_marker_commit(source, destination):
            if Path(destination).parent == self.root / '_deletions':
                raise OSError('Cannot retire anonymous generation')
            return original_replace(source, destination)
        with patch('editorial_store.os.replace', side_effect=fail_marker_commit):
            with self.assertRaises(OSError):
                store.migrate_record(SCOPE, 'b' * 20, anonymous, root=self.root)
        self.assertEqual(store.deletion_token(SCOPE, root=self.root), '')
        self.assertEqual(store.load_record(SCOPE, root=self.root)['final']['en'], 'Recoverable volunteer text')
        self.assertEqual(store.load_record('b' * 20, root=self.root)['final']['en'],
                         'Recoverable volunteer text')

    def test_migration_refuses_generated_source_even_if_input_omits_bundle(self):
        generated = workspace()
        store.save_record(SCOPE, generated, root=self.root)
        supplied = {'bundle': None, 'final': generated['final'], 'scope': SCOPE,
                    'storage_deletion_token': generated['storage_deletion_token']}
        with self.assertRaises(ValueError):
            store.migrate_record(SCOPE, 'b' * 20, supplied, root=self.root)
        self.assertIsNotNone(store.load_record(SCOPE, root=self.root)['bundle'])
        self.assertIsNone(store.load_record('b' * 20, root=self.root))

    def test_migration_cleanup_preserves_concurrent_fresh_anonymous_start(self):
        anonymous = {'bundle': None, 'final': {'en': 'Text assigned to Azra'}}
        store.save_record(SCOPE, anonymous, root=self.root)
        deleting, release, saving, saved = (threading.Event() for _ in range(4))
        original_unlink = os.unlink
        errors, results = [], []
        def pause_source_cleanup(path, *args, **kwargs):
            if Path(path).parent == self.root / SCOPE:
                deleting.set()
                if not release.wait(5):
                    raise TimeoutError('Source cleanup was not released')
            return original_unlink(path, *args, **kwargs)
        def migrate():
            try:
                results.append(store.migrate_record(SCOPE, 'b' * 20, anonymous, root=self.root))
            except Exception as exc:
                errors.append(exc)
        def fresh_start():
            saving.set()
            try:
                fresh = {'bundle': None, 'final': {'en': 'New anonymous volunteer work'},
                         'storage_deletion_token': store.deletion_token(SCOPE, root=self.root)}
                store.save_record(SCOPE, fresh, root=self.root)
            except Exception as exc:
                errors.append(exc)
            finally:
                saved.set()
        assignment = threading.Thread(target=migrate)
        new_workspace = threading.Thread(target=fresh_start)
        with patch('editorial_store.os.unlink', side_effect=pause_source_cleanup):
            try:
                assignment.start()
                self.assertTrue(deleting.wait(5))
                new_workspace.start()
                self.assertTrue(saving.wait(1))
                self.assertFalse(saved.wait(0.1))
            finally:
                release.set()
                assignment.join(5)
                if new_workspace.ident is not None:
                    new_workspace.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertTrue(saved.is_set())
        self.assertEqual(store.load_record(SCOPE, root=self.root)['final']['en'],
                         'New anonymous volunteer work')
        store.delete_record('b' * 20, root=self.root)
        self.assertEqual(store.load_record(SCOPE, root=self.root)['final']['en'],
                         'New anonymous volunteer work')

    def test_delete_removes_all_selected_data_and_preserves_other_animals_and_credentials(self):
        record = workspace()
        store.save_record(SCOPE, record, root=self.root)
        store.save_record(SCOPE, record, root=self.root)
        store.save_record('b' * 20, workspace(), root=self.root)
        credential = self.root.parent / 'oauth.json'
        credential.write_text('PRIVATE_CREDENTIAL', encoding='utf-8')
        extra = self.root / SCOPE / 'attachments'
        extra.mkdir()
        (extra / 'notes.txt').write_text('PRIVATE_SOURCE', encoding='utf-8')
        result = store.delete_record(SCOPE, root=self.root)
        self.assertEqual(result['deleted_snapshots'], 2)
        self.assertTrue(result['deletion_token'])
        self.assertFalse((self.root / SCOPE).exists())
        self.assertIsNone(store.load_record(SCOPE, root=self.root))
        self.assertEqual(store.load_record('b' * 20, root=self.root)['final']['en'], 'My own text')
        self.assertEqual(credential.read_text(encoding='utf-8'), 'PRIVATE_CREDENTIAL')
        markers = self.root / '_deletions'
        for path in markers.iterdir():
            contents = path.read_text(encoding='utf-8')
            self.assertNotIn('PRIVATE', contents)
            self.assertNotIn('Azra', contents)
            self.assertNotIn('My own text', contents)

    def test_sibling_custom_roots_own_all_markers_and_have_independent_generations(self):
        other_root = self.root.parent / 'other-editorial'
        other_root.mkdir()
        first, other = workspace(), workspace()
        store.save_record(SCOPE, first, root=self.root)
        store.save_record(SCOPE, other, root=other_root)
        deleted = store.delete_record(SCOPE, root=self.root)
        self.assertTrue(deleted['deletion_token'])
        self.assertEqual(store.deletion_token(SCOPE, root=other_root), '')
        other['final']['en'] = 'Independent workspace remains writable'
        store.save_record(SCOPE, other, root=other_root)
        self.assertEqual(store.load_record(SCOPE, root=other_root)['final']['en'],
                         'Independent workspace remains writable')
        self.assertTrue((self.root / '_deletions').is_dir())
        self.assertTrue((other_root / '_deletions').is_dir())
        self.assertFalse((self.root.parent / '_deletions').exists())

    def test_delete_missing_workspace_invalidates_old_tabs_and_allows_explicit_clean_restart(self):
        self.assertEqual(store.deletion_token(SCOPE, root=self.root), '')
        self.assertEqual(list(self.root.iterdir()), [])
        stale = workspace()
        result = store.delete_record(SCOPE, root=self.root)
        self.assertEqual(result['deleted_snapshots'], 0)
        self.assertEqual(store.deletion_token(SCOPE, root=self.root), result['deletion_token'])
        with self.assertRaises(store.WorkspaceDeletedError):
            store.save_record(SCOPE, stale, root=self.root)
        self.assertNotIn('storage_deletion_token', stale)
        clean = {'final': {'nl': '', 'en': 'New work'},
                 'storage_deletion_token': result['deletion_token']}
        store.save_record(SCOPE, clean, root=self.root)
        loaded = store.load_record(SCOPE, root=self.root)
        self.assertEqual(loaded['final']['en'], 'New work')
        self.assertEqual(loaded['storage_deletion_token'], result['deletion_token'])
        store.delete_record(SCOPE, root=self.root)
        with self.assertRaises(store.WorkspaceDeletedError):
            store.save_record(SCOPE, loaded, root=self.root)

    def test_delete_and_marker_reject_invalid_scope_without_touching_files(self):
        for invalid in ('../outside', '', 'A' * 20, 'a' * 20 + '/child', None):
            for operation in (store.delete_record, store.deletion_token):
                with self.subTest(scope=invalid, operation=operation), self.assertRaises(ValueError):
                    operation(invalid, root=self.root)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertFalse((self.root / '_deletions').exists())

    def test_delete_failure_raises_and_marker_prevents_resurrection_of_partial_data(self):
        stale = workspace()
        store.save_record(SCOPE, stale, root=self.root)
        with patch('editorial_store.os.unlink', side_effect=PermissionError('Cannot remove snapshot')):
            with self.assertRaises(OSError):
                store.delete_record(SCOPE, root=self.root)
        self.assertTrue(list((self.root / SCOPE).glob('*.json')))
        self.assertIsNone(store.load_record(SCOPE, root=self.root))
        with self.assertRaises(store.WorkspaceDeletedError):
            store.save_record(SCOPE, stale, root=self.root)

    def test_marker_write_failure_preserves_previous_workspace_and_generation(self):
        store.save_record(SCOPE, workspace(), root=self.root)
        with patch('editorial_store.os.replace', side_effect=OSError('Marker write failed')):
            with self.assertRaises(OSError):
                store.delete_record(SCOPE, root=self.root)
        self.assertEqual(store.deletion_token(SCOPE, root=self.root), '')
        self.assertEqual(store.load_record(SCOPE, root=self.root)['final']['en'], 'My own text')

    def test_delete_never_follows_nested_directory_link(self):
        store.save_record(SCOPE, workspace(), root=self.root)
        outside = self.root.parent / 'source-documents'
        outside.mkdir()
        source = outside / 'source.txt'
        source.write_text('Keep this source', encoding='utf-8')
        link = self.root / SCOPE / 'linked-source'
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('Directory links require Windows privileges unavailable to this test.')
        with self.assertRaises(ValueError):
            store.delete_record(SCOPE, root=self.root)
        self.assertEqual(source.read_text(encoding='utf-8'), 'Keep this source')

    @unittest.skipUnless(os.name == 'nt', 'Windows junction containment')
    def test_default_root_cannot_escape_through_junction_ancestor(self):
        outside = self.root.parent / 'external-source'
        outside.mkdir()
        (outside / 'editorial').mkdir()
        alias = self.root.parent / 'alias'
        completed = subprocess.run(['cmd', '/c', 'mklink', '/J', str(alias), str(outside)],
                                   capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        try:
            for root in (alias, alias / 'editorial'):
                with self.subTest(root=root), patch('editorial_store._ROOT', root):
                    with self.assertRaises(ValueError):
                        store.delete_record(SCOPE)
            self.assertFalse((outside / '_deletions').exists())
            self.assertFalse((outside / 'editorial' / '_deletions').exists())
        finally:
            os.rmdir(alias)

    @unittest.skipUnless(os.name == 'nt', 'Windows junction containment')
    def test_delete_refuses_linked_marker_directory(self):
        outside = self.root.parent / 'source-documents'
        outside.mkdir()
        link = self.root / '_deletions'
        completed = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)],
                                   capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        try:
            with self.assertRaises(ValueError):
                store.delete_record(SCOPE, root=self.root)
            self.assertEqual(list(outside.iterdir()), [])
        finally:
            os.rmdir(link)

    @unittest.skipUnless(os.name == 'nt', 'Windows junction containment')
    def test_delete_refuses_scope_and_nested_junctions(self):
        outside = self.root.parent / 'source-documents'
        outside.mkdir()
        source = outside / 'source.txt'
        source.write_text('Keep this source', encoding='utf-8')
        for link in (self.root / SCOPE, self.root / SCOPE / 'nested'):
            with self.subTest(link=link):
                if link.name == 'nested':
                    (self.root / SCOPE).mkdir()
                try:
                    completed = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)],
                                               capture_output=True, text=True)
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    with self.assertRaises(ValueError):
                        store.delete_record(SCOPE, root=self.root)
                    self.assertEqual(source.read_text(encoding='utf-8'), 'Keep this source')
                finally:
                    if link.exists():
                        os.rmdir(link)

    def test_threaded_inflight_save_cannot_resurrect_deleted_text(self):
        record = workspace()
        store.save_record(SCOPE, record, root=self.root)
        ready, release = threading.Event(), threading.Event()
        original_replace = os.replace
        errors = []
        def delayed_replace(source, destination):
            if Path(destination).parent == self.root / SCOPE:
                ready.set()
                if not release.wait(5):
                    raise TimeoutError('Test save was not released')
            return original_replace(source, destination)
        def run(operation):
            try:
                operation()
            except Exception as exc:
                errors.append(exc)
        with patch('editorial_store.os.replace', side_effect=delayed_replace):
            saving = threading.Thread(target=run, args=(lambda: store.save_record(SCOPE, record, root=self.root),))
            saving.start()
            self.assertTrue(ready.wait(5))
            deleting = threading.Thread(target=run, args=(lambda: store.delete_record(SCOPE, root=self.root),))
            deleting.start()
            release.set()
            saving.join(5)
            deleting.join(5)
        self.assertFalse(saving.is_alive())
        self.assertFalse(deleting.is_alive())
        self.assertEqual(errors, [])
        self.assertIsNone(store.load_record(SCOPE, root=self.root))
        self.assertFalse((self.root / SCOPE).exists())

    def test_process_inflight_save_is_serialized_with_deletion(self):
        store.save_record(SCOPE, workspace(), root=self.root)
        ready = self.root.parent / 'ready'
        release = self.root.parent / 'release'
        script = '''
import os, sys, time
from pathlib import Path
import editorial_store as store
root, scope, ready, release = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4])
record = store.load_record(scope, root=root)
original_replace = os.replace
def delayed_replace(source, destination):
    if Path(destination).parent == root / scope:
        ready.touch()
        until = time.monotonic() + 8
        while not release.exists():
            if time.monotonic() > until:
                raise TimeoutError('Parent did not release test save')
            time.sleep(0.01)
    return original_replace(source, destination)
store.os.replace = delayed_replace
store.save_record(scope, record, root=root)
'''
        process = subprocess.Popen([sys.executable, '-c', script, str(self.root), SCOPE,
                                    str(ready), str(release)], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        errors = []
        started, finished = threading.Event(), threading.Event()
        def remove():
            started.set()
            try:
                store.delete_record(SCOPE, root=self.root)
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()
        deleting = None
        try:
            until = time.monotonic() + 5
            while not ready.exists() and process.poll() is None and time.monotonic() < until:
                time.sleep(0.01)
            self.assertTrue(ready.exists(), 'Child process did not reach an in-flight save')
            deleting = threading.Thread(target=remove)
            deleting.start()
            self.assertTrue(started.wait(1))
            self.assertFalse(finished.wait(0.15), 'Deletion raced through the child save lock')
            release.touch()
            stdout, stderr = process.communicate(timeout=8)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            deleting.join(8)
            self.assertTrue(finished.is_set())
            self.assertEqual(errors, [])
            self.assertIsNone(store.load_record(SCOPE, root=self.root))
            self.assertFalse((self.root / SCOPE).exists())
        finally:
            release.touch()
            if process.poll() is None:
                process.kill()
                process.communicate()
            if deleting is not None:
                deleting.join(8)

    def test_restart_preserves_drafts_edits_reviews_and_pending_response(self):
        original = workspace()
        store.save_record(SCOPE, original, root=self.root)
        loaded = store.load_record(SCOPE, root=self.root)
        self.assertIsInstance(loaded['bundle'], GeneratedDrafts)
        self.assertEqual(loaded['bundle'].website_en_1.text, 'Azra may need care.\n\nAsk the rescuer.')
        for field in ('final', 'facebook', 'facebook_baseline', 'undo', 'usage',
                      'profile_snapshot', 'assessment', 'editor_assessments', 'pending_response', 'outdated'):
            self.assertEqual(loaded[field], original[field], field)

    def test_missing_scope_has_no_record_and_does_not_create_directories(self):
        self.assertIsNone(store.load_record(SCOPE, root=self.root))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_pending_batch_keeps_its_own_profile_for_free_restart_recovery(self):
        record = workspace()
        record['pending_response']['profile_snapshot'] = copy.deepcopy(record['profile_snapshot'])
        store.save_record(SCOPE, record, root=self.root)
        loaded = store.load_record(SCOPE, root=self.root)
        self.assertEqual(loaded['pending_response'].get('profile_snapshot'), record['profile_snapshot'])

    def test_revisions_and_concurrent_sessions_preserve_both_edits(self):
        initial = workspace()
        store.save_record(SCOPE, initial, root=self.root)
        first = store.load_record(SCOPE, root=self.root)
        second = store.load_record(SCOPE, root=self.root)
        self.assertNotEqual(first['storage_session_id'], second['storage_session_id'])
        first['final']['en'] = 'First volunteer'
        second['final']['en'] = 'Second volunteer'
        store.save_record(SCOPE, first, root=self.root)
        store.save_record(SCOPE, second, root=self.root)
        first['final']['en'] = 'First volunteer revision'
        store.save_record(SCOPE, first, root=self.root)
        snapshots = list((self.root / SCOPE).glob('*.json'))
        self.assertEqual(len(snapshots), 4)
        saved_texts = [json.loads(path.read_text(encoding='utf-8'))['record']['final']['en'] for path in snapshots]
        self.assertIn('First volunteer', saved_texts)
        self.assertIn('Second volunteer', saved_texts)
        self.assertEqual(store.load_record(SCOPE, root=self.root)['final']['en'], 'First volunteer revision')

    def test_bad_scope_cannot_escape_storage_root(self):
        for invalid in ('../outside', '', 'A' * 20, 'a' * 19, 'a' * 20 + '/child'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                store.save_record(invalid, workspace(), root=self.root)
            with self.assertRaises(ValueError):
                store.load_record(invalid, root=self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_animal_scopes_are_isolated(self):
        record = workspace()
        store.save_record(SCOPE, record, root=self.root)
        record['final']['en'] = 'Other animal'
        store.save_record('b' * 20, record, root=self.root)
        self.assertEqual(store.load_record(SCOPE, root=self.root)['final']['en'], 'My own text')
        self.assertEqual(store.load_record('b' * 20, root=self.root)['final']['en'], 'Other animal')

    def test_corrupt_latest_falls_back_with_visible_warning(self):
        record = workspace()
        store.save_record(SCOPE, record, root=self.root)
        before = set((self.root / SCOPE).glob('*.json'))
        record['final']['en'] = 'Newest'
        store.save_record(SCOPE, record, root=self.root)
        latest = (set((self.root / SCOPE).glob('*.json')) - before).pop()
        latest.write_text('{broken JSON', encoding='utf-8')
        loaded = store.load_record(SCOPE, root=self.root)
        self.assertEqual(loaded['final']['en'], 'My own text')
        self.assertTrue(loaded['storage_warnings'])

    def test_all_corrupt_snapshots_raise_recoverable_error(self):
        store.save_record(SCOPE, workspace(), root=self.root)
        next((self.root / SCOPE).glob('*.json')).write_text('[]', encoding='utf-8')
        with self.assertRaises(ValueError):
            store.load_record(SCOPE, root=self.root)

    def test_allowlists_exclude_settings_credentials_and_unknown_nested_fields(self):
        record = workspace()
        record.update(settings={'api_key': 'DO_NOT_SAVE_SETTINGS'}, api_key='DO_NOT_SAVE_KEY')
        record['usage']['provider_body'] = 'DO_NOT_SAVE_USAGE'
        record['profile_snapshot']['oauth'] = 'DO_NOT_SAVE_PROFILE'
        record['assessment']['questions'][0]['api_key'] = 'DO_NOT_SAVE_QUESTION'
        record['pending_response']['settings'] = {'api_key': 'DO_NOT_SAVE_PENDING'}
        record['pending_response']['candidate']['website_en_1']['api_key'] = 'DO_NOT_SAVE_DRAFT'
        record['final']['en'] = 'Bearer sensitive_value sk-secret12345678 ya29.sensitive'
        store.save_record(SCOPE, record, root=self.root)
        serialized = next((self.root / SCOPE).glob('*.json')).read_text(encoding='utf-8')
        self.assertNotIn('DO_NOT_SAVE', serialized)
        self.assertNotIn('sensitive_value', serialized)
        self.assertNotIn('sk-secret12345678', serialized)
        self.assertNotIn('ya29.sensitive', serialized)
        self.assertIn('[credential removed]', serialized)

    def test_atomic_replace_failure_preserves_prior_revision(self):
        record = workspace()
        store.save_record(SCOPE, record, root=self.root)
        record['final']['en'] = 'Not saved'
        with patch('editorial_store.os.replace', side_effect=OSError('Simulated disk failure')):
            with self.assertRaises(OSError):
                store.save_record(SCOPE, record, root=self.root)
        self.assertEqual(store.load_record(SCOPE, root=self.root)['final']['en'], 'My own text')
        self.assertEqual(len(list((self.root / SCOPE).iterdir())), 1)

    def test_confirmation_audit_is_append_only_and_never_recolors(self):
        record = workspace()
        original_profile = copy.deepcopy(record['profile_snapshot'])
        original_assessment = copy.deepcopy(record['assessment'])
        first = store.record_confirmation(record, 'care-question',
            {'note': 'Need details', 'status': 'needs_clarification', 'api_key': 'DROP'})
        saved_first = copy.deepcopy(first)
        second = store.record_confirmation(record, 'care-question',
            {'status': 'confirmed_by_rescuer', 'rescuer_response': 'Care may be needed.',
             'confirmation_date': '2026-11-05', 'attribution': 'Rescuer Ana'})
        self.assertEqual(record['confirmations'][0], saved_first)
        self.assertEqual(len(record['confirmations']), 2)
        self.assertEqual(second['issue_id'], 'care-question')
        self.assertNotIn('api_key', first)
        self.assertEqual(record['profile_snapshot'], original_profile)
        self.assertEqual(record['assessment'], original_assessment)
        self.assertFalse(record.get('manual_sources'))

    def test_confirmation_rejects_invalid_status_without_appending(self):
        record = workspace()
        with self.assertRaises(ValueError):
            store.record_confirmation(record, 'care-question', {'status': 'green'})
        self.assertFalse(record.get('confirmations'))

    def test_manual_source_requires_explicit_response_date_and_attribution(self):
        for missing in ('rescuer_response', 'confirmation_date', 'attribution'):
            record = workspace()
            details = {'status': 'confirmed_by_rescuer', 'rescuer_response': 'May need ongoing care.',
                       'confirmation_date': '2026-11-05', 'attribution': 'Rescuer Ana'}
            details[missing] = '   '
            store.record_confirmation(record, 'care-question', details)
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                store.add_manual_source(record, 'care-question')
            self.assertFalse(record.get('manual_sources'))

    def test_explicit_manual_source_keeps_uncertainty_and_stable_animal_issue_id(self):
        record = workspace()
        record['scope'] = SCOPE
        original_profile = copy.deepcopy(record['profile_snapshot'])
        store.record_confirmation(record, 'care-question', {'status': 'confirmed_by_rescuer',
            'rescuer_response': 'May need ongoing care; not independently confirmed.',
            'confirmation_date': '2026-11-05', 'attribution': 'Rescuer Ana'})
        source = store.add_manual_source(record, 'care-question')
        self.assertEqual(source['text'], 'May need ongoing care; not independently confirmed.')
        self.assertEqual(source['topic'], 'treatment')
        self.assertEqual(source['attribution'], 'Rescuer Ana')
        self.assertEqual(source['confirmation_date'], '2026-11-05')
        self.assertEqual(store.add_manual_source(record, 'care-question')['id'], source['id'])
        self.assertEqual(len(record['manual_sources']), 1)
        self.assertEqual(record['profile_snapshot'], original_profile)
        other = workspace()
        other['scope'] = 'b' * 20
        store.record_confirmation(other, 'care-question', {'status': 'confirmed_by_rescuer',
            'rescuer_response': 'Different animal also may need care.',
            'confirmation_date': '2026-11-05', 'attribution': 'Rescuer Ana'})
        self.assertNotEqual(store.add_manual_source(other, 'care-question')['id'], source['id'])

    def test_generation_history_and_editor_status_fingerprints_survive_restart(self):
        record = workspace()
        record.update(assessment_profile_fingerprint='profile-v2',
                      assessment_manual_sources_fingerprint='manual-v2',
                      editor_statuses={'final_en': {'finalized': True, 'text_hash': 'text-v2'}},
                      history=[{'bundle': record['bundle'].model_dump(mode='json'),
                                'generated_at': 'yesterday', 'profile_fingerprint': 'profile-v1',
                                'profile_snapshot': record['profile_snapshot'],
                                'assessment': record['assessment']}])
        record['editor_assessments']['final_en']['manual_sources_fingerprint'] = 'manual-v2'
        store.save_record(SCOPE, record, root=self.root)
        loaded = store.load_record(SCOPE, root=self.root)
        for field in ('assessment_profile_fingerprint', 'assessment_manual_sources_fingerprint',
                      'editor_statuses', 'history', 'editor_assessments'):
            self.assertEqual(loaded.get(field), record[field], field)

    def test_legacy_malformed_annotations_keep_all_eight_readable_texts(self):
        record = workspace()
        record['bundle'] = record['bundle'].model_dump(mode='json')
        record['bundle']['website_en_1']['claims'] = [{'excerpt': 'Azra', 'qualification': 'surprising'}]
        record['bundle']['website_nl_1']['claims'] = None
        store.save_record(SCOPE, record, root=self.root)
        loaded = store.load_record(SCOPE, root=self.root)
        for key in DRAFT_KEYS:
            self.assertEqual(getattr(loaded['bundle'], key).text, 'Azra may need care.\n\nAsk the rescuer.')
        self.assertTrue(loaded['bundle'].website_en_1.review_notes)

    def test_confirmation_keeps_volunteer_context_without_changing_animal_binding(self):
        record = workspace()
        record['scope'] = SCOPE
        entry = store.record_confirmation(record, 'new-issue', {'status': 'needs_clarification',
            'volunteer_note': 'Please check the dose.', 'topic': 'medication', 'scope': SCOPE,
            'animal_name': 'Azra', 'rescuer_response': 'Possibly daily medication.',
            'confirmation_date': '2026-11-05', 'attribution': 'Ana'})
        self.assertEqual(entry.get('volunteer_note'), 'Please check the dose.')
        self.assertEqual(store.add_manual_source(record, 'new-issue')['topic'], 'medication')
        with self.assertRaises(ValueError):
            store.record_confirmation(record, 'wrong-animal', {'status': 'confirmed_by_rescuer',
                                      'scope': 'b' * 20, 'animal_name': 'Different animal'})

    def test_unknown_attribution_cannot_create_a_manual_source(self):
        record = workspace()
        store.record_confirmation(record, 'care-question', {'status': 'confirmed_by_rescuer',
            'rescuer_response': 'May need care.', 'confirmation_date': '2026-11-05', 'attribution': 'unknown'})
        with self.assertRaises(ValueError):
            store.add_manual_source(record, 'care-question')

    def test_corrupt_editor_shape_falls_back_instead_of_losing_editors(self):
        record = workspace()
        store.save_record(SCOPE, record, root=self.root)
        before = set((self.root / SCOPE).glob('*.json'))
        store.save_record(SCOPE, record, root=self.root)
        latest = (set((self.root / SCOPE).glob('*.json')) - before).pop()
        raw = json.loads(latest.read_text(encoding='utf-8'))
        raw['record']['final'] = 'invalid editor mapping'
        latest.write_text(json.dumps(raw), encoding='utf-8')
        loaded = store.load_record(SCOPE, root=self.root)
        self.assertEqual(loaded['final']['en'], 'My own text')
        self.assertTrue(loaded['storage_warnings'])

    def test_wrong_scope_latest_is_not_installed(self):
        record = workspace()
        store.save_record(SCOPE, record, root=self.root)
        before = set((self.root / SCOPE).glob('*.json'))
        store.save_record(SCOPE, record, root=self.root)
        latest = (set((self.root / SCOPE).glob('*.json')) - before).pop()
        raw = json.loads(latest.read_text(encoding='utf-8'))
        raw['scope'] = 'b' * 20
        raw['record']['final']['en'] = 'Other animal text'
        latest.write_text(json.dumps(raw), encoding='utf-8')
        loaded = store.load_record(SCOPE, root=self.root)
        self.assertEqual(loaded['final']['en'], 'My own text')
        self.assertTrue(loaded['storage_warnings'])

    def test_confirmation_cannot_be_reused_after_switching_animal_scope(self):
        record = workspace()
        record['scope'] = SCOPE
        store.record_confirmation(record, 'care-question', {'status': 'confirmed_by_rescuer',
            'rescuer_response': 'Possibly needs care.', 'confirmation_date': '2026-11-05',
            'attribution': 'Ana'})
        record['scope'] = 'b' * 20
        with self.assertRaises(ValueError):
            store.add_manual_source(record, 'care-question')

    def test_invalid_status_type_is_a_safe_validation_error(self):
        with self.assertRaises(ValueError):
            store.record_confirmation(workspace(), 'care-question', {'status': []})

    def test_symlink_scope_cannot_alias_another_animal_directory(self):
        target = self.root / ('b' * 20)
        target.mkdir()
        try:
            (self.root / SCOPE).symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest('Directory links require Windows privileges unavailable to this test.')
        with self.assertRaises(ValueError):
            store.save_record(SCOPE, workspace(), root=self.root)
        self.assertEqual(list(target.iterdir()), [])

    def test_incomplete_assessment_and_attributed_audit_survive_restart(self):
        record = workspace()
        record['scope'] = SCOPE
        record['assessment']['incomplete'] = True
        store.record_confirmation(record, 'care-question', {'status': 'confirmed_by_rescuer',
            'rescuer_response': 'May need care.', 'confirmation_date': '2026-11-05', 'attribution': 'Ana'})
        store.add_manual_source(record, 'care-question')
        store.save_record(SCOPE, record, root=self.root)
        loaded = store.load_record(SCOPE, root=self.root)
        self.assertTrue(loaded['assessment'].get('incomplete'))
        self.assertEqual(loaded['confirmations'], record['confirmations'])
        self.assertEqual(loaded['manual_sources'], record['manual_sources'])

    def test_confirmation_rejects_impossible_calendar_date(self):
        record = workspace()
        for invalid in ('2026-02-30', 'tomorrow', '20261105'):
            with self.subTest(date=invalid), self.assertRaises(ValueError):
                store.record_confirmation(record, 'care-question', {'status': 'confirmed_by_rescuer',
                    'rescuer_response': 'May need care.', 'confirmation_date': invalid, 'attribution': 'Ana'})
        self.assertFalse(record.get('confirmations'))

    def test_manual_source_does_not_invent_current_temporal_certainty(self):
        record = workspace()
        store.record_confirmation(record, 'care-question', {'status': 'confirmed_by_rescuer',
            'rescuer_response': 'Was treated in 2023; current treatment is unknown.',
            'confirmation_date': '2026-11-05', 'attribution': 'Ana'})
        source = store.add_manual_source(record, 'care-question')
        self.assertEqual(source['temporal_status'], 'unclear')
        self.assertEqual(source['text'], 'Was treated in 2023; current treatment is unknown.')

    def test_assessment_and_editor_source_snapshots_survive_restart(self):
        record = workspace()
        record['assessment_profile_snapshot'] = {'identity': {'animal_name': 'Azra'},
            'unknown_information': ['Updated treatment is uncertain.'], 'settings': {'api_key': 'DROP_ME'}}
        record['editor_assessments']['final_en']['profile_snapshot'] = {
            'identity': {'animal_name': 'Azra'}, 'unknown_information': ['Earlier care is uncertain.']}
        store.save_record(SCOPE, record, root=self.root)
        loaded = store.load_record(SCOPE, root=self.root)
        self.assertEqual(loaded.get('assessment_profile_snapshot'), {
            'identity': {'animal_name': 'Azra'}, 'unknown_information': ['Updated treatment is uncertain.']})
        self.assertEqual(loaded['editor_assessments']['final_en'].get('profile_snapshot'),
                         record['editor_assessments']['final_en']['profile_snapshot'])
        self.assertEqual(loaded['profile_snapshot']['unknown_information'], ['Current care is uncertain.'])

    def test_manual_source_retains_explicit_animal_name_for_assessor_binding(self):
        record = workspace()
        store.record_confirmation(record, 'care-question', {'status': 'confirmed_by_rescuer',
            'rescuer_response': 'May need care.', 'confirmation_date': '2026-11-05', 'attribution': 'Ana'})
        source = store.add_manual_source(record, 'care-question')
        self.assertEqual(source.get('animal_name'), 'Azra')
        store.save_record(SCOPE, record, root=self.root)
        self.assertEqual(store.load_record(SCOPE, root=self.root)['manual_sources'][0].get('animal_name'), 'Azra')

    def test_corrupt_review_containers_fall_back_to_prior_snapshot(self):
        corruptions = [
            {'assessment': 'corrupt'},
            {'assessment': {'drafts': 'corrupt'}},
            {'assessment': {'drafts': {'website_en_1': 'corrupt'}}},
            {'assessment': {'drafts': {'website_en_1': {'findings': 'corrupt'}}}},
            {'assessment': {'drafts': {'website_en_1': {'passages': 'corrupt'}}}},
            {'assessment': {'drafts': {'website_en_1': {'findings': ['corrupt']}}}},
            {'assessment': {'questions': 'corrupt'}},
            {'assessment': {'questions': ['corrupt']}},
            {'editor_assessments': 'corrupt'},
            {'editor_assessments': None},
            {'editor_assessments': {'final_en': 'corrupt'}},
            {'editor_assessments': {'final_en': {'findings': ['corrupt']}}},
            {'editor_statuses': 'corrupt'},
            {'editor_statuses': {'final_en': 'corrupt'}},
            {'confirmations': 'corrupt'}, {'confirmations': ['corrupt']},
            {'manual_sources': 'corrupt'}, {'manual_sources': ['corrupt']},
            {'issues': 'corrupt'}, {'issues': ['corrupt']},
            {'profile_warnings': 'corrupt'}, {'profile_warnings': ['corrupt']},
            {'usage': 'corrupt'}, {'pending_response': 'corrupt'},
        ]
        for corruption in corruptions:
            with self.subTest(corruption=corruption):
                record = workspace()
                store.save_record(SCOPE, record, root=self.root)
                before = set((self.root / SCOPE).glob('*.json'))
                store.save_record(SCOPE, record, root=self.root)
                latest = (set((self.root / SCOPE).glob('*.json')) - before).pop()
                raw = json.loads(latest.read_text(encoding='utf-8'))
                raw['record'].update(corruption)
                latest.write_text(json.dumps(raw), encoding='utf-8')
                loaded = store.load_record(SCOPE, root=self.root)
                self.assertTrue(loaded.get('storage_warnings'), corruption)
                self.assertEqual(loaded['final']['en'], 'My own text')
                self.assertEqual(loaded['assessment'], record['assessment'])

    def test_all_structurally_corrupt_snapshots_raise_safe_storage_warning(self):
        store.save_record(SCOPE, workspace(), root=self.root)
        path = next((self.root / SCOPE).glob('*.json'))
        raw = json.loads(path.read_text(encoding='utf-8'))
        raw['record']['assessment'] = 'corrupt'
        path.write_text(json.dumps(raw), encoding='utf-8')
        with self.assertRaises(ValueError) as caught:
            store.load_record(SCOPE, root=self.root)
        self.assertNotIn('corrupt', str(caught.exception))
        self.assertTrue(path.exists())


if __name__ == '__main__':
    unittest.main()
