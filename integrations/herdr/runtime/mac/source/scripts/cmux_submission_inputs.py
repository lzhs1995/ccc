"""Parse literal peer-send inputs without treating message contents as commands.

This is an observation parser, not an interpreter. Dynamic expressions remain
unresolved and cannot authorize a consumption claim.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import shlex
from pathlib import Path

_TARGET = re.compile(r"(?:surface:\d+|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\Z")
_KINDS = {"submit_task_pack": "task", "submit_completion_callback": "callback",
          "submit_text": "text", "send": "text", "send_key": "text"}
_PYTHON_SCRIPT_FLAGS = frozenset(("-B", "-u", "-E", "-I", "-s", "-S"))


def is_bridge_help_command(command: str) -> bool:
    """Recognize one literal help invocation, never help text inside a send.

    Python may precede the script with flags that take no argument. Code/module
    execution, dynamic shell syntax and additional bridge arguments remain
    guarded. Called for each literal JS leaf as well as direct shell commands.
    """
    if any(char in command for char in ('\n', ';', '|', '&', '`', '$', '<', '>')):
        return False
    raw_segments, dynamic = _literal_shell_segments(command)
    if dynamic or len(raw_segments) != 1:
        return False
    try:
        args = shlex.split(raw_segments[0])
    except ValueError:
        return False
    if args[:1] == ['rtk']:
        args = args[1:]
        if args[:1] == ['proxy']:
            args = args[1:]
    if args and re.fullmatch(r'python(?:[23](?:\.\d+)?)?', Path(args[0]).name):
        args = args[1:]
        while args and args[0] in _PYTHON_SCRIPT_FLAGS:
            args = args[1:]
    if not args or Path(args[0]).name not in ('cmux-bridge-toolchain', 'cmux_bridge.py'):
        return False
    rest = args[1:]
    commands = {'submit-text', 'submit_text', 'submit-task-pack', 'submit_task_pack',
                'submit-completion-callback', 'submit_completion_callback', 'read-screen'}
    return (rest in (['--help'], ['-h']) or
            (len(rest) == 2 and rest[0] in commands and rest[1] in ('--help', '-h')))


class _LiteralParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)

    def exit(self, status=0, message=None):
        raise ValueError(message or "help is not a send")


def _helper_calls(args):
    """Mirror the public helper argv; the original message is positional data."""
    if len(args) < 2 or args[1] not in ("ask", "send", "broadcast", "reconcile"):
        return []
    operation = args[1]
    if "--help" in args[2:] or "-h" in args[2:]:
        return []
    parser = _LiteralParser(add_help=False, allow_abbrev=False)
    if operation != "broadcast":
        parser.add_argument("surface")
    if operation == "reconcile":
        parser.add_argument("--intent")
    else:
        parser.add_argument("--request-id", default="")
        parser.add_argument("--no-force-compose", action="store_true")
        parser.add_argument("message", nargs="+")
    try:
        values = vars(parser.parse_args(args[2:]))
    except (ValueError, argparse.ArgumentError):
        return [{"kind": "unresolved", "reason": "nonliteral or invalid helper arguments"}]
    return [{"kind": "helper", "operation": operation,
             "surface": target(values.get("surface")),
             "text": " ".join(values["message"]) if "message" in values else None,
             "request_id": values.get("request_id", ""), "intent": values.get("intent")}]

def _js_tokens(source):
    """Keep strings and comments opaque; never search their contents for calls."""
    tokens, pos = [], 0
    while pos < len(source):
        char = source[pos]
        if char.isspace():
            pos += 1
            continue
        if source.startswith('//', pos) or char == '#':
            end = source.find('\n', pos)
            pos = len(source) if end < 0 else end + 1
            continue
        if source.startswith('/*', pos):
            end = source.find('*/', pos + 2)
            pos = len(source) if end < 0 else end + 2
            continue
        if char in "\"'`":
            start, quote = pos, char
            pos += 1
            while pos < len(source):
                if source[pos] == '\\':
                    pos += 2
                elif source[pos] == quote:
                    pos += 1
                    break
                else:
                    pos += 1
            raw = source[start:pos]
            value = None
            try:
                if raw.endswith(quote) and len(raw) > 1:
                    if quote == '"':
                        value = json.loads(raw)
                    elif quote == "'":
                        value = ast.literal_eval(raw)
                    elif '${' not in raw and '\\' not in raw:
                        value = raw[1:-1]
            except (ValueError, SyntaxError):
                pass
            if quote == '`' and value is None:
                tokens.append(('dynamic_string', raw[1:-1]))
            else:
                tokens.append(('string', value))
            continue
        match = re.match(r'[A-Za-z_$][\w$]*', source[pos:])
        if match:
            tokens.append(('name', match[0]))
            pos += len(match[0])
        else:
            tokens.append(('punct', char))
            pos += 1
    return tokens


def _js_commands(source, depth=0):
    tokens = _js_tokens(source)
    found, commands = False, []

    def expression_end(start, stop):
        nesting, pos = [], start
        while pos < len(tokens):
            kind, value = tokens[pos]
            if not nesting and kind == 'punct' and value in stop:
                break
            if kind == 'punct':
                if value in ('{', '[', '('):
                    nesting.append(value)
                elif value in ('}', ']', ')'):
                    if not nesting or nesting[-1] != {'}':'{', ']':'[', ')':'('}[value]:
                        break
                    nesting.pop()
            pos += 1
        return pos

    def possible_delivery(expression, seen=frozenset()):
        # 仅追踪当前 cmd 表达式及其本地赋值中的静态片段；不执行 JS。
        fragments = []
        for kind, value in expression:
            if kind in ('string', 'dynamic_string') and isinstance(value, str):
                fragments.append(value)
            elif kind == 'name' and value not in seen and len(seen) < 8:
                for j in range(1, len(tokens)-1):
                    if (tokens[j] == ('name', value) and tokens[j+1] == ('punct', '=')
                            and tokens[j-1][0] == 'name' and tokens[j-1][1] in ('const', 'let', 'var')):
                        end = expression_end(j+2, {',', ';'})
                        if possible_delivery(tokens[j+2:end], seen | {value}):
                            return True
        # A help string inside a dynamic expression may be edited into a send.
        # Only a complete literal invocation receives the help exemption.
        return any(delivery_calls(value, depth+1, exclude_help=False)
                   for value in fragments + [' '.join(fragments)])

    for i, token in enumerate(tokens):
        if token != ('name', 'exec_command') or tokens[i+1:i+2] != [('punct', '(')]:
            continue
        # Only a direct call or the documented tools.exec_command form.
        if i and tokens[i-1] == ('punct', '.') and tokens[max(0, i-2):i] != [('name', 'tools'), ('punct', '.')]:
            continue
        found = True
        if tokens[i+2:i+3] != [('punct', '{')]:
            if possible_delivery(tokens[i+2:expression_end(i+2, {')'})]):
                commands.append(None)
            continue
        nesting, pos, values = ['{'], i+3, {}
        expressions = []
        valid = True
        while pos < len(tokens) and nesting:
            kind, value = tokens[pos]
            if len(nesting) == 1 and (pos == i+3 or tokens[pos-1] == ('punct', ',')):
                if kind in ('name', 'string') and value in ('cmd', 'command'):
                    start = pos+2 if tokens[pos+1:pos+2] == [('punct', ':')] else pos
                    expressions.append(tokens[start:expression_end(start, {',', '}'})])
                    if (tokens[pos+1:pos+2] == [('punct', ':')]
                            and len(tokens) > pos+3 and tokens[pos+2][0] == 'string'
                            and isinstance(tokens[pos+2][1], str)
                            and tokens[pos+3] in (('punct', ','), ('punct', '}'))
                            and value not in values):
                        values[value] = tokens[pos+2][1]
                    else:
                        valid = False
                elif tokens[pos:pos+3] == [('punct', '.')] * 3:
                    valid = False  # spread may override the literal command
            if kind == 'punct':
                if value in ('{', '[', '('):
                    nesting.append(value)
                elif value in ('}', ']', ')'):
                    if not nesting or nesting[-1] != {'}':'{', ']':'[', ')':'('}[value]:
                        valid = False
                        break
                    nesting.pop()
            pos += 1
        if valid and not nesting and tokens[pos:pos+1] == [('punct', ')')] and len(values) == 1:
            commands.extend(values.values())
        elif any(possible_delivery(expression) for expression in expressions):
            # 动态 target/text/marker 不能借静态片段取得本次回执。
            commands.append(None)
    return found, commands


def target(value):
    if not isinstance(value, str) or not _TARGET.fullmatch(value):
        return None
    return value if value.startswith("surface:") else value.upper()


def _callback_call(values):
    """只保留能证明只读模式的真实 bool；字符串/动态值不能取得反向身份。"""
    if (not isinstance(values.get("task_pack_path"), str)
            or not values["task_pack_path"]
            or type(values.get("confirm_lines", 200)) is not int
            or values.get("confirm_lines", 200) <= 0
            or any(type(values.get(key, False)) is not bool
                   for key in ("reconcile_only", "resume_queue_only"))
            or (values.get("reconcile_only") and values.get("resume_queue_only"))):
        return {"kind": "unresolved", "reason": "nonliteral or invalid callback arguments"}
    return {"kind": "callback", "surface": None, "pack": values["task_pack_path"],
            "text": None, "marker": None,
            "reconcile_only": values.get("reconcile_only", False)}


def _python_callback(node):
    names = ("task_pack_path", "confirm_lines")
    allowed = {*names, "reconcile_only", "resume_queue_only"}
    try:
        if len(node.args) > len(names):
            raise ValueError("extra positional arguments")
        values = {key: ast.literal_eval(value) for key, value in zip(names, node.args)}
        for keyword in node.keywords:
            if keyword.arg not in allowed or keyword.arg in values:
                raise ValueError("dynamic, duplicate or unknown callback keyword")
            values[keyword.arg] = ast.literal_eval(keyword.value)
        return _callback_call(values)
    except (ValueError, TypeError, SyntaxError, RecursionError):
        return {"kind": "unresolved", "reason": "nonliteral or invalid callback arguments"}


def _cli_callback(args):
    parser = _LiteralParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--task-pack", dest="task_pack_path", required=True)
    parser.add_argument("--confirm-lines", type=int, default=200)
    parser.add_argument("--reconcile-only", action="store_true")
    try:
        options = [word.partition("=")[0] for word in args
                   if word.partition("=")[0] in
                   ("--task-pack", "--confirm-lines", "--reconcile-only")]
        if len(options) != len(set(options)):
            raise ValueError("duplicate callback option")
        return _callback_call(vars(parser.parse_args(args)))
    except (ValueError, argparse.ArgumentError):
        return {"kind": "unresolved", "reason": "nonliteral or invalid callback arguments"}


def _python_calls(source):
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return []
    result = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else (
            node.func.id if isinstance(node.func, ast.Name) else "")
        if name not in _KINDS:
            continue
        if name == "submit_completion_callback":
            result.append(_python_callback(node))
            continue
        def literal(value):
            try:
                value = ast.literal_eval(value)
                return value if isinstance(value, str) else None
            except (ValueError, TypeError, SyntaxError, RecursionError):
                return None
        values = {k.arg: literal(k.value) for k in node.keywords if k.arg}
        names = (("task_pack_path",) if name == "submit_completion_callback" else
                 ("surface", "text", "task_pack_path", "marker") if name == "submit_task_pack" else
                 ("surface", "text", "marker"))
        for key, value in zip(names, node.args):
            values[key] = literal(value)
        result.append({"kind": _KINDS[name], "surface": target(values.get("surface")),
                       "pack": values.get("task_pack_path"), "text": values.get("text"),
                       "marker": values.get("marker")})
    return result


def _comments(command):
    # Legacy '# target surface:N' annotations apply only outside quoted payloads.
    quote = None
    escape = False
    for i, char in enumerate(command):
        if escape:
            escape = False
        elif char == "\\" and quote != "'":
            escape = True
        elif quote:
            if char == quote:
                quote = None
        elif char in "\"'`":
            quote = char
        elif char == "#" and (i == 0 or command[i-1].isspace()):
            match = re.match(r"#\s*target\s+(surface:\d+)\b", command[i:])
            if match:
                yield match[1]


def _literal_shell_segments(command):
    """保留引号边界；展开式不能借展开前的字面值获得旧请求回执。"""
    segments, current = [], []
    quote = None
    escaped = dynamic = False
    pos = 0
    while pos < len(command):
        char = command[pos]
        if escaped:
            current.append(char)
            if char == '\n':
                dynamic = True  # 不猜测 shell 的续行规约。
            escaped = False
        elif quote == "'":
            current.append(char)
            if char == quote:
                quote = None
        elif char == '\\':
            current.append(char)
            escaped = True
        elif quote == '"':
            current.append(char)
            if char == quote:
                quote = None
            elif char in '$`':
                dynamic = True
        elif char in "'\"":
            current.append(char)
            quote = char
        elif char == '#' and (not current or current[-1].isspace()):
            end = command.find('\n', pos)
            pos = len(command) if end < 0 else end
            continue
        elif char in ';&|\n':
            if current:
                segments.append(''.join(current))
            current = []
        else:
            if char in '$`*?[]{}()<>':
                dynamic = True
            if char == '~' and (not current or current[-1].isspace()):
                dynamic = True
            current.append(char)
        pos += 1
    if current:
        segments.append(''.join(current))
    return segments, dynamic or escaped or quote is not None


def literal_command_argv(command):
    """Return one complete static command's argv, or None for shell syntax.

    Equal arguments may use different quoting. Never drop a comment or an
    empty control tail, or treat expansion syntax as its unexpanded text.
    """
    if not isinstance(command, str) or any(char in command for char in '\r\n\0'):
        return None
    segments, dynamic = _literal_shell_segments(command)
    if dynamic or segments != [command]:
        return None
    try:
        return shlex.split(command, comments=False, posix=True) or None
    except ValueError:
        return None


def delivery_calls(command, depth=0, *, exclude_help=True):
    if depth > 8:
        return []
    if exclude_help and is_bridge_help_command(command):
        return []
    # Codex JS tool wrappers: decode only literal command arguments as data.
    found_js, commands = _js_commands(command, depth)
    if found_js:
        return [call for value in commands for call in (
            delivery_calls(value, depth+1, exclude_help=exclude_help) if value is not None else
            [{'kind': 'unresolved', 'reason': 'dynamic peer-send command'}])]

    # A direct Python invocation (including the body of a shell heredoc).
    direct = _python_calls(command)
    if direct:
        return direct
    heredoc = re.search(r"(^|\n)([^\n]*?)<<\s*(?P<quote>['\"]?)(?P<delimiter>\w+)(?P=quote)[^\n]*\n(?P<body>.*?)\n(?P=delimiter)(?:\n|$)", command, re.S)
    if heredoc:
        try:
            header = shlex.split(heredoc[2])
        except ValueError:
            header = []
        while header and header[0] in ('rtk', 'proxy'):
            header.pop(0)
        body = _python_calls(heredoc['body']) if header and Path(header[0]).name.startswith('python') else []
        if body and not heredoc['quote'] and any(c in heredoc['body'] for c in '$`'):
            body = [{'kind': 'unresolved', 'reason': 'shell-expanded Python heredoc'}]
        return (delivery_calls(command[:heredoc.start()], depth+1, exclude_help=exclude_help) + body
                + delivery_calls(command[heredoc.end():], depth+1, exclude_help=exclude_help))
    raw_segments, dynamic_shell = _literal_shell_segments(command)
    try:
        segments = [shlex.split(raw, comments=False, posix=True) for raw in raw_segments]
    except ValueError:
        return []
    result = []
    for raw, args in zip(raw_segments, segments):
        # A shell batch can contain both help and a real callback. Exclude only
        # an exact literal help leaf; never exempt the whole compound command.
        if exclude_help and not dynamic_shell and is_bridge_help_command(raw):
            continue
        while args and args[0] in ("rtk", "proxy"):
            args = args[1:]
        if not args:
            continue
        executable = Path(args[0]).name
        if executable in ('sh', 'bash', 'zsh') and '-c' in args:
            index = args.index('-c')
            if len(args) > index+1:
                result.extend(delivery_calls(args[index+1], depth+1, exclude_help=exclude_help))
            continue
        if executable.startswith("python") and "-c" in args:
            index = args.index("-c")
            if len(args) > index+1:
                result.extend(_python_calls(args[index+1]))
            continue
        if executable.startswith("python") and len(args) > 1:
            args = args[1:]
            while args and args[0] in _PYTHON_SCRIPT_FLAGS:
                args = args[1:]
            if not args:
                continue
            executable = Path(args[0]).name
        if executable in ("cmux-agent", "cmux_agent_adapter.py"):
            result.extend(_helper_calls(args))
            continue
        if executable not in ("cmux-bridge-toolchain", "cmux_bridge.py", "cmux_bridge",
                              "cmux-bridge", "cmux") or len(args) < 2:
            continue
        name = args[1].replace("-", "_")
        if name not in _KINDS:
            continue
        if name == "submit_completion_callback":
            result.append(_cli_callback(args[2:]))
            continue
        values = {}
        pos = 2
        while pos < len(args):
            option, sep, value = args[pos].partition("=")
            if option in ("--surface", "--target", "--target-surface", "--text", "--marker", "--task-pack"):
                if not sep and pos+1 < len(args):
                    pos += 1
                    value = args[pos]
                values[option] = value
            pos += 1
        result.append({"kind": _KINDS[name],
                       "surface": target(values.get("--surface") or values.get("--target") or values.get("--target-surface")),
                       "pack": values.get("--task-pack"), "text": values.get("--text"),
                       "marker": values.get("--marker")})
    if result and dynamic_shell:
        return [{'kind': 'unresolved', 'reason': 'nonliteral shell peer-send arguments'}]
    if not any(call.get("surface") for call in result):
        result.extend({"kind": "legacy", "surface": ref} for ref in _comments(command))
    return result
