from pathlib import Path
from PyInstaller.utils.win32.versioninfo import VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable, StringStruct
from runpy import run_path

root = Path(SPECPATH)
version = run_path(str(root / "interview_agent/__init__.py"))["__version__"]
numbers = tuple(map(int, version.split("."))) + (0,)
metadata = VSVersionInfo(ffi=FixedFileInfo(filevers=numbers, prodvers=numbers, OS=0x40004, fileType=1),
                       kids=[StringFileInfo([StringTable("040904B0", [StringStruct("FileVersion", version), StringStruct("ProductVersion", version), StringStruct("ProductName", "Interview Agent Updater")])])])
a = Analysis([str(root / "updater.py")], pathex=[str(root)], binaries=[], datas=[], hiddenimports=[],
             excludes=["streamlit", "httpx", "oss2", "pydantic", "pytest", "tkinter"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="interview-agent-updater", console=False, version=metadata)
