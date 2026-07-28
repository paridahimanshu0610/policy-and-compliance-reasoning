"""
mcp_server/server.py

Entrypoint. Importing resources and tools registers them on the shared
`mcp` instance (see instance.py); this module just triggers that import
and runs the server.

    python -m mcp_server.server                     # stdio -- local MCP
                                                      # clients, e.g. Claude
                                                      # Desktop
    MCP_TRANSPORT=http python -m mcp_server.server   # Streamable HTTP --
                                                      # for a deployed service
"""

import os
from dotenv import load_dotenv
load_dotenv(override=True)

from .instance import mcp
from . import resources  # noqa: F401  (registers finra-clause://, finra-rule://)
from . import tools      # noqa: F401  (registers search_clauses, ask_finra_compliance_agent, ...)


if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if transport == "http":
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
