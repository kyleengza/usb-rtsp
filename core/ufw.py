"""UFW management for the usb-rtsp admin panel.

Scoped intentionally narrow:

  - Read-only status of every UFW rule (numbered + verbose).
  - For a known list of usb-rtsp ports, a 3-state scope per port:
    `lan`     — ALLOW from the local /24 only
    `anywhere`— ALLOW from anywhere (v4 + v6 if active)
    `off`     — no rule at all (port closed by default-deny)

Free-form rule editing is deliberately not exposed: one wrong CIDR via a
web form locks you out of SSH. The scope picker handles 95% of the
day-to-day.

Sudo: the panel runs as a non-root user. ``sudo -n /usr/sbin/ufw …`` is
required; ``install.sh --enable-ufw-mgmt`` drops the right
``/etc/sudoers.d/usb-rtsp-ufw`` file. ``sudo_ok()`` reports whether
that's wired up — the panel grays out the toggles when it isn't.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import Literal


UFW_BIN = "/usr/sbin/ufw"
SCOPE = Literal["lan", "anywhere", "off"]


# Ports usb-rtsp owns. Anything outside this list is reported read-only
# and never modified — that includes SSH, RTP UDP, custom rules, etc.
MANAGED_PORTS: list[dict] = [
    {"group": "Admin",  "port": 8080, "proto": "tcp", "label": "Admin panel",    "comment": "usb-rtsp admin",                    "default": "lan",      "warn_off": True},
    {"group": "RTSP",   "port": 8554, "proto": "tcp", "label": "RTSP control",   "comment": "rtsp control",                      "default": "anywhere"},
    {"group": "RTSP",   "port": 8554, "proto": "udp", "label": "RTSP UDP",       "comment": "rtsp udp control",                  "default": "anywhere"},
    {"group": "RTSP",   "port": 8000, "proto": "udp", "label": "RTP media",      "comment": "rtp media (udp xport)",             "default": "anywhere"},
    {"group": "RTSP",   "port": 8001, "proto": "udp", "label": "RTCP",           "comment": "rtcp (udp xport)",                  "default": "anywhere"},
    {"group": "HLS",    "port": 8888, "proto": "tcp", "label": "HLS",            "comment": "usb-rtsp HLS",                      "default": "lan"},
    {"group": "WebRTC", "port": 8889, "proto": "tcp", "label": "WebRTC HTTP",    "comment": "usb-rtsp WebRTC HTTP",              "default": "anywhere"},
    {"group": "WebRTC", "port": 8189, "proto": "udp", "label": "WebRTC ICE/UDP", "comment": "usb-rtsp WebRTC ICE/media",         "default": "anywhere"},
    {"group": "WebRTC", "port": 8189, "proto": "tcp", "label": "WebRTC ICE/TCP", "comment": "usb-rtsp WebRTC ICE/TCP fallback",  "default": "anywhere"},
]


@dataclass
class UfwRule:
    number: int            # numbered position (rule shifts when others are deleted)
    raw: str               # the original status line, useful for display
    to: str                # "8080/tcp", "Anywhere", "8080", "OpenSSH"
    action: str            # "ALLOW IN" / "DENY IN" / etc
    from_: str             # "Anywhere", "192.168.100.0/24", "Anywhere (v6)", ...
    comment: str           # extracted "# foo" — empty if none
    v6: bool               # IPv6 rule


# ─── invocation helpers ────────────────────────────────────────────────────

def _run(*args: str, timeout: int = 6) -> tuple[int, str, str]:
    """Run `sudo -n /usr/sbin/ufw <args>` non-interactively.
    Returns (returncode, stdout, stderr)."""
    try:
        p = subprocess.run(
            ["sudo", "-n", UFW_BIN, *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return p.returncode, p.stdout, p.stderr
    except subprocess.SubprocessError as e:
        return 255, "", str(e)


def sudo_ok() -> bool:
    """True iff `sudo -n ufw status` works without a password."""
    rc, _, _ = _run("status")
    return rc == 0


# ─── parsing `ufw status numbered` ─────────────────────────────────────────

# Example lines:
#   [ 1] 22/tcp                     ALLOW IN    192.168.100.0/24           # ssh from LAN
#   [ 8] 8889/tcp                   ALLOW IN    Anywhere                   # usb-rtsp WebRTC HTTP
#   [11] 8554/tcp (v6)              ALLOW IN    Anywhere (v6)              # rtsp control
_RULE_RE = re.compile(
    r"^\[\s*(?P<num>\d+)\]\s+"
    r"(?P<to>.+?)\s{2,}"
    r"(?P<action>(?:ALLOW|DENY|REJECT|LIMIT)\s+(?:IN|OUT|FWD))\s+"
    r"(?P<from>.+?)"
    r"(?:\s+#\s*(?P<comment>.+?))?\s*$"
)


def _parse_status_numbered(text: str) -> tuple[bool, list[UfwRule]]:
    """Returns (active, rules)."""
    active = False
    rules: list[UfwRule] = []
    for line in text.splitlines():
        line = line.rstrip()
        if line.lower().startswith("status:"):
            active = "active" in line.lower()
            continue
        m = _RULE_RE.match(line)
        if not m:
            continue
        to = m["to"].strip()
        v6 = "(v6)" in to or "(v6)" in m["from"]
        rules.append(UfwRule(
            number=int(m["num"]),
            raw=line,
            to=to,
            action=m["action"].strip(),
            from_=m["from"].strip(),
            comment=(m["comment"] or "").strip(),
            v6=v6,
        ))
    return active, rules


def status() -> dict:
    """Returns active flag, parsed rules, raw text. Empty dict if sudo failed."""
    if not sudo_ok():
        rc, _, err = _run("status")
        return {"sudo_ok": False, "active": False, "rules": [], "raw": "", "error": err.strip()}
    rc, out, err = _run("status", "numbered")
    if rc != 0:
        return {"sudo_ok": True, "active": False, "rules": [], "raw": out, "error": err.strip()}
    active, rules = _parse_status_numbered(out)
    return {
        "sudo_ok": True,
        "active": active,
        "rules": [
            {
                "number": r.number,
                "to": r.to,
                "action": r.action,
                "from": r.from_,
                "comment": r.comment,
                "v6": r.v6,
                "raw": r.raw,
            }
            for r in rules
        ],
        "raw": out,
    }


# ─── managed-port scope detection ──────────────────────────────────────────

def _matches_port(rule: UfwRule, port: int, proto: str) -> bool:
    """Does this rule target our port+proto?"""
    # 'to' looks like '8889/tcp' or '8889/tcp (v6)' or '8080' (proto unspecified)
    head = rule.to.split()[0]
    if "/" in head:
        p, pr = head.split("/", 1)
        return p == str(port) and pr == proto
    # No proto specified — UFW treats this as both, but we only track explicit.
    return False


def matching_rules(rules: list[dict] | list[UfwRule], port: int, proto: str) -> list[UfwRule]:
    """Return the parsed rules that target a given port+proto."""
    out: list[UfwRule] = []
    for r in rules:
        if isinstance(r, dict):
            ur = UfwRule(
                number=r["number"], raw=r.get("raw", ""), to=r["to"],
                action=r["action"], from_=r["from"], comment=r.get("comment", ""),
                v6=r.get("v6", False),
            )
        else:
            ur = r
        if _matches_port(ur, port, proto):
            out.append(ur)
    return out


def detect_scope(rules: list[dict] | list[UfwRule], port: int, proto: str) -> SCOPE:
    """Return the current scope for a port+proto from a parsed rule list.

    Priority: explicit DENY → off; ALLOW Anywhere → anywhere; ALLOW from CIDR → lan;
    no matching rule at all → off (implicit, default-deny).
    """
    has_anywhere = False
    has_lan = False
    has_deny = False
    for ur in matching_rules(rules, port, proto):
        act = ur.action.upper()
        if act.startswith("DENY") or act.startswith("REJECT"):
            has_deny = True
        elif act.startswith("ALLOW"):
            f = ur.from_.lower()
            if f.startswith("anywhere"):
                has_anywhere = True
            elif "/" in ur.from_:
                has_lan = True
    if has_deny and not (has_anywhere or has_lan):
        return "off"
    if has_anywhere:
        return "anywhere"
    if has_lan:
        return "lan"
    return "off"


def lan_cidr() -> str:
    """Best-effort: the /24 of the LAN IP this Pi uses to reach the internet."""
    try:
        r = subprocess.run(
            ["ip", "-4", "-o", "route", "get", "1.1.1.1"],
            capture_output=True, text=True, timeout=2,
        )
        toks = r.stdout.split()
        if "src" in toks:
            ip = toks[toks.index("src") + 1]
            octets = ip.split(".")
            if len(octets) == 4:
                return f"{octets[0]}.{octets[1]}.{octets[2]}.0/24"
    except (OSError, ValueError, IndexError):
        pass
    return ""


# ─── rule application ──────────────────────────────────────────────────────

def _delete_matching(rules: list[UfwRule], port: int, proto: str) -> list[str]:
    """Delete every ALLOW or DENY rule that targets port+proto. Returns the
    messages emitted. Deletes by rule number in reverse order so renumbering
    doesn't bite."""
    msgs: list[str] = []
    targets = sorted(
        [
            r for r in rules
            if _matches_port(r, port, proto)
            and r.action.upper().startswith(("ALLOW", "DENY", "REJECT"))
        ],
        key=lambda r: r.number, reverse=True,
    )
    for r in targets:
        rc, out, err = _run("--force", "delete", str(r.number))
        msgs.append(f"delete #{r.number} ({r.to} from {r.from_}): rc={rc} {(out + err).strip()}")
    return msgs


