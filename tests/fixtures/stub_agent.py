#!/usr/bin/env python3
"""Small commandcode-shaped agent used by the disposable live E2E."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-file", type=Path, required=True)
    args = parser.parse_args()
    args.session_file.parent.mkdir(parents=True, exist_ok=True)
    args.session_file.write_text(
        json.dumps({"type": "session", "cwd": str(Path.cwd())}) + "\n",
        encoding="utf-8",
    )
    print("STUB_PROMPT>", flush=True)
    for line in iter(input, ""):
        prompt = line.rstrip("\r\n")
        with args.session_file.open("a", encoding="utf-8") as session:
            session.write(json.dumps({"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}}) + "\n")
            result = f"RESULT::{prompt}"
            session.write(json.dumps({"type": "message", "message": {"role": "assistant", "content": [{"type": "text", "text": result}]}}) + "\n")
        print(result, flush=True)
        print("STUB_PROMPT>", flush=True)


if __name__ == "__main__":
    main()
