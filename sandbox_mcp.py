"""A tiny MCP server (speaks JSON lines over stdin/stdout) with ONE tool, `run`: run a command in the Docker sandbox.

This is how the Claude coding agent runs code. It has no shell of its own on this PC (its built-in tools are only the file tools), so the only
way it can execute anything is through here, and here everything goes into a throwaway container with no network (see agent_sandbox.py).
The agent's folder comes from the SERA_AGENT_DIR environment variable that the bot sets in the config it hands the CLI."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_sandbox

AGENT_DIR = os.environ.get('SERA_AGENT_DIR', '')


def tool():
    langs = agent_sandbox.GENERAL_LANGUAGES.get(agent_sandbox.general_image(), 'a limited set of tools')
    return {'name': 'run',
            'description': ("Run a shell command in an isolated sandbox: a throwaway container with no network that is not this PC. Your folder's files "
                            "are its working directory (a copy), so use it to compile, run and test what you wrote. Changes made there are NOT kept: "
                            "write and edit files with your file tools, then run them here. Available: " + langs + ". Output is cut if long; "
                            "commands are stopped after the time limit."),
            'inputSchema': {'type': 'object',
                            'properties': {'command': {'type': 'string', 'description': "The shell command, e.g. 'python3 test.py' or 'gcc main.c -o m && ./m'"},
                                           'timeout_seconds': {'type': 'number', 'description': 'Optional, up to 120. Default 30.'}},
                            'required': ['command']}}


def handle(msg):
    """The reply to one request (a dict), or None for a notification."""
    method, mid = msg.get('method'), msg.get('id')
    if mid is None:
        return None  # notifications (like "initialized") get no reply
    ok = lambda result: {'jsonrpc': '2.0', 'id': mid, 'result': result}
    if method == 'initialize':
        return ok({'protocolVersion': (msg.get('params') or {}).get('protocolVersion') or '2024-11-05', 'capabilities': {'tools': {}},
                   'serverInfo': {'name': 'sandbox', 'version': '1'}})
    if method == 'ping':
        return ok({})
    if method == 'tools/list':
        return ok({'tools': [tool()]})
    if method == 'tools/call':
        params = msg.get('params') or {}
        if params.get('name') != 'run':
            return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32602, 'message': f"unknown tool {params.get('name')!r}"}}
        args = params.get('arguments') or {}
        if not AGENT_DIR or not os.path.isdir(AGENT_DIR):
            text = "❌ The sandbox has no folder to work from."
        else:
            try:
                text = agent_sandbox.run_command(args.get('command'), AGENT_DIR, args.get('timeout_seconds'))
            except Exception as e:  # a sandbox failure must reach the agent as text, not kill the server
                text = f"❌ The sandbox failed, so nothing was run: {e}"
        return ok({'content': [{'type': 'text', 'text': text}], 'isError': text.startswith('❌')})
    return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32601, 'message': f'unknown method {method!r}'}}


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            reply = handle(json.loads(line))
        except Exception:
            continue  # not JSON or not a request: ignore it, the next line may be fine
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + '\n')
            sys.stdout.flush()


if __name__ == '__main__':
    main()
