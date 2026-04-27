"""Plugin loader.

Discovers manifest.yml files in two locations:
  1. <repo>/plugins/<name>/                       — bundled with main repo
  2. ~/.local/share/usb-rtsp/plugins/<name>/      — user-installed

Reads the runtime enabled list at ~/.config/usb-rtsp/plugins-enabled.yml,
imports + registers each enabled plugin against the FastAPI app.

Plugin contract — a Python package that exposes optional symbols on its
top-level module:

    register(app, ctx)            FastAPI bootstrap hook (mount routers,
                                  templates, static files)
    render_paths(ctx) -> dict     called by core/renderer.py to emit
                                  this plugin's slice of mediamtx paths
"""
from __future__ import annotations

import importlib
import importlib.util
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

from .helpers import CONFIG_DIR, PLUGINS_DIR, PLUGINS_ENABLED_FILE, USER_PLUGINS_DIR

PLUGIN_SEARCH_PATHS = [PLUGINS_DIR, USER_PLUGINS_DIR]


@dataclass
class Plugin:
    name: str
    description: str
    version: str
    default_enabled: bool
    dir: Path                       # actual on-disk path (bundled or user-installed)
    config_dir: Path                # ~/.config/usb-rtsp/<name>/
    bundled: bool = False           # True if from <repo>/plugins/, False if user-installed
    module: Any | None = None       # imported python module (None until import)
    enabled: bool = False
    section_template: str = ""      # "<name>/section.html" if templates/section.html exists
    settings_template: str = ""     # "<name>/settings.html" if templates/settings.html exists
    order: int = 100                # dashboard render order (low = top); from manifest

    @property
    def title(self) -> str:
        return self.description or self.name


def _read_manifest(d: Path) -> dict | None:
    p = d / "manifest.yml"
    if not p.exists():
        return None
    try:
        return yaml.safe_load(p.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return None


def discover_plugins() -> list[Plugin]:
    """Walk every plugins-search-path's <name>/manifest.yml. Bundled wins
    on name collision; we warn on duplicates."""
    out: list[Plugin] = []
    seen: dict[str, Plugin] = {}
    for search_dir in PLUGIN_SEARCH_PATHS:
        bundled = (search_dir == PLUGINS_DIR)
        if not search_dir.is_dir():
            continue
        for d in sorted(search_dir.iterdir()):
            if not (d.is_dir() or d.is_symlink()):
                continue
            m = _read_manifest(d)
            if not m or not m.get("name"):
                continue
            name = str(m["name"]).strip()
            if name in seen:
                # bundled was added first via search-path order; second hit
                # is the lower-priority user dir → warn and skip.
                print(f"[loader] duplicate plugin {name!r}: keeping {seen[name].dir}, "
                      f"ignoring {d}", file=sys.stderr)
                continue
            section_tpl = ""
            if (d / "templates" / "section.html").exists():
                section_tpl = f"{name}/section.html"
            settings_tpl = ""
            if (d / "templates" / "settings.html").exists():
                settings_tpl = f"{name}/settings.html"
            p = Plugin(
                name=name,
                description=str(m.get("description", "")),
                version=str(m.get("version", "0.0.0")),
                default_enabled=bool(m.get("default_enabled", False)),
                dir=d,
                config_dir=CONFIG_DIR / name,
                bundled=bundled,
                section_template=section_tpl,
                settings_template=settings_tpl,
                order=int(m.get("order", 100)),
            )
            seen[name] = p
            out.append(p)
    out.sort(key=lambda p: (p.order, p.name))
    return out


def read_enabled_set() -> set[str]:
    """Read plugins-enabled.yml, falling back to the default-enabled set
    if the file doesn't exist."""
    if PLUGINS_ENABLED_FILE.exists():
        try:
            doc = yaml.safe_load(PLUGINS_ENABLED_FILE.read_text()) or {}
            names = doc.get("enabled") or []
            return {str(n).strip() for n in names if n}
        except (OSError, yaml.YAMLError):
            pass
    # bootstrap default
    return {p.name for p in discover_plugins() if p.default_enabled}


def write_enabled_set(names: set[str]) -> None:
    PLUGINS_ENABLED_FILE.parent.mkdir(parents=True, exist_ok=True)
    PLUGINS_ENABLED_FILE.write_text(yaml.safe_dump({"enabled": sorted(names)}, sort_keys=False))


def enabled_plugins() -> list[Plugin]:
    """Return discovered plugins that the runtime config has enabled,
    sorted by name. Each Plugin has its module attribute lazily set by
    register_all()."""
    enabled = read_enabled_set()
    out = [p for p in discover_plugins() if p.name in enabled]
    for p in out:
        p.enabled = True
    return out


def import_plugin(plugin: Plugin) -> Any:
    """Import the plugin's top-level module; cache on plugin.module.

    Plugins live under either <repo>/plugins/<name>/ or
    ~/.local/share/usb-rtsp/plugins/<name>/. We extend the 'plugins'
    package's __path__ to cover both, then fall back to importlib.util
    if the package import doesn't see the directory.
    """
    if plugin.module is not None:
        return plugin.module

    # Make sure the repo dir (which contains the bundled 'plugins' package)
    # is on sys.path so 'import plugins.<name>' is resolvable.
    repo_root = PLUGINS_DIR.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    # Extend the loaded 'plugins' package __path__ so user-dir plugins
    # also resolve via standard import.
    try:
        plugins_pkg = importlib.import_module("plugins")
        for extra in (str(USER_PLUGINS_DIR),):
            if USER_PLUGINS_DIR.is_dir() and extra not in plugins_pkg.__path__:
                plugins_pkg.__path__.append(extra)
    except ImportError:
        pass

    module_name = f"plugins.{plugin.name}"
    try:
        plugin.module = importlib.import_module(module_name)
    except ModuleNotFoundError:
        # Fallback: spec-from-file import for paths that namespace package
        # discovery missed.
        spec = importlib.util.spec_from_file_location(
            module_name,
            plugin.dir / "__init__.py",
            submodule_search_locations=[str(plugin.dir)],
        )
        if not spec or not spec.loader:
            raise
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)
        plugin.module = mod
    return plugin.module


