"""Assemble the self-contained Windows bundle (runs on Linux CI).

    python3 build_bundle.py <out_dir>

Produces <out_dir>/ReelMaker/ and <out_dir>/ReelMaker-windows.zip with:
  python/   embeddable CPython + tkinter (from python.org tcltk.msi) + Pillow (RAQM)
            + libfribidi-0.dll (MSYS2) so Arabic shapes correctly
            + auto mode: faster-whisper (CTranslate2), rapidfuzz, OpenCV (headless, 4.x for
              the Haar face detector), numpy, and the MSVC C++ runtime DLLs
            + cinematic mode: onnxruntime (word alignment), tkinterdnd2 (drag & drop);
              OpenCV's YuNet face model ships in data/
  ffmpeg/   ffmpeg.exe (BtbN GPL build: libx264 + AAC)
  app/ fonts/ START.bat README.md sample/ output/ models/ (speech model downloads here on first use)
Needs: curl-able internet, msitools (msiextract), zstd, pip.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile

PY = "3.12.10"
PYTAG = "312"
SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EMOJI_URL = "https://raw.githubusercontent.com/googlefonts/noto-emoji/v2.047/fonts/NotoColorEmoji.ttf"
MSYS = "https://mirror.msys2.org/mingw/mingw64/"


def get(url, dest):
    print("GET", url, flush=True)
    req = urllib.request.Request(url, headers={"User-Agent": "reelmaker-build"})
    tok = os.environ.get("GITHUB_TOKEN")
    if tok and "api.github.com" in url:
        req.add_header("Authorization", "Bearer " + tok)
    with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    return dest


def text(url):
    tmp = get(url, "/tmp/_page")
    with open(tmp, encoding="utf-8", errors="replace") as f:
        return f.read()


def find(root, name):
    hits = [p for p in glob.glob(os.path.join(root, "**", name), recursive=True)]
    if not hits:
        sys.exit("missing %s in %s" % (name, root))
    return hits[0]


def main():
    out = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "dist")
    work = os.path.join(out, "_work")
    dst = os.path.join(out, "ReelMaker")
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(work)
    py = os.path.join(dst, "python")
    os.makedirs(py)

    # 1. embeddable python
    z = get("https://www.python.org/ftp/python/%s/python-%s-embed-amd64.zip" % (PY, PY),
            os.path.join(work, "embed.zip"))
    zipfile.ZipFile(z).extractall(py)
    with open(os.path.join(py, "python%s._pth" % PYTAG), "w", newline="\r\n") as f:
        f.write("python%s.zip\n.\nLib\nLib\\site-packages\nimport site\n" % PYTAG)

    # 2. tkinter from the official tcltk.msi
    msi = get("https://www.python.org/ftp/python/%s/amd64/tcltk.msi" % PY, os.path.join(work, "tcltk.msi"))
    tk_dir = os.path.join(work, "tcltk")
    os.makedirs(tk_dir)
    subprocess.check_call(["msiextract", "-C", tk_dir, msi], stdout=subprocess.DEVNULL)
    for dll in ("_tkinter.pyd", "tcl86t.dll", "tk86t.dll"):
        shutil.copy2(find(tk_dir, dll), py)
    for extra in ("zlib1.dll",):
        hits = glob.glob(os.path.join(tk_dir, "**", extra), recursive=True)
        if hits:
            shutil.copy2(hits[0], py)
    tkpkg = os.path.dirname(find(tk_dir, os.path.join("tkinter", "__init__.py")))
    shutil.copytree(tkpkg, os.path.join(py, "Lib", "tkinter"),
                    ignore=shutil.ignore_patterns("test", "__pycache__"))
    tcl_init = find(tk_dir, "init.tcl")  # .../tcl/tcl8.6/init.tcl
    shutil.copytree(os.path.dirname(os.path.dirname(tcl_init)), os.path.join(py, "tcl"),
                    ignore=shutil.ignore_patterns("tix*"))

    # 3. Pillow (Windows wheel, RAQM built in; fribidi loaded at runtime) + the auto-mode stack
    wh = os.path.join(work, "wheels")
    subprocess.check_call([sys.executable, "-m", "pip", "download", "pillow", "faster-whisper",
                           "rapidfuzz", "opencv-python-headless<5", "msvc-runtime", "onnxruntime", "tkinterdnd2",
                           "--only-binary=:all:",
                           "--platform", "win_amd64", "--python-version", PY.rsplit(".", 1)[0],
                           "--implementation", "cp", "--abi", "cp" + PYTAG, "-d", wh, "-q"])
    site = os.path.join(py, "Lib", "site-packages")
    for w in sorted(glob.glob(os.path.join(wh, "*.whl"))):
        print("  +", os.path.basename(w), flush=True)
        zipfile.ZipFile(w).extractall(site)
    # wheel .data/data/*.dll (msvc-runtime: msvcp140.dll & co) go next to python.exe
    for dll in glob.glob(os.path.join(site, "*.data", "data", "*.dll")):
        if not os.path.exists(os.path.join(py, os.path.basename(dll))):
            shutil.copy2(dll, py)
    for d in glob.glob(os.path.join(site, "*.data")):
        shutil.rmtree(d)

    # 4. fribidi DLL from MSYS2 (mingw64, depends on msvcrt only)
    idx = text(MSYS)
    pk = sorted(set(re.findall(r'mingw-w64-x86_64-fribidi-[0-9][^"<>]*?-any\.pkg\.tar\.zst(?=")', idx)))[-1]
    pkf = get(MSYS + pk, os.path.join(work, "fribidi.pkg.tar.zst"))
    fd = os.path.join(work, "fribidi")
    os.makedirs(fd)
    subprocess.check_call(["tar", "--zstd", "-xf", pkf, "-C", fd])
    dll = find(fd, "libfribidi-0.dll")
    for name in ("libfribidi-0.dll", "fribidi.dll", "fribidi-0.dll"):
        shutil.copy2(dll, os.path.join(py, name))

    # 5. ffmpeg
    rel = json.loads(text("https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest"))
    asset = [a for a in rel["assets"]
             if re.fullmatch(r"ffmpeg-n7\.1-latest-win64-gpl-7\.1\.zip", a["name"])] or \
            [a for a in rel["assets"] if re.fullmatch(r"ffmpeg-n\d+\.\d+-latest-win64-gpl-\d+\.\d+\.zip", a["name"])]
    fz = get(asset[0]["browser_download_url"], os.path.join(work, "ffmpeg.zip"))
    fx = os.path.join(work, "ffmpeg")
    zipfile.ZipFile(fz).extractall(fx)
    os.makedirs(os.path.join(dst, "ffmpeg"))
    shutil.copy2(find(fx, "ffmpeg.exe"), os.path.join(dst, "ffmpeg", "ffmpeg.exe"))
    shutil.copy2(find(fx, "LICENSE.txt"), os.path.join(dst, "ffmpeg", "LICENSE.txt"))

    # 6. app, fonts, docs
    shutil.copytree(os.path.join(SRC, "app"), os.path.join(dst, "app"),
                    ignore=shutil.ignore_patterns("__pycache__", "selftest.py"))
    # tkdnd for other platforms isn't needed on Windows
    dnd = os.path.join(py, "Lib", "site-packages", "tkinterdnd2", "tkdnd")
    if os.path.isdir(dnd):
        for d in os.listdir(dnd):
            if not d.startswith("win-x64"):
                shutil.rmtree(os.path.join(dnd, d))
    shutil.copy2(os.path.join(SRC, "app", "selftest.py"), os.path.join(dst, "app", "selftest.py"))
    shutil.copytree(os.path.join(SRC, "fonts"), os.path.join(dst, "fonts"))
    os.makedirs(os.path.join(dst, "fonts", "emoji"), exist_ok=True)
    get(EMOJI_URL, os.path.join(dst, "fonts", "emoji", "NotoColorEmoji.ttf"))
    shutil.copytree(os.path.join(SRC, "sample"), os.path.join(dst, "sample"))
    shutil.copytree(os.path.join(SRC, "data"), os.path.join(dst, "data"))
    for f in ("START.bat", "README.md"):
        shutil.copy2(os.path.join(SRC, f), dst)
    for sub in ("output", "models"):
        os.makedirs(os.path.join(dst, sub))
        with open(os.path.join(dst, sub, ".keep"), "w") as f:
            f.write("")

    # 7. zip
    zp = os.path.join(out, "ReelMaker-windows.zip")
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for base, _, files in os.walk(dst):
            for n in files:
                full = os.path.join(base, n)
                zf.write(full, os.path.relpath(full, out))
    shutil.rmtree(work)
    print("built", zp, os.path.getsize(zp) // (1 << 20), "MB")


if __name__ == "__main__":
    main()
