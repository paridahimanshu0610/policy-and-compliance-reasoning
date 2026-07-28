"""
mcp_server/instance.py

Single shared FastMCP server instance. Every module that registers a tool
or resource imports `mcp` from here rather than constructing its own --
keeps registration centralized in one object and avoids import-order
surprises (resources.py and tools.py can be imported in either order).
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    name="finra-compliance-agent",
    instructions=(
        "Tools and resources for the FINRA Rule 2000/3000/4000 series clause "
        "knowledge base, and for running the full compliance-reasoning agent "
        "over a described situation.\n\n"
        "Use search_clauses to find clauses by topic. Use the finra-clause:// "
        "and finra-rule:// resources when you already know the exact "
        "clause_ref or rule_id and just want its record. Use "
        "resolve_cross_references to follow a clause's citations to other "
        "rules/clauses instead of leaving them as prose. Use "
        "ask_finra_compliance_agent when you want a full reasoned answer "
        "(with clarification questions, conflict detection, and a "
        "clause-by-clause trace) rather than raw search results."
    ),
)
