# YBase MCP server

Gives any MCP-compatible AI agent (Claude Code, Cursor, IDE assistants,
custom agents) access to your company's YBase memory: evidence-backed
answers, pre-action context briefings, decision search, and full evidence
chains — the same engine the YBase UI uses, over your workspace API key.

## Tools

| Tool | Use it to |
|---|---|
| `get_context_for_task` | Get the pre-action briefing before modifying anything: relevant decisions, reversed-decision warnings, open questions, people. No LLM call — fast. |
| `ask_ybase` | Ask "why" questions and get a cited, confidence-scored answer. |
| `search_memory` | Locate specific decisions/questions/entities/topics by name. |
| `get_decision` | Inspect one decision's full evidence chain and supersession links. |

## Setup

1. In YBase, as a workspace admin: **Settings → API keys → create key**
   (or `POST /api/workspace/api-keys`). Copy the `ybk_...` token — it is
   shown once.
2. Install:

   ```sh
   pip install ./mcp-server        # from the YBase repo root
   # or: uv tool install ./mcp-server
   ```

3. Register with your MCP client. For Claude Code, add to `.mcp.json`:

   ```json
   {
     "mcpServers": {
       "ybase": {
         "command": "ybase-mcp",
         "env": {
           "YBASE_BASE_URL": "https://your-ybase-host",
           "YBASE_API_KEY": "ybk_..."
         }
       }
     }
   }
   ```

The key is workspace-scoped: the agent sees exactly that workspace's memory,
nothing else. Revoke it any time from the same settings page; revocation is
immediate. Requests are rate-limited per key (`AGENT_RATE_PER_MINUTE`,
default 60/min).
