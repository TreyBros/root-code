"""Pack the rootcode package into one runnable file: python3 build.py → dist/rootcode.pyz

The .pyz is a zip that Python runs directly (python3 rootcode.pyz, or ./rootcode.pyz). install.sh
builds it the same way, so what you install is exactly the code in this folder.
"""

import os
import sys
import zipapp

here = os.path.dirname(os.path.abspath(__file__))
target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "dist", "rootcode.pyz")
os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
zipapp.create_archive(here, target, interpreter="/usr/bin/env python3", main="rootcode.cli:main",
                      filter=lambda p: p.parts[0] == "rootcode" and p.suffix == ".py")
print(f"built {target}")
