#!/usr/bin/env python3
"""istofel-harness — contrato operacional executável para agentes de codificação.

Um único arquivo, sem dependências externas (Python 3.8+).

Entradas de hook (chamadas pelo Claude Code via plugin):
  hook-guard    PreToolUse   bloqueia ações proibidas
  hook-stop     Stop         roda a verificação antes de o agente encerrar
  hook-session  SessionStart injeta o resumo do contrato
  hook-prompt   UserPromptSubmit libera a escalação quando o usuário responde

Comandos para o usuário (terminal):
  init, audit, status, verify, approve-tests, scope, reset
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

VERSION = "1.1.0"
HARNESS_DIR = ".harness"
CONFIG_FILE = "config.json"
BASELINE_FILE = "baseline.json"
STATE_DIR = "state"
STATE_FILE = "state.json"
SCOPE_FILE = "scope.txt"
CONTRACT_FILE = "HARNESS.md"

DEFAULT_CONFIG = {
    "version": 1,
    "verify": [],
    "verify_timeout_seconds": 300,
    "max_attempts": 4,
    "escalate_after_same_error": 2,
    "tests_mode": "append-only",
    "test_paths": ["tests/**"],
    "protected_paths": [".harness/**", "HARNESS.md", ".claude/settings.json"],
    "dependency_files": [
        "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb",
        "requirements.txt", "requirements-*.txt", "pyproject.toml", "poetry.lock", "uv.lock",
        "Pipfile", "Pipfile.lock",
    ],
    "allow_writes_outside_project": False,
    "lessons": {"enabled": True, "file": "docs/lessons.md", "inject_last": 5},
}

LESSON_PENDING = "(pendente)"
LESSONS_HEADER = (
    "# Lições\n\n"
    "Registradas pelo istofel-harness quando uma verificação falhou e depois passou.\n"
    "A evidência é escrita pelo hook; a lição, pelo agente. Lições que se repetem devem\n"
    "virar uma linha em `NUNCA` ou `Regras Não Óbvias` do CLAUDE.md.\n\n"
)

IGNORED_DIRS = {
    ".git", "node_modules", ".venv", "venv", "env", "__pycache__", ".next", "dist", "build",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".turbo", ".cache", "coverage", ".nuxt",
    ".svelte-kit", "target", ".idea", ".vscode",
}

USER_ONLY_SUBCOMMANDS = ("init", "approve-tests", "reset", "scope")
PY_CMD = "python" if os.name == "nt" else "python3"

FILE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


# --------------------------------------------------------------------------- util

def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_json(p: Path, default=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def _write_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, p)


def _emit(obj) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.flush()


def _disabled() -> bool:
    return os.environ.get("HARNESS_DISABLE", "").strip() not in ("", "0", "false", "False")


def norm_rel(p: str) -> str:
    """Normaliza caminho relativo: barras posix, sem './' nem '/' inicial."""
    p = p.strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def glob_to_regex(pattern: str) -> re.Pattern:
    """Glob estilo gitignore: ** cruza diretórios, * e ? não cruzam '/'."""
    pat = norm_rel(pattern)
    anchored = "/" in pat.rstrip("/")
    if pat.endswith("/"):
        pat += "**"
    i, out = 0, []
    while i < len(pat):
        c = pat[i]
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("/**", i) and i + 3 == len(pat):
            out.append("(?:/.*)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    body = "".join(out)
    if not anchored:  # nome simples casa em qualquer profundidade
        body = "(?:.*/)?" + body
    return re.compile("^" + body + "$")


def path_matches(pattern: str, rel: str) -> bool:
    rel = norm_rel(rel)
    return bool(glob_to_regex(pattern).match(rel))


def path_touches(pattern: str, rel: str) -> bool:
    """True se rel casa o padrão OU é um diretório que contém algo protegido."""
    rel = norm_rel(rel).rstrip("/")
    if rel in ("", "."):
        return True
    if path_matches(pattern, rel):
        return True
    # rel é diretório ancestral do que o padrão protege?
    root = re.split(r"[*?\[]", norm_rel(pattern), maxsplit=1)[0].rstrip("/")
    if root and (root == rel or root.startswith(rel + "/")):
        return True
    return path_matches(pattern, rel + "/__probe__")


# --------------------------------------------------------------------------- projeto

def find_project_root(start: str | None = None) -> Path | None:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    candidates = []
    if env:
        candidates.append(Path(env))
    if start:
        candidates.append(Path(start))
    candidates.append(Path.cwd())
    for c in candidates:
        try:
            c = c.resolve()
        except OSError:
            continue
        for d in [c, *c.parents]:
            if (d / HARNESS_DIR / CONFIG_FILE).is_file():
                return d
    return None


class ConfigError(Exception):
    pass


def load_config(root: Path) -> dict:
    p = root / HARNESS_DIR / CONFIG_FILE
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ConfigError(f"{p} não é JSON válido: {e}")
    if not isinstance(data, dict):
        raise ConfigError(f"{p} deve ser um objeto JSON")
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(data)
    if cfg["tests_mode"] not in ("append-only", "locked", "open"):
        raise ConfigError("tests_mode deve ser 'append-only', 'locked' ou 'open'")
    for item in cfg["verify"]:
        if not isinstance(item, dict) or not item.get("cmd"):
            raise ConfigError("cada item de 'verify' precisa de {\"name\", \"cmd\"}")
        item.setdefault("name", item["cmd"])
    les = dict(DEFAULT_CONFIG["lessons"])
    if isinstance(data.get("lessons"), dict):
        les.update(data["lessons"])
    cfg["lessons"] = les
    for k in ("max_attempts", "escalate_after_same_error", "verify_timeout_seconds"):
        if not isinstance(cfg[k], int) or cfg[k] < 1:
            raise ConfigError(f"'{k}' deve ser inteiro >= 1")
    return cfg


def state_path(root: Path) -> Path:
    return root / HARNESS_DIR / STATE_DIR / STATE_FILE


def load_state(root: Path) -> dict:
    st = _read_json(state_path(root), {}) or {}
    st.setdefault("reference_fp", None)
    st.setdefault("failing", False)
    st.setdefault("attempts", 0)
    st.setdefault("last_error_sig", None)
    st.setdefault("same_error_count", 0)
    st.setdefault("escalated", False)
    st.setdefault("escalation_notified", False)
    st.setdefault("last_result", None)
    st.setdefault("lesson_requested", False)
    st.setdefault("fail_log", None)
    return st


def save_state(root: Path, st: dict) -> None:
    _write_json(state_path(root), st)


def read_scope(root: Path) -> list[str]:
    p = root / HARNESS_DIR / SCOPE_FILE
    if not p.is_file():
        return []
    lines = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            lines.append(ln)
    return lines


def rel_to_root(root: Path, path_str: str, cwd: str | None = None) -> str | None:
    """Caminho relativo à raiz (posix) ou None se estiver fora do projeto."""
    p = Path(os.path.expanduser(path_str))
    if not p.is_absolute():
        p = Path(cwd or root) / p
    try:
        rp = Path(os.path.normpath(str(p)))
        try:
            rp = rp.resolve()
        except OSError:
            pass
        rel = rp.relative_to(root.resolve())
    except ValueError:
        return None
    s = rel.as_posix()
    return "" if s == "." else s


def _is_git(root: Path) -> bool:
    try:
        r = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=root,
                           capture_output=True, text=True, timeout=10)
        return r.returncode == 0 and r.stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def _walk_files(root: Path, limit: int = 50000):
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS
                       and not (rel_dir in ("", ".") and d == HARNESS_DIR)]
        for fn in filenames:
            count += 1
            if count > limit:
                return
            yield Path(dirpath) / fn


def list_test_files(root: Path, test_paths: list[str]) -> list[str]:
    out = []
    for f in _walk_files(root):
        rel = f.relative_to(root).as_posix()
        if any(path_matches(p, rel) for p in test_paths):
            out.append(rel)
    return sorted(out)


def fingerprint(root: Path) -> str:
    h = hashlib.sha256()
    skip = set()
    try:
        skip.add(lessons_rel(load_config(root)) or "")
    except Exception:
        pass
    if _is_git(root):
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                              text=True, timeout=10).stdout.strip()
        h.update(("HEAD:" + head + "\n").encode())
        st = subprocess.run(["git", "status", "--porcelain=v1", "-uall", "-z"], cwd=root,
                            capture_output=True, timeout=30).stdout
        entries = [e for e in st.decode("utf-8", "replace").split("\0") if e]
        paths = set()
        skip_next = False
        for e in entries:
            if skip_next:
                skip_next = False
                continue
            code, path = e[:2], e[3:]
            if code.startswith("R") or code.startswith("C"):
                skip_next = True
            paths.add(path)
        for path in sorted(paths):
            if path.startswith(HARNESS_DIR + "/" + STATE_DIR) or path in skip:
                continue
            fp = root / path
            if fp.is_file():
                h.update(f"{path}:{_sha256_file(fp)}\n".encode())
            else:
                h.update(f"{path}:<missing>\n".encode())
    else:
        for f in sorted(_walk_files(root)):
            if f.relative_to(root).as_posix() in skip:
                continue
            try:
                s = f.stat()
            except OSError:
                continue
            h.update(f"{f.relative_to(root).as_posix()}:{s.st_size}:{s.st_mtime_ns}\n".encode())
    return h.hexdigest()


# --------------------------------------------------------------------------- lições

def lessons_rel(cfg: dict) -> str | None:
    les = cfg.get("lessons") or {}
    return norm_rel(les.get("file", "docs/lessons.md")) if les.get("enabled", True) else None


def evidence_line(output: str) -> str:
    """Linha mais informativa do log: erro concreto, não separadores nem resumos."""
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    lines = [ln for ln in lines if len(set(ln) - set("=-_*#~ ")) > 3]
    prefs = [
        r"^E\s+\S",                                  # pytest
        r"\b\w*(Error|Exception)\b\s*:",             # TypeError: ..., AssertionError: ...
        r"\berror\b.*:|\berror TS\d+",               # eslint / tsc
        r"(AssertionError|assert |expected|Expected|toBe|toEqual)",
        r"(DELETADO|ALTERADO|NOVO|TIMEOUT)",
        r"\b(FAIL|FAILED|failed)\b",
    ]
    pick = None
    for rx in prefs:
        pick = next((ln for ln in lines if re.search(rx, ln)), None)
        if pick:
            break
    pick = pick or (lines[-1] if lines else "(sem saída)")
    pick = re.sub(r"^E\s+", "", pick).replace("`", "'")
    return pick[:160]


def append_lesson_stub(root: Path, cfg: dict, check: str, attempts: int, evidence: str) -> str | None:
    rel = lessons_rel(cfg)
    if not rel:
        return None
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text(LESSONS_HEADER, encoding="utf-8")
    day = _dt.date.today().isoformat()
    line = (f"- {day} · [{check}] {attempts} falha(s) · evidência: `{evidence}` · "
            f"lição: {LESSON_PENDING}\n")
    with open(p, "a", encoding="utf-8") as f:
        f.write(line)
    return rel


def read_lessons(root: Path, cfg: dict) -> list[dict]:
    rel = lessons_rel(cfg)
    if not rel or not (root / rel).is_file():
        return []
    out = []
    rx = re.compile(r"^- (\S+) · \[([^\]]+)\] .*?lição: (.+)$")
    for ln in (root / rel).read_text(encoding="utf-8", errors="ignore").splitlines():
        m = rx.match(ln.strip())
        if m:
            out.append({"date": m.group(1), "check": m.group(2), "lesson": m.group(3).strip(),
                        "pending": m.group(3).strip() == LESSON_PENDING})
    return out


# --------------------------------------------------------------------------- integridade

def compute_baseline(root: Path, cfg: dict) -> dict:
    files = {rel: _sha256_file(root / rel) for rel in list_test_files(root, cfg["test_paths"])}
    return {"created": _now(), "test_paths": cfg["test_paths"], "files": files}


def integrity_violations(root: Path, cfg: dict) -> list[str]:
    if cfg["tests_mode"] == "open":
        return []
    base = _read_json(root / HARNESS_DIR / BASELINE_FILE)
    if base is None:
        return ["baseline de testes ausente (.harness/baseline.json) — rode 'approve-tests'"]
    out = []
    known = base.get("files", {})
    for rel, digest in sorted(known.items()):
        p = root / rel
        if not p.is_file():
            out.append(f"teste existente DELETADO: {rel}")
        elif _sha256_file(p) != digest:
            out.append(f"teste existente ALTERADO: {rel}")
    if cfg["tests_mode"] == "locked":
        for rel in list_test_files(root, cfg["test_paths"]):
            if rel not in known:
                out.append(f"teste NOVO não aprovado (modo locked): {rel}")
    return out


# --------------------------------------------------------------------------- verificação

def run_checks(root: Path, cfg: dict, stream: bool = False) -> dict:
    """Retorna {ok, failed: {name, cmd, code, output}|None, passed: [names]}."""
    viol = integrity_violations(root, cfg)
    if viol:
        return {"ok": False, "passed": [], "failed": {
            "name": "integridade-dos-testes", "cmd": "(checagem interna)", "code": 1,
            "output": "\n".join(viol)}}
    passed = []
    env = dict(os.environ)
    # PYTHONDONTWRITEBYTECODE: evita bytecode velho quando o arquivo muda no mesmo segundo
    env.update({"CI": "1", "FORCE_COLOR": "0", "NO_COLOR": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    for item in cfg["verify"]:
        name, cmd = item["name"], item["cmd"]
        timeout = int(item.get("timeout_seconds", cfg["verify_timeout_seconds"]))
        if stream:
            print(f"▶ {name}: {cmd}", flush=True)
        try:
            r = subprocess.run(cmd, shell=True, cwd=root, env=env, capture_output=True,
                               text=True, errors="replace", timeout=timeout)
            output = (r.stdout or "") + (r.stderr or "")
            code = r.returncode
        except subprocess.TimeoutExpired as e:
            out = e.stdout or ""
            if isinstance(out, bytes):
                out = out.decode("utf-8", "replace")
            output = out + f"\n[harness] TIMEOUT após {timeout}s"
            code = 124
        if stream:
            print(output.rstrip()[-4000:], flush=True)
        if code != 0:
            return {"ok": False, "passed": passed,
                    "failed": {"name": name, "cmd": cmd, "code": code, "output": output}}
        passed.append(name)
    return {"ok": True, "passed": passed, "failed": None}


def _tail(text: str, lines: int = 80, chars: int = 6000) -> str:
    t = "\n".join(text.rstrip().splitlines()[-lines:])
    return t[-chars:]


def error_signature(failed: dict) -> str:
    norm = re.sub(r"\d+(\.\d+)?", "N", _tail(failed["output"], 40))
    norm = re.sub(r"0x[0-9a-fA-F]+", "ADDR", norm)
    return hashlib.sha1((failed["name"] + "|" + norm).encode()).hexdigest()


# --------------------------------------------------------------------------- análise de Bash

DESTRUCTIVE = [
    (re.compile(r"\bgit\s+push\b[^\n]*(\s--force\b|\s-f\b|\s--force-with-lease\b|\s\+\S)"),
     "push forçado (git push --force)"),
    (re.compile(r"\bgit\s+reset\s+[^\n]*--hard\b"), "git reset --hard descarta trabalho"),
    (re.compile(r"\bgit\s+clean\s+[^\n]*-[a-zA-Z]*f"), "git clean -f apaga arquivos não versionados"),
    (re.compile(r"\bgit\s+(commit|push)\b[^\n]*--no-verify\b"), "--no-verify pula verificações"),
    (re.compile(r"(^|[;&|\s])sudo\s"), "sudo não é permitido"),
    (re.compile(r"\bfind\b[^\n]*\s-delete\b"), "find -delete apaga em massa"),
    (re.compile(r"\b(curl|wget)\b[^\n|]*\|\s*(ba|z|da)?sh\b"), "executar script baixado (curl | sh)"),
    (re.compile(r"\bmkfs(\.\w+)?\b|\bdd\s+[^\n]*of=/dev/"), "comando destrutivo de disco"),
    (re.compile(r"\bchmod\s+(-R\s+)?777\b"), "chmod 777"),
    (re.compile(r"\b(Remove-Item|rm|del|rd|rmdir)\b[^\n]*-Recurse\b", re.I),
     "remoção recursiva (Remove-Item -Recurse) não é permitida"),
]

MUTATING_ANY_ARG = {"rm", "unlink", "truncate", "touch", "chmod", "chown", "shred", "rmdir"}
MUTATING_DEST = {"cp", "install", "rsync", "ln"}
INPLACE = {"sed", "perl", "ruby"}

INSTALL_PATTERNS = [
    re.compile(r"^(npm|pnpm|bun)\s+(install|i|add)\s+(?!-)\S"),
    re.compile(r"^(npm|pnpm|bun)\s+(install|i|add)\s+(-\S+\s+)*[^-\s]\S*"),
    re.compile(r"^yarn\s+add\b"),
    re.compile(r"^(pip|pip3)\s+install\b(?!.*\s-r\s)(?!\s+-e\s)(?!\s+--editable)"),
    re.compile(r"^(python3?|py)\s+-m\s+pip\s+install\b(?!.*\s-r\s)(?!\s+-e\s)"),
    re.compile(r"^uv\s+(add\b|pip\s+install\b(?!.*\s-r\s)(?!\s+-e\s))"),
    re.compile(r"^poetry\s+add\b"),
    re.compile(r"^cargo\s+add\b"),
    re.compile(r"^go\s+get\b"),
]

WRAPPERS = {"timeout", "time", "nice", "nohup", "stdbuf", "command", "builtin", "env", "xargs"}


def _segments(command: str) -> list[list[str]]:
    """Divide em subcomandos; redirecionamentos viram tokens '>' / '>>'."""
    cmd = command.replace("\\\n", " ")
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=";&|<>()\n")
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        tokens = re.split(r"\s+", cmd)
    segs, cur = [], []
    for t in tokens:
        if t and set(t) <= set(";&|()\n") and t not in ("",):
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        segs.append(cur)
    return segs


def _strip_wrappers(seg: list[str]) -> list[str]:
    i = 0
    while i < len(seg):
        t = seg[i]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t):
            i += 1
            continue
        if t in WRAPPERS:
            i += 1
            while i < len(seg) and seg[i].startswith("-"):
                i += 1
            if t == "timeout" and i < len(seg) and re.match(r"^\d", seg[i]):
                i += 1
            continue
        break
    return seg[i:]


def _redirect_targets(seg: list[str]) -> list[str]:
    out = []
    for i, t in enumerate(seg):
        if t in (">", ">>", "&>", ">|") or re.fullmatch(r"\d?>{1,2}", t):
            if i + 1 < len(seg) and seg[i + 1] not in (">", "&"):
                tgt = seg[i + 1]
                if tgt.startswith("&"):
                    continue
                out.append(tgt)
        else:
            m = re.match(r"^\d?>{1,2}(.+)$", t)
            if m and not m.group(1).startswith("&"):
                out.append(m.group(1))
    return [t for t in out if t not in ("/dev/null", "/dev/stderr", "/dev/stdout")]


def _arg_path(a: str) -> str:
    a = a.strip("'\"")
    m = re.split(r"[*?\[{]", a, maxsplit=1)
    base = m[0]
    if len(m) > 1:  # contém glob: use o prefixo como diretório
        base = base.rsplit("/", 1)[0] if "/" in base else ""
    return base


def analyze_bash(command: str, cfg: dict, root: Path, cwd: str | None, escalated: bool):
    """Retorna (decisão, motivo) — decisão em {'deny','ask',None}."""
    flat = " ".join(command.split())

    if re.search(r"harness(_core)?\.py\b|scripts/run\.sh\b", flat) and re.search(
            r"\b(" + "|".join(USER_ONLY_SUBCOMMANDS) + r")\b", flat):
        return "deny", ("Operação exclusiva do usuário (init/approve-tests/reset/scope). "
                        "Peça ao usuário para rodar no terminal.")

    for rx, why in DESTRUCTIVE:
        if rx.search(flat):
            return "deny", f"Bloqueado pelo harness: {why}."

    protected = list(cfg["protected_paths"])
    tests_mode = cfg["tests_mode"]
    test_paths = cfg["test_paths"] if tests_mode != "open" else []

    def hits_protected(arg: str) -> str | None:
        p = _arg_path(arg)
        if p == "" and arg.strip("'\"") in ("", "."):
            p = ""
        rel = rel_to_root(root, p or ".", cwd)
        if rel is None:
            return None
        for pat in protected + test_paths:
            if path_touches(pat, rel):
                return pat
        return None

    for seg in _segments(command):
        seg = _strip_wrappers(seg)
        if not seg:
            continue
        prog = os.path.basename(seg[0])
        args = [a for a in seg[1:] if a not in (">", ">>", "&>", ">|", "<")]
        flags = [a for a in args if a.startswith("-")]
        paths = [a for a in args if not a.startswith("-")]

        # rm -rf (qualquer ordem de flags)
        if prog == "rm":
            letters = "".join(f.lstrip("-") for f in flags if not f.startswith("--"))
            longs = {f for f in flags if f.startswith("--")}
            rec = "r" in letters or "R" in letters or "--recursive" in longs
            force = "f" in letters or "--force" in longs
            if rec and force:
                return "deny", "Bloqueado pelo harness: rm -rf não é permitido. Remova arquivos específicos."

        targets = []
        if prog in MUTATING_ANY_ARG:
            targets += paths
        elif prog in MUTATING_DEST and paths:
            targets.append(paths[-1])
        elif prog == "mv":
            targets += paths
        elif prog == "tee":
            targets += paths
        elif prog in INPLACE and any(f.startswith("-i") or f == "--in-place" for f in flags):
            targets += paths
        elif prog == "git" and len(seg) > 1 and seg[1] in ("rm", "mv"):
            targets += [a for a in seg[2:] if not a.startswith("-")]
        elif prog in ("python", "python3", "node", "ruby", "perl", "bash", "sh") and \
                any(f in ("-c", "-e") for f in flags):
            inline = " ".join(args)
            if re.search(r"open\(|write|unlink|remove|rmtree|rename|truncate|>|fs\.", inline):
                for pat in protected + test_paths:
                    root_lit = re.split(r"[*?\[]", pat, maxsplit=1)[0].strip("/")
                    if root_lit and root_lit in inline:
                        return "deny", (f"Bloqueado pelo harness: script inline altera caminho "
                                        f"protegido ({pat}).")
        targets += _redirect_targets(seg)

        for t in targets:
            pat = hits_protected(t)
            if pat:
                return "deny", (f"Bloqueado pelo harness: '{t}' é protegido ({pat}). "
                                "Testes existentes e arquivos do harness não podem ser alterados. "
                                "Para criar um teste NOVO, use a ferramenta Write.")

        if escalated and (targets or prog in MUTATING_ANY_ARG | MUTATING_DEST | {"mv", "tee"}):
            return "deny", ("Escalação ativa: edição autônoma pausada. Mostre o log, proponha a "
                            "correção e aguarde a confirmação do usuário.")

        seg_str = " ".join(seg)
        for rx in INSTALL_PATTERNS:
            if rx.search(seg_str):
                return "ask", ("Nova dependência: o contrato exige aprovação do usuário antes de "
                               "instalar pacotes.")
    return None, None


# --------------------------------------------------------------------------- hooks

def _read_hook_input() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _pretool(decision: str, reason: str) -> None:
    _emit({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                  "permissionDecision": decision,
                                  "permissionDecisionReason": reason}})


def _tool_paths(tool: str, tin: dict) -> list[str]:
    if tool == "NotebookEdit":
        return [tin.get("notebook_path", "")]
    if tool == "MultiEdit" or tool in ("Edit", "Write"):
        return [tin.get("file_path", "")]
    return []


def hook_guard() -> int:
    data = _read_hook_input()
    if _disabled():
        return 0
    root = find_project_root(data.get("cwd"))
    if root is None:
        return 0
    try:
        cfg = load_config(root)
    except ConfigError as e:
        _pretool("deny", f"Harness com configuração inválida — corrija antes de continuar: {e}")
        return 0
    tool = data.get("tool_name", "")
    tin = data.get("tool_input", {}) or {}
    st = load_state(root)
    cwd = data.get("cwd")

    if tool in FILE_TOOLS:
        for fp in _tool_paths(tool, tin):
            if not fp:
                continue
            rel = rel_to_root(root, fp, cwd)
            if rel is None:
                if cfg["allow_writes_outside_project"]:
                    continue
                real = os.path.realpath(os.path.expanduser(fp))
                safe = [os.path.realpath(tempfile.gettempdir()), "/tmp",
                        os.path.realpath(os.path.expanduser("~/.claude"))]
                if any(real == s or real.startswith(s + os.sep) for s in safe):
                    continue
                _pretool("deny", f"Fora do escopo: '{fp}' está fora do projeto.")
                return 0
            if st["escalated"]:
                _pretool("deny", "Escalação ativa: edição autônoma pausada. Mostre o log exato, "
                                 "proponha a correção e aguarde a confirmação do usuário.")
                return 0
            for pat in cfg["protected_paths"]:
                if path_matches(pat, rel):
                    _pretool("deny", f"'{rel}' é protegido pelo harness ({pat}). Só o usuário altera.")
                    return 0
            if cfg["tests_mode"] != "open" and any(path_matches(p, rel) for p in cfg["test_paths"]):
                if cfg["tests_mode"] == "locked":
                    _pretool("deny", f"Testes travados (modo locked): '{rel}' não pode ser criado "
                                     "nem alterado pelo agente.")
                    return 0
                base = _read_json(root / HARNESS_DIR / BASELINE_FILE)
                protected_test = (rel in base.get("files", {})) if isinstance(base, dict) \
                    else (root / rel).exists()
                if protected_test:
                    _pretool("deny", f"'{rel}' é um teste existente: não pode ser alterado. "
                                     "Corrija o código, não o teste. Testes novos são permitidos "
                                     "em arquivos novos.")
                    return 0
            scope = read_scope(root)
            if scope and rel != lessons_rel(cfg) and not any(path_matches(p, rel) for p in scope):
                _pretool("deny", f"'{rel}' está fora do escopo da tarefa (.harness/scope.txt: "
                                 f"{', '.join(scope)}).")
                return 0
            if any(path_matches(p, rel) for p in cfg["dependency_files"]):
                _pretool("ask", f"'{rel}' define dependências: requer aprovação do usuário.")
                return 0
        return 0

    if tool in ("Bash", "PowerShell"):
        decision, reason = analyze_bash(tin.get("command", ""), cfg, root, cwd, st["escalated"])
        if decision:
            _pretool(decision, reason)
        return 0
    return 0


def hook_stop() -> int:
    data = _read_hook_input()
    if _disabled():
        return 0
    root = find_project_root(data.get("cwd"))
    if root is None:
        return 0
    try:
        cfg = load_config(root)
    except ConfigError as e:
        _emit({"systemMessage": f"⚠️ Harness: configuração inválida, verificação não executada: {e}"})
        return 0
    st = load_state(root)
    fp = fingerprint(root)

    if st["escalated"]:
        if not st["escalation_notified"]:
            st["escalation_notified"] = True
            save_state(root, st)
        _emit({"systemMessage": "⛔ Harness: verificação falhando — aguardando sua decisão. "
                                "Responda ao agente para liberar a edição."})
        return 0

    if st["lesson_requested"]:
        st["lesson_requested"] = False  # pede uma única vez; nunca prende o agente em loop
        save_state(root, st)
        if fp == st["reference_fp"]:
            return 0

    if not st["failing"] and fp == st["reference_fp"]:
        return 0  # nada mudou desde a última verificação aprovada

    res = run_checks(root, cfg)
    if res["ok"]:
        recovered = st["failing"] and st.get("fail_log")
        fail_log = st.get("fail_log") or {}
        st.update({"reference_fp": fingerprint(root), "failing": False, "attempts": 0,
                   "last_error_sig": None, "same_error_count": 0, "fail_log": None,
                   "last_result": {"ok": True, "at": _now(), "passed": res["passed"]}})
        names = ", ".join(res["passed"]) or "integridade"
        rel = None
        if recovered:
            rel = append_lesson_stub(root, cfg, fail_log.get("check", "?"),
                                     fail_log.get("count", 1), fail_log.get("evidence", "?"))
        if rel:
            st["lesson_requested"] = True
            save_state(root, st)
            _emit({"decision": "block", "reason": (
                f"✅ Verificação aprovada ({names}) após {fail_log.get('count', 1)} falha(s). "
                f"Antes de encerrar, registre a lição: em {rel}, substitua '{LESSON_PENDING}' na "
                "última linha por UMA frase acionável que evitaria esse erro no futuro (causa → "
                "regra). Não altere a evidência. Depois, encerre normalmente.")})
            return 0
        save_state(root, st)
        _emit({"systemMessage": f"✅ Harness: verificação aprovada ({names})."})
        return 0

    failed = res["failed"]
    sig = error_signature(failed)
    st["same_error_count"] = st["same_error_count"] + 1 if sig == st["last_error_sig"] else 1
    st["last_error_sig"] = sig
    st["attempts"] += 1
    st["failing"] = True
    prev = st.get("fail_log") or {}
    st["fail_log"] = {"check": failed["name"], "count": int(prev.get("count", 0)) + 1,
                      "evidence": prev.get("evidence") or evidence_line(failed["output"])}
    st["last_result"] = {"ok": False, "at": _now(), "check": failed["name"], "code": failed["code"]}
    tail = _tail(failed["output"])
    escalate = (st["same_error_count"] >= cfg["escalate_after_same_error"]
                or st["attempts"] >= cfg["max_attempts"])
    if escalate:
        st["escalated"] = True
        st["escalation_notified"] = False
        save_state(root, st)
        why = (f"o mesmo erro se repetiu {st['same_error_count']}x"
               if st["same_error_count"] >= cfg["escalate_after_same_error"]
               else f"limite de {cfg['max_attempts']} tentativas atingido")
        reason = (
            f"ESCALAÇÃO DO HARNESS ({why}). Pare a edição autônoma — edições estão bloqueadas. "
            "Responda ao usuário agora com: (1) o log de erro exato abaixo, (2) a causa provável, "
            "(3) a correção proposta. Não declare a tarefa concluída. Aguarde a confirmação.\n\n"
            f"Verificação: {failed['name']}\nComando: {failed['cmd']}\nCódigo de saída: {failed['code']}\n"
            f"--- saída ---\n{tail}")
        _emit({"decision": "block", "reason": reason})
        return 0
    save_state(root, st)
    reason = (
        f"Verificação do harness FALHOU (tentativa {st['attempts']}/{cfg['max_attempts']}). "
        "A tarefa NÃO está concluída. Corrija a causa raiz no código; não altere nem delete "
        "testes existentes; não relaxe asserts.\n\n"
        f"Verificação: {failed['name']}\nComando: {failed['cmd']}\nCódigo de saída: {failed['code']}\n"
        f"--- saída ---\n{tail}")
    _emit({"decision": "block", "reason": reason})
    return 0


def hook_session() -> int:
    data = _read_hook_input()
    if _disabled():
        return 0
    root = find_project_root(data.get("cwd"))
    if root is None:
        return 0
    try:
        cfg = load_config(root)
    except ConfigError as e:
        _emit({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                      "additionalContext": f"Harness com configuração inválida: {e}"}})
        return 0
    st = load_state(root)
    if st["reference_fp"] is None and not st["failing"]:
        st["reference_fp"] = fingerprint(root)
        save_state(root, st)
    cmds = "; ".join(f"{v['name']}: `{v['cmd']}`" for v in cfg["verify"]) or "(nenhum)"
    scope = read_scope(root)
    lines = [
        "Este projeto opera sob o contrato istofel-harness (HARNESS.md). Regras executáveis:",
        f"- Ao encerrar, o harness roda automaticamente: {cmds}. Se falhar, você continua até "
        "passar; não declare sucesso antes.",
        f"- Testes ({', '.join(cfg['test_paths'])}) em modo '{cfg['tests_mode']}': testes "
        "existentes nunca são alterados ou deletados.",
        f"- Protegidos (somente usuário): {', '.join(cfg['protected_paths'])}.",
        "- Instalar dependências exige aprovação do usuário.",
        f"- Escalação: {cfg['escalate_after_same_error']} falhas iguais ou "
        f"{cfg['max_attempts']} tentativas → pare, mostre o log, proponha a correção e aguarde.",
    ]
    if scope:
        lines.append(f"- Escopo da tarefa atual: {', '.join(scope)} (fora disso, edição bloqueada).")
    n = int((cfg.get("lessons") or {}).get("inject_last", 5))
    done = [x for x in read_lessons(root, cfg) if not x["pending"]][-n:] if n > 0 else []
    if done:
        lines.append("Lições de falhas anteriores neste projeto (evite repetir):")
        lines += [f"  · [{x['check']}] {x['lesson'][:200]}" for x in done]
    if st["failing"]:
        lines.append("- ATENÇÃO: a última verificação falhou "
                     f"({(st.get('last_result') or {}).get('check', '?')}).")
    if st["escalated"]:
        lines.append("- ESCALAÇÃO ATIVA: aguarde a decisão do usuário antes de editar.")
    _emit({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                  "additionalContext": "\n".join(lines)}})
    return 0


def hook_prompt() -> int:
    data = _read_hook_input()
    if _disabled():
        return 0
    root = find_project_root(data.get("cwd"))
    if root is None:
        return 0
    st = load_state(root)
    if st["escalated"]:
        st.update({"escalated": False, "escalation_notified": False, "attempts": 0,
                   "same_error_count": 0})
        save_state(root, st)
        _emit({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                      "additionalContext": "Harness: escalação encerrada pelo "
                                      "usuário. Siga a instrução dele; a verificação continua "
                                      "obrigatória ao encerrar."}})
    return 0


# --------------------------------------------------------------------------- init

def detect_stack(root: Path) -> dict:
    info = {"node": False, "python": False, "scripts": {}, "pm": "npm", "py_tools": []}
    pkg = _read_json(root / "package.json")
    if isinstance(pkg, dict):
        info["node"] = True
        info["scripts"] = pkg.get("scripts", {}) or {}
        if (root / "pnpm-lock.yaml").exists():
            info["pm"] = "pnpm"
        elif (root / "yarn.lock").exists():
            info["pm"] = "yarn"
        elif (root / "bun.lockb").exists() or (root / "bun.lock").exists():
            info["pm"] = "bun"
    if any((root / f).exists() for f in ("pyproject.toml", "requirements.txt", "setup.py",
                                         "setup.cfg", "Pipfile")):
        info["python"] = True
        txt = ""
        for f in ("pyproject.toml", "requirements.txt", "requirements-dev.txt", "setup.cfg"):
            if (root / f).exists():
                txt += (root / f).read_text(encoding="utf-8", errors="ignore").lower()
        for tool in ("pytest", "ruff", "mypy", "flake8", "black"):
            if tool in txt:
                info["py_tools"].append(tool)
        if (root / "uv.lock").exists():
            info["py_runner"] = "uv run python -m"
        elif (root / ".venv" / "bin" / "python").exists():
            info["py_runner"] = ".venv/bin/python -m"
        elif (root / "venv" / "bin" / "python").exists():
            info["py_runner"] = "venv/bin/python -m"
        elif (root / ".venv" / "Scripts" / "python.exe").exists():
            info["py_runner"] = ".venv/Scripts/python.exe -m"
        else:
            info["py_runner"] = f"{PY_CMD} -m"
    return info


def _run_script(pm: str, script: str) -> str:
    if pm == "npm":
        return "npm test" if script == "test" else f"npm run {script}"
    return f"{pm} run {script}" if pm != "yarn" else f"yarn {script}"


def suggest_verify(info: dict) -> list[dict]:
    out = []
    if info["node"]:
        s = info["scripts"]
        for key in ("lint", "typecheck", "type-check", "check-types"):
            if key in s:
                out.append({"name": "lint" if key == "lint" else "typecheck",
                            "cmd": _run_script(info["pm"], key)})
        test = s.get("test", "")
        if test and "no test specified" not in test:
            cmd = _run_script(info["pm"], "test")
            out.append({"name": "test", "cmd": cmd})
    if info["python"]:
        py = info.get("py_runner", f"{PY_CMD} -m")
        if "ruff" in info["py_tools"]:
            out.append({"name": "lint-py", "cmd": f"{py} ruff check ."})
        if "mypy" in info["py_tools"]:
            out.append({"name": "typecheck-py", "cmd": f"{py} mypy ."})
        if "pytest" in info["py_tools"] or not info["node"]:
            out.append({"name": "test-py", "cmd": f"{py} pytest -q -p no:cacheprovider"})
    return out


def suggest_test_paths(root: Path, info: dict) -> list[str]:
    paths = []
    for d in ("tests", "test", "__tests__", "spec", "e2e"):
        if (root / d).is_dir():
            paths.append(f"{d}/**")
    if info["node"]:
        paths += ["**/*.test.*", "**/*.spec.*"]
    if info["python"] and not any(p.startswith("tests") for p in paths):
        paths += ["**/test_*.py", "**/*_test.py"]
    return paths or ["tests/**"]


CONTRACT_TEMPLATE = """# Contrato Operacional

