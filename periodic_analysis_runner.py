#!/usr/bin/env python3
"""Periodic runner extension that adds the rotating 1m history collector."""

from __future__ import annotations

import periodic_analysis_runner_base as base

ONE_MIN_HISTORY_LOG = base.DATA_DIR / "diamond_1m_history_runner.log"

_ORIGINAL_TASK_COMMANDS = base.task_commands
_ORIGINAL_RUN_TASK = base.run_task
_ORIGINAL_SELF_TEST = base.self_test


def task_commands():
    """Keep every existing task unchanged and insert one research-only task."""
    original = _ORIGINAL_TASK_COMMANDS()
    commands = {}

    for name, command in original.items():
        commands[name] = command
        if name == "list4_deep_scan":
            commands["one_min_history"] = [
                base.sys.executable,
                "diamond_1m_history_collector.py",
                "--batch",
                "20",
            ]

    return commands


def run_task(state, name, command, log_file):
    """Run the original task and collect 1m history once per 15m cycle."""
    exit_code = _ORIGINAL_RUN_TASK(
        state,
        name,
        command,
        log_file,
    )

    if name == "event_outcome" and not base.STOP_REQUESTED:
        _ORIGINAL_RUN_TASK(
            state,
            "one_min_history",
            task_commands()["one_min_history"],
            ONE_MIN_HISTORY_LOG,
        )

    return exit_code


def self_test():
    """Run all original tests unchanged, then test the extension."""
    patched_task_commands = base.task_commands
    patched_run_task = base.run_task

    try:
        base.task_commands = _ORIGINAL_TASK_COMMANDS
        base.run_task = _ORIGINAL_RUN_TASK
        _ORIGINAL_SELF_TEST()
    finally:
        base.task_commands = patched_task_commands
        base.run_task = patched_run_task

    state = base.default_state()
    command = state["tasks"]["one_min_history"]["command"]

    assert command[-3:] == [
        "diamond_1m_history_collector.py",
        "--batch",
        "20",
    ]
    assert ONE_MIN_HISTORY_LOG.name == "diamond_1m_history_runner.log"

    print("ONE_MIN_HISTORY_EXTENSION_SELF_TEST_OK")


base.task_commands = task_commands
base.run_task = run_task
base.self_test = self_test


if __name__ == "__main__":
    base.main()
