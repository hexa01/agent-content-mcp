"""Build MCP tools from a django-agent-content API description (GET <api root>).

Each registered resource becomes a small set of tools:

    list_<name>, get_<singular>, create_<singular>, update_<singular>     items
    <action>_<singular>                                                  registered actions
    get_<singular>_<inline>, set_<singular>_<inline>                     inline lists
    upload_<singular>_<field>                                            file and image fields
    get_<name>, update_<name>, upload_<name>_<field>                     singletons

Tool and argument descriptions come from the resource and its fields (label, help text, limits, choices),
so what the site documents on its models is what agents read.

Uploads take a path to a file on this computer. Only files whose type the site accepts for that field are sent,
and hidden files or anything inside a hidden folder (such as ~/.ssh or .env) are refused.
"""

import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from mcp.types import ToolAnnotations

SUPPORTED_API_VERSION = 1

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
CREATE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
UPDATE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
ACTION = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
REPLACE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False)

KIND_SCHEMAS = {
    'string': {'type': 'string'},
    'text': {'type': 'string'},
    'integer': {'type': 'integer'},
    'number': {'type': 'number'},
    'boolean': {'type': 'boolean'},
    'datetime': {'type': 'string', 'format': 'date-time'},
    'date': {'type': 'string', 'format': 'date'},
    'time': {'type': 'string', 'format': 'time'},
    'json': {},
}


@dataclass
class ToolSpec:
    """One tool: its MCP definition and how a call maps to an API request."""

    name: str
    description: str
    schema: dict
    annotations: ToolAnnotations
    method: str
    path: str                      # relative to the API root; {lookup} marks the item identifier
    lookup: str | None = None      # argument that fills {lookup}
    renamed: dict | None = None    # argument name -> API field name
    body_key: str | None = None    # wrap the body as {body_key: value}
    query: bool = False            # send arguments as query parameters
    files: dict | None = None      # argument holding a local path -> (multipart part name, upload description)

    def request(self, arguments: dict) -> tuple[str, str, dict | None, dict | None, dict | None]:
        """Return (method, path, query params, JSON body, files) for a call. Files are read from disk here."""
        args = {key: value for key, value in arguments.items() if value is not None}
        path = self.path
        if self.lookup:
            path = path.replace('{lookup}', quote(str(args.pop(self.lookup)), safe=''))
        files = {}
        for argument, (part, upload) in (self.files or {}).items():
            if argument in args:
                files[part] = read_upload(args.pop(argument), upload)
        for argument, field in (self.renamed or {}).items():
            if argument in args:
                args[field] = args.pop(argument)
        if self.query:
            return self.method, path, args, None, None
        if self.body_key:
            return self.method, path, None, {self.body_key: args.get(self.body_key, [])}, None
        return self.method, path, None, args, files or None


class UploadError(Exception):
    """A local file that can't be sent; the message is written for the agent."""


def _accepted(kind: str, accept: list[str]) -> bool:
    return not accept or any(kind == a or (a.endswith('/') and kind.startswith(a)) for a in accept)


def _types(accept: list[str]) -> str:
    """'image/' reads as 'image/*'."""
    return ', '.join(a + '*' if a.endswith('/') else a for a in accept)


def read_upload(path: str, upload: dict) -> tuple[str, bytes, str]:
    """(file name, contents, MIME type) for a local file, after the same type and size checks the site makes."""
    file = Path(path).expanduser()
    if not file.is_absolute():
        raise UploadError(f'Give an absolute path (or one starting with ~), not "{path}".')
    file = file.resolve()
    if any(part.startswith('.') for part in file.parts[1:]):
        raise UploadError(f'Hidden files and files inside hidden folders are never uploaded: {file}.')
    if not file.is_file():
        raise UploadError(f'No file at {file}.')
    kind = mimetypes.guess_type(file.name)[0] or ''
    if not _accepted(kind, upload.get('accept') or []):
        raise UploadError(f'{file.name} is {kind or "an unknown type"}; this field accepts {_types(upload["accept"])}.')
    size, limit = file.stat().st_size, upload.get('max_bytes')
    if limit and size > limit:
        raise UploadError(f'{file.name} is {size:,} bytes; this field accepts up to {limit:,}.')
    return file.name, file.read_bytes(), kind or 'application/octet-stream'


