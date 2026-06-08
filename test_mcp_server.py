"""
Test script: query Bitrix24 MCP server at https://mcp-dev.bitrix24.tech/mcp

MCP uses JSON-RPC 2.0 over HTTP POST.
We test: initialize → tools/list → resources/list → prompts/list
"""
import json
import urllib.error
import urllib.request

MCP_URL = "https://mcp-dev.bitrix24.tech/mcp"


def _parse_sse_body(body: str) -> dict | None:
    """Extract last JSON-RPC message from MCP SSE response."""
    for line in body.splitlines():
        if line.startswith("data: "):
            try:
                return json.loads(line[6:])
            except json.JSONDecodeError:
                continue
    return None


def mcp_request(method: str, params: dict | None = None) -> dict:
    """Send a JSON-RPC request to the MCP server and return parsed response."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params or {},
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        MCP_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            content_type = resp.headers.get("Content-Type", "")
            body = resp.read().decode("utf-8")
            print(f"[{method}] status={resp.status}, content-type={content_type}")
            print(f"[{method}] body preview: {body[:500]}...")
            if "application/json" in content_type:
                return json.loads(body)
            if "text/event-stream" in content_type:
                parsed = _parse_sse_body(body)
                if parsed:
                    return parsed
                return {"_sse": True, "body": body}
            return {"_raw": body}
    except urllib.error.HTTPError as e:
        print(f"[{method}] HTTP error: {e.code} {e.reason}")
        body = e.read().decode("utf-8", errors="replace")
        print(f"[{method}] body: {body[:500]}")
        return {"_error": str(e)}
    except Exception as e:
        print(f"[{method}] Error: {e}")
        return {"_error": str(e)}


def main():
    print("=" * 60)
    print("Bitrix24 MCP Server Test")
    print(f"URL: {MCP_URL}")
    print("=" * 60)

    print("\n--- Step 1: initialize ---")
    init_result = mcp_request(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {
                "name": "b24-ai-auditor-test",
                "version": "1.0.0",
            },
        },
    )
    if isinstance(init_result, dict):
        print(f"Init result keys: {list(init_result.keys())}")

    print("\n--- Step 2: tools/list ---")
    tools_result = mcp_request("tools/list")
    if isinstance(tools_result, dict):
        result = tools_result.get("result", {})
        tools = result.get("tools", []) if isinstance(result, dict) else []
        print(f"Tools count: {len(tools)}")
        for t in tools[:10]:
            name = t.get("name", "?")
            desc = (t.get("description") or "")[:100]
            print(f"  - {name}: {desc}")
        if len(tools) > 10:
            print(f"  ... and {len(tools) - 10} more")
        with open("mcp_tools_list.json", "w", encoding="utf-8") as f:
            json.dump(tools_result, f, ensure_ascii=False, indent=2)
        print("Full tools list saved to mcp_tools_list.json")

    print("\n--- Step 3: resources/list ---")
    resources_result = mcp_request("resources/list")
    if isinstance(resources_result, dict):
        result = resources_result.get("result", {})
        resources = result.get("resources", []) if isinstance(result, dict) else []
        print(f"Resources count: {len(resources)}")
        for r in resources[:5]:
            print(f"  - {r.get('name', '?')}: {r.get('uri', '?')}")

    print("\n--- Step 4: prompts/list ---")
    prompts_result = mcp_request("prompts/list")
    if isinstance(prompts_result, dict):
        result = prompts_result.get("result", {})
        prompts = result.get("prompts", []) if isinstance(result, dict) else []
        print(f"Prompts count: {len(prompts)}")

    print("\n" + "=" * 60)
    print("Test complete!")
    print("=" * 60)


if __name__ == "__main__":
    main()
