# agent-content-mcp

**MCP server for Django sites: AI agents read and edit content through tools generated from the site.**

Works with Claude Code, Claude Desktop, Cursor and other [MCP](https://modelcontextprotocol.io) clients, and any
Django site running [django-agent-content](https://github.com/hexa01/django-agent-content). Its tools aren't
hard-coded: at startup it reads the site's API description and creates tools for whatever the site has registered,
such as `list_posts`, `update_post` or `publish_post`. One server for every site, with no code to edit.

```
MCP client ──stdio──> agent-content-mcp ──HTTPS + API key──> site /api/agent/ ──> Django models
```

## Requirements

- A Django site with [django-agent-content](https://github.com/hexa01/django-agent-content) installed and an
  API key from it (`python manage.py agent_content_key`, or the site's admin under *Agent content › API keys*).
- [uv](https://docs.astral.sh/uv/) to run the server: `curl -LsSf https://astral.sh/uv/install.sh | sh`

## Setup

Run once, in the project folder where you use Claude Code:

```bash
claude mcp add agent-content \
  -e AGENT_CONTENT_URL=https://example.com/api/agent/ \
  -e AGENT_CONTENT_KEY=ac_your_key \
  -- uvx --from git+https://github.com/hexa01/agent-content-mcp agent-content-mcp
```

Then start Claude Code there; `claude mcp list` shows whether it connected.

| Setting | Meaning |
|---|---|
| `AGENT_CONTENT_URL` | The site's API root. Must be `https://`; plain `http://` is accepted only for `localhost`. |
| `AGENT_CONTENT_KEY` | The API key. It is stored in your Claude Code configuration, not in any repository. |

### Scopes

`claude mcp add` stores the entry for the current project, visible only to you. Other options:

| Option | Stored in | Available |
|---|---|---|
| *(default)* | your Claude Code config | you, in this project |
| `--scope user` | your Claude Code config | you, in all projects |
| `--scope project` | `.mcp.json` in the repository | anyone opening the repository; use `"AGENT_CONTENT_KEY": "${AGENT_CONTENT_KEY}"` so the key stays in each person's environment |

### Other MCP clients

Any client that starts stdio servers can run it with the same command and environment variables, for example
Claude Desktop's `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "agent-content": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/hexa01/agent-content-mcp", "agent-content-mcp"],
      "env": { "AGENT_CONTENT_URL": "https://example.com/api/agent/", "AGENT_CONTENT_KEY": "ac_your_key" }
    }
  }
}
```

## Tools

Each resource the site registers becomes a set of tools:

| Tools | When |
|---|---|
| `list_<name>`, `get_<item>` | always |
| `create_<item>` | the resource allows creating |
| `update_<item>` | the resource has writable fields; only the given fields change |
| `<action>_<item>` | for each registered action, e.g. `publish_post` |
| `get_<item>_<list>`, `set_<item>_<list>` | for each inline list; `set_` replaces the whole list |
| `get_<name>`, `update_<name>` | for single-object resources such as site settings |

Descriptions come from the site's models (labels, help texts, limits, choices) and resource descriptions, so better
documented models give agents better guidance. To see the tools for a site without an MCP client:

```bash
AGENT_CONTENT_URL=https://example.com/api/agent/ AGENT_CONTENT_KEY=ac_... \
  uvx --from git+https://github.com/hexa01/agent-content-mcp agent-content-mcp --check
```

### Asking before public changes

To have Claude Code ask for approval before a tool runs, add it to the project's `.claude/settings.json`:

```json
{ "permissions": { "ask": ["mcp__agent-content__publish_post"] } }
```

## Safety

- The key is sent only over HTTPS (or to localhost). The site stores only its hash and can expire or revoke it.
- What the key can do is limited by its Django user's permissions. The API has no delete operations.
- Every change is recorded in the site's admin history under the key's user.

## Development

```bash
git clone https://github.com/hexa01/agent-content-mcp.git && cd agent-content-mcp
poetry install                  # creates .venv/ here
.venv/bin/agent-content-mcp --check
```

To use a local checkout from Claude Code: `claude mcp add agent-content -e ... -- uvx --from /path/to/agent-content-mcp agent-content-mcp`.

| File | Purpose |
|---|---|
| `agent_content_mcp/server.py` | MCP server over stdio; settings; `--check` |
| `agent_content_mcp/tools.py` | builds tools from the API description and maps calls to requests |
| `agent_content_mcp/client.py` | HTTP client: authentication, the HTTPS rule, readable errors |

## License

MIT
