#!/usr/bin/env python3
"""
cmux_submit_confirmation_guard 的语义测试。

原则（来自本工程既有教训）：
- 走真实入口（subprocess + stdin JSON + 退出码），不只测函数定义。
- 正对照必做：先证探针能命中目标，再信任它报的「0 问题」。
- 负对照必做：故意构造「已消费」和「非投递」屏，必须不报警。
- 变异测试：把判据打瘸，测试必须转红；否则测试在测自己。
"""
from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
GUARD = SCRIPT_DIR / "cmux_submit_confirmation_guard.py"
PY = sys.executable

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import cmux_submit_confirmation_guard as guard  # noqa: E402

FAILURES: list[str] = []
PASSES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSES.append(name)
        print(f"  PASS  {name}")
    else:
        FAILURES.append(f"{name}: {detail}")
        print(f"  FAIL  {name}  {detail}")


# ---------------------------------------------------------------------------
# 真实屏幕形状（取自 2026-10-04 实测，不是想象的形状）
# ---------------------------------------------------------------------------

# 这是本次事故的真实屏幕：DONE 行粘在 Codex compose 框里，未提交。
SCREEN_PENDING_CODEX = "\n".join([
    "• Ran rtk proxy python3 - <<'PY' …",
    "  └ (no output)",
    "• Compacting context (1m 03s • esc to interrupt)",
    "  └ Making room to continue.",
    "› DONE|example-delivery-review|0123456789abcdef|REPORT=/tmp/example-project/report.md",
    "  example-continuation/review/executor-report.md",
    "  GPT-6-Astra xhigh · ~/example-project · Context 36% used · 258K window · Fast on",
    "  tab to queue message",
])

# 同一 surface 稍后：旧 glyph 只能作线索；没有原 attempt 不能证明这一次消费。
SCREEN_CONSUMED_CODEX = "\n".join([
    "• Ran rtk proxy python3 - <<'PY' …",
    "› DONE|example-delivery-review|0123456789abcdef|REPORT=/tmp/example-project/report.md",
    "  └ 已读取执行者报告",
    "• Ran rtk gh repo view example-org/example-repo",
    "  └ Tip: When the composer is empty, press Esc to step back and edit your last message.",
    "› Ask Codex to do anything",
    "  GPT-6-Astra xhigh · ~/example-project · Context 16% used · 258K window · Fast on",
    "  ? for shortcuts",
])

# 排队态：接收端明确显示待提交队列，按协议应等待而非重发。
SCREEN_QUEUED_CODEX = "\n".join([
    "• Working (2m 10s • esc to interrupt)",
    "  Messages to be submitted after next tool call",
    "  ↳ DONE|example-delivery-review|0123456789abcdef|REPORT=/tmp/example-project/report.md",
    "› Ask Codex to do anything",
    "  GPT-6-Astra xhigh · ~/example-project · Context 16% used · 258K window · Fast on",
    "  ? for shortcuts",
])

# 完全无关的空闲屏：不该报警。
SCREEN_IDLE = "\n".join([
    "• Ran ls",
    "  └ README.md",
    "› Ask Codex to do anything",
    "  GPT-6-Astra xhigh · ~/Documents/cnm · Context 5% used · 258K window · Fast on",
    "  ? for shortcuts",
])

MARKER = "0123456789abcdef"


# ---------------------------------------------------------------------------
# 0. 正对照：探针必须先证明自己能命中目标
# ---------------------------------------------------------------------------
print("\n=== 0. 正对照（证明判据非空转）===")

check(
    "guard file exists",
    GUARD.is_file(),
    f"missing {GUARD}",
)
check(
    "protocol-line regex matches a real DONE line",
    bool(guard._PROTOCOL_LINE_RE.search(
        "› DONE|task|nonce|REPORT=/x.md")),
    "regex matched nothing — probe is dead",
)
check(
    "protocol-line regex matches PREFLIGHT_ACK",
    bool(guard._PROTOCOL_LINE_RE.search(
        "› PREFLIGHT_ACK|t|claude:identity|READY|INLINE|21ff15e3")),
    "ACK shape not recognized",
)
check(
    "protocol-line regex does NOT match ordinary prose",
    not guard._PROTOCOL_LINE_RE.search("I am done with the review"),
    "regex over-matches prose → false positives",
)
check(
    "delivery detection fires on submit_completion_callback",
    guard.looks_like_delivery("python3 -c 'cmux_bridge.submit_completion_callback(p)'"),
    "would never inspect the very call that strands receipts",
)
check(
    "delivery detection ignores unrelated commands",
    not guard.looks_like_delivery("git status && ls -la"),
    "would add read-screen overhead to every Bash call",
)


