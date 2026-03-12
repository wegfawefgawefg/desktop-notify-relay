#!/usr/bin/env python3

import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


RUNNING = True


@dataclass
class Notification:
    app_name: str
    summary: str
    body: str

    @property
    def combined(self) -> str:
        return "\n".join((self.app_name, self.summary, self.body))


class Relay:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.config = self._load_config(config_path)
        self.hostname = socket.gethostname()
        self.include_patterns = [
            re.compile(pattern)
            for pattern in self.config["filters"].get("include_any_regex", [])
        ]
        self.required_app_patterns = [
            re.compile(pattern)
            for pattern in self.config["filters"].get("require_app_name_regex", [])
        ]
        self.exclude_patterns = [
            re.compile(pattern)
            for pattern in self.config["filters"].get("exclude_any_regex", [])
        ]
        self.log_all = self.config["filters"].get("log_all_notifications", False)
        self.dedupe_seconds = int(self.config["filters"].get("dedupe_seconds", 20))
        self.token_env = self.config["ntfy"].get("token_env", "NTFY_TOKEN")
        self.ntfy_token = os.environ.get(self.token_env, "")
        self.ntfy_url = self._ntfy_url()
        self.title_prefix = self.config["ntfy"].get("title_prefix", self.hostname)
        self.seen: dict[str, float] = {}

    def _load_config(self, path: Path) -> dict:
        with path.open() as fh:
            return json.load(fh)

    def _ntfy_url(self) -> str:
        base = self.config["ntfy"]["base_url"].rstrip("/")
        topic = self.config["ntfy"]["topic"].strip("/")
        return f"{base}/{topic}"

    def matches(self, notification: Notification) -> bool:
        combined = notification.combined
        if any(pattern.search(combined) for pattern in self.exclude_patterns):
            return False
        if self.required_app_patterns and not any(
            pattern.search(notification.app_name) for pattern in self.required_app_patterns
        ):
            return False
        if not self.include_patterns:
            return True
        return any(pattern.search(combined) for pattern in self.include_patterns)

    def is_duplicate(self, notification: Notification) -> bool:
        key = "\n".join((notification.app_name, notification.summary, notification.body))
        now = time.time()
        cutoff = now - self.dedupe_seconds
        self.seen = {k: v for k, v in self.seen.items() if v >= cutoff}
        if key in self.seen:
            return True
        self.seen[key] = now
        return False

    def publish(self, notification: Notification) -> None:
        if not self.ntfy_token:
            print("missing ntfy token; skipping publish", file=sys.stderr, flush=True)
            return

        title = f"{self.title_prefix}: {notification.summary or notification.app_name}"
        message = notification.body.strip() or notification.summary.strip() or notification.app_name
        safe_title = title.encode("latin-1", "replace").decode("latin-1")
        request = urllib.request.Request(
            self.ntfy_url,
            data=message.encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.ntfy_token}",
                "Title": safe_title,
                "Tags": "computer,terminal",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                response.read()
        except urllib.error.URLError as exc:
            print(f"publish failed: {exc}", file=sys.stderr, flush=True)

    def handle(self, notification: Notification) -> None:
        if self.log_all:
            print(
                json.dumps(
                    {
                        "event": "seen",
                        "app_name": notification.app_name,
                        "summary": notification.summary,
                        "body": notification.body,
                    }
                ),
                flush=True,
            )

        if not self.matches(notification):
            return
        if self.is_duplicate(notification):
            return

        print(
            json.dumps(
                {
                    "event": "forward",
                    "app_name": notification.app_name,
                    "summary": notification.summary,
                    "body": notification.body,
                }
            ),
            flush=True,
        )
        self.publish(notification)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    return parser.parse_args()


def extract_string(line: str) -> str:
    match = re.search(r'^string "(.*)"$', line.strip())
    if not match:
        return ""
    return match.group(1).replace('\\"', '"')


def iter_notifications() -> Notification:
    command = [
        "dbus-monitor",
        "--session",
        "interface='org.freedesktop.Notifications',member='Notify'",
    ]
    while RUNNING:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert process.stdout is not None

        capture = False
        arg_index = 0
        app_name = ""
        summary = ""
        body = ""
        skipping_container = False
        depth = 0

        for raw_line in process.stdout:
            line = raw_line.rstrip("\n")

            if "member=Notify" in line:
                capture = True
                arg_index = 0
                app_name = ""
                summary = ""
                body = ""
                skipping_container = False
                depth = 0
                continue

            if not capture:
                continue

            stripped = line.strip()
            if not stripped:
                continue

            if skipping_container:
                depth += stripped.count("[") + stripped.count("{")
                depth -= stripped.count("]") + stripped.count("}")
                if depth <= 0:
                    skipping_container = False
                continue

            if stripped.startswith("string "):
                value = extract_string(line)
                if arg_index == 0:
                    app_name = value
                elif arg_index == 3:
                    summary = value
                elif arg_index == 4:
                    body = value
                arg_index += 1
                continue

            if stripped.startswith(("uint32 ", "int32 ")):
                if stripped.startswith("int32 ") and arg_index >= 7:
                    yield Notification(app_name=app_name, summary=summary, body=body)
                    capture = False
                arg_index += 1
                continue

            if stripped.startswith(("array [", "dict entry(")):
                arg_index += 1
                skipping_container = True
                depth = stripped.count("[") + stripped.count("{")
                depth -= stripped.count("]") + stripped.count("}")
                if depth <= 0:
                    skipping_container = False
                continue

        process.wait(timeout=5)
        if RUNNING:
            time.sleep(1)


def handle_signal(_signum, _frame) -> None:
    global RUNNING
    RUNNING = False


def main() -> int:
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    args = parse_args()
    relay = Relay(Path(args.config).expanduser())
    for notification in iter_notifications():
        relay.handle(notification)
        if not RUNNING:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
