#!/usr/bin/env python3
"""witness: agent memory where every claim carries its own proof.

A claim is a sentence worth remembering ("auth tokens expire after 1h").
Each claim has witnesses: small checks against the repo that say whether
the claim still holds. `witness check` re-runs them, so memory is never
trusted just because it was written down.

States: fresh (holds) / drifted (the code under it moved) / broken (it no
longer holds) / unchecked (could not be run) / unwitnessed (no proof attached).
"""
import argparse, hashlib, json, os, re, subprocess, sys, datetime as dt
from pathlib import Path

DIR = ".witness"
ORDER = ["broken", "drifted", "unchecked", "unwitnessed", "fresh"]  # worst first
EVENTS, HISTORY = "events.jsonl", "history.jsonl"


# ---------- storage: an append-only event log (safe for concurrent sessions) ----------
def now():
    return os.environ.get("WITNESS_NOW") or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_root(arg):
    if arg:
        return Path(arg).resolve()
    p = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return Path(p.stdout.strip()) if p.returncode == 0 else Path.cwd()


def append(root, name, obj):
    d = root / DIR
    d.mkdir(exist_ok=True)
    with open(d / name, "a", encoding="utf-8") as f:  # one short line per write
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def read_lines(root, name):
    p = root / DIR / name
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass  # a torn line must not take the whole memory down
    return out


def load_claims(root):
    claims = {}
    for e in read_lines(root, EVENTS):
        if e["op"] == "add":
            claims[e["claim"]["id"]] = e["claim"]
        elif e["op"] == "ack" and e["id"] in claims:
            claims[e["id"]]["witnesses"] = e["witnesses"]
            claims[e["id"]]["verified"] = e["ts"]
        elif e["op"] == "rm":
            claims.pop(e["id"], None)
    return list(claims.values())


# ---------- witnesses ----------
def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def head(root):
    p = git(root, "rev-parse", "HEAD")
    return p.stdout.strip() if p.returncode == 0 else None


def safe_path(root, rel):
    p = (root / rel).resolve()
    return p if root.resolve() in p.parents or p == root.resolve() else None


def file_hash(root, spec):
    rel, _, rng = spec.partition("#")
    p = safe_path(root, rel)
    if not p or not p.is_file():
        return None
    text = p.read_text(encoding="utf-8", errors="replace")
    if rng:
        a, _, b = rng.partition("-")
        lines = text.splitlines()[int(a) - 1:int(b or a)]
        text = "\n".join(lines)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def make_witness(root, spec):
    kind, _, rest = spec.partition(":")
    if kind == "file":
        h = file_hash(root, rest)
        if h is None:
            sys.exit(f"witness: cannot read {rest}")
        return {"type": "file", "spec": rest, "hash": h}
    if kind == "grep":
        path, _, pat = rest.partition(":")
        re.compile(pat)
        if not safe_path(root, path) or not safe_path(root, path).is_file():
            sys.exit(f"witness: cannot read {path}")
        return {"type": "grep", "spec": path, "pattern": pat}
    if kind == "git":
        b = head(root)
        if not b:
            sys.exit("witness: git witness needs a git repo with a commit")
        return {"type": "git", "spec": rest, "base": b}
    if kind == "cmd":
        return {"type": "cmd", "spec": rest, "expect": 0}
    sys.exit(f"witness: unknown witness '{spec}' (use file:, grep:, git:, cmd:)")


def run_witness(root, w, allow_cmd):
    """Returns (state, detail). Never raises: a witness that crashes is 'unchecked'."""
    try:
        t = w["type"]
        if t == "file":
            h = file_hash(root, w["spec"])
            if h is None:
                return "broken", "file is gone"
            return ("fresh", "content unchanged") if h == w["hash"] else ("drifted", "content changed since recorded")
        if t == "grep":
            p = safe_path(root, w["spec"])
            if not p or not p.is_file():
                return "broken", "file is gone"
            n = len(re.findall(w["pattern"], p.read_text(encoding="utf-8", errors="replace"), re.M))
            return ("fresh", f"{n} match" + ("es" if n != 1 else "")) if n else ("broken", "pattern no longer found")
        if t == "git":
            p = safe_path(root, w["spec"])
            if not p or not p.exists():
                return "broken", "path is gone"
            r = git(root, "rev-list", "--count", f"{w['base']}..HEAD", "--", w["spec"])
            if r.returncode != 0:
                return "drifted", "recorded commit not found (history rewritten?)"
            n = int(r.stdout.strip() or 0)
            return ("fresh", "no commits since recorded") if n == 0 else ("drifted", f"behind {n} commit" + ("s" if n != 1 else ""))
        if t == "cmd":
            if not allow_cmd:
                return "unchecked", "command not run (pass --allow-cmd to trust this store)"
            r = subprocess.run(w["spec"], shell=True, cwd=root, capture_output=True, timeout=60)
            return ("fresh", "exit 0") if r.returncode == w.get("expect", 0) else ("broken", f"exit {r.returncode}")
    except Exception as e:  # noqa: BLE001
        return "unchecked", f"witness error: {e}"
    return "unchecked", "unknown witness type"


def evaluate(root, allow_cmd=False):
    out = []
    for c in load_claims(root):
        ws = []
        for w in c["witnesses"]:
            s, d = run_witness(root, w, allow_cmd)
            ws.append({**{k: w[k] for k in ("type", "spec")}, "state": s, "detail": d})
        states = [w["state"] for w in ws]
        status = "unwitnessed" if not states else next((s for s in ORDER if s in states), "fresh")
        out.append({**{k: c[k] for k in ("id", "text", "kind", "author", "source", "created")},
                    "verified": c.get("verified"), "status": status, "witnesses": ws})
    out.sort(key=lambda c: (ORDER.index(c["status"]), c["created"]))
    return out