# ---------------------------------------------------------------------------
# 1. 核心阳性：真实事故屏必须判 PENDING_UNSUBMITTED
# ---------------------------------------------------------------------------
print("\n=== 1. 核心阳性（本次事故的真实屏）===")

verdict_pending = guard.classify_surface(SCREEN_PENDING_CODEX, [MARKER])
check(
    "real stuck-in-compose screen -> PENDING_UNSUBMITTED",
    verdict_pending["verdict"] == "PENDING_UNSUBMITTED",
    f"got {verdict_pending['verdict']} evidence={verdict_pending.get('evidence')}",
)
check(
    "verdict carries compose evidence (not a bare boolean)",
    verdict_pending.get("evidence", {}).get("compose_has_protocol_line") is True
    or bool(verdict_pending.get("evidence", {}).get("marker_states")),
    f"evidence empty: {verdict_pending.get('evidence')}",
)


# ---------------------------------------------------------------------------
# 2. 负对照：已消费 / 排队 / 空闲 都不得报警
# ---------------------------------------------------------------------------
print("\n=== 2. 负对照（必须不报警）===")

verdict_consumed = guard.classify_surface(SCREEN_CONSUMED_CODEX, [MARKER])
check(
    "consumed screen is NOT flagged pending",
    verdict_consumed["verdict"] != "PENDING_UNSUBMITTED",
    f"false positive: {verdict_consumed}",
)
check(
    "historical marker and activity alone never grant CONSUMED",
    verdict_consumed["verdict"] != "CONSUMED",
    f"unbound historical screen was accepted: {verdict_consumed}",
)

verdict_queued = guard.classify_surface(SCREEN_QUEUED_CODEX, [MARKER])
check(
    "queued-at-receiver is NOT flagged pending (wait, never resend)",
    verdict_queued["verdict"] != "PENDING_UNSUBMITTED",
    f"false positive on queued delivery: {verdict_queued}",
)
# 这两条断言原本写成 `!= PENDING_UNSUBMITTED` / `in {NO_PROTOCOL_LINE, CONSUMED}`，
# 于是「把排队态/无协议行判成已消费」这个真缺陷照样全绿（2026-10-04 peer V5 指出）。
# 负向断言必须钉住确切 verdict，否则它对自己要防的那个回归是空转的。
check(
    "queued is NOT labelled CONSUMED (not-yet-read can never prove read)",
    verdict_queued["verdict"] == "QUEUED",
    f"got {verdict_queued['verdict']} — queued must be its own verdict",
)

verdict_idle = guard.classify_surface(SCREEN_IDLE, [MARKER])
check(
    "idle screen with no protocol line is not flagged",
    verdict_idle["verdict"] == "NO_PROTOCOL_LINE",
    f"got {verdict_idle['verdict']}",
)
check(
    "NO_PROTOCOL_LINE carries an explicit 'not proof of consumption' note",
    "NOT proof of consumption" in str(verdict_idle.get("evidence", {}).get("note", "")),
    f"missing disclaimer: {verdict_idle.get('evidence')}",
)


# ---------------------------------------------------------------------------
# 3. 未量到 != 零：读不到屏必须报 INDETERMINATE
# ---------------------------------------------------------------------------
print("\n=== 3. 未量到不算零 ===")

verdict_empty = guard.classify_surface("", [MARKER])
check(
    "empty screen -> INDETERMINATE (not silently PASS)",
    verdict_empty["verdict"] == "INDETERMINATE",
    f"got {verdict_empty['verdict']} — absence rendered as success",
)

verdict_nobridge = guard.classify_surface(SCREEN_PENDING_CODEX, [MARKER],
                                          bridge=object())
check(
    "missing bridge predicates still detect via compose shape OR reports INDETERMINATE",
    verdict_nobridge["verdict"] in {"PENDING_UNSUBMITTED", "INDETERMINATE"},
    f"got {verdict_nobridge['verdict']} — would hide a real defect",
)


# ---------------------------------------------------------------------------
# 4. evaluate() 层：注入 reader，验证动作与窗口记账
# ---------------------------------------------------------------------------
print("\n=== 4. evaluate() 行为 ===")

