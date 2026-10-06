"""
title: Word MCP Pipeline
author: open-webui
author_url: https://github.com/open-webui
version: 1.0.0
license: MIT
description: Integrates Word Automation MCP with SSE support, automatic tool injection, and path normalization
required_open_webui_version: 0.4.0
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx
from pydantic import BaseModel
from pydantic import Field


class Pipeline:
    class Valves(BaseModel):
        pipelines: list[str] = Field(
            default=['*'],
            description='Pipeline identifiers',
        )
        priority: int = Field(
            default=0,
            description='Pipeline priority',
        )
        MCP_SERVER_URL: str = Field(
            default='http://192.168.50.239:9000/mcp',
            description='Word MCP Server URL (SSE endpoint)',
        )
        TIMEOUT: int = Field(
            default=60,
            description='Request timeout in seconds',
        )
        ENABLE_DEBUG: bool = Field(
            default=True,
            description='Enable debug logging',
        )
        AUTO_INJECT_TOOLS: bool = Field(
            default=True,
            description='Auto-inject MCP tools',
        )
        FORCE_TOOL_CHOICE_AUTO: bool = Field(
            default=True,
            description='Force tool_choice=auto when injecting tools',
        )
        WINDOWS_PATH_PREFIX: str = Field(
            default='X:\\',
            description='Windows drive prefix (e.g., X:\\)',
        )
        LINUX_SHARE_DIR: str = Field(
            default='/mnt/data',
            description='Linux shared directory path',
        )
        FILE_SERVER_BASE_URL: str = Field(
            default='http://192.168.50.118:18080',
            description='HTTP file server base URL for downloads',
        )
        MAX_TOOL_CALLS_PER_TURN: int = Field(
            default=12,
            description='Maximum tool calls per turn',
        )

    def __init__(self):
        self.type = 'filter'
        self.name = 'Word MCP Pipeline'
        self.valves = self.Valves()
        self.available_tools: list[dict] = []
        self.tools_loaded: bool = False

    async def on_startup(self):
        """Called when the server starts"""
        print(f"[{self.name}] Starting up...")
        try:
            await self._fetch_mcp_tools()
            print(f"[{self.name}] ✅ Loaded {len(self.available_tools)} tools")
        except Exception as e:
            print(f"[{self.name}] ⚠️  Failed to load tools: {e}")

    async def on_shutdown(self):
        """Called when the server stops"""
        print(f"[{self.name}] Shutting down...")

    async def inlet(self, body: dict, user: dict | None = None) -> dict:
        """Process request before sending to model"""
        if self.valves.ENABLE_DEBUG:
            print(f"[{self.name}] Inlet - Model: {body.get('model')}")

        # Load tools if not already loaded
        if not self.tools_loaded:
            try:
                await self._fetch_mcp_tools()
            except Exception as e:
                if self.valves.ENABLE_DEBUG:
                    print(f"[{self.name}] Tool loading failed: {e}")

        # Inject tools if enabled
        if self.valves.AUTO_INJECT_TOOLS and self.tools_loaded and self.available_tools:
            if 'tools' not in body or body.get('tools') is None:
                body['tools'] = self.available_tools
                if self.valves.FORCE_TOOL_CHOICE_AUTO:
                    body['tool_choice'] = 'auto'
                if self.valves.ENABLE_DEBUG:
                    print(f"[{self.name}] ✅ Injected {len(self.available_tools)} tools")

        # Inject system prompt
        messages = body.get('messages', [])
        if isinstance(messages, list):
            body['messages'] = self._ensure_system_prompt(messages)

        return body

    async def outlet(self, body: dict, user: dict | None = None) -> dict:
        """Process response after receiving from model"""
        if self.valves.ENABLE_DEBUG:
            print(f"[{self.name}] Outlet")

        assistant_message = self._get_assistant_message(body)
        if not assistant_message:
            return body

        # Check for tool calls
        tool_calls = assistant_message.get('tool_calls')
        if not tool_calls:
            # Try JSON fallback
            planned = self._extract_json_tool_calls(assistant_message.get('content', ''))
            tool_calls = self._parse_planned_calls(planned)

        if not tool_calls:
            if self.valves.ENABLE_DEBUG:
                print(f"[{self.name}] No tool calls detected")
            return body

        # Execute tools
        results = await self._execute_tools(tool_calls)

        # Update response
        response_text = self._format_results(results)
        self._update_response(body, response_text)

        if self.valves.ENABLE_DEBUG:
            print(f"[{self.name}] ✅ Updated response")

        return body

    # ==================== MCP Communication ====================

    def _generate_rpc_id(self) -> str:
        """Generate unique RPC ID"""
        return f"rpc-{int(time.time() * 1000)}"

    async def _mcp_request(self, method: str, params: dict | None = None) -> dict:
        """Send JSON-RPC request to MCP server via SSE"""
        rpc_id = self._generate_rpc_id()
        request = {
            'jsonrpc': '2.0',
            'id': rpc_id,
            'method': method,
        }
        if params:
            request['params'] = params

        headers = {
            'Accept': 'text/event-stream',
            'Content-Type': 'application/json',
        }

        if self.valves.ENABLE_DEBUG:
            print(f"[{self.name}] MCP Request: {method} (id={rpc_id})")

        async with httpx.AsyncClient(timeout=self.valves.TIMEOUT) as client:
            async with client.stream(
                'POST',
                self.valves.MCP_SERVER_URL,
                json=request,
                headers=headers,
            ) as response:
                response.raise_for_status()

                data_lines = []
                async for line in response.aiter_lines():
                    if not line:
                        if data_lines:
                            payload = '\n'.join(data_lines)
                            data_lines = []
                            try:
                                obj = json.loads(payload)
                                if isinstance(obj, dict) and obj.get('id') == rpc_id:
                                    return obj
                            except json.JSONDecodeError:
                                pass
                    elif line.startswith('data:'):
                        data_lines.append(line[5:].strip())

        raise RuntimeError(f"No response received for {method}")

    async def _fetch_mcp_tools(self):
        """Fetch available tools from MCP server"""
        response = await self._mcp_request('tools/list')

        if 'error' in response:
            error_msg = response['error'].get('message', 'Unknown error')
            raise RuntimeError(f"MCP error: {error_msg}")

        tools = response.get('result', {}).get('tools', [])
        self.available_tools = [self._convert_tool_format(t) for t in tools]
        self.tools_loaded = True

    def _convert_tool_format(self, mcp_tool: dict) -> dict:
        """Convert MCP tool format to OpenAI function format"""
        return {
            'type': 'function',
            'function': {
                'name': mcp_tool['name'],
                'description': mcp_tool.get('description', ''),
                'parameters': mcp_tool.get(
                    'inputSchema', {
                        'type': 'object',
                        'properties': {},
                    },
                ),
            },
        }

    async def _call_mcp_tool(self, tool_name: str, arguments: dict) -> dict:
        """Execute a tool on MCP server"""
        response = await self._mcp_request(
            'tools/call', {
                'name': tool_name,
                'arguments': arguments,
            },
        )

        if 'error' in response:
            error_msg = response['error'].get('message', 'Unknown error')
            raise RuntimeError(error_msg)

        return response.get('result', {})

    # ==================== Path Normalization ====================

    def _normalize_windows_path(self, path: str) -> str:
        """Convert Linux paths to Windows paths"""
        if not path:
            return path

        path = str(path).strip()

        # Convert /mnt/data/... to X:\...
        linux_dir = self.valves.LINUX_SHARE_DIR.rstrip('/')
        if path.startswith(linux_dir + '/'):
            relative = path[len(linux_dir)+1:]
            prefix = self.valves.WINDOWS_PATH_PREFIX.rstrip('\\') + '\\'
            return prefix + relative.replace('/', '\\')

        # Already Windows path
        if len(path) >= 2 and path[1] == ':':
            return path.replace('/', '\\')

        # UNC path
        if path.startswith('\\\\'):
            return path.replace('/', '\\')

        # Relative path - prepend Windows prefix
        relative = path.lstrip('./\\').replace('/', '\\')
        prefix = self.valves.WINDOWS_PATH_PREFIX.rstrip('\\') + '\\'
        return prefix + relative

    def _fix_paths_in_arguments(self, arguments: dict) -> dict:
        """Fix file paths in tool arguments"""
        path_keys = {
            'file_path', 'filename', 'source_file', 'target_file',
            'source_path', 'target_path', 'output_path', 'pdf_path',
            'path', 'document_path', 'doc_path',
        }

        for key in path_keys:
            if key in arguments and isinstance(arguments[key], str):
                original = arguments[key]
                fixed = self._normalize_windows_path(original)
                if fixed != original:
                    arguments[key] = fixed
                    if self.valves.ENABLE_DEBUG:
                        print(f"[{self.name}] Path fix: {key} -> {fixed}")

        return arguments

    # ==================== Tool Execution ====================

    async def _execute_tools(self, tool_calls: list[dict]) -> dict:
        """Execute multiple tool calls"""
        max_calls = self.valves.MAX_TOOL_CALLS_PER_TURN
        executed_files = []
        successful = []
        failed = []

        for i, call in enumerate(tool_calls[:max_calls]):
            tool_name = call.get('name')
            arguments = call.get('arguments', {})

            if not tool_name:
                continue

            if self.valves.ENABLE_DEBUG:
                print(f"[{self.name}] Executing: {tool_name}")

            try:
                # Fix paths
                arguments = self._fix_paths_in_arguments(arguments)

                # Extract output filename
                output_file = self._extract_filename(arguments)
                if output_file:
                    executed_files.append(output_file)

                # Execute tool
                await self._call_mcp_tool(tool_name, arguments)
                successful.append(tool_name)

                if self.valves.ENABLE_DEBUG:
                    print(f"[{self.name}] ✅ {tool_name} succeeded")

            except Exception as e:
                error_msg = str(e)
                failed.append(f"{tool_name}: {error_msg}")
                if self.valves.ENABLE_DEBUG:
                    print(f"[{self.name}] ❌ {tool_name} failed: {error_msg}")

        return {
            'files': list(dict.fromkeys(executed_files)),
            'successful': successful,
            'failed': failed,
        }

    def _extract_filename(self, arguments: dict) -> str | None:
        """Extract output filename from arguments"""
        for key in ['file_path', 'filename', 'output_path', 'target_path', 'document_path']:
            if key in arguments and isinstance(arguments[key], str):
                path = arguments[key].replace('/', '\\')
                return path.split('\\')[-1]
        return None

    def _format_results(self, results: dict) -> str:
        """Format execution results as user-friendly text"""
        lines = []

        if results['successful']:
            lines.append('✅ Executed tools: ' + ', '.join(results['successful']))

        if results['failed']:
            lines.append('❌ Failed:')
            for error in results['failed']:
                lines.append(f"  - {error}")

        if results['files']:
            base_url = self.valves.FILE_SERVER_BASE_URL.rstrip('/')
            lines.append('\n📥 Download links:')
            for filename in results['files']:
                lines.append(f"  - {base_url}/{filename}")

        return '\n'.join(lines)

    # ==================== JSON Fallback ====================

    def _extract_json_tool_calls(self, content: str) -> Any | None:
        """Extract tool calls from JSON blocks in text"""
        if not content:
            return None

        # Try <mcp_tool_calls>...</mcp_tool_calls>
        match = re.search(
            r'<mcp_tool_calls>(.*?)</mcp_tool_calls>',
            content,
            re.DOTALL | re.IGNORECASE,
        )
        if match:
            try:
                return json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                pass

        # Try ```json...```
        match = re.search(
            r'```json\s*(.*?)\s*```',
            content,
            re.DOTALL | re.IGNORECASE,
        )
        if match:
            try:
                return json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                pass

        return None

    def _parse_planned_calls(self, planned: Any) -> list[dict]:
        """Parse planned tool calls into standard format"""
        if not planned:
            return []

        calls = []

        if isinstance(planned, dict):
            if 'name' in planned and 'arguments' in planned:
                calls.append({
                    'name': planned['name'],
                    'arguments': planned.get('arguments', {}),
                })
        elif isinstance(planned, list):
            for item in planned:
                if isinstance(item, dict) and 'name' in item:
                    calls.append({
                        'name': item['name'],
                        'arguments': item.get('arguments', item.get('parameters', {})),
                    })

        return calls

    # ==================== Message Handling ====================

    def _ensure_system_prompt(self, messages: list[dict]) -> list[dict]:
        """Add system prompt with tool usage instructions"""
        system_prompt = self._build_system_prompt()

        # Check if already exists
        for msg in messages:
            if msg.get('role') == 'system' and '[WORD_MCP]' in msg.get('content', ''):
                return messages

        # Prepend system message
        return [{'role': 'system', 'content': system_prompt}] + messages

    def _build_system_prompt(self) -> str:
        """Build system prompt with instructions"""
        base_url = self.valves.FILE_SERVER_BASE_URL.rstrip('/')
        example_path = self.valves.WINDOWS_PATH_PREFIX.rstrip('\\\\') + '\\\\example.docx'

        return f"""[WORD_MCP] You have access to Word MCP tools for creating/editing .docx and .pdf files.

