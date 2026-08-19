r"""
build.py
========
PyInstaller packaging script -- produces a portable folder at
``dist/OfflineSmartHUD/`` that runs on an air-gapped machine.

    py build.py                    # normal build -> dist/OfflineSmartHUD/
    py build.py --with-model       # also copy models/*.gguf into dist
    py build.py --model X.gguf     # copy one specific model file into dist
    py build.py --console          # keep a console window (debugging builds)
    py build.py --clean-only       # just wipe build/ and dist/
    py build.py --outdir D:\out    # build off a synced drive

Deliberate choices
------------------
* ``--onedir`` (not ``--onefile``): onefile unpacks ~200 MB of Qt to a temp
  directory on *every* launch, which is painfully slow and breaks the
  "relative ./models path" contract. onedir starts instantly.
* The GGUF model is **never** bundled into the binary. It stays in
  ``dist/OfflineSmartHUD/models/`` so it can be swapped without rebuilding,
  and so the executable stays a reasonable size.
* Unused Qt modules (WebEngine, 3D, Charts, ...) are excluded -- they account
  for most of PySide6's footprint.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn, Optional


def is_installed(module: str) -> bool:
    """True if ``module`` can be imported, without actually importing it."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False

HERE = Path(__file__).resolve().parent
APP_NAME = "OfflineSmartHUD"
ENTRY = HERE / "main.py"

# Output roots. ``--outdir`` re-points these, which is handy when the source
# lives on a synced drive (OneDrive/Dropbox) that you would rather not fill
# with a few hundred megabytes of build artefacts.
DIST = HERE / "dist"
BUILD = HERE / "build"
SPEC = HERE / f"{APP_NAME}.spec"


def set_outdir(root: Path) -> None:
    global DIST, BUILD
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    DIST = root / "dist"
    BUILD = root / "build"
    info(f"output root: {root}")

# Source files that must exist before we bother invoking PyInstaller.
REQUIRED_SOURCES = ("main.py", "ui_components.py", "llm_engine.py",
                    "db_manager.py", "scheduler_service.py",
                    "config.py", "crash_handler.py", "outlook_service.py")

# Qt modules the HUD never touches. Excluding them roughly halves the output.
EXCLUDED_MODULES = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel", "PySide6.QtWebSockets",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtQuick3D", "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtQuickWidgets",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtSerialPort", "PySide6.QtSensors", "PySide6.QtTest",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtSql", "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtUiTools",
    # Scientific / GUI stacks that sneak in through transitive imports.
    "tkinter", "matplotlib", "numpy.testing", "pytest", "IPython",
    "PyQt5", "PyQt6", "notebook", "scipy",
    # llama_cpp.llama_chat_format has *optional* imports for HF tokenizers and
    # multimodal handlers. Analysing them drags in the entire ML stack --
    # measured at 650 MB of torch/cv2/pyarrow/transformers that this app never
    # calls. None of it is needed to run a GGUF through llama.cpp.
    "torch", "torchvision", "torchaudio", "transformers", "tokenizers",
    "cv2", "pyarrow", "onnxruntime", "pandas", "PIL", "sklearn",
    "scikit-learn", "sympy", "networkx", "datasets", "huggingface_hub",
    "safetensors", "fastapi", "uvicorn", "starlette", "pydantic_settings",
]

# Imports PyInstaller's static analysis tends to miss.
HIDDEN_IMPORTS = [
    "apscheduler.schedulers.background",
    "apscheduler.triggers.interval",
    "apscheduler.executors.pool",
    "apscheduler.jobstores.memory",
    "tzlocal",
    "sqlite3",
    # Outlook COM. These are imported lazily inside outlook_service, so
    # PyInstaller's static analysis never sees them.
    "win32com",
    "win32com.client",
    "pythoncom",
    "pywintypes",
    "win32timezone",
]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def info(msg: str) -> None:
    print(f"[build] {msg}", flush=True)