CMD_CALLBACK = (
    "python3 -c \"import cmux_bridge; "
    "cmux_bridge.submit_completion_callback('/tmp/task-pack.json')\" "
    "# target surface:42 marker 0123456789abcdef"
)

res_warn = guard.evaluate(
    {"tool_input": {"command": CMD_CALLBACK}},
    reader=lambda surface, n: SCREEN_PENDING_CODEX,
)
check(
    "evaluate -> warn on stuck compose",
    res_warn["action"] == "warn",
    f"got {res_warn}",
)
check(
    "evaluate records confirm_lines (a reading is only a fact with its window)",
    isinstance(res_warn.get("confirm_lines"), int) and res_warn["confirm_lines"] >= 200,
    f"confirm_lines={res_warn.get('confirm_lines')}",
)

res_pass = guard.evaluate(
    {"tool_input": {"command": CMD_CALLBACK}},
    reader=lambda surface, n: SCREEN_CONSUMED_CODEX,
)
check(
    "evaluate -> pass on consumed screen",
    res_pass["action"] == "pass",
    f"got {res_pass}",
)

res_skip = guard.evaluate({"tool_input": {"command": "git log --oneline -5"}})
check(
    "evaluate -> skip on non-delivery command",
    res_skip["action"] == "skip",
    f"got {res_skip}",
)

res_readfail = guard.evaluate(
    {"tool_input": {"command": CMD_CALLBACK}},
    reader=lambda surface, n: (_ for _ in ()).throw(RuntimeError("cmux unavailable")),
)
check(
    "read failure -> INDETERMINATE per-surface, not a pass claim",
    any(v.get("verdict") == "INDETERMINATE"
        for v in res_readfail.get("surfaces", {}).values()),
    f"got {res_readfail}",
)


# ---------------------------------------------------------------------------
# 5. 真实入口：subprocess + stdin + 退出码
# ---------------------------------------------------------------------------
print("\n=== 5. 真实 hook 入口（subprocess/exit code）===")