def delete_rule(number: int) -> dict:
    """Delete a single UFW rule by its current numbered position."""
    if not sudo_ok():
        return {"ok": False, "error": "sudo -n ufw not available"}
    if number <= 0:
        return {"ok": False, "error": f"invalid rule number: {number}"}
    rc, out, err = _run("--force", "delete", str(number))
    return {"ok": rc == 0, "output": (out + err).strip()}


def set_port_scope(port: int, proto: str, scope: SCOPE, comment: str = "") -> dict:
    """Apply a scope change. Returns ``{ok, messages, scope_after}``."""
    if scope not in ("lan", "anywhere", "off"):
        return {"ok": False, "error": f"invalid scope: {scope}"}
    if not sudo_ok():
        return {"ok": False, "error": "sudo -n ufw not available — run install.sh --enable-ufw-mgmt"}
    rc, out, err = _run("status", "numbered")
    if rc != 0:
        return {"ok": False, "error": (out + err).strip()}
    _, rules = _parse_status_numbered(out)
    msgs = _delete_matching(rules, port, proto)

    if scope == "off":
        # Add an explicit DENY so the rule list shows the port as closed
        # (instead of relying on default-deny + an absent rule).
        args = ["deny", f"{port}/{proto}"]
        if comment:
            args += ["comment", comment]
        rc, out, err = _run(*args)
        msgs.append(f"deny {port}/{proto}: rc={rc} {(out + err).strip()}")
        if rc != 0:
            return {"ok": False, "messages": msgs, "error": (out + err).strip()}
    elif scope == "anywhere":
        args = ["allow", f"{port}/{proto}"]
        if comment:
            args += ["comment", comment]
        rc, out, err = _run(*args)
        msgs.append(f"allow {port}/{proto}: rc={rc} {(out + err).strip()}")
        if rc != 0:
            return {"ok": False, "messages": msgs, "error": (out + err).strip()}
    elif scope == "lan":
        cidr = lan_cidr()
        if not cidr:
            return {"ok": False, "messages": msgs, "error": "could not determine LAN CIDR"}
        args = ["allow", "from", cidr, "to", "any", "port", str(port), "proto", proto]
        if comment:
            args += ["comment", comment]
        rc, out, err = _run(*args)
        msgs.append(f"allow from {cidr} {port}/{proto}: rc={rc} {(out + err).strip()}")
        if rc != 0:
            return {"ok": False, "messages": msgs, "error": (out + err).strip()}

    # re-read to confirm
    rc, out, _ = _run("status", "numbered")
    _, rules_after = _parse_status_numbered(out) if rc == 0 else (False, [])
    return {
        "ok": True,
        "messages": msgs,
        "scope_after": detect_scope(rules_after, port, proto),
    }


