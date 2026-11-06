import json
from collections.abc import Callable
from typing import Any

import httpx

PROTOCOL_VERSION = "2025-03-26"


class McpError(Exception):
    pass


class StreamableHttpMcp:
    """One Google remote MCP server over Streamable HTTP."""

    def __init__(self, url: str, token_provider: Callable[[], str], http: httpx.Client | None = None):
        self.url = url
        self._token = token_provider
        self._http = http or httpx.Client(timeout=60)
        self._owns_http = http is None
        self._session_id = ""
        self._handshaken = False
        self._protocol = False
        self._next_id = 1

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self._handshake()
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        return unwrap_tool_result(result)

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def _handshake(self) -> None:
        if self._handshaken:
            return
        try:
            self._rpc(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "st-newsroom-agent", "version": "0.2.0"},
                },
            )
        except McpError as exc:
            if _transport_error(exc):
                raise
            self._handshaken = True
            return
        self._handshaken = True
        self._protocol = True
        try:
            self._notify("notifications/initialized")
        except McpError as exc:
            if _transport_error(exc):
                raise
            return

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        message = self._post({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params})
        self._next_id += 1
        if "error" in message:
            error = message["error"]
            detail = error.get("message") if isinstance(error, dict) else str(error)
            raise McpError(detail or f"{method} failed.")
        result = message.get("result")
        if not isinstance(result, dict):
            raise McpError(f"{method} returned no result.")
        return result

    def _notify(self, method: str) -> None:
        self._post({"jsonrpc": "2.0", "method": method, "params": {}})

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self._token()}",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if self._protocol:
            headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
        try:
            response = self._http.post(self.url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise McpError(f"MCP request failed: {exc}") from exc
        session = response.headers.get("mcp-session-id")
        if session:
            self._session_id = session
        if response.status_code >= 400:
            raise McpError(f"MCP server returned HTTP {response.status_code}.")
        if not response.content:
            return {}
        message = parse_mcp_body(response.headers.get("content-type", ""), response.text)
        if not isinstance(message, dict):
            raise McpError("MCP server returned an unreadable response.")
        return message


def _transport_error(exc: McpError) -> bool:
    return str(exc).startswith("MCP request failed")


def parse_mcp_body(content_type: str, body: str) -> dict[str, Any]:
    if "text/event-stream" in content_type:
        return _parse_sse(body)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise McpError("MCP server did not return JSON.") from exc
    if not isinstance(payload, dict):
        raise McpError("MCP server did not return a JSON object.")
    return payload


def unwrap_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("isError"):
        text = _content_text(result) or "MCP tool failed."
        raise McpError(text)
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and structured:
        return structured
    text = _content_text(result)
    if text:
        parsed = _maybe_json(text)
        if isinstance(parsed, dict):
            return parsed
        return {"text": text}
    return result


def resource_id(payload: dict[str, Any]) -> str:
    found = _find_id(payload)
    if found:
        return found
    raise McpError("Tool result had no id.")


def document_link(payload: dict[str, Any], document_id: str) -> str:
    link = _find_link(payload)
    if link:
        return link
    return f"https://docs.google.com/document/d/{document_id}/edit"


def _find_id(payload: dict[str, Any]) -> str:
    value = payload.get("id")
    if isinstance(value, str) and value:
        return value
    for key in ("file", "document", "event", "message"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            found = _find_id(nested)
            if found:
                return found
    return ""


def _find_link(payload: dict[str, Any]) -> str:
    for key in ("webViewLink", "htmlLink", "url", "link"):
        value = payload.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    for key in ("file", "document"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            found = _find_link(nested)
            if found:
                return found
    return ""


def _parse_sse(body: str) -> dict[str, Any]:
    last: dict[str, Any] | None = None
    data_lines: list[str] = []

    def flush() -> None:
        nonlocal last, data_lines
        if not data_lines:
            return
        raw = "\n".join(data_lines).strip()
        data_lines = []
        if not raw or raw == "[DONE]":
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise McpError("MCP event stream did not contain JSON.") from exc
        if isinstance(payload, dict):
            last = payload

    for line in body.splitlines():
        if line == "":
            flush()
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    flush()
    if last is None:
        raise McpError("MCP server returned an empty event stream.")
    return last


def _content_text(result: dict[str, Any]) -> str:
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(parts).strip()


def _maybe_json(text: str) -> object:
    stripped = text.strip()
    if not stripped.startswith("{") and not stripped.startswith("["):
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None