def run_guard(payload: dict, env_extra: dict | None = None) -> tuple[int, str]:
    env = dict(os.environ)
    env.pop("CMUX_SUBMIT_GUARD_DISABLE", None)
    env.pop("CMUX_SUBMIT_GUARD_ADVISORY", None)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [PY, str(GUARD)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    return proc.returncode, proc.stderr


rc_benign, _ = run_guard({"tool_input": {"command": "ls -la"}})
check(
    "benign payload exits 0 through real entrypoint",
    rc_benign == 0,
    f"rc={rc_benign} — guard would block unrelated work",
)

rc_empty, _ = run_guard({})
check(
    "empty payload exits 0 (fail-open on no command)",
    rc_empty == 0,
    f"rc={rc_empty}",
)

rc_disabled, _ = run_guard(
    {"tool_input": {"command": CMD_CALLBACK}},
    {"CMUX_SUBMIT_GUARD_DISABLE": "1"},
)
check(
    "kill switch honoured",
    rc_disabled == 0,
    f"rc={rc_disabled}",
)

rc_malformed, _ = run_guard({"tool_input": "not-a-dict"})
check(
    "malformed payload does not crash the guard",
    rc_malformed == 0,
    f"rc={rc_malformed}",
)


# ---------------------------------------------------------------------------
# 6. 变异测试：打瘸判据，测试必须转红
# ---------------------------------------------------------------------------
print("\n=== 6. 变异测试（证明测试不是在测自己）===")

import re as _re  # noqa: E402

# 必须把 has_protocol_shape 真正查的每一个判据都关掉。只关其中两个，
# 剩下的仍可能命中，变异测试就变成了「测试自己」——2026-10-04 新增冒号
# 形态判据后这里一度只关 2/4。判据集扩张时这份名单必须同步。
_NEVER = _re.compile(r"(?!x)x")
_SHAPE_PREDICATE_NAMES = (
    "_PROTOCOL_LINE_RE",
    "_PROTOCOL_COLON_RE",
    "_EXECUTOR_REPORT_RE",
    "_REPORT_BINDING_RE",
)

# 正对照：先证明这份名单覆盖了 has_protocol_shape 的全部判据来源。
_shape_src = inspect.getsource(guard.has_protocol_shape)
_referenced = {n for n in _SHAPE_PREDICATE_NAMES if n in _shape_src}
_unlisted = sorted(
    n
    for n in dir(guard)
    if n.endswith("_RE") and n in _shape_src and n not in _SHAPE_PREDICATE_NAMES
)
check(
    "mutation list covers every predicate has_protocol_shape consults",
    len(_referenced) == len(_SHAPE_PREDICATE_NAMES) and not _unlisted,
    f"referenced={sorted(_referenced)} unlisted={_unlisted}",
)

_saved = {name: getattr(guard, name) for name in _SHAPE_PREDICATE_NAMES}
try:
    for name in _SHAPE_PREDICATE_NAMES:
        setattr(guard, name, _NEVER)
    check(
        "all shape predicates are truly disabled under mutation",
        not guard.has_protocol_shape(
            "› DONE|t|0123456789abcdef|REPORT=/x.md\nSTATUS: TASK_ID=t\nEXECUTOR REPORT | TASK_ID=t"
        ),
        "mutation left some shape still matching — mutation was partial",
    )
    mutated = guard.classify_surface(SCREEN_PENDING_CODEX, ["zzzzzzzzzzzzzzzz"])
    check(
        "with predicates disabled AND a wrong marker, the real screen is NOT flagged",
        mutated["verdict"] != "PENDING_UNSUBMITTED",
        "mutation did not change the verdict — the assertion was vacuous",
    )
finally:
    for name, value in _saved.items():
        setattr(guard, name, value)

check(
    "predicates restored after mutation",
    all(getattr(guard, n) is _saved[n] for n in _SHAPE_PREDICATE_NAMES),
    "mutation leaked into module state",
)

# 恢复后同一屏必须重新被判为阳性（证明上面的红是变异造成的）
reverify = guard.classify_surface(SCREEN_PENDING_CODEX, [MARKER])
check(
    "same screen is flagged again after restore",
    reverify["verdict"] == "PENDING_UNSUBMITTED",
    f"got {reverify['verdict']} — restore failed",
)


# ---------------------------------------------------------------------------
# 7. 两项 peer 复核修复的回归钉（2026-10-04 V3 冒号形态 / V5 UUID 目标）
#
# 这两项此前只有注释和变异测试的负向断言提到，没有任何正向断言钉住 ——
# 把 regex 削弱回去（例如冒号形态只认行首）全套测试照样全绿。
# ---------------------------------------------------------------------------
print("\n=== 7. 形态与目标解析回归钉 ===")

# 7a. 冒号形态：取自 executor-protocol.md 的真实投递串，不是想象的形状。
COLON_SHAPES = [
    "STATUS: TASK_ID=example-task MILESTONE=numeric EVIDENCE=/tmp/e.json",
    "DONE: EXECUTOR REPORT | TASK_ID=t | STATUS=DONE | EVIDENCE=/tmp/r.md",
    "BLOCKED: EXECUTOR REPORT | TASK_ID=t | STATUS=BLOCKED | EVIDENCE=/tmp/r.md",
    "STATUS: CLAUDE_HEALTH_READY_4474_1112",
    # receiver 是 SHELL 时 compose 里躺的是整条命令，协议行在行中而非行首
    'cmux-agent ask surface:42 "STATUS: TASK_ID=t MILESTONE=m EVIDENCE=/tmp/e.json"',
]
check(
    "colon-form protocol lines are all detected",
    all(guard.has_protocol_shape(s) for s in COLON_SHAPES),
    f"missed: {[s[:48] for s in COLON_SHAPES if not guard.has_protocol_shape(s)]}",
)
check(
    "pipe-form still detected (no regression from the colon fix)",
    guard.has_protocol_shape("› DONE|t|0123456789abcdef|REPORT=/tmp/r.md")
    and guard.has_protocol_shape("PREFLIGHT_ACK|t|claude:identity|READY|INLINE|21ff15e3"),
    "pipe form broke",
)
# 负对照：讲解协议的散文必须不命中，否则 guard 会在读文档时自我报警。
PROSE_CONTROLS = [
    "the executor must actively send `STATUS:`, `DONE:`, or `BLOCKED:` to the supervisor",
    "- Match a screen terminal marker only when `DONE:` or `BLOCKED:` begins the line",
    "The DONE criteria are documented in the executor protocol.",
    "这些指标分别涉及资源、能力、需求和评价，不能统称为未满足的照料需求。",
]
check(
    "prose ABOUT the protocol does not match (backtick form stays excluded)",
    not any(guard.has_protocol_shape(s) for s in PROSE_CONTROLS),
    f"false positive on prose: {[s[:48] for s in PROSE_CONTROLS if guard.has_protocol_shape(s)]}",
)

# 7b. UUID 绑定目标：`_run()` 在发送前把 --surface <ref> 改写成 target_surface_uuid，
#     所以 hook 真正看到的命令常常只有 UUID。只认 surface:N 会静默 skip。
EXEC_UUID = "483B084A-A135-470C-8562-DD89C545318E"
WS_UUID = "C72EE6C7-6338-4328-9EA7-6F81C3E5305D"
CMD_UUID = (
    f"cmux send --workspace {WS_UUID} --surface {EXEC_UUID} -- "
    "'DONE|t|0123456789abcdef|REPORT=/tmp/r.md'"
)
res_uuid = guard.evaluate(
    {"tool_input": {"command": CMD_UUID}},
    reader=lambda surface, n: SCREEN_PENDING_CODEX,
)
check(
    "UUID-bound delivery resolves a target (no longer silently skipped)",
    res_uuid["action"] == "warn" and EXEC_UUID in res_uuid.get("surfaces", {}),
    f"got action={res_uuid.get('action')} surfaces={list(res_uuid.get('surfaces', {}))}",
)
check(
    "workspace UUID is not mistaken for a read target",
    WS_UUID not in res_uuid.get("surfaces", {}),
    f"scraped the wrong UUID: {list(res_uuid.get('surfaces', {}))}",
)
# 负对照：没有 --surface/--target 上下文的裸 UUID 不是投递目标。
res_bare = guard.evaluate(
    {"tool_input": {"command": f"cmux_bridge # task {EXEC_UUID} mentioned in prose"}},
    reader=lambda surface, n: SCREEN_PENDING_CODEX,
)
check(
    "bare UUID without target context is NOT treated as a surface",
    not res_bare.get("surfaces"),
    f"over-matched: {list(res_bare.get('surfaces', {}))}",
)
check(
    "surface:N form still resolves (no regression from the UUID fix)",
    EXEC_UUID
    not in guard.evaluate(
        {"tool_input": {"command": "cmux_bridge submit_text --surface surface:42"}},
        reader=lambda surface, n: SCREEN_IDLE,
    ).get("surfaces", {})
    and "surface:42"
    in guard.evaluate(
        {"tool_input": {"command": "cmux_bridge submit_text --surface surface:42"}},
        reader=lambda surface, n: SCREEN_IDLE,
    ).get("surfaces", {}),
    "surface:N resolution broke",
)


# ---------------------------------------------------------------------------
# 8. Real main entry with isolated screen/ledger; no terminal input or service.
# ---------------------------------------------------------------------------
print("\n=== 8. 嵌套 Codex 输入与真实 main ===")
import contextlib
import io
import tempfile
from unittest.mock import patch
import cmux_bridge as bridge
from cmux_delivery_evidence import digest

def isolated_main(payload, screen, *, route=None, pack=None):
    stderr = io.StringIO()
    with tempfile.TemporaryDirectory(prefix="submit-guard-main-") as temporary:
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(sys, "stdin", io.StringIO(json.dumps(payload))))
            stack.enter_context(contextlib.redirect_stderr(stderr))
            stack.enter_context(patch.object(guard, "_LEDGER", Path(temporary) / "audit.jsonl"))
            stack.enter_context(patch.object(guard, "_bridge", return_value=bridge))
            reader = stack.enter_context(patch.object(bridge, "read_screen", return_value=screen))
            sender = stack.enter_context(patch.object(bridge, "send_text", side_effect=AssertionError("no input")))
            enter = stack.enter_context(patch.object(bridge, "send_key", side_effect=AssertionError("no key")))
            route_call = stack.enter_context(patch.object(bridge, "pin_workspace", return_value=route))
            if pack is not None:
                stack.enter_context(patch.object(bridge, "validate_task_pack_contract", return_value=pack))
            stack.enter_context(patch.dict(os.environ, {"CMUX_SUBMIT_GUARD_DISABLE": "0",
                                                      "CMUX_SUBMIT_GUARD_ADVISORY": "0"}))
            rc = guard.main()
            audit = Path(temporary) / "audit.jsonl"
            records = [json.loads(s) for s in audit.read_text().splitlines()] if audit.exists() else []
            assert sender.call_count == enter.call_count == 0
            return rc, stderr.getvalue(), records, reader.call_count, route_call.call_count

