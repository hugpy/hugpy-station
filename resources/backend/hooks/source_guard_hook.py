#!/usr/bin/env python3
"""PreToolUse(Bash) guard: the source is the directory on this server.

Operator 2026-09-30: "the source is the source" / "github doesn't dictate
anything on this server" / "it happens all the time, i can't stop it".
Agents kept copying a source out of place (git worktree/clone into /tmp,
cp -r to a snapshot), shipping from the copy, and never folding it back.
This hook refuses those commands before they run. Work happens IN the source
directory; releases = `cd <source dir> && abstract-pypit`.

A source = the target of /srv/vm_mgr/src/*/source, or any package directory under
/srv/pyit/dev, symlinked entries resolved to their real path. The hugpy live tree (/srv/hugpy/src/hugpy) is not
covered: its /tmp worktree landing is the sanctioned exception.

Blocked (exit 2, reason on stderr -> shown to the model):
  * git worktree add, run in / against (-C) a source
  * git clone of a source path, or of a remote whose repo name is a source's
  * cp -r/-R/-a, rsync -r/-a whose SOURCE argument is a source dir (or inside one)
  * any direct twine upload (the publisher here is abstract-pypit, via push.sh)
  * any write to /etc/ufw/* (sed -i, tee, >, >>, cp/mv/install/rm/dd onto it, or the
    Write/Edit tools): a hand edit with one invalid line took the whole host off the
    network on 2026-09-30. Firewall rules go through the gate action
    `ufw.apply-rules` (validate -> backup -> apply -> health check -> auto-restore).
Everything else passes (exit 0). Never blocks on its own error.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shlex
import sys

REGISTRY_GLOB = "/srv/vm_mgr/src/*/source"
DEV_ROOT = "/srv/pyit/dev"
MSG = ("source guard: {what}. The source is the directory on this server: {src}. "
       "Never copy/worktree/clone it. To change it: /srv/vm_mgr/src/<project>/push.sh stage, "
       "edit /srv/vm_mgr/src/<project>/live, then push.sh release (host-triggered: upload, then "
       "the source is swapped). GitHub decides nothing here.")


def sources(registry_glob=REGISTRY_GLOB, dev_root=DEV_ROOT):
    out = set()
    for link in glob.glob(registry_glob):
        try:
            out.add(os.path.realpath(link))
        except OSError:
            pass
    try:
        for name in os.listdir(dev_root):
            # a symlinked entry (/srv/pyit/dev/hugpy_video -> the hugpy tree) IS that
            # package's source: resolve it, never skip it
            p = os.path.realpath(os.path.join(dev_root, name))
            if os.path.isdir(p) and any(os.path.exists(os.path.join(p, f))
                                        for f in (".git", "pyproject.toml", "setup.py", "setup.cfg")):
                out.add(p)
    except OSError:
        pass
    return sorted(out)


def _owner(path, srcs):
    """The source dir `path` is (inside), or None."""
    rp = os.path.realpath(path)
    for s in srcs:
        if rp == s or rp.startswith(s + os.sep):
            return s
    return None


def _names(srcs):
    n = {}
    for s in srcs:
        b = os.path.basename(s).lower()
        n[b] = s
        n[b.replace("_", "-")] = s
        n[b.replace("-", "_")] = s
    return n


def _segments(cmd):
    # split on shell separators; good enough for agent one-liners
    for part in re.split(r"&&|\|\||;|\||\n", cmd):
        part = part.strip()
        if part:
            try:
                yield shlex.split(part)
            except ValueError:
                yield part.split()


UFW_DIR = "/etc/ufw/"
UFW_MSG = ("source guard: direct edits to %s are refused (a single invalid line dropped all "
           "loopback and outbound traffic on 2026-09-30). Fetch the current file with: sudo hugpy-gate run "
           "ufw.fetch-rules -p path=/etc/ufw/<file> -p dest=/srv/vm_mgr/gate/ufw/<file> ; edit that copy, then: sudo hugpy-gate approve ufw.apply-rules && "
           "sudo hugpy-gate run ufw.apply-rules -p target=<before.rules|...> -p file=<candidate> "
           "--confirm ufw.apply-rules  (it tests the rules first and restores itself if the host "
           "loses loopback/DNS/outbound).")
_UFW_REDIRECT = re.compile(r">>?\s*/etc/ufw/")


def check_ufw(cmd):
    """A shell command that WRITES into /etc/ufw/ -> refusal, else None.
    Reading or copying out of it is fine."""
    if UFW_DIR not in cmd:
        return None
    bad = UFW_MSG % UFW_DIR
    for raw in re.split(r"&&|\|\||;|\||\n", cmd):
        if _UFW_REDIRECT.search(raw):
            return bad
        try:
            argv = shlex.split(raw)
        except ValueError:
            argv = raw.split()
        while argv and (argv[0] in ("sudo", "doas", "env", "command", "nohup") or argv[0].startswith("-")
                        or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0])):
            argv = argv[1:]
        if not argv:
            continue
        prog, args = os.path.basename(argv[0]), [a for a in argv[1:] if not a.startswith("-")]
        hit = [a for a in args if a.startswith(UFW_DIR)]
        if prog == "tee" and hit:
            return bad
        if prog == "sed" and hit and any(a.startswith("-i") or a == "--in-place" for a in argv[1:]):
            return bad
        if prog in ("cp", "mv", "install", "ln", "rsync") and args and args[-1].startswith(UFW_DIR):
            return bad
        if prog in ("rm", "truncate", "chmod", "chown", "chattr", "unlink", "shred") and hit:
            return bad
        if prog == "dd" and any(a.startswith("of=" + UFW_DIR) for a in argv[1:]):
            return bad
        if prog in ("vi", "vim", "nano", "emacs", "ed") and hit:
            return bad
    return None


def check(cmd, cwd, srcs):
    """Return a refusal message, or None."""
    names = _names(srcs)
    here = cwd or os.getcwd()
    for argv in _segments(cmd):
        while argv and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]):   # env prefixes
            argv = argv[1:]
        if not argv:
            continue
        prog = os.path.basename(argv[0])
        if prog == "cd" and len(argv) > 1:
            here = os.path.join(here, os.path.expanduser(argv[1]))
            continue
        if prog == "git":
            gdir, rest, i = here, [], 1
            while i < len(argv):
                if argv[i] == "-C" and i + 1 < len(argv):
                    gdir = os.path.join(gdir, os.path.expanduser(argv[i + 1]))
                    i += 2
                    continue
                if argv[i] == "-c" and i + 1 < len(argv):
                    i += 2
                    continue
                rest = argv[i:]
                break
            if rest[:2] == ["worktree", "add"]:
                s = _owner(gdir, srcs)
                if s:
                    return MSG.format(what="`git worktree add` would put a second checkout of a source elsewhere", src=s)
            if rest[:1] == ["clone"]:
                args = [a for a in rest[1:] if not a.startswith("-")]
                if args:
                    origin = args[0]
                    s = _owner(os.path.join(here, os.path.expanduser(origin)), srcs) if "://" not in origin and "@" not in origin else None
                    if s is None:
                        repo = re.sub(r"\.git$", "", origin.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]).lower()
                        s = names.get(repo)
                    if s:
                        return MSG.format(what="`git clone` would make a copy of a source", src=s)
            continue
        if prog in ("cp", "rsync"):
            flags = "".join(a[1:] for a in argv[1:] if a.startswith("-") and not a.startswith("--"))
            longf = [a for a in argv[1:] if a.startswith("--")]
            recursive = (any(c in flags for c in "rRa") or "--recursive" in longf or "--archive" in longf)
            if not recursive:
                continue
            paths = [a for a in argv[1:] if not a.startswith("-")]
            for p in paths[:-1]:                                 # every SOURCE argument (last = destination)
                full = os.path.join(here, os.path.expanduser(p.rstrip("/") or "/"))
                if os.path.isdir(full):
                    s = _owner(full, srcs)
                    dest = os.path.join(here, os.path.expanduser(paths[-1]))
                    if s and _owner(dest, srcs) != s:
                        return MSG.format(what="`%s` would copy a source tree out of place" % prog, src=s)
            continue
        if prog == "twine" or (prog.startswith("python") and argv[1:3] == ["-m", "twine"]):
            if "upload" in argv:
                return ("source guard: direct `twine upload` bypasses the release loop. Release = "
                        "/srv/vm_mgr/src/<project>/push.sh release (host runner: abstract-pypit uploads "
                        "staging live; only then is the source swapped). If abstract-pypit fails, fix it "
                        "(/srv/pyit/dev/abstract_pypit).")
            continue
    return None


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:                                            # noqa: BLE001
        return 0
    tool, ti = data.get("tool_name"), (data.get("tool_input") or {})
    if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        fp = os.path.realpath(ti.get("file_path") or ti.get("notebook_path") or "")
        if fp.startswith(UFW_DIR):
            sys.stderr.write(UFW_MSG % fp + "\n")
            return 2
        return 0
    if tool != "Bash":
        return 0
    cmd = ti.get("command") or ""
    why_ufw = check_ufw(cmd)
    if why_ufw:
        sys.stderr.write(why_ufw + "\n")
        return 2
    try:
        why = check(cmd, data.get("cwd") or os.getcwd(), sources())
    except Exception:                                            # noqa: BLE001 — never block on our own bug
        return 0
    if why:
        sys.stderr.write(why + "\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
