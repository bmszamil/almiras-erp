"""Summarise fatoora -validate logs: one line per file with stage results and rule codes (E=error, W=warning)."""
import re
import sys

for path in sys.argv[1:]:
    text = open(path, encoding="utf-8", errors="replace").read()
    stages = re.findall(r"\[(\w+)\] validation result : (\w+)", text)
    errs = re.findall(r"\[ERROR\] ValidationProcessorImpl - CODE : ([^,]+), MESSAGE", text)
    warns = re.findall(r"\[WARN\] ValidationProcessorImpl - CODE : ([^,]+), MESSAGE", text)
    glob = re.search(r"GLOBAL VALIDATION RESULT = (\w+)", text)
    name = path.rsplit("/", 1)[-1][:-4]
    st = " ".join(f"{s}:{'ok' if r == 'PASSED' else 'FAIL'}" for s, r in stages)
    print(f"{(glob.group(1) if glob else 'NO-RESULT'):7} {name:70} {st}"
          + (f" | E: {' '.join(sorted(set(e.strip() for e in errs)))}" if errs else "")
          + (f" | W: {' '.join(sorted(set(w.strip() for w in warns)))}" if warns else ""))
