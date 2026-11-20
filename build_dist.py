"""Build nwstreams wheel + sdist with the standard library only (no setuptools/build download).

    python build_dist.py        ->  dist/nwstreams-<ver>-py3-none-any.whl, dist/nwstreams-<ver>.tar.gz
"""
import base64
import hashlib
import io
import re
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent
PKG = ROOT / "src" / "nwstreams"
DIST = ROOT / "dist"

text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
field = lambda k: re.search(rf'^{k}\s*=\s*"([^"]*)"', text, re.M).group(1)
NAME, VERSION, SUMMARY, REQ_PY = field("name"), field("version"), field("description"), field("requires-python")
classifiers = re.findall(r'^\s*"([^"]+ :: [^"]+)",?$', text, re.M)
keywords = re.search(r"^keywords\s*=\s*\[(.*)\]", text, re.M).group(1)
keywords = ",".join(k.strip().strip('"') for k in keywords.split(","))
readme = (ROOT / "README.md").read_text(encoding="utf-8")

METADATA = "\n".join(
    ["Metadata-Version: 2.1", f"Name: {NAME}", f"Version: {VERSION}", f"Summary: {SUMMARY}",
     f"Keywords: {keywords}", "License: MIT", f"Requires-Python: {REQ_PY}"]
    + [f"Classifier: {c}" for c in classifiers]
    + ["Provides-Extra: test", 'Requires-Dist: pytest>=7; extra == "test"',
       "Description-Content-Type: text/markdown", "", readme])
WHEEL = "Wheel-Version: 1.0\nGenerator: nwstreams-build_dist\nRoot-Is-Purelib: true\nTag: py3-none-any\n"


def sources():
    return sorted(p for p in PKG.rglob("*.py") if "__pycache__" not in p.parts)


def build_wheel() -> Path:
    distinfo = f"{NAME}-{VERSION}.dist-info"
    path = DIST / f"{NAME}-{VERSION}-py3-none-any.whl"
    record = []
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        def add(arc, data: bytes):
            z.writestr(arc, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            record.append(f"{arc},sha256={digest},{len(data)}")
        for p in sources():
            add(f"{NAME}/{p.relative_to(PKG).as_posix()}", p.read_bytes())
        add(f"{distinfo}/METADATA", METADATA.encode("utf-8"))
        add(f"{distinfo}/WHEEL", WHEEL.encode())
        add(f"{distinfo}/LICENSE", (ROOT / "LICENSE").read_bytes())
        add(f"{distinfo}/top_level.txt", f"{NAME}\n".encode())
        record.append(f"{distinfo}/RECORD,,")
        z.writestr(f"{distinfo}/RECORD", "\n".join(record) + "\n")
    return path


def build_sdist() -> Path:
    base = f"{NAME}-{VERSION}"
    path = DIST / f"{base}.tar.gz"
    files = [ROOT / "pyproject.toml", ROOT / "README.md", ROOT / "CHANGELOG.md", ROOT / "LICENSE",
             ROOT / "build_dist.py", *sources()]
    files += sorted(p for d in ("tests", "examples", "docs") for p in (ROOT / d).rglob("*")
                    if p.is_file() and "__pycache__" not in p.parts and ".pytest_cache" not in p.parts)
    with tarfile.open(path, "w:gz") as t:
        for p in files:
            t.add(p, arcname=f"{base}/{p.relative_to(ROOT).as_posix()}")
        info = tarfile.TarInfo(f"{base}/PKG-INFO")
        data = METADATA.encode("utf-8")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    return path


if __name__ == "__main__":
    DIST.mkdir(exist_ok=True)
    for p in (build_wheel(), build_sdist()):
        print(f"built {p.relative_to(ROOT)}  ({p.stat().st_size} bytes)")
