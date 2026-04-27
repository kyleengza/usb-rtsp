"""Plugin loader.

Discovers `plugins/<name>/manifest.yml`, reads the runtime enabled list at
`~/.config/usb-rtsp/plugins-enabled.yml`, and imports + registers each
enabled plugin against the FastAPI app.

Plugin contract: a Python package under plugins/<name>/ that exposes
optional symbols on its top-level module:

    register(app, ctx)            FastAPI bootstrap hook (mount routers,
                                  templates, static files)
    render_paths(ctx) -> dict     called by core/renderer.py to emit
                                  this plugin's slice of mediamtx paths
"""
from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

from .helpers import PLUGINS_DIR, PLUGINS_ENABLED_FILE, CONFIG_DIR


@dataclass
class Plugin:
    name: str
    description: str
    version: str
    default_enabled: bool
    dir: Path                       # plugins/<name>/
    config_dir: Path                # ~/.config/usb-rtsp/<name>/
    module: Any | None = None       # imported python module (None until import)
    enabled: bool = False
    section_template: str = ""      # "<name>/section.html" if templates/section.html exists

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
    """Walk plugins/<name>/manifest.yml and return Plugin metadata for each."""
    out: list[Plugin] = []
    if not PLUGINS_DIR.is_dir():
        return out
    for d in sorted(PLUGINS_DIR.iterdir()):
        if not d.is_dir():
            continue
        m = _read_manifest(d)
        if not m or not m.get("name"):
            continue
        name = str(m["name"]).strip()
        section_tpl = ""
        if (d / "templates" / "section.html").exists():
            section_tpl = f"{name}/section.html"
        out.append(Plugin(
            name=name,
            description=str(m.get("description", "")),
            version=str(m.get("version", "0.0.0")),
            default_enabled=bool(m.get("default_enabled", False)),
            dir=d,
            config_dir=CONFIG_DIR / name,
            section_template=section_tpl,
        ))
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
    """Import the plugin's top-level module; cache on plugin.module."""
    if plugin.module is not None:
        return plugin.module
    repo_root = plugin.dir.parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    module_name = f"plugins.{plugin.name}"
    plugin.module = importlib.import_module(module_name)
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