def register_all(app, templates) -> list[Plugin]:
    """Import every enabled plugin and call its register(app, ctx) hook
    if defined. Returns the list of registered plugins (so the caller
    can pass them to the dashboard template)."""
    plugins = enabled_plugins()
    for p in plugins:
        p.config_dir.mkdir(parents=True, exist_ok=True)
        try:
            mod = import_plugin(p)
        except Exception as e:
            print(f"[loader] failed to import plugin {p.name}: {e}", file=sys.stderr)
            continue
        register: Callable | None = getattr(mod, "register", None)
        if callable(register):
            try:
                register(app, _make_ctx(p, templates))
            except Exception as e:
                print(f"[loader] register({p.name}) raised: {e}", file=sys.stderr)
    return plugins


def render_all_paths(ctx_factory) -> dict:
    """Loop over enabled plugins, call render_paths(ctx). Merge results.
    Used by core/renderer.py to build the mediamtx config."""
    paths: dict = {}
    for p in enabled_plugins():
        try:
            mod = import_plugin(p)
        except Exception as e:
            print(f"[loader] failed to import {p.name} for render: {e}", file=sys.stderr)
            continue
        render = getattr(mod, "render_paths", None)
        if not callable(render):
            continue
        try:
            ctx = ctx_factory(p)
            piece = render(ctx) or {}
            paths.update(piece)
        except Exception as e:
            print(f"[loader] {p.name}.render_paths raised: {e}", file=sys.stderr)
    return paths


def _make_ctx(plugin: Plugin, templates):
    """Per-plugin context object passed to register/render hooks."""
    from . import auth  # local to avoid circular at module import time
    return _Ctx(plugin=plugin, templates=templates, auth=auth)


@dataclass
class _Ctx:
    plugin: Plugin
    templates: Any
    auth: Any


# ─── install / uninstall / refresh ──────────────────────────────────────────

