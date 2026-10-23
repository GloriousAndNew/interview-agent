"""Package only the generated application folder, never local interview data."""
from hashlib import sha256
from pathlib import Path
from runpy import run_path
from zipfile import ZipFile, ZIP_DEFLATED
import sys
import shutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from interview_agent.update_bundle import write_manifest, verify_folder


def main():
    root = Path(__file__).resolve().parents[1]
    folder = root / "dist/interview-agent"
    if not (folder / "interview-agent.exe").is_file() or not (folder / "_internal").is_dir():
        raise SystemExit("Build the application folder first.")
    (folder / "使用说明.md").write_bytes((root / "docs/windows-exe.md").read_bytes())
    (folder / "automatic-updates.md").write_bytes((root / "docs/automatic-updates.md").read_bytes())
    (folder / "personal-information.md").write_bytes((root / "docs/personal-information.md").read_bytes())
    version = run_path(str(root / "interview_agent/__init__.py"))["__version__"]
    shutil.copy2(root / "dist/interview-agent-updater.exe", folder / "interview-agent-updater.exe")
    write_manifest(folder, version)
    verify_folder(folder, version)
    target = root / f"dist/interview-agent-v{version}-windows-x64.zip"
    partial = target.with_suffix(".zip.tmp")
    with ZipFile(partial, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
        for file in sorted(folder.rglob("*")):
            if file.is_file():
                archive.write(file, file.relative_to(root / "dist"))
    with ZipFile(partial) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("Archive integrity check failed.")
    partial.replace(target)
    digest = sha256()
    with target.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    target.with_suffix(".zip.sha256").write_text(digest.hexdigest() + "  " + target.name + "\n", encoding="ascii")
    print(f"package={target.name}; size_mb={target.stat().st_size / 1024 ** 2:.1f}; sha256={digest.hexdigest()}")


if __name__ == "__main__":
    main()
