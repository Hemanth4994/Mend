"""Write orchestration/.env: the Kestra login and, optionally, your Gemini API key and the S3 archive settings, as Kestra secrets.

    python orchestration/make_env.py

Keys are read with getpass (not echoed, not in your shell history, not in the chat). The file is ignored by git.
Re-run it any time to change the login or the key; it asks before overwriting, and keeps a MEND_KESTRA_PORT line you added by hand.
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


BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")


PORT_LINE = re.compile(r"^MEND_KESTRA_PORT=(\d{1,5})[ \t]*$", re.MULTILINE)


def kept_port(old_env: str) -> str | None:
    """The MEND_KESTRA_PORT line someone added to an existing .env by hand (for when 8080 is taken), if it is a usable port."""
    found = PORT_LINE.search(old_env)
    return found.group(1) if found and 1 <= int(found.group(1)) <= 65535 else None


def render(user: str, password: str, gemini_key: str, archive: dict | None = None, port: str | None = None) -> str:
    """archive: {bucket, region, access_key_id, secret_access_key}, or None for no S3 archive. port: a MEND_KESTRA_PORT to keep."""
    a = archive or {}
    return (f"MEND_KESTRA_USER={user}\nMEND_KESTRA_PASSWORD={password}\n"
            f"SECRET_GOOGLE_API_KEY={encode_secret(gemini_key) if gemini_key else ''}\n"
            f"MEND_ARCHIVE_BUCKET={a.get('bucket', '')}\nMEND_ARCHIVE_REGION={a.get('region', '')}\n"
            f"SECRET_AWS_ACCESS_KEY_ID={encode_secret(a['access_key_id']) if a else ''}\n"
            f"SECRET_AWS_SECRET_ACCESS_KEY={encode_secret(a['secret_access_key']) if a else ''}\n"
            + (f"MEND_KESTRA_PORT={port}\n" if port else ""))


def ask_archive() -> dict | None:
    """The optional S3 archive. Returns None to skip it; raises ValueError on a value that cannot be right."""
    bucket = input("S3 bucket for the archive (Enter to skip; the name `terraform output bucket` printed): ").strip()
    if not bucket:
        return None
    if not BUCKET.match(bucket):
        raise ValueError("that is not a valid S3 bucket name")
    region = input("Its region (the one in your infra/terraform.tfvars, for example ap-southeast-2): ").strip()
    if not REGION.match(region):
        raise ValueError("that is not an AWS region name (for example ap-southeast-2)")
    key_id = getpass.getpass("Access key id of the mend-archive-writer user: ").strip()
    secret = getpass.getpass("Its secret access key: ").strip()
    if not key_id or not secret:
        raise ValueError("both the access key id and the secret access key are needed")
    return {"bucket": bucket, "region": region, "access_key_id": key_id, "secret_access_key": secret}


def main() -> int:
    port = None
    if ENV.exists():
        if input(f"{ENV} exists. Overwrite? [y/N] ").strip().lower() != "y":
            print("left it as it was")
            return 0
        port = kept_port(ENV.read_text(encoding="utf-8", errors="replace"))      # a port added by hand must survive the rewrite
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
    try:
        archive = ask_archive()
    except ValueError as exc:
        print(exc)
        return 1
    ENV.write_text(render(user, password, key, archive, port), encoding="utf-8")
    print(f"wrote {ENV}" + ("" if key else " (no Gemini key: the repair step will say so until you re-run this)")
          + ("" if archive else " (no S3 archive: the archive steps will do nothing)")
          + (f" (kept MEND_KESTRA_PORT={port})" if port else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