def set_ufw_enabled(enable: bool) -> dict:
    """Enable or disable UFW itself."""
    if not sudo_ok():
        return {"ok": False, "error": "sudo -n ufw not available"}
    rc, out, err = _run("--force", "enable" if enable else "disable")
    return {
        "ok": rc == 0,
        "active": enable if rc == 0 else None,
        "output": (out + err).strip(),
    }


# ─── per-IP / per-CIDR blocklist ───────────────────────────────────────────
# These are deny-from-<X> rules with no port restriction (`to == Anywhere`),
# inserted at position 1 so they preempt every later allow rule. Independent
# of the per-port scope toggles — the latter only touches rules where `to`
# matches `<port>/<proto>`.

import ipaddress


def _parse_source(source: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    """Parse 'X.Y.Z.W' or 'X.Y.Z.0/24' into an ip_network. None on garbage."""
    try:
        return ipaddress.ip_network(source.strip(), strict=False)
    except (ValueError, TypeError):
        return None


def is_blockable(source: str, requester_ip: str = "") -> tuple[bool, str]:
    """Refuse to block loopback, the LAN /24, the requester's own IP, and
    obviously-too-wide CIDRs. Returns (ok, reason). Reason is empty when ok.
    """
    net = _parse_source(source)
    if net is None:
        return False, f"not a valid IP or CIDR: {source!r}"
    if net.prefixlen < 8:
        return False, f"refusing to block /{net.prefixlen} (too wide; bug guard at /8)"
    if net.is_loopback:
        return False, "refusing to block loopback"
    if net.is_link_local:
        return False, "refusing to block link-local addresses"
    # LAN /24 protection (skip if we couldn't determine it).
    lc = lan_cidr()
    if lc:
        try:
            lan_net = ipaddress.ip_network(lc, strict=False)
            if net.version == lan_net.version and net.subnet_of(lan_net):
                return False, f"refusing to block your LAN ({lc})"
        except (ValueError, TypeError):
            pass
    # Requester foot-gun guard.
    if requester_ip:
        try:
            req = ipaddress.ip_address(requester_ip.strip())
            if req in net:
                return False, "refusing to block the IP making this request"
        except (ValueError, TypeError):
            pass
    return True, ""


def block(source: str, comment: str = "") -> dict:
    """Insert a `deny from <source>` rule at position 1. Returns {ok, output}.

    Caller should validate via is_blockable() first; this function just runs
    the command without re-checking (so server-side endpoints can pass extra
    context like the requester's IP).
    """
    if not sudo_ok():
        return {"ok": False, "error": "sudo -n ufw not available"}
    args = ["--force", "insert", "1", "deny", "from", source.strip()]
    if comment:
        args += ["comment", comment]
    rc, out, err = _run(*args)
    return {"ok": rc == 0, "output": (out + err).strip()}


def list_blocks() -> list[dict]:
    """Return all DENY-from-<X> rules (with no port restriction) — i.e. blocklist
    entries, distinct from the per-port DENYs the scope toggles use."""
    rc, out, _ = _run("status", "numbered")
    if rc != 0:
        return []
    _, rules = _parse_status_numbered(out)
    blocks: list[dict] = []
    for r in rules:
        if not r.action.upper().startswith("DENY"):
            continue
        # Skip per-port denials — those have a numeric port in `to`.
        head = r.to.split()[0]
        if "/" in head:
            try:
                int(head.split("/", 1)[0])
                continue  # port-scoped rule, not a blocklist entry
            except ValueError:
                pass
        # Skip "Anywhere" deny rules with no `from` — those are policy not blocks.
        if not r.from_ or r.from_.lower().startswith("anywhere"):
            continue
        blocks.append({
            "number":  r.number,
            "source":  r.from_,
            "comment": r.comment,
            "v6":      r.v6,
            "raw":     r.raw,
        })
    return blocks


def unblock(source: str) -> dict:
    """Delete every blocklist rule whose `from_` matches the given source.
    Returns {ok, deleted, messages}."""
    if not sudo_ok():
        return {"ok": False, "error": "sudo -n ufw not available"}
    src_net = _parse_source(source)
    if src_net is None:
        return {"ok": False, "error": f"invalid source: {source!r}"}
    src_str = str(src_net)
    rc, out, err = _run("status", "numbered")
    if rc != 0:
        return {"ok": False, "error": (out + err).strip()}
    _, rules = _parse_status_numbered(out)
    targets: list[UfwRule] = []
    for r in rules:
        if not r.action.upper().startswith("DENY"):
            continue
        head = r.to.split()[0]
        if "/" in head:
            try:
                int(head.split("/", 1)[0])
                continue
            except ValueError:
                pass
        from_net = _parse_source(r.from_)
        if from_net and str(from_net) == src_str:
            targets.append(r)
    targets.sort(key=lambda r: r.number, reverse=True)
    msgs: list[str] = []
    for r in targets:
        rc, out, err = _run("--force", "delete", str(r.number))
        msgs.append(f"delete #{r.number} (deny from {r.from_}): rc={rc} {(out + err).strip()}")
    return {"ok": True, "deleted": len(targets), "messages": msgs}
