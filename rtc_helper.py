#!/usr/bin/python3
# HINT managed RTC helper
"""Restricted privileged RTC operation used by HINT."""

from __future__ import annotations

import subprocess
import sys

RTCWAKE = "/usr/sbin/rtcwake"
RTC_TIMEOUT = 2


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: hint-rtc disable|SECONDS", file=sys.stderr)
        return 2
    value = argv[0]
    seconds = None
    # Reject bad input before touching the existing hardware alarm.
    if value != "disable":
        try:
            seconds = int(value)
        except (TypeError, ValueError):
            print("wake delay must be whole seconds", file=sys.stderr)
            return 2
        if seconds < 5:
            print("wake delay must be at least 5 seconds", file=sys.stderr)
            return 2
    try:
        result = subprocess.run([RTCWAKE, "-m", "disable"], timeout=RTC_TIMEOUT)
        if result.returncode or seconds is None:
            return result.returncode
        result = subprocess.run(
            [RTCWAKE, "-a", "-m", "no", "-s", str(seconds)], timeout=RTC_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        print("hint-rtc: hardware wake operation timed out", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"hint-rtc: could not run rtcwake: {exc}", file=sys.stderr)
        return 1
    if result.returncode:
        print(f"hint-rtc: failed to arm wake in {seconds} seconds", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
