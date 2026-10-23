"""Build AdsDashboard.html: src/dashboard.html with the Cairo font embedded (works offline).

    python3 build.py
fonts/cairo-subset.woff2 = Cairo (OFL) subset to Arabic + Latin with
    pyftsubset Cairo.ttf --unicodes=U+0020-007E,U+00A0-00FF,U+0600-06FF,U+0750-077F,U+FB50-FDFF,U+FE70-FEFF,U+200C-200F,U+2010-2027,U+2030-203A,U+2190-2193,U+2212,U+25B2,U+25BC,U+2605,U+2713,U+00D7,U+066B,U+066C --layout-features=* --flavor=woff2
"""
import base64
import os

HERE = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(HERE, "src", "dashboard.html"), encoding="utf-8").read()
font = base64.b64encode(open(os.path.join(HERE, "fonts", "cairo-subset.woff2"), "rb").read()).decode()
assert "/*CAIRO_WOFF2*/" in src
out = src.replace("/*CAIRO_WOFF2*/", font)
with open(os.path.join(HERE, "AdsDashboard.html"), "w", encoding="utf-8", newline="\n") as f:
    f.write(out)
print("AdsDashboard.html", len(out.encode()) // 1024, "KB")
