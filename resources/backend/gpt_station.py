"""GPT launch settings and metadata from the exact live Codex process."""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile


DEFAULTS = {"default_model": "", "model_reasoning_effort": "", "dangerous": True}
EFFORTS = ("", "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def read_config(path):
    try:
        data = Path(path).read_text()
    except FileNotFoundError:
        return {}
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("GPT configuration must be an object")
    return value


def settings(config):
    return {key: config.get(key, default) for key, default in DEFAULTS.items()}


def update_config(path, patch):
    if not isinstance(patch, dict) or not patch or set(patch) - DEFAULTS.keys():
        raise ValueError("Supply default_model, model_reasoning_effort, or dangerous")
    patch = dict(patch)
    if "default_model" in patch:
        value = patch["default_model"]
        if value is None:
            value = ""
        if not isinstance(value, str) or (value and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+\[\]-]{0,199}", value)):
            raise ValueError("invalid default_model")
        patch["default_model"] = value
    if "model_reasoning_effort" in patch and patch["model_reasoning_effort"] not in EFFORTS:
        raise ValueError("invalid model_reasoning_effort")
    if "dangerous" in patch and type(patch["dangerous"]) is not bool:
        raise ValueError("dangerous must be a boolean")
    path = Path(path)
    config = read_config(path)
    config.update(patch)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".gpt-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(config, stream, indent=2)
            stream.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return config


def wrapper_binary():
    found = shutil.which("abstract-gpt")
    if found:
        return found
    for folder in (".local/bin", "miniconda3/bin"):
        candidate = Path.home() / folder / "abstract-gpt"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def launch_flags(config, guidance=""):
    cfg = settings(config)
    flags = (["--dangerously-bypass-approvals-and-sandbox"] if cfg["dangerous"] else
             ["--sandbox", "workspace-write", "--ask-for-approval", "on-request"])
    if cfg["default_model"]:
        flags += ["--model", cfg["default_model"]]
    if cfg["model_reasoning_effort"]:
        flags += ["-c", "model_reasoning_effort=" + json.dumps(cfg["model_reasoning_effort"])]
    if guidance:
        flags += ["-c", "developer_instructions=" + json.dumps(guidance)]
    return flags


def launch_command(config, config_path, guidance="", remote=False):
    wrapper = "abstract-gpt" if remote else wrapper_binary()
    flags = launch_flags(config, guidance)
    if wrapper:
        prefix = ["env", "AG_ROOT=" + str(Path(config_path).parent), wrapper, "launch", "--"]
    else:
        prefix = ["codex"]
    return shlex.join(prefix + flags)


def transcript_metadata(path, limit=1048576):
    """Read metadata only; bound I/O and never return message/tool contents."""
    path = Path(path)
    result = {"transcript": str(path), "last_activity": path.stat().st_mtime}
    with path.open("rb") as stream:
        head = stream.readline(limit)
        stream.seek(0, 2)
        start = max(0, stream.tell() - limit)
        stream.seek(start)
        if start:
            stream.readline()  # discard a potentially partial JSONL record
        lines = [head] + stream.read(limit).splitlines()
    for line in lines:
        try:
            row = json.loads(line)
            p = row.get("payload") or {}
            if not isinstance(p, dict):
                continue
        except (ValueError, AttributeError):
            continue
        kind = row.get("type")
        if kind == "session_meta":
            result.update(thread_id=p.get("id") or p.get("session_id"),
                          cwd=p.get("cwd"), cli_version=p.get("cli_version"))
        elif kind == "turn_context":
            for key in ("model", "approval_policy"):
                if p.get(key):
                    result[key] = p[key]
            effort = p.get("effort") or p.get("model_reasoning_effort")
            if effort:
                result["model_reasoning_effort"] = effort
            sandbox = p.get("sandbox_policy") or {}
            if isinstance(sandbox, dict) and sandbox.get("type"):
                result["sandbox_mode"] = sandbox["type"]
        elif kind == "event_msg":
            if p.get("type") == "task_started":
                result["busy"] = True
            elif p.get("type") in ("task_complete", "turn_aborted"):
                result["busy"] = False
            elif p.get("type") == "token_count" and isinstance(p.get("info"), dict):
                info = p["info"]
                result["usage"] = {key: info.get(key) for key in
                                   ("last_token_usage", "total_token_usage", "model_context_window")}
    # A long tool-heavy turn can put its turn_context well outside the first
    # tail window. Expand a bounded read rather than showing a blank model.
    if not result.get("model") and path.stat().st_size > limit and limit < 16777216:
        return transcript_metadata(path, min(limit * 2, 16777216))
    return result


def tracker(socket="console", proc_root=Path("/proc"), pane_pid=None):
    result = {"provider": "codex", "seat": "keeper-codex", "alive": False,
              "pid": None, "transcript": None, "thread_id": None, "model": None,
              "unrestricted": None, "last_activity": None}
    try:
        if pane_pid is None:
            run = subprocess.run(["tmux", "-L", socket, "display-message", "-p",
                                  "-t", "=keeper-codex:", "#{pane_pid}"],
                                 capture_output=True, text=True, timeout=4)
            pane_pid = int(run.stdout.strip())
        queue, seen = [int(pane_pid)], set()
        while queue and len(seen) < 128:
            pid = queue.pop(0)
            if pid in seen:
                continue
            seen.add(pid)
            proc = Path(proc_root) / str(pid)
            try:
                argv = [os.fsdecode(x) for x in (proc / "cmdline").read_bytes().split(b"\0") if x]
                children = proc / "task" / str(pid) / "children"
                if children.exists():
                    queue.extend(int(x) for x in children.read_text().split())
                if not argv or Path(argv[0]).name != "codex":
                    continue
                result.update(alive=True, pid=pid, unrestricted=(
                    "--dangerously-bypass-approvals-and-sandbox" in argv or "--yolo" in argv))
                candidates = []
                for fd in (proc / "fd").iterdir():
                    try:
                        target = Path(os.readlink(fd))
                        if target.name.startswith("rollout-") and target.suffix == ".jsonl" and target.is_file():
                            candidates.append(target)
                    except OSError:
                        continue
                if candidates:
                    result.update(transcript_metadata(max(candidates, key=lambda p: p.stat().st_mtime)))
                    if result.get("sandbox_mode") and result.get("approval_policy"):
                        result["unrestricted"] = (result["sandbox_mode"] == "danger-full-access"
                                                  and result["approval_policy"] == "never")
                    return result
            except (OSError, ValueError):
                continue
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return result
