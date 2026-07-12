"""Build and place the target-triple Nebula Core sidecar for Tauri 2."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def target_triple() -> str:
    output = subprocess.check_output(["rustc", "-vV"], text=True)
    for line in output.splitlines():
        if line.startswith("host: "):
            return line.removeprefix("host: ").strip()
    raise RuntimeError("rustc did not report a host target triple")


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    frontend = root / "ui" / "dist"
    if not (frontend / "index.html").is_file():
        raise RuntimeError("build ui/ before freezing Nebula Core")
    arguments = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--name",
        "nebula-core",
        "--specpath",
        str(root / "build" / "pyinstaller"),
        "--paths",
        str(root / "src"),
        "--collect-submodules",
        "nebula.v3",
        "--add-data",
        f"{root / 'src/nebula/v3/migrations'}:nebula/v3/migrations",
        "--add-data",
        f"{frontend}:ui/dist",
    ]
    # The Tauri Core sidecar has no legacy GUI or notebook surface. Explicit
    # exclusions prevent hooks installed in a shared build environment from
    # pulling the PyQt maintenance application and unrelated dev tools into it.
    for module in (
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "IPython",
        "matplotlib",
        "tkinter",
        "pytest",
        "sphinx",
        "black",
        "astroid",
        "docutils",
        "jedi",
        "nbformat",
        "nbclient",
        "nbconvert",
        "notebook",
        "jupyter",
        "jupyter_core",
        "jupyter_server",
        "jupyterlab",
        "ipykernel",
        "accelerate",
        "chromadb",
        "cloudpickle",
        "cv2",
        "h5py",
        "langchain",
        "langchain_chroma",
        "langchain_classic",
        "langchain_community",
        "langchain_experimental",
        "langchain_huggingface",
        "langchain_ollama",
        "langchain_openai",
        "lxml",
        "altair",
        "appdirs",
        "argon2",
        "babel",
        "bokeh",
        "dask",
        "distributed",
        "fsspec",
        "grpc",
        "intake",
        "lz4",
        "magic",
        "markdown",
        "mistune",
        "nltk",
        "opentelemetry",
        "numba",
        "numpy",
        "pandas",
        "panel",
        "patsy",
        "PIL",
        "plotly",
        "pygraphviz",
        "pyarrow",
        "pyviz_comms",
        "qdarkstyle",
        "scipy",
        "sentence_transformers",
        "sklearn",
        "skimage",
        "spacy",
        "statsmodels",
        "thinc",
        "torch",
        "transformers",
        "unstructured",
        "Whoosh",
        "xarray",
        "xyzservices",
        "zmq",
    ):
        arguments.extend(["--exclude-module", module])
    arguments.append(str(root / "scripts" / "nebula_core_entry.py"))
    subprocess.run(arguments, cwd=root, check=True)
    suffix = ".exe" if sys.platform == "win32" else ""
    source = root / "dist" / f"nebula-core{suffix}"
    destination = (
        root
        / "ui"
        / "src-tauri"
        / "binaries"
        / f"nebula-core-{target_triple()}{suffix}"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    print(destination)


if __name__ == "__main__":
    main()
