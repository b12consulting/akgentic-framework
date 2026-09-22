"""Tool definitions for the agent team."""

import os

from akgentic.tool.knowledge_graph import KnowledgeGraphTool
from akgentic.tool.mcp import MCPHTTPConnectionConfig, MCPTool
from akgentic.tool.planning import PlanningTool, UpdatePlanning
from akgentic.tool.search import SearchTool, WebFetch, WebSearch
from akgentic.tool.workspace import WorkspaceTool

search_tool = SearchTool(
    web_search=WebSearch(max_results=3),
    web_fetch=WebFetch(timeout=30),
    web_crawl=False
)

knowledge_graph_tool = KnowledgeGraphTool()

planning_tool = PlanningTool(
    update_planning=UpdatePlanning(
        instructions="""The plan is the team's shared record of coordinated work.
Use it ONLY when BOTH are true:
- the work is complex: it needs several distinct steps, and
- it involves other team members: you delegate at least one step to someone else.

Do NOT create tasks for work you handle alone, for a single question or answer,
or for a simple one-step delegation. Just do the work and reply.

When the plan is warranted:
- Create one task per delegated step, owned by the member who will do it.
- Update a task's status when it starts, completes, or is blocked.
- Record the output on the task when it is done.

If you already own a task in the plan, keep it current before ending your turn."""
    )
)

WORKSPACE_ID = "myDocuments"
# Sandboxed execution is off by default; pass workspace_exec=True to enable it
# (run ./scripts/build-sandbox-image.sh to build the sandbox image first).
workspace_tool = WorkspaceTool(workspace_id=WORKSPACE_ID)

MCP_BEARER_TOKEN = os.getenv("MCP_BEARER_TOKEN")

# GitHub MCP server, exposed to the LLM as github_* tools
github_tool = MCPTool(
    connection=MCPHTTPConnectionConfig(
        url="https://api.githubcopilot.com/mcp/",
        bearer_token=MCP_BEARER_TOKEN,
        transport="streamable-http",
        tool_prefix="github",
    ),
)

tools = [search_tool, workspace_tool, planning_tool, knowledge_graph_tool]

# The GitHub MCP server rejects anonymous calls, so only wire it in with a token
if MCP_BEARER_TOKEN:
    tools.append(github_tool)
