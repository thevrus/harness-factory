"""Ecosystem, command, workspace, agent and default-branch detection.

All detection is static: manifests are parsed as text (Python 3.9 has no
tomllib), nothing is executed. `commands` is a best guess the advance skill
shows the user to confirm.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

COMMAND_KEYS = ("setup", "build", "test", "lint", "typecheck", "run")

# npm's placeholder test script is not a real test command.
NPM_PLACEHOLDER = "no test specified"


# ---------------------------------------------------------------- tiny parsers
def toml_section(text: str, name: str) -> Optional[str]:
    """Body of a [name] table, up to the next table header."""
    m = re.search(r"(?m)^\[" + re.escape(name) + r"\]\s*$", text)
    if not m:
        return None
    rest = text[m.end():]
    n = re.search(r"(?m)^\[", rest)
    return rest[: n.start()] if n else rest


def toml_array(body: Optional[str], key: str) -> List[str]:
    if not body:
        return []
    m = re.search(r"(?ms)^\s*" + re.escape(key) + r"\s*=\s*\[(.*?)\]", body)
    if not m:
        return []
    return re.findall(r"[\"']([^\"']+)[\"']", m.group(1))


def make_targets(text: str) -> List[str]:
    targets = []
    for m in re.finditer(r"(?m)^([A-Za-z0-9][A-Za-z0-9_.\-/]*)\s*:(?!=)", text):
        t = m.group(1)
        if not t.startswith(".") and t not in targets:
            targets.append(t)
    for m in re.finditer(r"(?m)^\.PHONY\s*:\s*(.+)$", text):
        for t in m.group(1).split():
            if t not in targets:
                targets.append(t)
    return targets


def just_recipes(text: str) -> List[str]:
    out = []
    for m in re.finditer(r"(?m)^@?([A-Za-z0-9][A-Za-z0-9_\-]*)(?:\s+[^:=\n]*)?:(?!=)", text):
        if m.group(1) not in ("set", "export", "import", "mod", "alias") and m.group(1) not in out:
            out.append(m.group(1))
    return out


def npm_scripts(ctx, rel: str = "package.json") -> Dict[str, str]:
    data, _ = ctx.load_json(rel)
    if isinstance(data, dict) and isinstance(data.get("scripts"), dict):
        return {k: v for k, v in data["scripts"].items() if isinstance(v, str)}
    return {}


def _first(d: Dict[str, str], *names: str) -> Optional[str]:
    for n in names:
        if n in d:
            return n
    return None


# ---------------------------------------------------------------- ecosystems
def node_pm(ctx) -> str:
    data, _ = ctx.load_json("package.json")
    if isinstance(data, dict) and isinstance(data.get("packageManager"), str):
        name = data["packageManager"].split("@", 1)[0]
        if name in ("npm", "pnpm", "yarn", "bun"):
            return name
    if ctx.isfile("pnpm-lock.yaml"):
        return "pnpm"
    if ctx.isfile("yarn.lock"):
        return "yarn"
    if ctx.isfile("bun.lockb") or ctx.isfile("bun.lock"):
        return "bun"
    return "npm"


def python_manager(ctx, pyproject: str) -> str:
    if ctx.isfile("uv.lock") or "[tool.uv" in pyproject:
        return "uv"
    if ctx.isfile("poetry.lock") or "[tool.poetry" in pyproject:
        return "poetry"
    if ctx.isfile("Pipfile"):
        return "pipenv"
    if ctx.isfile("pdm.lock") or "[tool.pdm" in pyproject:
        return "pdm"
    return "pip"


def _python_text(ctx) -> str:
    """Concatenated Python dependency/config text used for tool sniffing."""
    parts = []
    for rel in ("pyproject.toml", "setup.cfg", "tox.ini", "Pipfile", "setup.py"):
        t = ctx.read_text(rel)
        if t:
            parts.append(t)
    for f in ctx.files:
        if "/" not in f and re.match(r"requirements.*\.(txt|in)$", f):
            parts.append(ctx.read_text(f) or "")
    return "\n".join(parts)


def detect_ecosystems(ctx) -> Tuple[List[str], Dict[str, str]]:
    eco: List[str] = []
    pms: Dict[str, str] = {}
    if ctx.isfile("package.json"):
        eco.append("node")
        pms["node"] = node_pm(ctx)
    pyproject = ctx.read_text("pyproject.toml") or ""
    if (pyproject or ctx.isfile("setup.py") or ctx.isfile("setup.cfg") or ctx.isfile("Pipfile")
            or any("/" not in f and re.match(r"requirements.*\.txt$", f) for f in ctx.files)):
        eco.append("python")
        pms["python"] = python_manager(ctx, pyproject)
    if ctx.isfile("go.mod"):
        eco.append("go")
    if ctx.isfile("Cargo.toml"):
        eco.append("rust")
    if ctx.isfile("Gemfile"):
        eco.append("ruby")
        pms["ruby"] = "bundler"
    if ctx.isfile("pom.xml"):
        eco.append("java")
        pms["java"] = "maven"
    if any(ctx.isfile(f) for f in ("build.gradle", "build.gradle.kts", "settings.gradle",
                                   "settings.gradle.kts")):
        if "java" not in eco:
            eco.append("java")
        pms["java"] = "gradle" if "java" not in pms else pms["java"] + "+gradle"
        if ctx.isfile("build.gradle.kts") or any(f.endswith(".kt") for f in ctx.files[:5000]):
            eco.append("kotlin")
    if ctx.isfile("composer.json"):
        eco.append("php")
        pms["php"] = "composer"
    if ctx.find_ci("Makefile", "GNUmakefile", "makefile"):
        eco.append("make")
    if ctx.find_ci("justfile", ".justfile"):
        eco.append("just")
    return eco, pms


# ---------------------------------------------------------------- commands
def _node_commands(ctx, pm: str) -> Dict[str, str]:
    s = npm_scripts(ctx)
    run = {"npm": "npm run", "pnpm": "pnpm run", "yarn": "yarn run", "bun": "bun run"}[pm]
    berry = ctx.isfile(".yarnrc.yml")
    setup = {"npm": "npm ci" if ctx.isfile("package-lock.json") else "npm install",
             "pnpm": "pnpm install --frozen-lockfile",
             "yarn": "yarn install --immutable" if berry else "yarn install --frozen-lockfile",
             "bun": "bun install --frozen-lockfile"}[pm]
    out = {"setup": setup}
    if "test" in s and NPM_PLACEHOLDER not in s["test"]:
        # `bun test` runs bun's own runner, not the script.
        out["test"] = "bun run test" if pm == "bun" else "%s test" % pm
    if "build" in s:
        out["build"] = "%s build" % run
    lint = _first(s, "lint", "eslint", "check:lint")
    if lint:
        out["lint"] = "%s %s" % (run, lint)
    tc = _first(s, "typecheck", "type-check", "types", "check-types", "tsc", "check:types")
    if tc:
        out["typecheck"] = "%s %s" % (run, tc)
    elif ctx.isfile("tsconfig.json"):
        out["typecheck"] = {"npm": "npx", "pnpm": "pnpm exec", "yarn": "yarn exec",
                            "bun": "bunx"}[pm] + " tsc --noEmit"
    r = _first(s, "dev", "start", "serve")
    if r:
        out["run"] = "%s start" % pm if (r == "start" and pm != "bun") else "%s %s" % (run, r)
    return out


def _python_commands(ctx, mgr: str) -> Dict[str, str]:
    pre = {"uv": "uv run ", "poetry": "poetry run ", "pipenv": "pipenv run ",
           "pdm": "pdm run "}.get(mgr, "")
    txt = _python_text(ctx)
    low = txt.lower()
    out: Dict[str, str] = {}
    if mgr == "uv":
        out["setup"] = "uv sync"
    elif mgr == "poetry":
        out["setup"] = "poetry install"
    elif mgr == "pipenv":
        out["setup"] = "pipenv install --dev"
    elif mgr == "pdm":
        out["setup"] = "pdm install"
    elif ctx.isfile("requirements-dev.txt"):
        out["setup"] = "pip install -r requirements-dev.txt"
    elif ctx.isfile("requirements.txt"):
        out["setup"] = "pip install -r requirements.txt"
    elif ctx.isfile("pyproject.toml") or ctx.isfile("setup.py"):
        out["setup"] = "pip install -e ."
    if ("pytest" in low or ctx.isfile("pytest.ini") or ctx.isfile("conftest.py")
            or ctx.isfile("tests/conftest.py")):
        out["test"] = pre + "pytest"
    elif ctx.isfile("tox.ini"):
        out["test"] = "tox"
    elif ctx.isfile("noxfile.py"):
        out["test"] = "nox"
    elif any(re.search(r"(^|/)tests?/test_[^/]*\.py$", f) for f in ctx.files):
        out["test"] = pre + "python -m unittest discover"
    if "ruff" in low or ctx.isfile("ruff.toml") or ctx.isfile(".ruff.toml"):
        out["lint"] = pre + "ruff check ."
    elif "flake8" in low or ctx.isfile(".flake8"):
        out["lint"] = pre + "flake8"
    elif "pylint" in low:
        out["lint"] = pre + "pylint ."
    if "mypy" in low or ctx.isfile("mypy.ini") or ctx.isfile(".mypy.ini"):
        out["typecheck"] = pre + "mypy ."
    elif "pyright" in low or ctx.isfile("pyrightconfig.json"):
        out["typecheck"] = pre + "pyright"
    pyproject = ctx.read_text("pyproject.toml") or ""
    if toml_section(pyproject, "build-system") is not None:
        out["build"] = "uv build" if mgr == "uv" else (
            "poetry build" if mgr == "poetry" else "python -m build")
    scripts = toml_section(pyproject, "project.scripts")
    if scripts:
        m = re.search(r"(?m)^\s*([A-Za-z0-9_.\-]+)\s*=", scripts)
        if m:
            out["run"] = pre + m.group(1)
    return out


def _go_commands(ctx) -> Dict[str, str]:
    out = {"setup": "go mod download", "build": "go build ./...", "test": "go test ./...",
           "typecheck": "go vet ./..."}
    if any(ctx.isfile(f) for f in (".golangci.yml", ".golangci.yaml", ".golangci.toml")):
        out["lint"] = "golangci-lint run"
    else:
        out["lint"] = "go vet ./..."
    if ctx.isfile("main.go"):
        out["run"] = "go run ."
    else:
        cmds = sorted({f.split("/")[1] for f in ctx.files
                       if f.startswith("cmd/") and f.endswith("main.go") and f.count("/") == 2})
        if len(cmds) == 1:
            out["run"] = "go run ./cmd/%s" % cmds[0]
    return out


def _rust_commands(ctx) -> Dict[str, str]:
    out = {"setup": "cargo fetch", "build": "cargo build", "test": "cargo test",
           "lint": "cargo clippy -- -D warnings", "typecheck": "cargo check"}
    if ctx.isfile("src/main.rs"):
        out["run"] = "cargo run"
    return out


def _ruby_commands(ctx) -> Dict[str, str]:
    gem = (ctx.read_text("Gemfile") or "").lower()
    rails = "rails" in gem and ctx.isfile("bin/rails")
    out = {"setup": "bundle install"}
    if ctx.isdir("spec") or "rspec" in gem:
        out["test"] = "bundle exec rspec"
    elif rails:
        out["test"] = "bin/rails test"
    elif ctx.isfile("Rakefile"):
        out["test"] = "bundle exec rake test"
    if ctx.isfile(".rubocop.yml") or "rubocop" in gem:
        out["lint"] = "bundle exec rubocop"
    elif ctx.isfile(".standard.yml") or "standard" in gem:
        out["lint"] = "bundle exec standardrb"
    if ctx.isfile("sorbet/config"):
        out["typecheck"] = "bundle exec srb tc"
    elif ctx.isfile("Steepfile"):
        out["typecheck"] = "bundle exec steep check"
    if rails:
        out["run"] = "bin/rails server"
    return out


def _java_commands(ctx, pm: str) -> Dict[str, str]:
    if pm.startswith("maven"):
        mvn = "./mvnw" if ctx.isfile("mvnw") else "mvn"
        return {"setup": "%s -B dependency:resolve" % mvn, "build": "%s -B package -DskipTests" % mvn,
                "test": "%s -B test" % mvn}
    g = "./gradlew" if ctx.isfile("gradlew") else "gradle"
    out = {"build": "%s assemble" % g, "test": "%s test" % g}
    text = (ctx.read_text("build.gradle") or "") + (ctx.read_text("build.gradle.kts") or "")
    for plugin, task in (("detekt", "detekt"), ("ktlint", "ktlintCheck"),
                         ("spotless", "spotlessCheck"), ("checkstyle", "checkstyleMain")):
        if plugin in text:
            out["lint"] = "%s %s" % (g, task)
            break
    return out


def _php_commands(ctx) -> Dict[str, str]:
    data, _ = ctx.load_json("composer.json")
    scripts = data.get("scripts", {}) if isinstance(data, dict) else {}
    scripts = scripts if isinstance(scripts, dict) else {}
    out = {"setup": "composer install"}
    if "test" in scripts:
        out["test"] = "composer test"
    elif ctx.isfile("phpunit.xml") or ctx.isfile("phpunit.xml.dist"):
        out["test"] = "vendor/bin/phpunit"
    elif ctx.isfile("pest.xml") or ctx.isfile("tests/Pest.php"):
        out["test"] = "vendor/bin/pest"
    if "lint" in scripts:
        out["lint"] = "composer lint"
    elif ctx.isfile("phpcs.xml") or ctx.isfile("phpcs.xml.dist"):
        out["lint"] = "vendor/bin/phpcs"
    if ctx.isfile("phpstan.neon") or ctx.isfile("phpstan.neon.dist"):
        out["typecheck"] = "vendor/bin/phpstan analyse"
    elif ctx.isfile("psalm.xml") or ctx.isfile("psalm.xml.dist"):
        out["typecheck"] = "vendor/bin/psalm"
    return out


# Wrapper targets win over ecosystem guesses: they are the repo's own entry points.
WRAPPER_NAMES = {
    "setup": ("setup", "bootstrap", "install", "deps"),
    "build": ("build",),
    "test": ("test", "tests", "check"),
    "lint": ("lint",),
    "typecheck": ("typecheck", "type-check", "types", "check-types", "mypy"),
    "run": ("run", "dev", "start", "serve"),
}


def _wrapper_commands(ctx) -> Tuple[Dict[str, str], Dict[str, str]]:
    out: Dict[str, str] = {}
    src: Dict[str, str] = {}
    mk = ctx.find_ci("Makefile", "GNUmakefile", "makefile")
    jf = ctx.find_ci("justfile", ".justfile")
    for tool, rel, names in (("make", mk, make_targets(ctx.read_text(mk) or "") if mk else []),
                             ("just", jf, just_recipes(ctx.read_text(jf) or "") if jf else [])):
        for key, cands in WRAPPER_NAMES.items():
            if key in out:
                continue
            for c in cands:
                if c in names:
                    out[key] = "%s %s" % (tool, c)
                    src[key] = "%s target `%s`" % (rel, c)
                    break
    return out, src


def detect_commands(ctx, eco: List[str], pms: Dict[str, str]) -> Tuple[Dict[str, str], Dict[str, str]]:
    cmds, src = _wrapper_commands(ctx)
    layers = []
    for e in eco:
        if e == "node":
            layers.append(("package.json", _node_commands(ctx, pms["node"])))
        elif e == "python":
            layers.append(("python manifests (%s)" % pms["python"], _python_commands(ctx, pms["python"])))
        elif e == "go":
            layers.append(("go.mod", _go_commands(ctx)))
        elif e == "rust":
            layers.append(("Cargo.toml", _rust_commands(ctx)))
        elif e == "ruby":
            layers.append(("Gemfile", _ruby_commands(ctx)))
        elif e == "java":
            layers.append((pms["java"], _java_commands(ctx, pms["java"])))
        elif e == "php":
            layers.append(("composer.json", _php_commands(ctx)))
    for source, guess in layers:
        for k, v in guess.items():
            if k not in cmds and v:
                cmds[k] = v
                src[k] = source
    return {k: cmds.get(k, "") for k in COMMAND_KEYS}, src


# ---------------------------------------------------------------- workspaces
def detect_workspaces(ctx) -> List[dict]:
    ws: List[dict] = []
    data, _ = ctx.load_json("package.json")
    if isinstance(data, dict) and data.get("workspaces"):
        w = data["workspaces"]
        members = w if isinstance(w, list) else (w.get("packages", []) if isinstance(w, dict) else [])
        ws.append({"tool": "npm-workspaces", "members": [m for m in members if isinstance(m, str)]})
    pnpm = ctx.read_text("pnpm-workspace.yaml")
    if pnpm:
        ws.append({"tool": "pnpm", "members": re.findall(r"(?m)^\s*-\s*[\"']?([^\"'\n#]+?)[\"']?\s*$", pnpm)})
    cargo = ctx.read_text("Cargo.toml") or ""
    body = toml_section(cargo, "workspace")
    if body is not None:
        ws.append({"tool": "cargo", "members": toml_array(body, "members")})
    gowork = ctx.read_text("go.work")
    if gowork:
        uses = re.findall(r"(?m)^\s*use\s+(\S+)", gowork)
        m = re.search(r"(?s)use\s*\((.*?)\)", gowork)
        if m:
            uses += [u.strip() for u in m.group(1).splitlines() if u.strip() and not u.strip().startswith("//")]
        ws.append({"tool": "go-work", "members": uses})
    pyproject = ctx.read_text("pyproject.toml") or ""
    body = toml_section(pyproject, "tool.uv.workspace")
    if body is not None:
        ws.append({"tool": "uv", "members": toml_array(body, "members")})
    for f, tool in (("lerna.json", "lerna"), ("nx.json", "nx"), ("turbo.json", "turbo"),
                    ("rush.json", "rush")):
        if ctx.isfile(f):
            ws.append({"tool": tool, "members": []})
    gradle = (ctx.read_text("settings.gradle") or "") + (ctx.read_text("settings.gradle.kts") or "")
    inc = re.findall(r"include\s*\(?\s*[\"']([^\"']+)[\"']", gradle)
    if inc:
        ws.append({"tool": "gradle", "members": inc})
    pom = ctx.read_text("pom.xml") or ""
    mods = re.findall(r"<module>([^<]+)</module>", pom)
    if mods:
        ws.append({"tool": "maven", "members": mods})
    return ws


# ---------------------------------------------------------------- agents
def detect_agents(ctx) -> List[str]:
    """Which coding agents have configuration in the repo."""
    # Footprints come from each agent's AgentSpec. AGENTS.md and .agents/skills/
    # are shared by several agents, so they name no agent on their own.
    from .agents import all_agents
    found = []
    for a in all_agents():
        for fp in a.footprints:
            if (ctx.isdir(fp.rstrip("/")) if fp.endswith("/") else ctx.isfile(fp)):
                found.append(a.id)
                break
    return found


# ---------------------------------------------------------------- branch
def detect_default_branch(ctx) -> str:
    cfg = ctx.harness_config().get("default_branch")
    if isinstance(cfg, str) and cfg:
        return cfg
    if not ctx.is_git:
        return ""
    r = ctx.git("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if r and r[0] == 0 and r[1].strip():
        return r[1].strip().rsplit("/", 1)[-1]
    for b in ("main", "master", "trunk", "develop"):
        r = ctx.git("rev-parse", "--verify", "--quiet", "refs/heads/" + b)
        if r and r[0] == 0:
            return b
    r = ctx.git("symbolic-ref", "--quiet", "--short", "HEAD")
    if r and r[0] == 0:
        return r[1].strip()
    return ""


def detect(ctx) -> dict:
    eco, pms = detect_ecosystems(ctx)
    cmds, src = detect_commands(ctx, eco, pms)
    return {
        "ecosystems": eco,
        "package_managers": pms,
        "commands": cmds,
        "command_sources": src,
        "workspaces": detect_workspaces(ctx),
        "agents": detect_agents(ctx),
        "default_branch": detect_default_branch(ctx),
    }
