"""
Placeholder MCP entry point.

Mongoose exposes HTTP endpoints first. A proper MCP server should wrap those endpoints later so that MCP tools remain behind Mongoose policy checks. This stub exists so LM Studio configuration has a named integration point without bypassing the local policy layer.
"""

import sys

if __name__ == "__main__":
    sys.stderr.write("Mongoose MCP wrapper is a later extension. Use https://127.0.0.1:8765 for now.\n")
    sys.exit(1)
