#!/usr/bin/env python3
"""Check that every morse-logger verb is documented in --help, install.sh and README.

Run after adding a subcommand:

    python3 tools/audit_verbs.py        # exit 1 if anything is missing

This exists because `install.sh`'s command list was hand-maintained and quietly
fell five verbs behind (wpm, dial, translate, config, logs) - --help and the
README had them, so only a real install showed the gap.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from morse_logger import cli                                    # noqa: E402


def main():
    ap = cli.build_parser()
    sub = next(a for a in ap._actions
               if a.__class__.__name__ == "_SubParsersAction")
    verbs = sorted(v for v in sub.choices if v != "run")   # run is internal

    install = open(os.path.join(ROOT, "install.sh"), encoding="utf-8").read()
    readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    helpout = ap.format_help()

    bad = 0
    print(f"{'verb':12}{'--help':8}{'install.sh':12}{'README':8}")
    for v in verbs:
        in_help = bool(re.search(rf"^\s+{v}\s", helpout, re.M))
        in_inst = bool(re.search(rf"morse-logger {v}\b", install))
        in_rd = bool(re.search(rf"morse-logger {v}\b", readme))
        flag = "" if (in_help and in_inst and in_rd) else "  <-- MISSING"
        bad += 0 if not flag else 1
        print(f"{v:12}{'yes' if in_help else 'NO':8}"
              f"{'yes' if in_inst else 'MISSING':12}{'yes' if in_rd else 'MISSING':8}{flag}")

    # and the reverse: a documented command that no longer exists. Only look at
    # things that start a line (optionally after |, `, - or spaces) so prose like
    # "the morse-logger package" is not mistaken for a command.
    for name, text in (("install.sh", install), ("README.md", readme)):
        # verbs may contain hyphens (route-audio), so match them, not just [a-z]
        for found in sorted(set(re.findall(r"(?mi)^[\s|`$*+-]*morse-logger ([a-z][a-z-]{2,15})\b", text))):
            if found not in sub.choices:
                print(f"  warning: {name} mentions unknown verb 'morse-logger {found}'")
                bad += 1
    print(f"\n{len(verbs)} verbs, {bad} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
