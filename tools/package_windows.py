"""Package only the generated application folder, never local interview data."""
from hashlib import sha256
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED


def main():
    root = Path(__file__).resolve().parents[1]
    folder = root / "dist/interview-agent"
    if not (folder / "interview-agent.exe").is_file() or not (folder / "_internal").is_dir():
        raise SystemExit("Build the application folder first.")
    (folder / "使用说明.md").write_bytes((root / "docs/windows-exe.md").read_bytes())
    target = root / "dist/interview-agent-windows-x64.zip"
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
