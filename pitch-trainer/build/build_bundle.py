"""Assemble the self-contained Windows bundle (runs on Linux CI).

    python3 build_bundle.py <out_dir>

Produces <out_dir>/PitchTrainer/ and <out_dir>/PitchTrainer-windows.zip with:
  python/   embeddable CPython + tkinter (from python.org tcltk.msi) + numpy,
            sounddevice (PortAudio DLL inside the wheel), cffi
  app/ START.bat README.md
Needs: internet, msitools (msiextract), pip.
"""
import glob
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile

PY = "3.12.10"
PYTAG = "312"
SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get(url, dest):
    print("GET", url, flush=True)
    req = urllib.request.Request(url, headers={"User-Agent": "pitchtrainer-build"})
    tok = os.environ.get("GITHUB_TOKEN")
    if tok and "api.github.com" in url:
        req.add_header("Authorization", "Bearer " + tok)
    with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    return dest


def find(root, name):
    hits = [p for p in glob.glob(os.path.join(root, "**", name), recursive=True)]
    if not hits:
        sys.exit("missing %s in %s" % (name, root))
    return hits[0]


def main():
    out = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "dist")
    work = os.path.join(out, "_work")
    dst = os.path.join(out, "PitchTrainer")
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

    # 3. numpy + sounddevice (Windows wheels; the sounddevice wheel carries PortAudio)
    wh = os.path.join(work, "wheels")
    plat = ["--only-binary=:all:", "--platform", "win_amd64", "--python-version", PY.rsplit(".", 1)[0],
            "--implementation", "cp", "--abi", "cp" + PYTAG, "-d", wh, "-q"]
    subprocess.check_call([sys.executable, "-m", "pip", "download", "numpy", "sounddevice"] + plat)
    site = os.path.join(py, "Lib", "site-packages")
    for w in glob.glob(os.path.join(wh, "*.whl")):
        print("  +", os.path.basename(w))
        zipfile.ZipFile(w).extractall(site)
    find(site, "libportaudio64bit.dll")

    # 6. app, docs
    shutil.copytree(os.path.join(SRC, "app"), os.path.join(dst, "app"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    for f in ("START.bat", "README.md"):
        shutil.copy2(os.path.join(SRC, f), dst)

    # 7. zip
    zp = os.path.join(out, "PitchTrainer-windows.zip")
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for base, _, files in os.walk(dst):
            for n in files:
                full = os.path.join(base, n)
                zf.write(full, os.path.relpath(full, out))
    shutil.rmtree(work)
    print("built", zp, os.path.getsize(zp) // (1 << 20), "MB")


if __name__ == "__main__":
    main()
