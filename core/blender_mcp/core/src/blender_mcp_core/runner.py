"""Entry-point helper shared by the variant servers."""


def run_stdio(mcp) -> None:
    """Run the MCP server"""
    mcp.run()
