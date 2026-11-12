"""Write orchestration/.env: the Kestra login and, optionally, your Gemini API key as a Kestra secret.

    python orchestration/make_env.py

The key is read with getpass (not echoed, not in your shell history, not in the chat). The file is ignored by git.
Re-run it any time to change the login or the key; it asks before overwriting.
"""
from __future__ import annotations

import base64
import getpass
import re
import sys
from pathlib import Path

ENV = Path(__file__).resolve().parent / ".env"
EMAIL = re.compile(r"^[^@\s\"$\\]+@[^@\s\"$\\]+\.[^@\s\"$\\]+$")
FORBIDDEN = set('"$\\\n\r')          # the value is embedded in Kestra's YAML configuration and in compose interpolation


def check_password(pw: str) -> str | None:
    if len(pw) < 8 or not re.search(r"[A-Z]", pw) or not re.search(r"\d", pw):
        return "at least 8 characters, with an uppercase letter and a number (Kestra's rule)"
    if FORBIDDEN & set(pw):
        return 'it must not contain " $ or \\ (they would break the generated configuration)'
    return None


def encode_secret(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def render(user: str, password: str, gemini_key: str) -> str:
    return (f"MEND_KESTRA_USER={user}\nMEND_KESTRA_PASSWORD={password}\n"
            f"SECRET_GOOGLE_API_KEY={encode_secret(gemini_key) if gemini_key else ''}\n")


def main() -> int:
    if ENV.exists() and input(f"{ENV} exists. Overwrite? [y/N] ").strip().lower() != "y":
        print("left it as it was")
        return 0
    user = input("Kestra login (an email address, local only): ").strip()
    if not EMAIL.match(user):
        print("that is not an email address Kestra will accept")
        return 1
    password = getpass.getpass("Kestra password: ")
    problem = check_password(password)
    if problem:
        print(problem)
        return 1
    key = getpass.getpass("Gemini API key (Enter to skip; needed only for the repair step): ").strip()
    ENV.write_text(render(user, password, key), encoding="utf-8")
    print(f"wrote {ENV}" + ("" if key else " (no Gemini key: the repair step will say so until you re-run this)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
