from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

SK_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")
ASSIGNED_KEY_PATTERN = re.compile(
    r"^\s*(?:export\s+)?(?P<name>API_KEY|DEEPSEEK_API_KEY)"
    r"\s*=\s*(?P<value>.+?)\s*$"
)


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        check=True,
        capture_output=True,
    )
    return [
        Path(raw.decode("utf-8"))
        for raw in result.stdout.split(b"\0")
        if raw
    ]


def main() -> int:
    failures: list[str] = []
    for path in tracked_files():
        if path.name == ".env" or (
            path.name.startswith(".env.") and path.name != ".env.example"
        ):
            failures.append(f"{path}: environment file is tracked")
            continue
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        for line_number, line in enumerate(content.splitlines(), 1):
            if SK_PATTERN.search(line):
                failures.append(f"{path}:{line_number}: possible API key")
            match = ASSIGNED_KEY_PATTERN.match(line)
            if match:
                value = match.group("value").strip().strip("'\"")
                if value and path.name != ".env.example":
                    failures.append(
                        f"{path}:{line_number}: "
                        f"{match.group('name')} has a value"
                    )

    if failures:
        print("Potential secrets found:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    print("No tracked environment files or obvious API keys found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
