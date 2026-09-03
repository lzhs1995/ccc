#!/usr/bin/env python3
"""Read-only Claude Code render-grid sampler.

This program never writes the supervisor config and never sends terminal input.
It freezes the Claude surfaces present at startup, samples only viewport
``row_spans`` through ``terminal.replay``, and stores structural booleans and
hashes rather than terminal text.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import re
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import cmux_codex_watch as core


ACTIVE_SPINNER_RE = re.compile(
    r"^\s*\S{1,3}\s+.+(?:…|\.\.\.)\s*\([^)]*(?:\d+\s*[hms]|↓|tokens?)[^)]*\)\s*$",
    re.IGNORECASE,
)
ACTIVE_TOOL_RE = re.compile(r"^\s*◐\s+\S.*(?:…|\.\.\.)\s*(?:\([^)]*\))?\s*$", re.IGNORECASE)
PROGRESS_RE = re.compile(r"^\s*▸\s*\(\s*\d+\s*/\s*\d+\s*\)", re.IGNORECASE)
COMPLETED_RE = re.compile(r"^\s*✻\s+.+\s+for\s+\d+(?:\.\d+)?\s*[hms]\b", re.IGNORECASE)
QUESTION_RE = re.compile(
    r"(?:do you want to|would you like to|permission required|press enter to confirm)",
    re.IGNORECASE,
)
ASK_FOOTER_RE = re.compile(r"^\s*✓\s*AskUserQuestion(?:\s*[×x]\s*\d+)?\s*$", re.IGNORECASE)
NEW_TASK_RE = re.compile(r"(?:new task\?|/clear to save)", re.IGNORECASE)


@dataclasses.dataclass(frozen=True)
class ClaudeFrame:
    state: str
    signature: str
    composer: str
    active_spinner: bool
    active_tool: bool
    progress: bool
    waiting_user: bool
    completed_marker: bool
    new_task_hint: bool
    cursor_visible: bool
    prompt_row_distance: int | None
    evidence_hashes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _spans_on_row(grid: core.Grid, row: int) -> list[core.Span]:
    return sorted((span for span in grid.spans if span.row == row), key=lambda span: span.column)


def _faint(grid: core.Grid, span: core.Span) -> bool:
    return bool(grid.style(span.style_id).get("faint", False))


def _composer_state(grid: core.Grid) -> tuple[str, int | None]:
    """Return empty/busy/unverified using the visible cursor and nearest ❯ row."""

    candidates: list[tuple[int, int]] = []
    # Claude's cmux render currently reports cursor.visible=false even while
    # its input row is present. For read-only sampling, the bottom prompt row
    # plus its surrounding separators is the safer anchor; this does not relax
    # Codex's send gate.
    # The row/column remain accurate even when Claude marks the cursor hidden.
    anchor_row = grid.cursor.row
    for row in range(max(0, anchor_row - 6), min(grid.rows, anchor_row + 2)):
        text = grid.lines[row]
        prompt_column = text.find("❯")
        if prompt_column >= 0 and not text[:prompt_column].strip():
            candidates.append((abs(row - grid.cursor.row), row))
    if not candidates:
        return "unverified", None
    distance, prompt_row = min(candidates)
    if distance > 1 and grid.cursor.visible:
        return "unverified", distance

    prompt_end = None
    for span in _spans_on_row(grid, prompt_row):
        offset = span.text.find("❯")
        if offset >= 0:
            prompt_end = span.column + offset + 1
            break
    if prompt_end is None:
        return "unverified", distance
    for span in _spans_on_row(grid, prompt_row):
        if span.column + span.cell_width <= prompt_end:
            continue
        if span.column <= prompt_end <= span.column + span.cell_width:
            prompt_offset = max(0, prompt_end - span.column)
            remainder = span.text[prompt_offset:]
            if not remainder.replace("\u00a0", "").strip():
                continue
        if not span.text.strip():
            continue
        # Claude placeholders are faint; any real non-faint text after the
        # prompt means the user has typed something and must never be disturbed.
        if not _faint(grid, span):
            return "busy", distance
    return "empty", distance


def classify_claude_grid(grid: core.Grid) -> ClaudeFrame:
    composer, prompt_distance = _composer_state(grid)
    prompt_rows = [
        row for row, text in enumerate(grid.lines)
        if text.lstrip().startswith("❯") and not text[: text.find("❯")].strip()
    ]
    prompt_row = max(prompt_rows) if prompt_rows else (grid.cursor.row if prompt_distance is not None else grid.rows - 1)
    start = max(0, prompt_row - 14)
    end = min(grid.rows, prompt_row + 3)
    nearby = [(row, grid.lines[row]) for row in range(start, end) if grid.lines[row].strip()]

    active_spinner_lines = [text for _, text in nearby if ACTIVE_SPINNER_RE.search(text)]
    active_tool_lines = [text for _, text in nearby if ACTIVE_TOOL_RE.search(text)]
    progress_lines = [text for _, text in nearby if PROGRESS_RE.search(text)]
    completed_lines = [text for _, text in nearby if COMPLETED_RE.search(text)]
    question_lines = [
        text
        for _, text in nearby
        if QUESTION_RE.search(text) and not ASK_FOOTER_RE.search(text)
    ]
    new_task_lines = [text for _, text in nearby if NEW_TASK_RE.search(text)]

    waiting_user = bool(question_lines)
    active = bool(active_spinner_lines or active_tool_lines or progress_lines)
    if waiting_user:
        state = "waiting_user"
    elif active:
        state = "active"
    elif composer == "busy":
        state = "composer_busy"
    elif composer == "empty":
        # Deliberately not called "done": a completed Claude turn and a silent
        # stall are visually identical until sampling establishes a threshold.
        state = "idle_probeable"
    else:
        state = "incompatible"

    evidence = active_spinner_lines + active_tool_lines + progress_lines + question_lines + completed_lines
    return ClaudeFrame(
        state=state,
        signature=grid.signature(),
        composer=composer,
        active_spinner=bool(active_spinner_lines),
        active_tool=bool(active_tool_lines),
        progress=bool(progress_lines),
        waiting_user=waiting_user,
        completed_marker=bool(completed_lines),
        new_task_hint=bool(new_task_lines),
        cursor_visible=grid.cursor.visible,
        prompt_row_distance=prompt_distance,
        evidence_hashes=tuple(core._short_hash(value) for value in evidence),
    )


def discover_claude_targets(client: Any) -> list[dict[str, str]]:
    tree = client.tree()
    classified = core.classify_surface_processes(client.top_all())
    targets = []
    for record in core.main_surface_records(tree):
        if core.surface_process_label(classified, record)["agent_kind"] == "claude":
            targets.append(dict(record))
    return sorted(targets, key=lambda item: (item.get("workspace_ref", ""), item.get("ref", "")))


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


class SamplingSummary:
    def __init__(self, targets: Iterable[Mapping[str, str]], started_at: float) -> None:
        self.targets = list(targets)
        self.started_at = started_at
        self.frame_count = 0
        self.errors: Counter[str] = Counter()
        self.states: Counter[str] = Counter()
        self.state_durations: dict[str, list[float]] = defaultdict(list)
        self.change_intervals: list[float] = []
        self._last: dict[str, tuple[str, str, float]] = {}

    def add(self, surface_id: str, frame: ClaudeFrame, captured_at: float) -> None:
        self.frame_count += 1
        self.states[frame.state] += 1
        previous = self._last.get(surface_id)
        if previous is not None:
            old_signature, old_state, old_at = previous
            elapsed = max(0.0, captured_at - old_at)
            if frame.signature != old_signature:
                self.change_intervals.append(elapsed)
            if frame.signature != old_signature or frame.state != old_state:
                self.state_durations[old_state].append(elapsed)
                old_at = captured_at
            self._last[surface_id] = (frame.signature, frame.state, old_at)
        else:
            self._last[surface_id] = (frame.signature, frame.state, captured_at)

    def fail(self, kind: str) -> None:
        self.errors[kind] += 1

    def finish(self, ended_at: float) -> dict[str, Any]:
        for _, state, since in self._last.values():
            self.state_durations[state].append(max(0.0, ended_at - since))
        change = self.change_intervals
        return {
            "schema": "claude-readonly-sampling.v1",
            "started_at": self.started_at,
            "ended_at": ended_at,
            "duration_sec": max(0.0, ended_at - self.started_at),
            "target_count": len(self.targets),
            "frame_count": self.frame_count,
            "states": dict(self.states),
            "errors": dict(self.errors),
            "signature_change_interval_sec": {
                "count": len(change),
                "min": min(change) if change else None,
                "median": statistics.median(change) if change else None,
                "p95": _percentile(change, 0.95),
                "max": max(change) if change else None,
            },
            "stable_streak_sec_by_state": {
                state: {
                    "count": len(values),
                    "median": statistics.median(values) if values else None,
                    "p95": _percentile(values, 0.95),
                    "max": max(values) if values else None,
                }
                for state, values in sorted(self.state_durations.items())
            },
            "privacy": "no terminal text stored; only booleans, signatures, and short evidence hashes",
            "decision": "sampling only; no Claude send threshold selected",
        }


def summarize_frames(path: Path, metadata_path: Path | None = None) -> dict[str, Any]:
    """Summarize an existing JSONL capture without contacting cmux."""

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        raise ValueError(f"no frames in {path}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path and metadata_path.exists() else {}
    targets = metadata.get("targets", [{}] * len({row.get("surface_key") for row in rows}))
    started = float(metadata.get("started_at", rows[0].get("captured_at", time.time())))
    summary = SamplingSummary(targets, started)
    for row in rows:
        if row.get("state") == "sample_error":
            summary.fail(str(row.get("error_kind", "sample_error")))
            continue
        frame = ClaudeFrame(
            state=str(row.get("state", "incompatible")),
            signature=str(row.get("signature", "")),
            composer=str(row.get("composer", "unverified")),
            active_spinner=bool(row.get("active_spinner")),
            active_tool=bool(row.get("active_tool")),
            progress=bool(row.get("progress")),
            waiting_user=bool(row.get("waiting_user")),
            completed_marker=bool(row.get("completed_marker")),
            new_task_hint=bool(row.get("new_task_hint")),
            cursor_visible=bool(row.get("cursor_visible")),
            prompt_row_distance=row.get("prompt_row_distance"),
            evidence_hashes=tuple(row.get("evidence_hashes", ())),
        )
        summary.add(str(row.get("surface_key", "")), frame, float(row.get("captured_at", started)))
    ended = float(rows[-1].get("captured_at", started))
    result = summary.finish(ended)
    result["source_frames"] = str(path)
    result["privacy"] = "no terminal text stored; only booleans, signatures, and short evidence hashes"
    return result


def run_sampling(
    client: Any,
    output_dir: Path,
    *,
    duration_sec: float,
    interval_sec: float,
    targets: Sequence[Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    if interval_sec <= 0 or duration_sec < 0:
        raise ValueError("interval_sec must be positive and duration_sec cannot be negative")
    frozen_targets = list(targets) if targets is not None else discover_claude_targets(client)
    output_dir.mkdir(parents=True, exist_ok=True)
    started_at = time.time()
    summary = SamplingSummary(frozen_targets, started_at)
    metadata = {
        "schema": "claude-readonly-sampling.v1",
        "started_at": started_at,
        "duration_requested_sec": duration_sec,
        "interval_sec": interval_sec,
        "target_count": len(frozen_targets),
        "targets": [
            {
                "surface_key": core._short_hash(str(target["surface_id"])),
                "workspace_ref": target.get("workspace_ref", ""),
                "pane_ref": target.get("pane_ref", ""),
                "surface_ref": target.get("ref", ""),
            }
            for target in frozen_targets
        ],
        "read_only": True,
    }
    (output_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    deadline = time.monotonic() + duration_sec
    first = True
    with (output_dir / "frames.jsonl").open("w", encoding="utf-8") as handle:
        while first or time.monotonic() < deadline:
            first = False
            cycle_started = time.monotonic()
            captured_at = time.time()
            for target in frozen_targets:
                surface_id = str(target["surface_id"])
                base = {
                    "captured_at": captured_at,
                    "surface_key": core._short_hash(surface_id),
                    "workspace_ref": target.get("workspace_ref", ""),
                    "pane_ref": target.get("pane_ref", ""),
                    "surface_ref": target.get("ref", ""),
                }
                try:
                    payload = client.replay(str(target["workspace_id"]), surface_id)
                    frame = classify_claude_grid(core.Grid.from_rpc(payload, surface_id))
                    record = {**base, **frame.to_dict()}
                    summary.add(surface_id, frame, captured_at)
                except core.GlobalIncompatibleError:
                    summary.fail("global_incompatible")
                    raise
                except (core.CmuxError, RuntimeError, ValueError) as exc:
                    kind = type(exc).__name__
                    summary.fail(kind)
                    record = {**base, "state": "sample_error", "error_kind": kind}
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sleep_for = min(remaining, max(0.0, interval_sec - (time.monotonic() - cycle_started)))
            if sleep_for:
                time.sleep(sleep_for)

    result = summary.finish(time.time())
    (output_dir / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only Claude Code render-grid sampler")
    parser.add_argument("--duration-sec", type=float, default=1800.0)
    parser.add_argument("--interval-sec", type=float, default=5.0)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--config", type=Path, default=core.DEFAULT_CONFIG_PATH)
    parser.add_argument("--summarize-frames", type=Path, help="summarize an existing frames.jsonl without contacting cmux")
    parser.add_argument("--metadata", type=Path, help="metadata.json matching --summarize-frames")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.summarize_frames:
        result = summarize_frames(args.summarize_frames, args.metadata or args.summarize_frames.with_name("metadata.json"))
        output = args.summarize_frames.with_name("summary.json")
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"output": str(output), **result}, ensure_ascii=False, indent=2))
        return 0
    config = core.ConfigStore(args.config).load()
    client = core.CmuxClient(str(config.get("cmux_path", core.DEFAULT_CMUX)))
    client.capabilities()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    output_dir = args.output_dir or (core.PROJECT_DIR / "observations" / f"claude-{stamp}")
    result = run_sampling(
        client,
        output_dir,
        duration_sec=args.duration_sec,
        interval_sec=args.interval_sec,
    )
    print(json.dumps({"output_dir": str(output_dir), **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