def fail(msg: str) -> NoReturn:
    print(f"[build] ERROR: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def human_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def clean() -> None:
    """Remove previous build artefacts (keeps databases and logs).

    ``ignore_errors`` is not enough here: a half-deleted ``build/localpycs``
    left behind by an interrupted build makes the next PyInstaller run die with
    ``PermissionError [WinError 5]``. Retry briefly (an AV scanner or an
    Explorer window usually lets go within a second) and say so if it persists.
    """
    for target in (BUILD, DIST):
        if not target.exists():
            continue
        info(f"removing {target.name}/")
        for attempt in range(4):
            shutil.rmtree(target, ignore_errors=True)
            if not target.exists():
                break
            time.sleep(0.75)
        if target.exists():
            fail(f"could not remove {target}\n"
                 "        Close any Explorer window or antivirus scan holding it,\n"
                 "        or build elsewhere with:  py build.py --outdir D:\\out")
    if SPEC.exists():
        try:
            SPEC.unlink()
        except OSError as exc:
            info(f"could not remove spec ({exc}); continuing")


def check_dependencies() -> None:
    missing = [
        package
        for module, package in (
            ("PySide6", "PySide6"),
            ("apscheduler", "APScheduler"),
            ("PyInstaller", "pyinstaller"),
        )
        if not is_installed(module)
    ]
    if missing:
        fail("missing packages: " + ", ".join(missing)
             + f"\n        install with:  {Path(sys.executable).name} -m pip install "
             + " ".join(missing))

    if is_installed("win32com"):
        info("pywin32 found -> Outlook awaited-email watcher will work")
    else:
        info("NOTE: pywin32 missing; the build runs but awaited-email "
             "reminders stay disabled (pip install pywin32)")

    if is_installed("llama_cpp"):
        info("llama-cpp-python found -> chat will work in the build")
    else:
        info("WARNING: llama-cpp-python not installed. The build will run, but the "
             "AI chat tab stays disabled. Install it before building for release:")
        info("         pip install llama-cpp-python "
             "--extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu")


def generate_icon() -> Path | None:
    """Render ``assets/app.ico`` from the in-code icon painter (no art assets)."""
    icon_path = HERE / "assets" / "app.ico"
    icon_path.parent.mkdir(parents=True, exist_ok=True)
    if icon_path.exists():
        return icon_path
    try:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtGui import QGuiApplication  # noqa: PLC0415

        app = QGuiApplication.instance() or QGuiApplication([])
        from ui_components import make_app_icon  # noqa: PLC0415

        # 256px so Windows Explorer has a crisp large icon.
        pixmap = make_app_icon(256).pixmap(256, 256)
        if pixmap.save(str(icon_path), "ICO"):
            info(f"generated {icon_path.relative_to(HERE)}")
            return icon_path
        del app
    except Exception as exc:                       # noqa: BLE001
        info(f"icon generation skipped ({exc})")
    return icon_path if icon_path.exists() else None


def generate_sound() -> Path | None:
    """Pre-generate ``assets/notify.wav`` so the packaged app never writes to
    its own (possibly read-only) install directory on first alarm."""
    wav = HERE / "assets" / "notify.wav"
    if wav.exists():
        return wav
    try:
        from ui_components import NotificationSound  # noqa: PLC0415

        wav.parent.mkdir(parents=True, exist_ok=True)
        NotificationSound._write_chime(str(wav))
        info(f"generated {wav.relative_to(HERE)}")
        return wav
    except Exception as exc:                       # noqa: BLE001
        info(f"chime generation skipped ({exc})")
        return None


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #

def build(console: bool = False, with_model: bool = False,
          model_file: Optional[Path] = None) -> Path:
    for name in REQUIRED_SOURCES:
        if not (HERE / name).exists():
            fail(f"source file missing: {name}")

    check_dependencies()
    icon = generate_icon()
    sound = generate_sound()

    args: list[str] = [
        sys.executable, "-m", "PyInstaller",
        str(ENTRY),
        "--name", APP_NAME,
        "--onedir",                       # portable folder, instant startup
        "--noconfirm",
        "--clean",
        "--noupx",                        # UPX corrupts some Qt DLLs
        "--distpath", str(DIST),
        "--workpath", str(BUILD),
        "--specpath", str(HERE),
    ]
    args += ["--console"] if console else ["--windowed"]
    if icon:
        args += ["--icon", str(icon)]
    if sound:
        # ``assets`` is read at runtime through a path relative to the exe.
        args += ["--add-data", f"{sound}{os.pathsep}assets"]

    for module in HIDDEN_IMPORTS:
        args += ["--hidden-import", module]
    for module in EXCLUDED_MODULES:
        args += ["--exclude-module", module]

    # llama-cpp ships a native DLL plus data files. Collect those explicitly
    # rather than with --collect-all: collecting every submodule also analyses
    # llama_cpp.server and the optional HF chat handlers, which pulls in
    # torch/transformers/cv2 and adds ~650 MB of dead weight.
    if is_installed("llama_cpp"):
        args += ["--collect-binaries", "llama_cpp",
                 "--collect-data", "llama_cpp",
                 "--hidden-import", "llama_cpp",
                 "--hidden-import", "llama_cpp.llama_chat_format",
                 "--hidden-import", "diskcache"]

    info("running PyInstaller… (this takes a few minutes)")
    info(" ".join(f'"{a}"' if " " in a else a for a in args[3:]))
    started = time.time()
    result = subprocess.run(args, cwd=str(HERE))
    if result.returncode != 0:
        fail(f"PyInstaller exited with code {result.returncode}")

    out_dir = DIST / APP_NAME
    if not out_dir.exists():
        fail(f"expected output folder not found: {out_dir}")

    finalise(out_dir, with_model=with_model, model_file=model_file)
    info(f"done in {time.time() - started:.0f}s -> {out_dir}  ({human_size(dir_size(out_dir))})")
    return out_dir


#: Shipped inside every build. The app keeps its databases next to the
#: executable (deliberately -- portable, air-gap friendly, one folder to copy),
#: which makes "replace the folder" the obvious upgrade and also the one that
#: destroys the user's schedules. This script makes the safe path the easy one.
UPGRADE_PS1 = r"""# OfflineSmartHUD 업그레이드 (데이터 보존)
#
#   .\upgrade.ps1 -Target "C:\FTC_downloads\OfflineSmartHUD"
#
# 새 빌드 폴더에서 실행하세요. 프로그램 파일만 교체하고
# 일정/메일규칙/설정/로그는 그대로 둡니다.
param(
    [Parameter(Mandatory = $true)][string]$Target
)
$ErrorActionPreference = "Stop"
$Source = $PSScriptRoot

# 사용자 데이터 - 절대 덮어쓰지 않는다
$Keep = @("schedules.db", "chat_history.db", "config.json",
          "schedules.db-wal", "schedules.db-shm",
          "chat_history.db-wal", "chat_history.db-shm")

if (-not (Test-Path "$Target\OfflineSmartHUD.exe")) {
    Write-Host "[!] 대상 폴더에 OfflineSmartHUD.exe 가 없습니다: $Target" -ForegroundColor Red
    Write-Host "    설치 폴더 경로를 -Target 으로 지정하세요." -ForegroundColor Red
    exit 1
}
if ((Resolve-Path $Source).Path -eq (Resolve-Path $Target).Path) {
    Write-Host "[!] 원본과 대상이 같은 폴더입니다." -ForegroundColor Red
    exit 1
}

$running = Get-Process OfflineSmartHUD -ErrorAction SilentlyContinue
if ($running) {
    Write-Host "[*] 실행 중인 앱을 종료합니다..."
    $running | Stop-Process -Force
    Start-Sleep -Seconds 2
}

$stamp  = Get-Date -Format "yyyyMMdd_HHmmss"
$backup = Join-Path $Target "backups\pre-upgrade-$stamp"
New-Item -ItemType Directory -Force -Path $backup | Out-Null
foreach ($f in $Keep) {
    if (Test-Path "$Target\$f") { Copy-Item "$Target\$f" $backup -Force }
}
Write-Host "[*] 기존 데이터 백업: $backup"

# _internal 은 통째로 교체한다(이전 버전 잔여 파일 제거).
if (Test-Path "$Target\_internal") { Remove-Item "$Target\_internal" -Recurse -Force }
Copy-Item "$Source\_internal" $Target -Recurse -Force
Copy-Item "$Source\OfflineSmartHUD.exe" $Target -Force
foreach ($doc in @("README.md", "upgrade.ps1")) {
    if (Test-Path "$Source\$doc") { Copy-Item "$Source\$doc" $Target -Force }
}

# models 는 건드리지 않는다. 대상에 GGUF 가 없고 원본에 있으면 그때만 복사.
$hasModel = Get-ChildItem "$Target\models" -Filter *.gguf -ErrorAction SilentlyContinue
if (-not $hasModel) {
    $newModel = Get-ChildItem "$Source\models" -Filter *.gguf -ErrorAction SilentlyContinue
    if ($newModel) { Copy-Item $newModel.FullName "$Target\models" -Force }
}

Write-Host ""
Write-Host "[OK] 업그레이드 완료" -ForegroundColor Green
Write-Host "     유지됨 : 일정 DB / 메일 감지 규칙 / 대화 기록 / 설정 / 로그 / 모델"
Write-Host "     교체됨 : OfflineSmartHUD.exe, _internal"
Write-Host "     백업   : $backup"
Write-Host ""
Write-Host "     실행: $Target\OfflineSmartHUD.exe"
"""


def _write_upgrade_script(out_dir: Path) -> None:
    try:
        # utf-8-sig, not utf-8: Windows PowerShell 5.1 reads a BOM-less script
        # as ANSI, which turns every Korean message in it into mojibake.
        (out_dir / "upgrade.ps1").write_text(UPGRADE_PS1, encoding="utf-8-sig")
        info("wrote upgrade.ps1 (data-preserving upgrade helper)")
    except OSError as exc:
        info(f"could not write upgrade.ps1 ({exc})")


def finalise(out_dir: Path, with_model: bool = False,
             model_file: Optional[Path] = None) -> None:
    """Create the runtime layout the app expects next to the executable."""
    models_out = out_dir / "models"
    models_out.mkdir(parents=True, exist_ok=True)

    readme = models_out / "PUT_YOUR_GGUF_MODEL_HERE.txt"
    readme.write_text(
        "Offline Smart HUD - local model folder\n"
        "======================================\n\n"
        "Place your GGUF model in THIS folder, next to OfflineSmartHUD.exe:\n\n"
        "    models/qwen2.5-1.5b-instruct-q4_k_m.gguf\n\n"
        "Any other *.gguf file in this folder is picked up automatically if the\n"
        "expected filename is absent. The model is intentionally NOT bundled\n"
        "inside the executable, so you can swap it without rebuilding.\n\n"
        "Without a model the app still runs: schedule entry uses the built-in\n"
        "offline parser; only the AI chat tab is disabled.\n",
        encoding="utf-8",
    )

    _write_upgrade_script(out_dir)

    source_models = HERE / "models"
    if model_file is not None:
        if not model_file.is_file():
            fail(f"--model file not found: {model_file}")
        info(f"copying model {model_file.name} "
             f"({human_size(model_file.stat().st_size)})… this takes a moment")
        shutil.copy2(model_file, models_out / model_file.name)
    elif with_model and source_models.is_dir():
        for gguf in source_models.glob("*.gguf"):
            info(f"copying model {gguf.name} ({human_size(gguf.stat().st_size)})…")
            shutil.copy2(gguf, models_out / gguf.name)

    # Ship the user-facing readme alongside the exe.
    for doc in ("README.md",):
        src = HERE / doc
        if src.exists():
            shutil.copy2(src, out_dir / doc)

    info(f"runtime layout ready: {out_dir.name}/models/, README.md")


# --------------------------------------------------------------------------- #

def main() -> int:
    argv = sys.argv[1:]
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return 0

    if "--outdir" in argv:
        index = argv.index("--outdir")
        if index + 1 >= len(argv):
            fail("--outdir requires a path")
        set_outdir(Path(argv[index + 1]))

    model_file: Optional[Path] = None
    if "--model" in argv:
        index = argv.index("--model")
        if index + 1 >= len(argv):
            fail("--model requires a path to a .gguf file")
        model_file = Path(argv[index + 1]).expanduser()

    clean()
    if "--clean-only" in argv:
        info("cleaned.")
        return 0

    out = build(console="--console" in argv, with_model="--with-model" in argv,
                model_file=model_file)

    print()
    info("NEXT STEPS")
    info(f"  1. copy the whole folder  {out}  to the target machine")
    info("  2. drop the GGUF file into  OfflineSmartHUD/models/")
    info(f"  3. run  OfflineSmartHUD/{APP_NAME}.exe")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
