"""Build the standalone executable and the Claude Desktop extension (.mcpb) for the platform this runs on.

    python packaging/build.py                  # -> dist/sentry-mcp-<version>-<target>[.exe] and .mcpb
    python packaging/build.py --print-binary   # path of the built executable (for tests)
"""

import json
import platform
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"

# macOS: an unsigned binary unpacked from a zip may lose its executable bit and carry Gatekeeper's quarantine flag.
# Claude Desktop starts this wrapper with /bin/sh (no executable bit needed), which fixes both before exec.
RUN_SH = """#!/bin/sh
here="$(cd "$(dirname "$0")" && pwd)"
chmod +x "$here/sentry-mcp" 2>/dev/null
xattr -d com.apple.quarantine "$here/sentry-mcp" 2>/dev/null
exec "$here/sentry-mcp" "$@"
"""


def version() -> str:
    sys.path.insert(0, str(ROOT / "src"))
    from sentry_mcp import __version__

    return __version__


def target() -> str:
    system = {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}[platform.system()]
    machine = platform.machine().lower()
    arch = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64"}.get(machine, machine)
    return f"{system}-{arch}"


def is_windows() -> bool:
    return platform.system() == "Windows"


def binary_path() -> Path:
    suffix = ".exe" if is_windows() else ""
    return DIST / f"sentry-mcp-{version()}-{target()}{suffix}"


def build_binary() -> Path:
    subprocess.run(
        [
            sys.executable, "-m", "PyInstaller",
            "--noconfirm", "--clean", "--onefile",
            "--name", "sentry-mcp",
            "--distpath", str(BUILD / "pyinstaller-dist"),
            "--workpath", str(BUILD / "pyinstaller-work"),
            "--specpath", str(BUILD),
            # Loaded dynamically at runtime, so PyInstaller cannot see them by import analysis alone.
            "--collect-submodules", "anyio",
            "--collect-submodules", "keyring.backends",
            "--copy-metadata", "keyring",
            "--collect-data", "certifi",
            str(ROOT / "packaging" / "entry.py"),
        ],
        check=True,
    )
    built = BUILD / "pyinstaller-dist" / ("sentry-mcp.exe" if is_windows() else "sentry-mcp")
    DIST.mkdir(exist_ok=True)
    out = binary_path()
    shutil.copy2(built, out)
    out.chmod(out.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return out


def build_bundle(binary: Path) -> Path:
    manifest = json.loads((ROOT / "packaging" / "manifest.json").read_text(encoding="utf-8"))
    manifest["version"] = version()
    config = manifest["server"]["mcp_config"]
    files: dict[str, tuple[bytes, int]] = {}

    if is_windows():
        manifest["server"]["entry_point"] = "server/sentry-mcp.exe"
        config["command"] = "${__dirname}/server/sentry-mcp.exe"
        manifest["compatibility"]["platforms"] = ["win32"]
        files["server/sentry-mcp.exe"] = (binary.read_bytes(), 0o755)
    else:
        manifest["compatibility"]["platforms"] = ["darwin"] if platform.system() == "Darwin" else ["linux"]
        config["command"] = "/bin/sh"
        config["args"] = ["${__dirname}/server/run.sh", *config["args"]]
        files["server/sentry-mcp"] = (binary.read_bytes(), 0o755)
        files["server/run.sh"] = (RUN_SH.encode(), 0o755)

    files["manifest.json"] = (json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8"), 0o644)

    out = DIST / f"sentry-mcp-{version()}-{target()}.mcpb"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, (data, mode) in files.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = (stat.S_IFREG | mode) << 16  # keep the executable bit for macOS/Linux
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(info, data)
    return out


def main() -> None:
    if "--print-binary" in sys.argv:
        print(binary_path())
        return
    binary = build_binary()
    bundle = build_bundle(binary)
    print(f"built {binary.name} ({binary.stat().st_size // 1024} KB)")
    print(f"built {bundle.name} ({bundle.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