delivery_command = "cmux-bridge-toolchain send --surface surface:42 --text 'DONE|t|0123456789abcdef|REPORT=/tmp/x.md'"
input_shapes = [
    {"tool_name": "Bash", "tool_input": {"command": delivery_command}},
    {"tool_name": "exec_command", "tool_input": {"cmd": delivery_command}},
    {"tool_name": "functions.exec", "tool_input": "await tools.exec_command(" + json.dumps({"cmd": delivery_command}) + ");"},
    {"tool_name": "functions.exec", "tool_input": {"code": "await tools.exec_command(" + json.dumps({"cmd": delivery_command}) + ");"}},
    {"tool_name": "exec_command", "tool_input": json.dumps({"cmd": delivery_command})},
    {"tool_name": "parallel", "tool_input": {"tool_uses": [{"parameters": {"cmd": delivery_command}}]}},
]
for index, payload in enumerate(input_shapes, 1):
    rc, output, records, reads, _ = isolated_main(payload, SCREEN_PENDING_CODEX)
    check(f"main nested input {index}: pending -> exit 2 with one read and zero writes",
          rc == 2 and reads == 1 and "未提交" in output
          and records[0]["surfaces"]["surface:42"] == "PENDING_UNSUBMITTED",
          f"rc={rc}, reads={reads}, output={output}")