## Entrega
- (preencher)

## Critérios de Aceitação
- (preencher) — evidência: `comando ou teste`

## Limites
- (preencher: fora de escopo, ADRs e invariantes que o agente tentaria violar)

## Ferramentas
- (nenhum comando fora do padrão)

## Protocolo de Verificação
{verify_list}

## Escalação
- Mesmo erro {same}x **ou** {max_attempts} tentativas → parar, mostrar o log, propor a correção e aguardar
"""

CLAUDE_IMPORT_BLOCK = """
## Contrato
@HARNESS.md
"""

GITHUB_WORKFLOW = """name: harness
on:
  push:
  pull_request:
jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
{setup_steps}      - name: Verificação do contrato (integridade + comandos)
        run: python3 .harness/harness.py verify --ci
"""


def _workflow_setup(info: dict) -> str:
    s = ""
    if True:  # python é necessário para rodar .harness/harness.py
        s += ("      - uses: actions/setup-python@v5\n"
              "        with:\n          python-version: '3.12'\n")
    if info["node"]:
        s += ("      - uses: actions/setup-node@v4\n"
              "        with:\n          node-version: '20'\n")
        if info["pm"] == "pnpm":
            s += "      - run: npm install -g pnpm\n      - run: pnpm install --frozen-lockfile\n"
        elif info["pm"] == "yarn":
            s += "      - run: yarn install --frozen-lockfile\n"
        elif info["pm"] == "bun":
            s += "      - run: npm install -g bun\n      - run: bun install\n"
        else:
            s += "      - run: npm ci\n"
    if info["python"]:
        if info.get("py_runner", "").startswith("uv "):
            s += ("      - run: |\n"
                  "          pip install uv\n"
                  "          uv sync --all-extras || uv sync\n")
        else:
            s += ("      - run: |\n"
                  "          python -m venv .venv\n"
                  "          echo \"$PWD/.venv/bin\" >> \"$GITHUB_PATH\"\n"
                  "          .venv/bin/pip install --upgrade pip\n"
                  "          if [ -f requirements.txt ]; then .venv/bin/pip install -r requirements.txt; fi\n"
                  "          if [ -f requirements-dev.txt ]; then .venv/bin/pip install -r requirements-dev.txt; fi\n"
                  "          if [ -f pyproject.toml ]; then .venv/bin/pip install -e '.[dev]' || .venv/bin/pip install -e . || true; fi\n"
                  "          .venv/bin/pip install pytest\n")
    return s


def _edit_rule(pattern: str) -> str:
    p = norm_rel(pattern)
    return f"Edit(/{p})"


def _sandbox_path(pattern: str) -> str | None:
    p = norm_rel(pattern)
    p = re.sub(r"/\*\*$", "", p)
    if re.search(r"[*?\[]", p):
        return None  # Linux ignora curingas em denyWrite
    return "./" + p


def merge_settings(root: Path, cfg: dict, sandbox: bool) -> Path:
    p = root / ".claude" / "settings.json"
    data = _read_json(p, {}) or {}
    perms = data.setdefault("permissions", {})
    deny = perms.setdefault("deny", [])
    ask = perms.setdefault("ask", [])
    want_deny = [_edit_rule(x) for x in cfg["protected_paths"]]
    if cfg["tests_mode"] == "locked":
        want_deny += [_edit_rule(x) for x in cfg["test_paths"]]
    want_deny += ["Bash(rm -rf *)", "Bash(rm -fr *)", "Bash(git push --force *)",
                  "Bash(git push -f *)", "Bash(git reset --hard *)", "Bash(sudo *)"]
    for r in want_deny:
        if r not in deny:
            deny.append(r)
    for r in ["Bash(git push *)"]:
        if r not in ask:
            ask.append(r)
    env = data.setdefault("env", {})
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    if sandbox:
        sb = data.setdefault("sandbox", {})
        sb["enabled"] = True
        fs = sb.setdefault("filesystem", {})
        dw = fs.setdefault("denyWrite", [])
        for pat in cfg["test_paths"]:
            sp = _sandbox_path(pat)
            if sp and sp not in dw:
                dw.append(sp)
    _write_json(p, data)
    return p


def cmd_init(a) -> int:
    root = Path(a.root).resolve()
    hd = root / HARNESS_DIR
    cfg_path = hd / CONFIG_FILE
    if cfg_path.exists() and not a.force:
        print(f"Harness já inicializado em {root}. Use --force para reconfigurar.")
        return 1
    info = detect_stack(root)
    verify = []
    for spec in a.verify or []:
        m = re.match(r"^([\w.-]+)=(.+)$", spec.strip())
        if m:
            name, cmd = m.group(1), m.group(2)
        else:
            cmd = spec.strip()
            name = cmd.split()[-1] if cmd.split() else cmd
        verify.append({"name": name.strip(), "cmd": cmd.strip()})
    if not verify:
        verify = suggest_verify(info)
    test_paths = a.test_paths or suggest_test_paths(root, info)
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({"verify": verify, "test_paths": test_paths, "tests_mode": a.tests_mode,
                "max_attempts": a.max_attempts, "escalate_after_same_error": a.same_error})
    if not verify:
        print("AVISO: nenhum comando de verificação detectado. Informe com --verify 'nome=comando'.")
    hd.mkdir(parents=True, exist_ok=True)
    _write_json(cfg_path, cfg)
    (hd / ".gitignore").write_text("state/\n", encoding="utf-8")
    shutil.copyfile(Path(__file__).resolve(), hd / "harness.py")
    _write_json(hd / BASELINE_FILE, compute_baseline(root, cfg))

    created = [str(cfg_path.relative_to(root)), ".harness/harness.py", ".harness/baseline.json"]
    contract = root / CONTRACT_FILE
    if not contract.exists():
        vlist = "\n".join(f"{i}. {v['name']}: `{v['cmd']}`" for i, v in enumerate(verify, 1)) \
            or "1. (defina com --verify)"
        contract.write_text(CONTRACT_TEMPLATE.format(
            verify_list=vlist, same=a.same_error, max_attempts=a.max_attempts), encoding="utf-8")
        created.append(CONTRACT_FILE)

    claude_md = root / "CLAUDE.md"
    existing = claude_md.read_text(encoding="utf-8") if claude_md.exists() else ""
    if "@HARNESS.md" not in existing:
        prefix = existing if existing else "# CLAUDE.md\n"
        claude_md.write_text(prefix.rstrip("\n") + "\n" + CLAUDE_IMPORT_BLOCK, encoding="utf-8")
        created.append("CLAUDE.md (import @HARNESS.md)")

    sp = merge_settings(root, cfg, a.sandbox)
    created.append(str(sp.relative_to(root)))

    if a.ci == "github":
        wf = root / ".github" / "workflows" / "harness.yml"
        if not wf.exists() or a.force:
            wf.parent.mkdir(parents=True, exist_ok=True)
            wf.write_text(GITHUB_WORKFLOW.format(setup_steps=_workflow_setup(info)), encoding="utf-8")
            created.append(".github/workflows/harness.yml")

    st = load_state(root)
    st.update({"reference_fp": fingerprint(root), "failing": False, "attempts": 0,
               "escalated": False, "same_error_count": 0, "last_error_sig": None})
    save_state(root, st)

    print("Harness inicializado:")
    for c in created:
        print(f"  + {c}")
    print(f"Verificação: {', '.join(v['cmd'] for v in verify) or '(nenhuma)'}")
    print(f"Testes protegidos ({a.tests_mode}): {', '.join(test_paths)} "
          f"— {len(_read_json(hd / BASELINE_FILE)['files'])} arquivo(s) na baseline")
    return 0


# --------------------------------------------------------------------------- auditoria

def _word_count(text: str) -> int:
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    return len(re.findall(r"[\wÀ-ÿ@`./-]+", text))


def _cmd_resolvable(root: Path, cmd: str, info: dict) -> tuple[bool, str]:
    toks = cmd.split()
    if not toks:
        return False, "comando vazio"
    if toks[0] in ("npm", "pnpm", "yarn", "bun"):
        script = None
        if toks[0] == "npm" and len(toks) > 1 and toks[1] in ("test", "t"):
            script = "test"
        elif len(toks) > 2 and toks[1] == "run":
            script = toks[2]
        elif toks[0] in ("pnpm", "yarn", "bun") and len(toks) > 1 and toks[1] not in ("run", "exec", "dlx"):
            script = toks[1]
        if script:
            s = info["scripts"].get(script)
            if not s:
                return False, f"script '{script}' não existe no package.json"
            if "no test specified" in s:
                return False, f"script '{script}' é o placeholder padrão do npm"
        return True, ""
    if toks[0] in ("python", "python3") and len(toks) > 2 and toks[1] == "-m":
        return True, ""
    if shutil.which(toks[0]) or (root / toks[0]).exists():
        return True, ""
    return False, f"executável '{toks[0]}' não encontrado no PATH"


def cmd_audit(a) -> int:
    root = Path(a.root).resolve()
    results = []  # (nível, ok, mensagem, correção)

    def add(level, ok, msg, fix=""):
        results.append((level, ok, msg, fix))

    info = detect_stack(root)
    add("crítico", _is_git(root), "Repositório Git (seguro para desfazer erros)", "git init && git add -A && git commit -m 'base'")
    contract = root / CONTRACT_FILE
    add("crítico", contract.is_file(), "HARNESS.md (contrato) existe", "rode /istofel-dev:istofel-harness")
    cm = root / "CLAUDE.md"
    imported = cm.is_file() and "@HARNESS.md" in cm.read_text(encoding="utf-8", errors="ignore")
    add("crítico", imported, "CLAUDE.md importa @HARNESS.md (o agente lê o contrato)",
        "adicione a linha '@HARNESS.md' ao CLAUDE.md")
    cfg = None
    try:
        if (root / HARNESS_DIR / CONFIG_FILE).is_file():
            cfg = load_config(root)
            add("crítico", True, ".harness/config.json válido")
        else:
            add("crítico", False, ".harness/config.json existe", "rode o init do harness")
    except ConfigError as e:
        add("crítico", False, f".harness/config.json inválido: {e}", "corrija o JSON")
    if cfg:
        add("crítico", bool(cfg["verify"]), "Há comandos de verificação executáveis",
            "adicione itens em 'verify'")
        for v in cfg["verify"]:
            ok, why = _cmd_resolvable(root, v["cmd"], info)
            add("crítico", ok, f"Comando '{v['cmd']}' resolve" + (f" — {why}" if why else ""),
                "ajuste o comando ou crie o script")
        tf = list_test_files(root, cfg["test_paths"])
        add("alto", bool(tf), f"Arquivos de teste encontrados ({len(tf)})",
            "escreva testes a partir dos critérios de aceitação")
        base = root / HARNESS_DIR / BASELINE_FILE
        add("alto", base.is_file(), "Baseline de integridade dos testes existe",
            f"{PY_CMD} .harness/harness.py approve-tests")
        if base.is_file():
            viol = integrity_violations(root, cfg)
            add("alto", not viol, "Testes batem com a baseline" + ("" if not viol else ": " + "; ".join(viol[:3])),
                "reverta (git checkout -- <arquivo>) ou aprove: approve-tests")
        gi = root / HARNESS_DIR / ".gitignore"
        add("médio", gi.is_file() and "state" in gi.read_text(), ".harness/state fora do Git",
            "crie .harness/.gitignore com a linha state/")
    if cm.is_file():
        ctext = cm.read_text(encoding="utf-8", errors="ignore")
        words = _word_count(ctext)
        add("médio", words <= 200, f"CLAUDE.md enxuto ({words} palavras; teto 200)",
            "corte o que o agente descobre no código: árvore, stack, env vars, comandos padrão")
        tree = bool(re.search(r"[├└]──", ctext))
        add("médio", not tree, "CLAUDE.md sem árvore de diretórios (descobrível no código)",
            "remova a árvore; aponte para docs/spec.md em uma linha")
    if contract.is_file():
        text = contract.read_text(encoding="utf-8", errors="ignore")
        add("médio", "(preencher)" not in text, "Contrato sem seções pendentes '(preencher)'",
            "preencha Entrega, Critérios de Aceitação e Limites")
        hw = _word_count(text)
        add("médio", hw <= 300, f"HARNESS.md enxuto ({hw} palavras; teto 300 — é carregado em toda sessão)",
            "mantenha só o que o agente erraria sem; regras genéricas já vêm do hook")
        add("médio", bool(re.search(r"^##\s*Ferramentas", text, re.M | re.I)),
            "Guia de ferramentas definido (seção 'Ferramentas', mesmo que vazia)",
            "adicione '## Ferramentas' com comandos fora do padrão (ou '(nenhum comando fora do padrão)')")
        mapa = bool(re.search(r"^##\s*Mapa", text, re.M | re.I)) or bool(re.search(r"[├└]──", text))
        add("médio", not mapa, "HARNESS.md sem mapa/árvore do projeto (descobrível no código)",
            "remova a seção Mapa; a SPEC já documenta a estrutura")
        secs = re.findall(r"##\s*Protocolo de Verifica[çc][ãa]o[^\n]*\n(.*?)(?=\n##\s|\Z)", text, re.S | re.I)
        if secs:
            vague = [ln.strip() for sec in secs for ln in sec.splitlines()
                     if re.match(r"\s*(\d+\.|-)\s", ln) and "`" not in ln]
            add("alto", not vague, "Toda etapa de verificação é um comando executável"
                + ("" if not vague else f" — vagas: {vague[:2]}"),
                "troque etapas subjetivas por comandos (ex.: `npm run validate:schema`)")
        if cfg:
            nums = {int(n) for n in re.findall(r"(\d+)\s*(?:x\b|vezes|tentativas)", text, re.I)}
            allowed = {cfg["max_attempts"], cfg["escalate_after_same_error"]}
            contra = sorted(nums - allowed)
            add("alto", not contra, "Contrato e config sem números contraditórios"
                + ("" if not contra else f" — HARNESS.md cita {contra}, config usa {sorted(allowed)}"),
                "alinhe os números no HARNESS.md com .harness/config.json")
    if cfg and lessons_rel(cfg):
        lessons = read_lessons(root, cfg)
        pend = [x for x in lessons if x["pending"]]
        add("médio", not pend, f"Lições registradas ({len(lessons)}; pendentes: {len(pend)})",
            f"complete as lições '(pendente)' em {lessons_rel(cfg)}")
        counts = {}
        for x in lessons:
            counts[x["check"]] = counts.get(x["check"], 0) + 1
        rec = sorted(k for k, v in counts.items() if v >= 2)
        add("baixo", not rec, "Nenhuma falha recorrente sem regra no CLAUDE.md"
            + ("" if not rec else f" — recorrentes: {', '.join(rec)}"),
            "promova a lição recorrente a uma linha em NUNCA/Regras Não Óbvias do CLAUDE.md")
    st = _read_json(root / ".claude" / "settings.json", {}) or {}
    deny = (st.get("permissions") or {}).get("deny", [])
    add("alto", any("rm -rf" in d for d in deny), "settings.json bloqueia rm -rf", "rode o init")
    add("alto", any(".harness" in d for d in deny), "settings.json protege .harness/", "rode o init")
    add("médio", bool((st.get("sandbox") or {}).get("enabled")),
        "Sandbox nativo ativo (bloqueio no nível do SO)", "init --sandbox (macOS/Linux)")
    wf = root / ".github" / "workflows"
    has_ci = wf.is_dir() and any("harness.py verify" in f.read_text(errors="ignore")
                                 for f in wf.glob("*.y*ml"))
    add("médio", has_ci, "CI roda a verificação fora da máquina do agente", "init --ci github")

    icon = {True: "✅", False: "❌"}
    print(f"Auditoria do harness — {root}\n")
    crit_fail = 0
    for level, ok, msg, fix in results:
        print(f"{icon[ok]} [{level}] {msg}")
        if not ok:
            if fix:
                print(f"     ↳ correção: {fix}")
            if level == "crítico":
                crit_fail += 1
    total_fail = sum(1 for r in results if not r[1])
    print(f"\nResultado: {len(results) - total_fail}/{len(results)} ok · {crit_fail} falha(s) crítica(s)")
    return 1 if crit_fail else 0


# --------------------------------------------------------------------------- CLI usuário

def _root_or_die(a) -> Path:
    root = find_project_root(a.root) if a.root else find_project_root()
    if root is None:
        print("Harness não encontrado (sem .harness/config.json). Rode o init primeiro.")
        sys.exit(2)
    return root


def cmd_status(a) -> int:
    root = _root_or_die(a)
    cfg = load_config(root)
    st = load_state(root)
    print(f"Projeto: {root}")
    print(f"Verificação: {', '.join(v['cmd'] for v in cfg['verify']) or '(nenhuma)'}")
    print(f"Testes: {cfg['tests_mode']} · {', '.join(cfg['test_paths'])}")
    print(f"Escopo da tarefa: {', '.join(read_scope(root)) or '(livre)'}")
    print(f"Estado: {'FALHANDO' if st['failing'] else 'ok'} · tentativas {st['attempts']}/"
          f"{cfg['max_attempts']} · escalação {'ATIVA' if st['escalated'] else 'não'}")
    if st.get("last_result"):
        print(f"Última verificação: {json.dumps(st['last_result'], ensure_ascii=False)}")
    viol = integrity_violations(root, cfg)
    print("Integridade dos testes: " + ("ok" if not viol else "; ".join(viol)))
    return 0


def cmd_verify(a) -> int:
    root = _root_or_die(a)
    cfg = load_config(root)
    res = run_checks(root, cfg, stream=True)
    if res["ok"]:
        print(f"\n✅ Verificação aprovada: {', '.join(res['passed']) or 'integridade'}")
        if not a.ci:
            st = load_state(root)
            st.update({"reference_fp": fingerprint(root), "failing": False, "attempts": 0,
                       "same_error_count": 0, "last_error_sig": None, "fail_log": None,
                       "last_result": {"ok": True, "at": _now(), "passed": res["passed"]}})
            save_state(root, st)
        return 0
    f = res["failed"]
    print(f"\n❌ Falhou: {f['name']} (código {f['code']})\n{_tail(f['output'], 40)}")
    return 1


def cmd_approve(a) -> int:
    root = _root_or_die(a)
    cfg = load_config(root)
    old = (_read_json(root / HARNESS_DIR / BASELINE_FILE, {}) or {}).get("files", {})
    new = compute_baseline(root, cfg)
    _write_json(root / HARNESS_DIR / BASELINE_FILE, new)
    added = sorted(set(new["files"]) - set(old))
    removed = sorted(set(old) - set(new["files"]))
    changed = sorted(k for k in set(old) & set(new["files"]) if old[k] != new["files"][k])
    print(f"Baseline atualizada: {len(new['files'])} teste(s).")
    for label, items in (("novos", added), ("removidos", removed), ("alterados", changed)):
        if items:
            print(f"  {label}: {', '.join(items)}")
    return 0


def cmd_scope(a) -> int:
    root = _root_or_die(a)
    p = root / HARNESS_DIR / SCOPE_FILE
    if a.clear or not a.patterns:
        if p.exists():
            p.unlink()
        print("Escopo da tarefa liberado.")
        return 0
    p.write_text("\n".join(a.patterns) + "\n", encoding="utf-8")
    print(f"Escopo da tarefa: {', '.join(a.patterns)}")
    return 0


def cmd_reset(a) -> int:
    root = _root_or_die(a)
    st = load_state(root)
    st.update({"failing": False, "attempts": 0, "escalated": False, "escalation_notified": False,
               "same_error_count": 0, "last_error_sig": None, "reference_fp": fingerprint(root),
               "fail_log": None, "lesson_requested": False})
    save_state(root, st)
    print("Estado do harness zerado.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="harness", description="istofel-harness " + VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for h in ("hook-guard", "hook-stop", "hook-session", "hook-prompt"):
        sub.add_parser(h)
    p = sub.add_parser("init", help="configura o harness no projeto")
    p.add_argument("--root", default=".")
    p.add_argument("--verify", action="append", help="'nome=comando' (repetível)")
    p.add_argument("--test-paths", nargs="+")
    p.add_argument("--tests-mode", default="append-only", choices=["append-only", "locked", "open"])
    p.add_argument("--max-attempts", type=int, default=4)
    p.add_argument("--same-error", type=int, default=2)
    p.add_argument("--ci", default="github", choices=["github", "none"])
    p.add_argument("--sandbox", action="store_true")
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("audit", help="audita o harness do projeto")
    p.add_argument("--root", default=".")
    for name in ("status", "approve-tests", "reset"):
        p = sub.add_parser(name)
        p.add_argument("--root", default=None)
    p = sub.add_parser("verify")
    p.add_argument("--root", default=None)
    p.add_argument("--ci", action="store_true")
    p = sub.add_parser("scope", help="restringe os arquivos editáveis na tarefa")
    p.add_argument("patterns", nargs="*")
    p.add_argument("--clear", action="store_true")
    p.add_argument("--root", default=None)
    a = ap.parse_args(argv)
    table = {
        "hook-guard": lambda: hook_guard(), "hook-stop": lambda: hook_stop(),
        "hook-session": lambda: hook_session(), "hook-prompt": lambda: hook_prompt(),
        "init": lambda: cmd_init(a), "audit": lambda: cmd_audit(a),
        "status": lambda: cmd_status(a), "verify": lambda: cmd_verify(a),
        "approve-tests": lambda: cmd_approve(a), "scope": lambda: cmd_scope(a),
        "reset": lambda: cmd_reset(a),
    }
    if a.cmd.startswith("hook-"):
        try:
            return table[a.cmd]()
        except Exception as e:  # nunca derrubar a sessão; guard falha fechado
            if a.cmd == "hook-guard":
                _pretool("deny", f"Harness: erro interno no guard ({type(e).__name__}: {e}). "
                                 "Defina HARNESS_DISABLE=1 para contornar e reporte.")
            else:
                _emit({"systemMessage": f"⚠️ Harness: erro interno em {a.cmd}: {type(e).__name__}: {e}"})
            return 0
    return table[a.cmd]()


if __name__ == "__main__":
    sys.exit(main())
