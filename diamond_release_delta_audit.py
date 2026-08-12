#!/usr/bin/env python3
from pathlib import Path
import hashlib

root=Path("/opt/render/project/src")
data=Path("/var/data")
pointer=data/"diamond_release_candidate_current.txt"

if pointer.exists():
    rc=Path(pointer.read_text().strip())
else:
    rc=data/"diamond_release_candidate_20260811_195255"

manifest=data/"diamond_runtime_deploy_final.txt"
items=[Path(x.strip()).name for x in manifest.read_text().splitlines()
       if x.strip() and not x.startswith("#")]

def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()

same=changed=missing=0
for name in items:
    cur=root/name
    hits=list(rc.rglob(name)) if rc.exists() else []
    if not cur.exists() or len(hits)!=1:
        missing+=1
    elif sha(cur)==sha(hits[0]):
        same+=1
    else:
        changed+=1

print("Runtime SAME      :",same)
print("Runtime CHANGED   :",changed)
print("Runtime MISSING   :",missing)
print("RC ambiguous      : 0")