def upload_schema(info: dict, label: str) -> dict:
    """The path argument for one upload, described from the site's metadata."""
    notes = [f'Path to a file on this computer to upload as the {label}\'s {info.get("label", "file")}']
    if info.get('help'):
        notes.append(info['help'])
    if info.get('accept'):
        notes.append(f'Accepted: {_types(info["accept"])}')
    if info.get('max_bytes'):
        notes.append(f'Up to {info["max_bytes"] / 1_000_000:g} MB')
    return {'type': 'string', 'description': ' '.join(note.rstrip('.') + '.' for note in notes)}


def field_schema(info: dict) -> dict:
    """JSON schema for one field, with a description assembled from its metadata."""
    kind = info.get('kind', 'string')
    by_pk = info.get('refer_by', 'pk') == 'pk'
    if kind == 'reference':
        schema = {'type': 'integer' if by_pk else 'string'}
    elif kind == 'list':
        schema = {'type': 'array', 'items': {'type': 'integer' if by_pk else 'string'}}
    else:
        schema = dict(KIND_SCHEMAS.get(kind, {'type': 'string'}))
    if info.get('max_length'):
        schema['maxLength'] = info['max_length']
    if info.get('choices') and kind not in ('reference', 'list'):
        schema['enum'] = info['choices']

    notes = []
    if info.get('label'):
        notes.append(info['label'].capitalize())
    if info.get('help'):
        notes.append(info['help'])
    if not by_pk:
        found = 'missing ones are created' if info.get('creates_missing') else 'they must already exist'
        notes.append(f'Refer to items by {info["refer_by"]}; {found}')
    if kind == 'list':
        notes.append('Replaces the whole list')
    if info.get('locked_when_published'):
        notes.append('Cannot change once published')
    if notes:
        schema['description'] = ' '.join(note.rstrip('.') + '.' for note in notes)
    return schema


def _object(properties: dict, required: list[str]) -> dict:
    schema = {'type': 'object', 'properties': properties, 'additionalProperties': False}
    if required:
        schema['required'] = required
    return schema


def build(index: dict) -> list[ToolSpec]:
    """All tools for an API description."""
    tools = []
    for resource in index.get('resources', []):
        tools.extend(_resource_tools(resource))
    return tools