Instructions:
1. Use function calling (tool_calls) when available
2. All file paths must use Windows format: {self.valves.WINDOWS_PATH_PREFIX}...
3. After completing operations, provide download links: {base_url}/<filename>
4. Never use sandbox:/ paths in responses

Example tool call:
{{"name": "create_document", "arguments": {{"file_path": "{example_path}", "title": "Document"}}}}
"""

    def _get_assistant_message(self, body: dict) -> dict | None:
        """Extract assistant message from response body"""
        # Try messages array
        messages = body.get('messages', [])
        if messages and isinstance(messages[-1], dict):
            return messages[-1]

        # Try choices
        choices = body.get('choices', [])
        if choices and isinstance(choices[0], dict):
            return choices[0].get('message')

        return None

    def _update_response(self, body: dict, text: str):
        """Update response body with new text"""
        # Update messages array
        if 'messages' in body and body['messages']:
            if body['messages'][-1].get('role') == 'assistant':
                body['messages'][-1]['content'] = text
                body['messages'][-1].pop('tool_calls', None)
            else:
                body['messages'].append({'role': 'assistant', 'content': text})
            return

        # Update choices
        if 'choices' in body and body['choices']:
            message = body['choices'][0].get('message', {})
            message['role'] = 'assistant'
            message['content'] = text
            message.pop('tool_calls', None)
            body['choices'][0]['message'] = message
            return

        # Fallback
        body['content'] = text
