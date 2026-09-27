"""Use the real ref-keyed cmux mapper to verify fixture exclusion and cleanup."""
import unittest
import uuid
from tools.batch_native_acceptance import workspace_surfaces_by_id, close_fixture_surface
import cmux_codex_watch as core


class Client:
    def __init__(self):
        self.workspace = str(uuid.uuid4()).upper()
        self.original, self.owned = str(uuid.uuid4()).upper(), str(uuid.uuid4()).upper()
        self.calls = []
        self.value = {'windows': [{'id': 'window-id', 'workspaces': [{'id': self.workspace,
            'panes': [{'id': 'pane-id', 'surfaces': [
                {'id': self.original, 'ref': 'surface:741'}, {'id': self.owned, 'ref': 'surface:900'}]}]}]}]}
    def tree(self):
        return self.value
    def _run(self, args, **_):
        self.calls.append(args)


class NativeFixtureScopeTests(unittest.TestCase):
    def test_actual_ref_keyed_mapper_is_converted_to_immutable_ids(self):
        client = Client()
        self.assertEqual(set(core.workspace_surface_records(client.tree(), client.workspace)), {'surface:741', 'surface:900'})
        self.assertEqual(set(workspace_surfaces_by_id(client, client.workspace)), {client.original, client.owned})

    def test_close_pins_workspace_context_and_rejects_originals_or_another_workspace(self):
        client = Client()
        close_fixture_surface(client, client.workspace, client.owned, {client.original})
        self.assertEqual(client.calls, [['close-surface', '--workspace', client.workspace, '--surface', client.owned]])
        for workspace, surface in ((client.workspace, client.original), (str(uuid.uuid4()), client.owned)):
            with self.assertRaises(AssertionError):
                close_fixture_surface(client, workspace, surface, {client.original})
        self.assertEqual(len(client.calls), 1)


if __name__ == '__main__':
    unittest.main()