def counts(res):
    n = {s: 0 for s in ORDER}
    for c in res:
        n[c["status"]] += 1
    return n


# ---------- commands ----------
COL = {"fresh": "32", "drifted": "33", "broken": "31", "unchecked": "36", "unwitnessed": "90"}


def paint(s, state):
    return f"\033[{COL[state]}m{s}\033[0m" if sys.stdout.isatty() else s


def cmd_add(a, root):
    cid = "w-" + hashlib.sha1((a.text + now()).encode()).hexdigest()[:6]
    claim = {"id": cid, "text": a.text, "kind": a.kind, "author": a.author, "source": a.source or "",
             "created": now(), "witnesses": [make_witness(root, s) for s in (a.witness or [])]}
    append(root, EVENTS, {"op": "add", "claim": claim})
    print(f"{cid}  {len(claim['witnesses'])} witness(es) recorded" + ("" if claim["witnesses"] else "  (unwitnessed: nothing will prove this later)"))


def cmd_check(a, root):
    res = evaluate(root, a.allow_cmd)
    append(root, HISTORY, {"ts": now(), "counts": counts(res), "head": head(root)})
    for c in res:
        print(f"{paint(c['status'].upper().ljust(11), c['status'])} {c['id']}  {c['text']}")
        for w in c["witnesses"]:
            if w["state"] != "fresh" or a.verbose:
                print(f"{' ' * 12}{w['type']}:{w['spec']}  {w['detail']}")
    n = counts(res)
    print("\n" + "  ".join(f"{k} {v}" for k, v in n.items() if v))
    sys.exit(1 if n["broken"] else 0)


def cmd_brief(a, root):
    """Session-start digest. Re-checks live, so the digest itself is never stale."""
    res = evaluate(root, False)
    if not res:
        return
    n = counts(res)
    print(f"witness: {len(res)} remembered claims, checked just now: " + ", ".join(f"{v} {k}" for k, v in n.items() if v))
    shown = [c for c in res if c["status"] != "fresh"][: a.limit]
    for c in shown:
        why = next((w["detail"] for w in c["witnesses"] if w["state"] != "fresh"), "no proof attached")
        print(f"- {c['status'].upper()} {c['id']} {c['text']}: {why}")
    fresh = [c for c in res if c["status"] == "fresh"][: max(0, a.limit - len(shown))]
    for c in fresh:
        print(f"- ok {c['id']} {c['text']}")
    if shown:
        print("Re-verify the flagged claims before relying on them; `witness ack ID` after confirming one.")


def cmd_ack(a, root):
    c = next((c for c in load_claims(root) if c["id"] == a.id), None)
    if not c:
        sys.exit(f"witness: no claim {a.id}")
    ws = []
    for w in c["witnesses"]:
        if w["type"] == "file":
            w = {**w, "hash": file_hash(root, w["spec"]) or w["hash"]}
        elif w["type"] == "git":
            w = {**w, "base": head(root) or w["base"]}
        ws.append(w)
    append(root, EVENTS, {"op": "ack", "id": a.id, "witnesses": ws, "ts": now()})
    print(f"{a.id} re-witnessed against the current repo")


def cmd_rm(a, root):
    append(root, EVENTS, {"op": "rm", "id": a.id, "ts": now()})
    print(f"{a.id} removed")


def cmd_report(a, root):
    res = evaluate(root, a.allow_cmd)
    if not a.no_record:
        append(root, HISTORY, {"ts": now(), "counts": counts(res), "head": head(root)})
    data = {"repo": root.name, "generated": now(), "claims": res, "history": read_lines(root, HISTORY)[-60:]}
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8").replace("__DATA__", blob)
    out = Path(a.output)
    out.write_text(html, encoding="utf-8")
    print(f"wrote {out}  ({len(res)} claims)")


def main():
    ap = argparse.ArgumentParser(prog="witness", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="repo root (default: git top-level or cwd)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add", help="remember a claim and attach proof")
    p.add_argument("text")
    p.add_argument("-w", "--witness", action="append", help="file:PATH[#a-b] | grep:PATH:REGEX | git:PATH | cmd:COMMAND")
    p.add_argument("--kind", default="fact", choices=["fact", "decision", "gotcha", "constraint"])
    p.add_argument("--author", default="human")
    p.add_argument("--source", help="where this came from (a commit, a doc, a conversation)")
    p.set_defaults(fn=cmd_add)
    p = sub.add_parser("check", help="re-run every witness; exit 1 if any claim is broken")
    p.add_argument("--allow-cmd", action="store_true", help="run cmd witnesses (they execute shell: only for stores you trust)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_check)
    p = sub.add_parser("brief", help="short session-start digest (for hooks)")
    p.add_argument("--limit", type=int, default=12)
    p.set_defaults(fn=cmd_brief)
    p = sub.add_parser("ack", help="after confirming a drifted claim by hand, re-baseline its witnesses")
    p.add_argument("id")
    p.set_defaults(fn=cmd_ack)
    p = sub.add_parser("rm", help="forget a claim")
    p.add_argument("id")
    p.set_defaults(fn=cmd_rm)
    p = sub.add_parser("report", help="write the visual dashboard (one self-contained HTML file)")
    p.add_argument("-o", "--output", default="witness-report.html")
    p.add_argument("--allow-cmd", action="store_true")
    p.add_argument("--no-record", action="store_true", help="do not add this run to the trend")
    p.set_defaults(fn=cmd_report)
    a = ap.parse_args()
    a.fn(a, find_root(a.root))


if __name__ == "__main__":
    main()
