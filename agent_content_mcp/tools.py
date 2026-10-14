"""Build MCP tools from a django-agent-content API description (GET <api root>).

Each registered resource becomes a small set of tools:

    list_<name>, get_<singular>, create_<singular>, update_<singular>     items
    <action>_<singular>                                                  registered actions
    get_<singular>_<inline>, set_<singular>_<inline>                     inline lists
    get_<name>, update_<name>                                            singletons

Tool and argument descriptions come from the resource and its fields (label, help text, limits, choices),
so what the site documents on its models is what agents read.
"""

import json
from dataclasses import dataclass
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

    def request(self, arguments: dict) -> tuple[str, str, dict | None, dict | None]:
        """Return (method, path, query params, JSON body) for a call."""
        args = {key: value for key, value in arguments.items() if value is not None}
        path = self.path
        if self.lookup:
            path = path.replace('{lookup}', quote(str(args.pop(self.lookup)), safe=''))
        for argument, field in (self.renamed or {}).items():
            if argument in args:
                args[field] = args.pop(argument)
        if self.query:
            return self.method, path, args, None
        if self.body_key:
            return self.method, path, None, {self.body_key: args.get(self.body_key, [])}
        return self.method, path, None, args


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
    readable = ', '.join(list(fields) + list(r.get('computed', [])))

    if r.get('singleton'):
        return [
            ToolSpec(f'get_{name}', f'Read the {label} ({readable}).{about}', _object({}, []), READ, 'GET', f'{name}/'),
            ToolSpec(f'update_{name}', f'Change the {label}. Only the given fields change.{about}',
                     _object({k: field_schema(v) for k, v in writable.items()}, []), UPDATE, 'PATCH', f'{name}/'),
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
        tools.append(ToolSpec(
            f'create_{one}', f'Create a {label}.{forced}{about}',
            _object({k: field_schema(v) for k, v in writable.items()}, [k for k, v in writable.items() if v.get('required')]),
            CREATE, 'POST', f'{name}/'))

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
    return tools


def instructions(index: dict) -> str:
    """Guidance sent to the agent when it connects: general rules, then each resource's own description."""
    lines = [
        'These tools edit a website\'s content through its API. Nothing can be deleted.',
        'Base content on what the site owner provides; ask when information is missing rather than inventing it.',
        'Run public-facing actions (such as publishing) only when the owner asks for them.',
    ]
    lines += [f'{r["name"]}: {r["description"]}' for r in index.get('resources', []) if r.get('description')]
    return '\n'.join(lines)


def result_text(result: Any) -> str:
    return json.dumps(result, ensure_ascii=False, indent=1, default=str)
