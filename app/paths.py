import os
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def repo_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return repo_root() / path


def project_dir(name: str) -> Path:
    env_key = {"fixtures": "FIXTURES_DIR", "static": "STATIC_DIR"}[name]
    override = os.environ.get(env_key)
    if override:
        return Path(override)
    beside_repo = repo_root() / name
    if beside_repo.exists():
        return beside_repo
    return Path.cwd() / name
