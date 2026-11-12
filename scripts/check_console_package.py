"""Fail packaging checks when a release wheel omits the built console."""

from pathlib import Path
from zipfile import ZipFile


def main() -> None:
    wheels = sorted(Path("dist").glob("max_agent-*.whl"))
    if not wheels:
        raise SystemExit("No MAX wheel found. Build the frontend, then run uv build.")
    for wheel in wheels:
        with ZipFile(wheel) as archive:
            names = archive.namelist()
            if "max_agent/web/dist/index.html" not in names or not any(
                name.startswith("max_agent/web/dist/assets/") and name.endswith(".js")
                for name in names
            ):
                raise SystemExit(f"Console assets missing from {wheel.name}")
        print(f"Console assets verified: {wheel.name}")


if __name__ == "__main__":
    main()
