"""Tool building, tool calls and uploads, without a real site: python -m unittest"""

import json
import tempfile
import unittest
from pathlib import Path

import httpx

from agent_content_mcp import tools
from agent_content_mcp.client import ContentAPI
from agent_content_mcp.server import run_tool

INDEX = {'version': 1, 'resources': [
    {'name': 'posts', 'singular': 'post', 'model': 'post', 'lookup': 'slug', 'create': True,
     'create_defaults': {'status': 'draft'},
     'fields': {'title': {'kind': 'string', 'writable': True, 'required': True},
                'slug': {'kind': 'string', 'writable': True, 'required': True}},
     'actions': {'publish': {'description': 'Make it public.', 'params': {}}},
     'uploads': {'cover': {'kind': 'image', 'label': 'cover', 'required_on_create': False,
                           'accept': ['image/'], 'max_bytes': 100}}},
    {'name': 'documents', 'singular': 'document', 'model': 'document', 'lookup': 'pk', 'create': True,
     'fields': {'title': {'kind': 'string', 'writable': True, 'required': True}},
     'uploads': {'file': {'kind': 'file', 'label': 'file', 'required_on_create': True,
                          'accept': ['application/pdf'], 'max_bytes': 1000}}},
    {'name': 'site', 'singular': 'site', 'model': 'site', 'singleton': True,
     'fields': {'tagline': {'kind': 'string', 'writable': True}},
     'uploads': {'logo': {'kind': 'image', 'label': 'logo', 'accept': ['image/'], 'max_bytes': 1000}}},
]}


class FakeAPI:
    """Records requests instead of sending them."""

    def __init__(self, reply=None, fail=None):
        self.calls, self.reply, self.fail = [], reply if reply is not None else {'ok': True}, fail

    async def request(self, method, path, json=None, params=None, files=None):
        self.calls.append((method, path, json, params, files))
        if self.fail:
            raise self.fail
        return self.reply


class FileTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.specs = {spec.name: spec for spec in tools.build(INDEX)}

    def tearDown(self):
        for path in sorted(self.dir.rglob('*'), reverse=True):
            path.rmdir() if path.is_dir() else path.unlink()
        self.dir.rmdir()

    def file(self, name, content=b'png'):
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return str(path)


class BuildTests(FileTestCase):
    def test_tools_come_from_the_description(self):
        self.assertEqual(sorted(self.specs), [
            'create_document', 'create_post', 'get_document', 'get_post', 'get_site', 'list_documents', 'list_posts',
            'publish_post', 'update_document', 'update_post', 'update_site', 'upload_document_file',
            'upload_post_cover', 'upload_site_logo',
        ])
        cover = self.specs['upload_post_cover'].schema
        self.assertEqual(cover['required'], ['slug', 'path'])
        self.assertIn('Accepted: image/*. Up to 0.0001 MB.', cover['properties']['path']['description'])
        # a file the item can't exist without is a required argument of create
        self.assertIn('file_path', self.specs['create_document'].schema['required'])
        self.assertNotIn('cover_path', self.specs['create_post'].schema['required'])


class CallTests(FileTestCase):
    async def test_a_call_becomes_a_request_and_its_reply(self):
        api = FakeAPI(reply={'slug': 'hello'})
        text, error = await run_tool(api, self.specs, None, 'update_post', {'slug': 'hello', 'title': 'Hi'})
        self.assertFalse(error)
        self.assertEqual(json.loads(text), {'slug': 'hello'})
        self.assertEqual(api.calls, [('PATCH', 'posts/hello/', {'title': 'Hi'}, None, None)])

    async def test_problems_are_answered_not_raised(self):
        api = FakeAPI(fail=RuntimeError('403: no permission'))
        self.assertEqual(await run_tool(api, self.specs, None, 'get_post', {'slug': 'x'}), ('403: no permission', True))
        self.assertEqual(await run_tool(api, self.specs, None, 'nope', {}), ('Unknown tool nope.', True))
        self.assertEqual(await run_tool(api, self.specs, None, 'get_post', {}), ("Missing argument 'slug'.", True))
        text, error = await run_tool(api, {}, 'site unreachable', 'get_post', {'slug': 'x'})
        self.assertTrue(error)
        self.assertIn('site unreachable', text)


class UploadTests(FileTestCase):
    async def test_upload_reads_the_file_and_sends_it_as_the_file_part(self):
        api = FakeAPI()
        path = self.file('cover.png', b'\x89PNG')
        text, error = await run_tool(api, self.specs, None, 'upload_post_cover', {'slug': 'hello', 'path': path})
        self.assertFalse(error)
        method, url, body, query, files = api.calls[0]
        self.assertEqual((method, url, body), ('POST', 'posts/hello/cover/', {}))
        self.assertEqual(files, {'file': ('cover.png', b'\x89PNG', 'image/png')})

    async def test_create_sends_the_fields_and_the_file_together(self):
        api = FakeAPI()
        path = self.file('guide.pdf', b'%PDF-1.7')
        await run_tool(api, self.specs, None, 'create_document', {'title': 'Guide', 'file_path': path})
        method, url, body, query, files = api.calls[0]
        self.assertEqual((method, url, body), ('POST', 'documents/', {'title': 'Guide'}))
        self.assertEqual(files, {'file': ('guide.pdf', b'%PDF-1.7', 'application/pdf')})

    async def test_singleton_upload(self):
        api = FakeAPI()
        await run_tool(api, self.specs, None, 'upload_site_logo', {'path': self.file('logo.svg', b'<svg/>')})
        self.assertEqual(api.calls[0][1], 'site/logo/')

    async def test_refused_files_are_never_sent(self):
        api = FakeAPI()
        cases = {
            self.file('.ssh/id_rsa', b'secret'): 'Hidden files',
            self.file('.env', b'KEY=1'): 'Hidden files',
            self.file('notes.txt', b'text'): 'accepts image/*',
            self.file('big.png', b'x' * 101): 'up to 100',
            'cover.png': 'absolute path',
            str(self.dir / 'missing.png'): 'No file',
        }
        for path, reason in cases.items():
            text, error = await run_tool(api, self.specs, None, 'upload_post_cover', {'slug': 'x', 'path': path})
            self.assertTrue(error, path)
            self.assertIn('Nothing was uploaded', text)
            self.assertIn(reason, text)
        self.assertEqual(api.calls, [])


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_uploads_are_multipart_with_the_json_in_a_data_part(self):
        seen = {}

        def handler(request):
            seen['type'] = request.headers['content-type']
            seen['body'] = request.content
            return httpx.Response(201, json={'pk': 1})

        api = ContentAPI('https://example.com/api/agent/', 'ac_test')
        api.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=api.client.headers)
        reply = await api.request('POST', 'documents/', json={'title': 'Guide'},
                                  files={'file': ('guide.pdf', b'%PDF-1.7', 'application/pdf')})
        self.assertEqual(reply, {'pk': 1})
        self.assertTrue(seen['type'].startswith('multipart/form-data'))
        self.assertIn(b'name="data"', seen['body'])
        self.assertIn(b'{"title": "Guide"}', seen['body'])
        self.assertIn(b'filename="guide.pdf"', seen['body'])

    def test_plain_http_is_refused_except_for_localhost(self):
        with self.assertRaises(SystemExit):
            ContentAPI('http://example.com/api/agent/', 'ac_test')
        ContentAPI('http://localhost:8000/api/agent/', 'ac_test')


if __name__ == '__main__':
    unittest.main()
