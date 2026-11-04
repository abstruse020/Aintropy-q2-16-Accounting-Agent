"""Minimal MCP server (stdio, newline-delimited JSON-RPC) exposing the tools.

Launched by `claude -p` through --mcp-config. Mode and DB path come from env:
  ACCT_MODE = sync | chat      ACCT_DB = path to the sqlite file
"""
import json
import os
import sys

from . import db
from .tools import ToolError, call_tool, tools_for

PROTOCOL = "2024-11-05"


def handle(conn, mode, msg):
    """Return a response dict, or None for notifications."""
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:                      # notification (e.g. notifications/initialized)
        return None
    if method == "initialize":
        version = (msg.get("params") or {}).get("protocolVersion", PROTOCOL)
        result = {"protocolVersion": version, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "acct", "version": "0.1"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": n, "description": d, "inputSchema": schema}
                            for n, (_, d, schema, _) in tools_for(mode).items()]}
    elif method == "tools/call":
        params = msg.get("params") or {}
        try:
            out = call_tool(conn, mode, params.get("name"), params.get("arguments"))
            result = {"content": [{"type": "text", "text": json.dumps(out)}], "isError": False}
        except ToolError as e:
            result = {"content": [{"type": "text", "text": json.dumps({"error": str(e)})}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(conn, mode, stdin=sys.stdin, stdout=sys.stdout):
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        resp = handle(conn, mode, json.loads(line))
        if resp is not None:
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()


if __name__ == "__main__":
    serve(db.connect(os.environ["ACCT_DB"]), os.environ.get("ACCT_MODE", "chat"))
