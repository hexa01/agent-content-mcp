"""agent-content-mcp: an MCP server (stdio) whose tools come from a django-agent-content site.

Settings, from the environment (Claude Code passes them with `claude mcp add -e ...`):

    AGENT_CONTENT_URL  the site's API root, e.g. https://example.com/api/agent/ (plain http only for localhost)
    AGENT_CONTENT_KEY  an API key from the site: `manage.py agent_content_key` or its admin

`agent-content-mcp --check` prints the tools it would offer, to test a setup without an MCP client.
"""

import asyncio
import os
import sys

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from . import __version__, tools
from .client import ContentAPI


async def load(api: ContentAPI):
    """The site's description and the tools built from it, or (None, error) when the site can't be read."""
    try:
        index = await api.request('GET', '')
    except Exception as error:
        return None, str(error)
    if index.get('version') != tools.SUPPORTED_API_VERSION:
        print(f'agent-content-mcp: the site serves API version {index.get("version")}, '
              f'this server supports {tools.SUPPORTED_API_VERSION}.', file=sys.stderr)
    return index, None


def make_server(api: ContentAPI, index: dict | None, problem: str | None) -> Server:
    specs = {spec.name: spec for spec in tools.build(index)} if index else {}
    if problem:
        # start anyway with a single tool that reports the problem, so the client shows why tools are missing
        specs = {}
        status_tool = types.Tool(
            name='agent_content_status', description='Explains why the site\'s content tools are unavailable.',
            input_schema={'type': 'object', 'properties': {}}, annotations=tools.READ,
        )

    async def list_tools(ctx, params):
        if problem:
            return types.ListToolsResult(tools=[status_tool])
        return types.ListToolsResult(tools=[
            types.Tool(name=s.name, description=s.description, input_schema=s.schema, annotations=s.annotations)
            for s in specs.values()
        ])

    async def call_tool(ctx, params):
        def text(message, error=False):
            return types.CallToolResult(content=[types.TextContent(type='text', text=message)], is_error=error)

        if problem:
            return text(f'The site\'s API could not be loaded when the server started: {problem} '
                        'Check AGENT_CONTENT_URL and AGENT_CONTENT_KEY, then restart the MCP server.', error=True)
        spec = specs.get(params.name)
        if spec is None:
            return text(f'Unknown tool {params.name}.', error=True)
        try:
            method, path, query, body = spec.request(params.arguments or {})
            return text(tools.result_text(await api.request(method, path, json=body, params=query)))
        except KeyError as missing:
            return text(f'Missing argument {missing}.', error=True)
        except Exception as error:
            return text(str(error), error=True)

    return Server(
        'agent-content', version=__version__,
        instructions=tools.instructions(index) if index else None,
        on_list_tools=list_tools, on_call_tool=call_tool,
    )


def main() -> None:
    url, key = os.environ.get('AGENT_CONTENT_URL'), os.environ.get('AGENT_CONTENT_KEY')
    if not url or not key:
        raise SystemExit('agent-content-mcp: set AGENT_CONTENT_URL and AGENT_CONTENT_KEY. '
                         'See https://github.com/hexa01/agent-content-mcp#setup')
    api = ContentAPI(url, key)

    if '--check' in sys.argv[1:]:
        index, problem = asyncio.run(load(api))
        if problem:
            raise SystemExit(f'Could not read {url}: {problem}')
        for spec in tools.build(index):
            print(f'{spec.name:32} {spec.description[:90]}')
        return

    async def serve():
        index, problem = await load(api)
        server = make_server(api, index, problem)
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    asyncio.run(serve())


if __name__ == '__main__':
    main()