VALID_PLUGIN_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def _derive_name_from_url(url: str) -> str:
    """github.com/owner/usb-rtsp-plugin-relay → 'relay' (strip prefixes)."""
    base = url.rstrip("/").split("/")[-1]
    if base.endswith(".git"):
        base = base[:-4]
    for prefix in ("usb-rtsp-plugin-", "usb-rtsp-"):
        if base.startswith(prefix):
            base = base[len(prefix):]
            break
    return base.lower()


def install_plugin_from_git(url: str) -> Plugin:
    """git clone url → USER_PLUGINS_DIR/<derived-name>/.

    After clone, re-discovers and returns the new Plugin. Caller is
    responsible for scheduling an admin restart so the plugin loads.
    """
    name = _derive_name_from_url(url)
    if not VALID_PLUGIN_NAME.match(name):
        raise ValueError(f"derived plugin name {name!r} is not a valid identifier")
    USER_PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    target = USER_PLUGINS_DIR / name
    if target.exists():
        raise FileExistsError(f"{target} already exists; remove it first")
    p = subprocess.run(
        ["git", "clone", "--depth=1", url, str(target)],
        capture_output=True, text=True, timeout=120,
    )
    if p.returncode != 0:
        raise RuntimeError(f"git clone failed: {(p.stdout + p.stderr).strip()}")
    # validate manifest
    if not (target / "manifest.yml").exists():
        shutil.rmtree(target, ignore_errors=True)
        raise RuntimeError("cloned repo has no manifest.yml at the root")
    found = next((pl for pl in discover_plugins() if pl.name == name), None)
    if not found:
        shutil.rmtree(target, ignore_errors=True)
        raise RuntimeError(f"plugin {name!r} did not appear in discover_plugins()")
    return found


def install_plugin_from_path(src: Path) -> Plugin:
    """Copy a local dir into USER_PLUGINS_DIR/<manifest-name>/.

    If src is already a child of USER_PLUGINS_DIR (or a symlink that points
    inside the repo), we still re-discover it without copying.
    """
    src = Path(src).expanduser().resolve()
    if not (src / "manifest.yml").exists():
        raise FileNotFoundError(f"{src}/manifest.yml not found")
    m = _read_manifest(src) or {}
    name = str(m.get("name", "")).strip()
    if not VALID_PLUGIN_NAME.match(name):
        raise ValueError(f"plugin manifest name {name!r} is not a valid identifier")
    USER_PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    target = USER_PLUGINS_DIR / name
    # already user-installed via symlink — just refresh discovery
    if target.exists():
        if target.resolve() == src:
            return next((p for p in discover_plugins() if p.name == name), None)
        raise FileExistsError(f"{target} already exists; remove it first")
    # copy as a real directory (snapshot, not symlinked)
    shutil.copytree(src, target)
    found = next((p for p in discover_plugins() if p.name == name), None)
    if not found:
        shutil.rmtree(target, ignore_errors=True)
        raise RuntimeError(f"plugin {name!r} did not appear in discover_plugins()")
    return found


def uninstall_plugin(name: str) -> None:
    """rm the user-installed plugin dir. Refuses to touch bundled plugins."""
    if not VALID_PLUGIN_NAME.match(name or ""):
        raise ValueError(f"invalid plugin name: {name!r}")
    found = next((p for p in discover_plugins() if p.name == name), None)
    if not found:
        raise FileNotFoundError(f"plugin {name!r} not found")
    if found.bundled:
        raise PermissionError(f"plugin {name!r} is bundled with this repo; cannot uninstall")
    target = USER_PLUGINS_DIR / name
    if not target.exists():
        raise FileNotFoundError(f"{target} does not exist")
    if target.is_symlink():
        target.unlink()
    else:
        shutil.rmtree(target)
    # also remove from the enabled list so it doesn't error on next start
    enabled = read_enabled_set()
    if name in enabled:
        enabled.discard(name)
        write_enabled_set(enabled)


def refresh() -> list[Plugin]:
    """Re-walk the search paths. Doesn't re-import in the running process —
    the panel's API endpoint schedules an admin restart instead."""
    return discover_plugins()