rc, output, records, reads, _ = isolated_main(
    {"tool_input": {"cmd": "rtk proxy pwd"}, "tool_response": {"command": delivery_command}},
    SCREEN_PENDING_CODEX)
check("tool response text is never promoted into another delivery command", rc == 0 and reads == 0)

for name, screen, expected in [
    ("queued", SCREEN_QUEUED_CODEX, "QUEUED"),
    ("historical glyph", SCREEN_CONSUMED_CODEX, "INDETERMINATE"),
    ("empty", "", "INDETERMINATE"),
    ("absent", SCREEN_IDLE, "NO_PROTOCOL_LINE"),
]:
    rc, output, records, reads, _ = isolated_main(input_shapes[1], screen)
    check(f"main {name}: prints exact non-consumption state", rc == 0 and expected in output
          and records[0]["surfaces"]["surface:42"] == expected and "CONSUMED:" not in output,
          output)
rc, output, _, _, _ = isolated_main({"tool_input": {"cmd": "cmux_bridge.submit_text(target, text)"}}, SCREEN_IDLE)
check("unresolved delivery target is visibly unknown, not skipped", rc == 0 and "INDETERMINATE" in output)
rendered = guard._render({"surfaces": {}})
check("pending guidance rejects queued/old glyph as proof and never directs a blind key",
      "排队、旧 glyph" in rendered and "本 Hook 不补按 Enter/Tab" in rendered
      and "判据三选一才算送达" not in rendered)

