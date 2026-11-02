"""System command risk classification.

GuidePilot never executes shell commands on the user's behalf. When guidance
*contains* a command (for example "install PostgreSQL"), the command is
classified so the UI can explain exactly what it would change and require
explicit approval before it is even presented as copyable.

Classification is conservative: unknown commands are treated as ``modify``.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Any

LEVELS = ("none", "inspect", "modify", "destructive")

_DESTRUCTIVE = [
    (re.compile(r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+)+(/|/\*|~|\$HOME)\b"), "recursive delete of a home or root path"),
    # A recursive, forced delete is unrecoverable even when the path is not the
    # filesystem root, and it is the single most common way to lose work.
    (re.compile(r"\brm\s+(-[a-zA-Z]*[rf][a-zA-Z]*\s+|--(recursive|force)\s+)+\S+"), "recursively deletes files without confirmation"),
    (re.compile(r"\bmkfs(\.\w+)?\b"), "formats a filesystem, destroying its contents"),
    (re.compile(r"\bdd\b[^\n]*\bof=/dev/(sd|nvme|hd|vd)"), "writes raw data over a disk device"),
    (re.compile(r"\b(shred|wipefs)\b"), "irreversibly erases data"),
    (re.compile(r":\(\)\s*\{.*\};:"), "fork bomb"),
    (re.compile(r"\b(chmod|chown)\s+-R\s+(777|000)\s+/(\s|$)"), "recursive permission change on the filesystem root"),
    (re.compile(r">\s*/dev/(sd|nvme|hd)"), "raw redirect onto a disk device"),
    (re.compile(r"\b(userdel|groupdel|passwd)\b"), "changes account credentials or removes accounts"),
    (re.compile(r"\bvisudo\b|/etc/sudoers"), "edits privileged sudo configuration"),
    (re.compile(r"\biptables\s+-F\b|\bnft\s+flush\b"), "flushes firewall rules"),
    (re.compile(r"\bgit\s+push\b[^\n]*--force"), "force-pushes, rewriting remote history"),
    (re.compile(r"\b(drop|truncate)\s+(database|table)\b", re.IGNORECASE), "destroys database data"),
    (re.compile(r"\bcurl\b[^\n|]*\|\s*(sudo\s+)?(bash|sh)\b"), "pipes remote content straight into a shell"),
    (re.compile(r"\bwget\b[^\n|]*\|\s*(sudo\s+)?(bash|sh)\b"), "pipes remote content straight into a shell"),
    (re.compile(r"\bkill\s+-9\s+-1\b"), "kills every process the user can signal"),
]

_MODIFY = [
    (re.compile(r"\b(apt|apt-get|dpkg|dnf|yum|pacman|zypper)\b"), "installs, upgrades or removes system packages"),
    (re.compile(r"\bpip3?\s+install\b|\bnpm\s+install\s+-g\b|\byarn\s+global\b"), "installs software into the system"),
    (re.compile(r"\bsystemctl\s+(start|stop|restart|enable|disable|mask)\b"), "changes a system service state"),
    (re.compile(r"\bservice\s+\w+\s+(start|stop|restart)\b"), "changes a system service state"),
    (re.compile(r"\b(mount|umount)\b"), "mounts or unmounts filesystems"),
    (re.compile(r"\b(chmod|chown|usermod|useradd|groupadd)\b"), "changes permissions or accounts"),
    (re.compile(r"\b(git\s+(clone|push|pull|reset|checkout|clean))\b"), "modifies a git repository"),
    (re.compile(r"\b(docker|podman)\s+(run|rm|rmi|compose)\b"), "changes containers or images"),
    (re.compile(r"\bcrontab\b"), "changes scheduled jobs"),
    (re.compile(r"\b(sed|tee)\b[^\n]*\s-[a-zA-Z]*i"), "rewrites files in place"),
    (re.compile(r">\s*\S+"), "redirects output into a file"),
    (re.compile(r"\bsudo\b"), "requests elevated privileges"),
    (re.compile(r"\b(nmap|masscan|hydra|sqlmap|metasploit|msfconsole)\b"), "runs an active security tool that may scan or attack systems"),
]

_INSPECT = [
    (re.compile(r"^\s*(ls|cat|less|more|head|tail|grep|rg|which|whereis|type|file|stat|pwd|whoami|id|date|uname)\b"), "reads local information"),
    (re.compile(r"^\s*(apt-cache|apt\s+policy|dpkg\s+-l|rpm\s+-q)\b"), "queries the package database without changing it"),
    (re.compile(r"^\s*(ps|pgrep|top|htop|ss|netstat|lsof|free|df|du|uptime)\b"), "reads process or resource information"),
    (re.compile(r"^\s*(--version|-V|--help|-h)\b"), "prints version or help text"),
    (re.compile(r"^\s*(systemctl\s+status|journalctl)\b"), "reads service status or logs"),
    (re.compile(r"^\s*(git\s+status|git\s+log|git\s+diff)\b"), "reads repository state"),
    (re.compile(r"^\s*(python3?\s+--version|node\s+--version|npm\s+--version|go\s+version)\b"), "prints a tool version"),
]

_NETWORK_READ = re.compile(r"^\s*(curl|wget|ping|dig|host|nslookup)\b")


@dataclass
class CommandRisk:
    command: str
    level: str
    reasons: list[str] = field(default_factory=list)
    requires_confirmation: bool = True
    explanation: str = ""
    executable_by_app: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "level": self.level,
            "reasons": self.reasons,
            "requires_confirmation": self.requires_confirmation,
            "explanation": self.explanation,
            "executable_by_app": self.executable_by_app,
        }


def _argv(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def classify_command(command: str) -> CommandRisk:
    """Classify a shell command's risk and explain it in one sentence."""
    cleaned = (command or "").strip()
    if not cleaned:
        return CommandRisk(command="", level="none", requires_confirmation=False, explanation="No command")
    if "\n" in cleaned:
        risks = [classify_command(line) for line in cleaned.splitlines() if line.strip()]
        level = max((r.level for r in risks), key=lambda l: LEVELS.index(l), default="none")
        reasons: list[str] = []
        for risk in risks:
            reasons.extend(risk.reasons)
        return CommandRisk(
            command=cleaned,
            level=level,
            reasons=reasons,
            requires_confirmation=level != "inspect",
            explanation=f"Multi-command block; highest risk is {level}.",
        )

    reasons: list[str] = []
    writes_file = bool(re.search(r"(?<![0-9&])>>?\s*\S+", cleaned))
    for pattern, reason in _DESTRUCTIVE:
        if pattern.search(cleaned):
            return CommandRisk(
                command=cleaned,
                level="destructive",
                reasons=reasons + [reason],
                requires_confirmation=True,
                explanation=f"This command is destructive: {reason}. GuidePilot will never run it automatically.",
            )
    for pattern, reason in _MODIFY:
        if pattern.search(cleaned):
            reasons.append(reason)
    for pattern, reason in _INSPECT:
        if pattern.search(cleaned):
            if writes_file and not any(r == "redirects output into a file" for r in reasons):
                reasons.append("the command itself only reads, but the shell writes its output to a file")
                return CommandRisk(
                    command=cleaned,
                    level="modify",
                    reasons=reasons,
                    requires_confirmation=True,
                    explanation=f"Read-only command, but the redirection writes to a file: {reason}.",
                )
            return CommandRisk(
                command=cleaned,
                level="inspect",
                reasons=reasons + [reason],
                requires_confirmation=False,
                explanation=f"Read-only: {reason}.",
            )
    if _NETWORK_READ.search(cleaned):
        return CommandRisk(
            command=cleaned,
            level="inspect",
            reasons=["reads data from the network"],
            requires_confirmation=False,
            explanation="Read-only network request; it downloads or queries, but changes nothing locally.",
        )
    if reasons:
        return CommandRisk(
            command=cleaned,
            level="modify",
            reasons=reasons,
            requires_confirmation=True,
            explanation=f"This command changes your system: {reasons[0]}. Review it before running.",
        )
    argv = _argv(cleaned)
    if argv and argv[0] not in {"sudo", "su"}:
        level = "modify"
        explanation = "Unrecognised command; treated as system-modifying until you confirm otherwise."
    else:
        level = "destructive" if "sudo" in (argv[:1] or []) else "modify"
        explanation = "Privilege escalation command; treated as high risk."
    return CommandRisk(
        command=cleaned,
        level=level,
        reasons=["unclassified command"],
        requires_confirmation=level != "inspect",
        explanation=explanation,
    )


def summarize_for_prompt(commands: list[str]) -> list[dict[str, Any]]:
    return [classify_command(c).to_dict() for c in commands]
