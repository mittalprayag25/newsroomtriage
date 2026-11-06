import argparse
import json
import sys
from pathlib import Path

from app.models import InboundEmail
from app.paths import project_dir
from app.settings import Settings
from app.triage import run_triage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print triage JSON for a newsroom email.")
    parser.add_argument("path", nargs="?", help="Path to an email JSON file.")
    parser.add_argument("--all", action="store_true", help="Triage every file in the fixtures directory.")
    args = parser.parse_args(argv)
    settings = Settings.from_env()

    if args.all:
        paths = sorted(project_dir("fixtures").glob("*.json"))
        if not paths:
            print("No fixtures found.", file=sys.stderr)
            return 1
    elif args.path:
        paths = [Path(args.path)]
    else:
        parser.print_help(sys.stderr)
        return 2

    for path in paths:
        email = InboundEmail.model_validate(json.loads(path.read_text(encoding="utf-8")))
        result = run_triage(email, settings)
        print(json.dumps(result.model_dump(by_alias=True), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
