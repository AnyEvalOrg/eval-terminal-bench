"""Scan job bytes without printing or persisting the in-memory router key."""
import base64
import json
import os
from pathlib import Path
import sys
from urllib.parse import quote


def scan(root, key):
    files = [p for p in root.rglob("*") if p.is_file() and not p.is_symlink()]
    if not key:
        return {"status": "not_performed", "reason": "Router key was not available",
                "files_present": len(files), "matches": None}
    needles = {"literal": key.encode(), "base64": base64.b64encode(key.encode()),
               "url_encoded": quote(key, safe="").encode(),
               "json_escaped": json.dumps(key)[1:-1].encode()}
    matches, errors = [], []
    for path in files:
        try:
            data = path.read_bytes()
            forms = [name for name, needle in needles.items() if needle in data]
            if forms:
                matches.append({"path": str(path.relative_to(root)), "forms": forms})
        except OSError:
            errors.append(str(path.relative_to(root)))
    return {"status": "completed" if not errors else "incomplete", "files_scanned": len(files)-len(errors),
            "matches": matches, "unreadable_paths": errors}


if __name__ == "__main__":
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "jobs"
    result = scan(root, os.environ.get("OPENAI_API_KEY"))
    print(json.dumps(result, indent=2))
    sys.exit(1 if result.get("matches") else 0)
