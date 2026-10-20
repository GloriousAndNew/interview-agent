# Windows x64 folder bundle: the entire output folder is the application.
from pathlib import Path
from runpy import run_path
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules, copy_metadata
from PyInstaller.utils.win32.versioninfo import VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable, StringStruct, VarFileInfo, VarStruct

root = Path(SPECPATH)
version = run_path(str(root / "interview_agent/__init__.py"))["__version__"]
version_tuple = tuple(int(part) for part in version.split(".")) + (0,)
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=version_tuple, prodvers=version_tuple, OS=0x40004, fileType=1),
    kids=[StringFileInfo([StringTable("040904B0", [
        StringStruct("FileDescription", "Interview Agent"),
        StringStruct("ProductName", "Interview Agent"),
        StringStruct("FileVersion", version),
        StringStruct("ProductVersion", version),
        StringStruct("OriginalFilename", "interview-agent.exe"),
    ])]), VarFileInfo([VarStruct("Translation", [1033, 1200])])],
)
ff_data, ff_binary, ff_imports = collect_all("imageio_ffmpeg")
datas = collect_data_files("streamlit") + copy_metadata("streamlit", recursive=True) + ff_data
datas += [(str(root / "app.py"), "."), (str(root / "interview_agent/ui.css"), "interview_agent")]
hidden = collect_submodules("interview_agent")
hidden += collect_submodules("streamlit", filter=lambda name: not name.startswith(("streamlit.testing", "streamlit.hello")))
hidden += collect_submodules("aliyunsdkcore") + collect_submodules("aliyunsdkkms")
hidden += ff_imports + ["worker", "tkinter", "tkinter.messagebox"]
a = Analysis([str(root / "desktop.py")], pathex=[str(root)], binaries=ff_binary, datas=datas,
             hiddenimports=hidden, excludes=["pytest", "IPython", "matplotlib", "scipy", "streamlit.testing"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="interview-agent", debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False, version=version_info)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="interview-agent")
