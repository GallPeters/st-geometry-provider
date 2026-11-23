"""Build releases/st_geometry_provider-<version>.zip (QGIS > Plugins > Install from ZIP).

Usage:  python tools/build_release.py [path/to/connections.json]

The version is read from metadata.txt. The zip contains the plugin folder st_geometry_provider/
with the plugin files only (no tests, docs sources, tools or releases). An optional
connections.json is bundled for distribution inside your own network; it is never part of
the repository.
"""
import configparser
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = "st_geometry_provider"      # plugin folder inside the zip
ZIP_NAME = "st_geometry_provider"     # release file name
FILES = ["__init__.py", "metadata.txt", "LICENSE", "icon.png", "icon.svg", "README.md",
         "connections.example.json"]
DIRS = ["src"]


def version():
    cp = configparser.ConfigParser()
    cp.read(os.path.join(ROOT, "metadata.txt"), encoding="utf-8")
    return cp["general"]["version"]


def main():
    out_dir = os.path.join(ROOT, "releases")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "%s-%s.zip" % (ZIP_NAME, version()))
    extra = sys.argv[1] if len(sys.argv) > 1 else None
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in FILES:
            z.write(os.path.join(ROOT, f), "%s/%s" % (PACKAGE, f))
        for d in DIRS:
            for base, dirs, files in os.walk(os.path.join(ROOT, d)):
                dirs[:] = [x for x in dirs if x != "__pycache__"]
                for f in sorted(files):
                    if f.endswith(".pyc"):
                        continue
                    full = os.path.join(base, f)
                    rel = os.path.relpath(full, ROOT).replace(os.sep, "/")
                    z.write(full, "%s/%s" % (PACKAGE, rel))
        if extra:
            z.write(extra, "%s/connections.json" % PACKAGE)
    print("written", out)


if __name__ == "__main__":
    main()