# ---------------------------------------------------------------------------
# 9. Durable callback proof through real main. Actual filesystem/hash/parser;
#    only transport identity and contract loading are fixtures.
# ---------------------------------------------------------------------------
print("\n=== 9. 原 attempt 绑定与完整 payload 正负控 ===")
with tempfile.TemporaryDirectory(prefix="submit-guard-proof-") as temporary:
    root = Path(temporary)
    pack_path = root / "task-pack.json"
    report_path = root / "executor-report.md"
    report_path.write_text("scoped report\n")
    callback = "DONE|t|" + MARKER + "|REPORT=" + str(report_path)
    pack = {"callback_target": "surface:42", "completion_nonce": MARKER,
            "completion_callback": callback, "report": str(report_path)}
    pack_path.write_text(json.dumps(pack))
    route = {"workspace_uuid": WS_UUID, "caller_surface_uuid": "11111111-2222-3333-4444-555555555555",
             "target_surface_uuid": EXEC_UUID, "target_pane_uuid": "66666666-7777-8888-9999-000000000000"}
    attempt_path = root / ".local/state/multi-agent-collaboration/deliveries-v1" / (
        digest(json.dumps([route["caller_surface_uuid"], MARKER], sort_keys=True)) + ".json")
    attempt_path.parent.mkdir(parents=True)
    after = "› " + callback + "\n• Read this report\n› Ask Codex to do anything\nGPT-6-Astra\n? for shortcuts"
    attempt = {"version": 1, "identity": route, "marker": MARKER, "payload_sha256": digest(callback),
               "binding": {str(pack_path.resolve()): digest(pack_path.read_text()), str(report_path): digest(report_path.read_text())},
               "phase": "confirmed", "paste_intent": True, "enter_attempts": 1,
               "before_screen": SCREEN_IDLE, "last_screen": after}
    callback_command = "python3 -c \"import cmux_bridge; cmux_bridge.submit_completion_callback('" + str(pack_path) + "')\""
    hook = {"tool_input": {"cmd": callback_command}}
    cases = [
        ("exact payload and fresh bound attempt", {}, True),
        ("marker-only transcript", {"last_screen": "› " + MARKER + "\n• Working\n› Ask Codex to do anything"}, False),
        ("old nonce in original before", {"before_screen": after}, False),
        ("changed payload digest", {"payload_sha256": digest("other")}, False),
        ("changed target identity", {"identity": dict(route, target_surface_uuid=WS_UUID)}, False),
        ("changed bound report digest", {"binding": dict(attempt["binding"], **{str(report_path): digest("stale")})}, False),
        ("no paste intent", {"paste_intent": False}, False),
        ("paste already consumed without Enter", {"enter_attempts": 0}, True),
        ("zero Enter without paste intent", {"enter_attempts": 0, "paste_intent": False}, False),
        ("zero Enter without complete transcript", {"enter_attempts": 0, "last_screen": "› " + MARKER + "\n• Working\n› Ask Codex to do anything"}, False),
        ("boolean Enter count", {"enter_attempts": True}, False),
        ("negative Enter count", {"enter_attempts": -1}, False),
        ("unknown phase", {"phase": "unknown"}, False),
        ("queued evidence", {"last_screen": "Messages to be submitted after next tool call\n" + callback}, False),
        ("edited protocol payload", {"last_screen": after.replace("DONE|t|", "DONE|wrong-task|")}, False),
    ]
    with patch.object(Path, "home", return_value=root):
        for name, changes, consumed in cases:
            attempt_path.write_text(json.dumps(dict(attempt, **changes)))
            rc, output, records, reads, route_calls = isolated_main(hook, SCREEN_IDLE, route=route, pack=pack)
            verdict = records[0]["surfaces"]["surface:42"] if records else None
            check(f"main attempt {name}", rc == 0 and (verdict == "CONSUMED") == consumed
                  and reads == 1 and route_calls >= 1, f"verdict={verdict} output={output}")
        attempt_path.write_text(json.dumps(attempt))
        for name, screen in [("live pending", SCREEN_PENDING_CODEX), ("live queued", SCREEN_QUEUED_CODEX)]:
            rc, output, records, _, route_calls = isolated_main(hook, screen, route=route, pack=pack)
            check(f"{name} takes priority over durable confirmed flag",
                  records[0]["surfaces"]["surface:42"] != "CONSUMED" and route_calls == 0, output)
        cli_hook = {"tool_name": "exec_command", "tool_input": {
            "cmd": "cmux-bridge-toolchain submit-completion-callback --task-pack '" + str(pack_path) + "'"}}
        for name, changes, consumed in [
            ("exact zero Enter", {"enter_attempts": 0}, True),
            ("missing paste intent", {"enter_attempts": 0, "paste_intent": False}, False),
            ("edited payload", {"last_screen": after.replace("DONE|t|", "DONE|wrong-task|")}, False),
        ]:
            attempt_path.write_text(json.dumps(dict(attempt, **changes)))
            rc, output, records, reads, route_calls = isolated_main(cli_hook, SCREEN_IDLE, route=route, pack=pack)
            verdict = records[0]["surfaces"]["surface:42"] if records else None
            check(f"main CLI callback {name}", rc == 0 and (verdict == "CONSUMED") == consumed
                  and reads == 1 and route_calls >= 1, f"verdict={verdict} output={output}")

# ---------------------------------------------------------------------------
print("\n" + "=" * 60)
print(f"PASS {len(PASSES)}   FAIL {len(FAILURES)}")
if FAILURES:
    print("\nFAILURES:")
    for item in FAILURES:
        print(f"  - {item}")
    sys.exit(1)
print("ALL SEMANTIC TESTS PASSED")
sys.exit(0)
