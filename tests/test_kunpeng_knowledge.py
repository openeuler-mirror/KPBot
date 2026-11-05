"""Protocol behavior tests for the read-only Wiki client (no remote service)."""
import importlib.util
import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest import mock
import urllib.error

PATH = Path(__file__).resolve().parents[1] / 'Plugins/public/kunpeng-knowledge/skills/kunpeng-knowledge/scripts/query.py'
SPEC = importlib.util.spec_from_file_location('wiki_query', PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class Response(io.BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(body.encode())
        self.headers = headers or {}


class FakeOpener:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        return next(self.responses)


class WikiClientTests(unittest.TestCase):
    def client(self, responses):
        client = MODULE.Client('https://example.invalid/mcp', 'test-secret', 3)
        client.opener = FakeOpener(responses)
        return client

    def test_handshake_session_and_unicode_search(self):
        client = self.client([
            Response(json.dumps({'id': 1, 'result': {'protocolVersion': '2024-11-05'}}), {'Mcp-Session-Id': 'session-a'}),
            Response(''), Response(json.dumps({'id': 3, 'result': {'content': []}}))])
        client.initialize()
        result = client.request('tools/call', {'name': 'wiki_search', 'arguments': {'query': '鲲鹏'}})
        self.assertEqual(result, {'content': []})
        requests = client.opener.requests
        self.assertNotIn('id', json.loads(requests[1].data))
        self.assertEqual(requests[2].get_header('Mcp-session-id'), 'session-a')
        self.assertEqual(json.loads(requests[2].data)['params']['arguments']['query'], '鲲鹏')

    def test_sse_skips_notifications_and_joins_data(self):
        client = self.client([Response('data: {"method":"notice"}\n\ndata: {"id":1,\ndata: "result":{"content":[]}}\n\n', {'Content-Type': 'text/event-stream'})])
        self.assertEqual(client.request('tools/call'), {'content': []})

    def test_errors_are_not_empty_results(self):
        for body in ['{"id":1,"error":{"code":-32602}}',
                     '{"id":1,"result":{"isError":true}}',
                     '{"id":9,"result":{}}', 'not json']:
            with self.subTest(body=body), self.assertRaises(MODULE.ClientError):
                self.client([Response(body)]).request('tools/call')

    def test_http_error_does_not_print_body_or_token(self):
        client = self.client([])
        def fail(*args, **kwargs):
            raise urllib.error.HTTPError(client.url, 401, 'test-secret', {}, None)
        client.opener.open = fail
        with self.assertRaisesRegex(MODULE.ClientError, '^MCP HTTP error: 401$'):
            client.request('tools/call')

    def test_redirect_is_blocked(self):
        self.assertIsNone(MODULE.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.invalid'))

    def test_tool_error_preserves_actionable_reason(self):
        client = self.client([Response(json.dumps({
            'id': 1, 'result': {'isError': True, 'content': [
                {'type': 'text', 'text': 'ambiguous slug; specify knowledge_base_id'}
            ]}
        }))])
        with self.assertRaisesRegex(MODULE.ClientError, 'specify knowledge_base_id'):
            client.request('tools/call')

    def test_invalid_port_is_safe_error(self):
        with self.assertRaises(MODULE.ClientError):
            MODULE.Client('https://example.invalid:bad/mcp', 'secret', 3)

    def test_token_only_uses_default_endpoint(self):
        with mock.patch.dict(MODULE.os.environ, {'WEKNORA_MCP_TOKEN': 'test-token'}, clear=True), \
                mock.patch.object(MODULE.sys, 'argv', ['query.py', 'search', 'mmap']), \
                mock.patch.object(MODULE, 'Client') as client, \
                contextlib.redirect_stdout(io.StringIO()):
            client.return_value.request.return_value = {'content': []}
            self.assertEqual(MODULE.main(), 0)
            client.assert_called_once_with(MODULE.DEFAULT_MCP_URL, 'test-token', 30)

    def test_missing_token_explains_application_channel(self):
        output = io.StringIO()
        with mock.patch.dict(MODULE.os.environ, {}, clear=True), \
                mock.patch.object(MODULE.sys, 'argv', ['query.py', 'search', 'mmap']), \
                contextlib.redirect_stderr(output), \
                mock.patch.object(MODULE, 'Client') as client:
            self.assertEqual(MODULE.main(), 1)
            client.assert_not_called()
        self.assertIn('维护者', json.loads(output.getvalue())['error'])


if __name__ == '__main__':
    unittest.main()