def _resource_tools(r: dict) -> list[ToolSpec]:
    name, one, label = r['name'], r['singular'], r.get('model', r['singular'])
    about = f' {r["description"]}' if r.get('description') else ''
    fields = r.get('fields', {})
    writable = {key: info for key, info in fields.items() if info.get('writable')}

    uploads = r.get('uploads') or {}
    readable = ', '.join(list(fields) + list(uploads) + list(r.get('computed', [])))

    if r.get('singleton'):
        return [
            ToolSpec(f'get_{name}', f'Read the {label} ({readable}).{about}', _object({}, []), READ, 'GET', f'{name}/'),
            ToolSpec(f'update_{name}', f'Change the {label}. Only the given fields change.{about}',
                     _object({k: field_schema(v) for k, v in writable.items()}, []), UPDATE, 'PATCH', f'{name}/'),
        ] + [
            ToolSpec(f'upload_{name}_{field}', f'Upload a file from this computer as the {label}\'s '
                     f'{info.get("label", field)}; it replaces the current one.',
                     _object({'path': upload_schema(info, label)}, ['path']), ACTION, 'POST', f'{name}/{field}/',
                     files={'path': ('file', info)})
            for field, info in uploads.items()
        ]

    lookup = r['lookup']
    item = f'{name}/{{lookup}}/'
    identify = {lookup: {'type': 'string', 'description': f'The {label}\'s {lookup}.'}}
    tools = []

    filters = {}
    for key in r.get('filters', []):
        filters[key] = {**(field_schema(fields[key]) if key in fields else {'type': 'string'}),
                        'description': f'Only items with this {key}.'}
    if r.get('search'):
        filters['q'] = {'type': 'string', 'description': 'Full-text search.'}
    tools.append(ToolSpec(f'list_{name}', f'List {name}.{about}', _object(filters, []), READ, 'GET', f'{name}/', query=True))

    tools.append(ToolSpec(f'get_{one}', f'Read one {label} in full ({readable}).{about}',
                          _object(identify, [lookup]), READ, 'GET', item, lookup=lookup))

    if r.get('create'):
        defaults = r.get('create_defaults') or {}
        forced = f' New items always get {", ".join(f"{k}={v}" for k, v in defaults.items())}.' if defaults else ''
        # each upload is a <field>_path argument; the file goes in the multipart part named after the field
        file_args = {f'{field}_path': (field, info) for field, info in uploads.items() if f'{field}_path' not in writable}
        properties = {k: field_schema(v) for k, v in writable.items()}
        properties.update({arg: upload_schema(info, label) for arg, (_, info) in file_args.items()})
        required = [k for k, v in writable.items() if v.get('required')]
        required += [arg for arg, (_, info) in file_args.items() if info.get('required_on_create')]
        tools.append(ToolSpec(f'create_{one}', f'Create a {label}.{forced}{about}', _object(properties, required),
                              CREATE, 'POST', f'{name}/', files=file_args or None))

    if writable:
        # the identifier argument keeps its name; a writable field with the same name becomes new_<name>
        renamed = {f'new_{k}': k for k in writable if k == lookup}
        properties = {**identify, **{(f'new_{k}' if k == lookup else k): field_schema(v) for k, v in writable.items()}}
        tools.append(ToolSpec(f'update_{one}', f'Change a {label}. Only the given fields change.{about}',
                              _object(properties, [lookup]), UPDATE, 'PATCH', item, lookup=lookup, renamed=renamed))

    for action, spec in (r.get('actions') or {}).items():
        params = {key: {'type': 'string', 'description': text} for key, text in (spec.get('params') or {}).items()}
        tools.append(ToolSpec(f'{action}_{one}', (spec.get('description') or f'Run "{action}" on a {label}.') + about,
                              _object({**identify, **params}, [lookup]), ACTION, 'POST', f'{item}{action}/', lookup=lookup))

    for inline, spec in (r.get('inlines') or {}).items():
        columns = spec.get('fields', {})
        row = _object({k: field_schema(v) for k, v in columns.items()}, [k for k, v in columns.items() if v.get('required')])
        tools.append(ToolSpec(f'get_{one}_{inline}', f'Read a {label}\'s {inline}, in order.',
                              _object(identify, [lookup]), READ, 'GET', f'{item}{inline}/', lookup=lookup))
        tools.append(ToolSpec(f'set_{one}_{inline}', f'Replace a {label}\'s {inline} with this list, in this order. '
                              'Items left out are removed.',
                              _object({**identify, 'items': {'type': 'array', 'items': row}}, [lookup, 'items']),
                              REPLACE, 'PUT', f'{item}{inline}/', lookup=lookup, body_key='items'))

    for field, info in uploads.items():
        tools.append(ToolSpec(f'upload_{one}_{field}', f'Upload a file from this computer as a {label}\'s '
                              f'{info.get("label", field)}; it replaces the current one.{about}',
                              _object({**identify, 'path': upload_schema(info, label)}, [lookup, 'path']),
                              ACTION, 'POST', f'{item}{field}/', lookup=lookup, files={'path': ('file', info)}))
    return tools


def instructions(index: dict) -> str:
    """Guidance sent to the agent when it connects: general rules, then each resource's own description."""
    lines = [
        'These tools edit a website\'s content through its API. Nothing can be deleted.',
        'Base content on what the site owner provides; ask when information is missing rather than inventing it.',
        'Run public-facing actions (such as publishing) only when the owner asks for them.',
    ]
    if any(r.get('uploads') for r in index.get('resources', [])):
        lines.append('Upload tools send a file from this computer, given its path; upload only files the owner names '
                     'or created for this purpose. The result includes the file\'s URL on the site.')
    lines += [f'{r["name"]}: {r["description"]}' for r in index.get('resources', []) if r.get('description')]
    return '\n'.join(lines)


def result_text(result: Any) -> str:
    return json.dumps(result, ensure_ascii=False, indent=1, default=str)
