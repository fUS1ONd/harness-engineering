"""Детерминированный разбор Android logcat на предмет крашей (Java/Native) и ANR."""

from __future__ import annotations

import re

from .core import LogAnomaly

_JAVA_CRASH_START = re.compile(r"AndroidRuntime:\s+FATAL EXCEPTION:")
_JAVA_CRASH_CONT = re.compile(r"AndroidRuntime:\s+")
_NATIVE_SIGNAL = re.compile(r"\bFatal signal\s+\d+\b")
_TOMBSTONE_BANNER = re.compile(r"DEBUG\s*:\s+\*\*\* \*\*\* \*\*\*")
_TOMBSTONE_LINE = re.compile(r"DEBUG\s*:")
_ANR_START = re.compile(r"ActivityManager:\s+ANR in\s+")
_ANR_CONT = re.compile(r"ActivityManager:\s+(?:PID:|Reason:|Parent:|Load:|CPU usage)")


def _matches_package(lines: list[str], package: str | None) -> bool:
    if not package:
        return True
    blob = "\n".join(lines)
    return package in blob


def analyze_logcat(
    raw_text: str,
    *,
    package: str | None = None,
    max_block_lines: int = 40,
) -> tuple[list[LogAnomaly], list[LogAnomaly]]:
    """Извлекает блоки Java/Native crash и ANR из вывода `adb logcat -d -v threadtime`."""
    lines = raw_text.splitlines()
    crashes: list[LogAnomaly] = []
    anrs: list[LogAnomaly] = []

    i = 0
    total = len(lines)
    while i < total:
        line = lines[i]

        if _JAVA_CRASH_START.search(line):
            block = [line]
            i += 1
            while i < total and len(block) < max_block_lines and _JAVA_CRASH_CONT.search(lines[i]):
                if _JAVA_CRASH_START.search(lines[i]):
                    break
                block.append(lines[i])
                i += 1
            if _matches_package(block, package):
                summary = line.split("AndroidRuntime:", 1)[-1].strip()
                crashes.append(LogAnomaly(kind="crash", summary=summary, lines=block))
            continue

        if _NATIVE_SIGNAL.search(line):
            block = [line]
            i += 1
            while i < total and len(block) < max_block_lines:
                nxt = lines[i]
                if _TOMBSTONE_BANNER.search(nxt) or _TOMBSTONE_LINE.search(nxt):
                    block.append(nxt)
                    i += 1
                else:
                    break
            if _matches_package(block, package):
                m = _NATIVE_SIGNAL.search(line)
                summary = line[m.start() :].strip() if m else line.strip()
                crashes.append(LogAnomaly(kind="crash", summary=summary, lines=block))
            continue

        if _TOMBSTONE_BANNER.search(line):
            block = [line]
            i += 1
            while i < total and len(block) < max_block_lines and _TOMBSTONE_LINE.search(lines[i]):
                if _TOMBSTONE_BANNER.search(lines[i]):
                    break
                block.append(lines[i])
                i += 1
            if _matches_package(block, package):
                crashes.append(
                    LogAnomaly(kind="crash", summary="Native crash (DEBUG tombstone)", lines=block)
                )
            continue

        if _ANR_START.search(line):
            block = [line]
            i += 1
            while i < total and len(block) < max_block_lines and _ANR_CONT.search(lines[i]):
                block.append(lines[i])
                i += 1
            if _matches_package(block, package):
                summary = line.split("ActivityManager:", 1)[-1].strip()
                anrs.append(LogAnomaly(kind="anr", summary=summary, lines=block))
            continue

        i += 1

    return crashes, anrs
