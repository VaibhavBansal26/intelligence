"""Export the OpenAPI document to docs/openapi.json (deterministic) or check it is fresh.

uv run python -m app.tools.export_openapi          # write
uv run python -m app.tools.export_openapi --check  # exit 1 if stale
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from app.config import Settings
from app.main import create_app

OUT = Path(__file__).resolve().parents[2] / "docs" / "openapi.json"


def render() -> str:
    tmp = tempfile.mkdtemp()
    app = create_app(
        Settings(env="test", database_url=f"sqlite:///{tmp}/x.db", model_dir=tmp, admin_api_key="x", log_level="WARNING"),
        use_env_llm=False,
    )
    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str]) -> int:
    text = render()
    if "--check" in argv:
        if not OUT.exists() or OUT.read_text() != text:
            print("docs/openapi.json is stale: run `uv run python -m app.tools.export_openapi`")
            return 1
        return 0
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(text)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
