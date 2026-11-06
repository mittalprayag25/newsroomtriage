import os
from pathlib import Path


def project_dir(name: str) -> Path:
    env_key = {"fixtures": "FIXTURES_DIR", "static": "STATIC_DIR"}[name]
    override = os.environ.get(env_key)
    if override:
        return Path(override)
    beside_repo = Path(__file__).resolve().parents[1] / name
    if beside_repo.exists():
        return beside_repo
    return Path.cwd() / name
