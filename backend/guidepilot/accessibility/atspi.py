"""AT-SPI accessibility-tree client.

GuidePilot prefers accessibility metadata over pixels: element names, roles and
*exact screen bounds* come straight from the toolkit's accessibility bridge.
That is what makes a highlight land precisely on a real button instead of on an
approximation produced by a vision model.

Implementation notes
--------------------
* No Python AT-SPI bindings are required. The client talks to the accessibility
  bus through ``gdbus`` (present on Debian/Kali by default) and parses the
  GVariant replies with :mod:`guidepilot.accessibility.gvariant`.
* Metadata for a node is fetched once per snapshot and cached in-process.
* Sibling/level traversal is parallelised with a small thread pool so a typical
  application tree costs tens of milliseconds, not seconds.
* Every failure is reported through ``notes``/``available`` instead of raising,
  so guidance can fall back to OCR or a vision model with an honest explanation.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .. import util
from ..models import Element, ElementSource, Rect
from . import gvariant

A11Y_ROOT_PATH = "/org/a11y/atspi/accessible/root"
A11Y_REGISTRY = "org.a11y.atspi.Registry"

# AT-SPI state indices (atspi-constants.h, AtspiStateType). Verified against
# at-spi2-core main: INVALID=0, ACTIVE=1, ARMED=2, BUSY=3, CHECKED=4, ...
STATE_CHECKED = 4
STATE_EDITABLE = 7
STATE_ENABLED = 8
STATE_SELECTED = 23
STATE_REQUIRED = 33
STATE_INVALID_ENTRY = 36


def state_set(bits: list[int], index: int) -> bool:
    """Whether bit ``index`` is set in an AT-SPI state word list."""
    word = index // 32
    offset = index % 32
    if word >= len(bits):
        return False
    return bool((bits[word] >> offset) & 1)


# Roles that can hold a user-facing label worth matching against.
INTERACTIVE_ROLES = {
    "push button",
    "toggle button",
    "check box",
    "radio button",
    "entry",
    "text",
    "password text",
    "combo box",
    "menu item",
    "menu",
    "check menu item",
    "radio menu item",
    "page tab",
    "page tab list",
    "link",
    "spin button",
    "slider",
    "list item",
    "tree item",
    "table cell",
    "icon",
    "dialog",
    "alert",
    "status bar",
    "label",
    "heading",
    "list box",
    "push button",
    "tool bar",
    "panel",
    "frame",
    "window",
    "document web",
    "section",
    "form",
    "scroll bar",
    "table",
    "menu bar",
}

# Structural roles kept for layout context but not treated as actionable.
CONTAINER_ROLES = {
    "frame",
    "window",
    "panel",
    "filler",
    "section",
    "form",
    "document web",
    "menu bar",
    "tool bar",
    "page tab list",
    "list",
    "table",
}

_SKIP_ROLES = {
    "filler",
    "separator",
    "scroll pane",
    "viewport",
    "application",
    "redundant object",
    "unknown",
    "invalid",
    "root pane",
    "glass pane",
    "layered pane",
    "content pane",
    "scroll bar",
}


def fields_unreadable(support: Sequence[bool]) -> bool:
    """Whether field text is genuinely unavailable for this window.

    ``support`` holds one entry per text field: whether that node implements the
    AT-SPI Text interface. A form whose fields implement Text is readable — even
    when every field is empty — so this only reports unreadable when there is at
    least one field and none of them support Text.
    """
    checked = list(support)
    return bool(checked) and not any(checked)


@dataclass
class AtspiApp:
    dest: str
    name: str
    path: str = A11Y_ROOT_PATH


@dataclass
class AtspiSnapshot:
    available: bool
    elements: list[Element] = field(default_factory=list)
    app_name: str = ""
    frame_title: str = ""
    node_count: int = 0
    truncated: bool = False
    duration_ms: int = 0
    notes: list[str] = field(default_factory=list)
    tree_hash: str = ""
    # True when the application publishes structure but no text at all, so
    # field values cannot be read (Chromium in "basic" accessibility mode).
    values_unavailable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "values_unavailable": self.values_unavailable,
            "app_name": self.app_name,
            "frame_title": self.frame_title,
            "node_count": self.node_count,
            "truncated": self.truncated,
            "duration_ms": self.duration_ms,
            "notes": self.notes,
            "tree_hash": self.tree_hash,
            "element_count": len(self.elements),
        }


def _bus_address_from_env() -> str:
    return os.environ.get("AT_SPI_BUS_ADDRESS", "").strip()


def discover_bus_address() -> str:
    """Ask the session bus for the accessibility bus address."""
    env = _bus_address_from_env()
    if env:
        return env
    busctl = util.which("busctl")
    if busctl:
        result = util.run_cmd(
            [busctl, "--user", "call", "org.a11y.Bus", "/org/a11y/bus", "org.a11y.Bus", "GetAddress"], timeout=4
        )
        if result.ok:
            address = result.stdout.strip()
            # busctl prints: s "unix:path=/run/user/1000/at-spi/bus_0,guid=..."
            if address.startswith("s "):
                address = address[2:].strip()
            address = address.strip('"')
            if address.startswith("unix:"):
                return address
    gdbus = util.which("gdbus")
    if gdbus:
        result = util.run_cmd(
            [
                gdbus,
                "call",
                "--session",
                "--dest",
                "org.a11y.Bus",
                "--object-path",
                "/org/a11y/bus",
                "--method",
                "org.a11y.Bus.GetAddress",
            ],
            timeout=4,
        )
        if result.ok:
            try:
                address = gvariant.parse_call_result(result.stdout)
            except gvariant.GVariantError:
                address = ""
            if isinstance(address, str) and address.startswith("unix:"):
                return address
    return ""


def atspi_available() -> tuple[bool, str]:
    """Check whether accessibility data can be read on this machine."""
    if not util.which("gdbus"):
        return False, "gdbus (glib2.0-bin) is not installed, so the accessibility bus cannot be queried"
    address = discover_bus_address()
    if not address:
        return False, "The AT-SPI accessibility bus is not running (is at-spi2-core installed and enabled?)"
    result = util.run_cmd(
        [
            util.which("gdbus") or "gdbus",
            "call",
            "--address",
            address,
            "--dest",
            A11Y_REGISTRY,
            "--object-path",
            A11Y_ROOT_PATH,
            "--method",
            "org.a11y.atspi.Accessible.GetChildren",
        ],
        timeout=4,
    )
    if not result.ok:
        return False, f"AT-SPI registry did not answer: {util.truncated(result.stderr.strip(), 160)}"
    return True, ""


class AtspiClient:
    """Reads accessibility trees for running applications."""

    def __init__(self, max_nodes: int = 320, cache_ttl_ms: int = 1200) -> None:
        self.max_nodes = max_nodes
        self.cache_ttl_ms = cache_ttl_ms
        self._lock = threading.RLock()
        self._apps_cache: tuple[int, list[AtspiApp]] | None = None
        self._gdbus = util.which("gdbus")
        self._address = ""
        self._address_checked_at = 0
        self._frame_cache: dict[str, tuple[int, list[tuple[str, str, str]]]] = {}
        self.last_error = ""
        self._status_enabled = False

    def ensure_status_enabled(self) -> bool:
        """Announce that assistive technology is present on this desktop.

        ``org.a11y.Status.IsEnabled`` is how the session tells Chromium, Electron
        and GNOME applications that a client needs the full accessibility tree.
        Without it those applications publish structure only — roles and names,
        but no field values and no text — so typed values cannot be verified.
        Real assistive technology sets this flag; GuidePilot sets it once per
        process. Applications started *before* the flag was set have to be
        restarted before they publish text, which the UI reports when it detects
        entries without values.
        """
        if self._status_enabled:
            return True
        if not self._gdbus:
            return False
        result = util.run_cmd(
            [
                self._gdbus,
                "call",
                "--session",
                "--dest",
                "org.a11y.Bus",
                "--object-path",
                "/org/a11y/bus",
                "--method",
                "org.freedesktop.DBus.Properties.Set",
                "org.a11y.Status",
                "IsEnabled",
                "<true>",
            ],
            timeout=3.0,
        )
        self._status_enabled = result.ok
        return self._status_enabled

    # -- low level ---------------------------------------------------------
    def _ensure_address(self) -> str:
        with self._lock:
            now = util.now_ms()
            if self._address and now - self._address_checked_at < 30000:
                return self._address
            self._address = discover_bus_address()
            self._address_checked_at = now
            return self._address

    def _call(self, dest: str, path: str, iface: str, method: str, *args: str, timeout: float = 3.0) -> Any:
        if not self._gdbus:
            return None
        address = self._ensure_address()
        if not address:
            return None
        argv = [
            self._gdbus,
            "call",
            "--address",
            address,
            "--dest",
            dest,
            "--object-path",
            path,
            "--method",
            f"{iface}.{method}",
            *args,
        ]
        result = util.run_cmd(argv, timeout=timeout)
        if not result.ok:
            if result.stderr:
                self.last_error = util.truncated(result.stderr.strip(), 200)
            return None
        try:
            return gvariant.parse_call_result(result.stdout)
        except gvariant.GVariantError as exc:
            self.last_error = str(exc)
            return None

    def _properties_all(self, dest: str, path: str) -> dict[str, Any]:
        """Fetch all Accessible properties in one round-trip (name, child count, interfaces)."""
        value = self._call(
            dest,
            path,
            "org.freedesktop.DBus.Properties",
            "GetAll",
            "org.a11y.atspi.Accessible",
        )
        return value if isinstance(value, dict) else {}

    def _property(self, dest: str, path: str, name: str) -> str | None:
        value = self._call(
            dest,
            path,
            "org.freedesktop.DBus.Properties",
            "Get",
            "org.a11y.atspi.Accessible",
            name,
        )
        if isinstance(value, str):
            return value
        if isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, str):
                    return item
        return None

    def _role(self, dest: str, path: str) -> str:
        value = self._call(dest, path, "org.a11y.atspi.Accessible", "GetRoleName")
        return value if isinstance(value, str) else ""

    def _children(self, dest: str, path: str) -> list[tuple[str, str]]:
        value = self._call(dest, path, "org.a11y.atspi.Accessible", "GetChildren")
        out: list[tuple[str, str]] = []
        if isinstance(value, (list, tuple)):
            for entry in value:
                if isinstance(entry, (list, tuple)) and len(entry) >= 2 and isinstance(entry[0], str):
                    child_path = entry[1]
                    if isinstance(child_path, str):
                        out.append((entry[0], child_path))
        return out

    def _interfaces(self, dest: str, path: str) -> set[str]:
        value = self._call(dest, path, "org.a11y.atspi.Accessible", "GetInterfaces")
        if isinstance(value, (list, tuple)):
            return {v for v in value if isinstance(v, str)}
        return set()

    def _extents(self, dest: str, path: str) -> Rect | None:
        value = self._call(dest, path, "org.a11y.atspi.Component", "GetExtents", "0")
        rect = None
        while isinstance(value, (list, tuple)) and value and isinstance(value[0], (list, tuple)):
            value = value[0]
        if isinstance(value, (list, tuple)) and len(value) == 4:
            try:
                rect = Rect(int(value[0]), int(value[1]), int(value[2]), int(value[3]))
            except (TypeError, ValueError):
                return None
        return rect

    def _text_supported(self, dest: str, path: str) -> bool:
        """Whether this node implements the AT-SPI Text interface at all.

        An empty field and an application that publishes no text both read back
        as ``""``. The difference is whether the call *answered*: a readable but
        empty field returns an empty string, while a missing Text interface
        fails and yields ``None``. Asking for one character is enough to tell
        them apart, and avoids guessing from the empty reading itself — which is
        how a "no text support" flag ends up stuck on a perfectly good form.
        """
        value = self._call(dest, path, "org.a11y.atspi.Text", "GetText", "0", "1")
        return value is not None

    def _text_value(self, dest: str, path: str) -> str | None:
        # A positive end offset is required: gdbus would parse "-1" as a flag.
        value = self._call(dest, path, "org.a11y.atspi.Text", "GetText", "0", "10000")
        if isinstance(value, str):
            # Combo boxes report an object-replacement placeholder when empty.
            cleaned = value.replace("\ufffc", "").strip()
            if cleaned:
                return util.truncated(cleaned, 4000)
        return None

    def _combo_value(self, dest: str, path: str) -> str | None:
        """Read the selected option of a combo box via the SELECTED state.

        Toolkits do not expose a combo box's current text through ``Text``, so the
        selected child option is located in the accessibility tree instead.
        """
        for container_dest, container_path in self._children(dest, path):
            options = self._children(container_dest, container_path)
            if not options:
                options = [(container_dest, container_path)]
            for option_dest, option_path in options:
                states = self._states(option_dest, option_path)
                if states and state_set(states, STATE_SELECTED):
                    name = self._property(option_dest, option_path, "Name") or ""
                    if name.strip():
                        return util.truncated(name.strip(), 200)
        return None

    def _states(self, dest: str, path: str) -> list[int]:
        """Read the AT-SPI state bitfield (see atspi-constants.h AtspiStateType)."""
        value = self._call(dest, path, "org.a11y.atspi.Accessible", "GetState")
        if isinstance(value, (list, tuple)):
            out: list[int] = []
            for item in value:
                try:
                    out.append(int(item))
                except (TypeError, ValueError):
                    continue
            return out
        return []

    # -- applications ------------------------------------------------------
    def apps(self, force: bool = False) -> list[AtspiApp]:
        with self._lock:
            cached = self._apps_cache
            if cached and not force and util.now_ms() - cached[0] < self.cache_ttl_ms:
                return list(cached[1])
        children = self._children(A11Y_REGISTRY, A11Y_ROOT_PATH)
        apps: list[AtspiApp] = []
        # App names are cheap; fetch concurrently.
        def describe(child: tuple[str, str]) -> AtspiApp | None:
            dest, path = child
            name = self._property(dest, path, "Name")
            if not name:
                return None
            return AtspiApp(dest=dest, name=name, path=path)

        with ThreadPoolExecutor(max_workers=8) as pool:
            for app in pool.map(describe, children):
                if app is not None:
                    apps.append(app)
        with self._lock:
            self._apps_cache = (util.now_ms(), apps)
        return apps

    def _frame_titles(self, app: AtspiApp) -> list[tuple[str, str, str]]:
        """Cached list of (title, dest, path) for an application's frames."""
        with self._lock:
            cached = self._frame_cache.get(app.dest)
            if cached and util.now_ms() - cached[0] < self.cache_ttl_ms:
                return cached[1]
        frames: list[tuple[str, str, str]] = []
        for child_dest, child_path in self._children(app.dest, app.path)[:6]:
            if self._role(child_dest, child_path) != "frame":
                continue
            title = self._property(child_dest, child_path, "Name") or ""
            frames.append((title, child_dest, child_path))
        with self._lock:
            self._frame_cache[app.dest] = (util.now_ms(), frames)
        return frames

    def find_app(self, hint: str | None, title: str | None = None) -> AtspiApp | None:
        """Find the accessibility application that owns the observed window.

        Toolkit name is matched first (cheap). Browser-based apps report generic
        foundation names (``electron``, ``chromium``, ``firefox``), so a window
        title match against the app's frames disambiguates them. Frame titles are
        cached for a moment to keep repeated observations cheap.
        """
        apps = self.apps()
        if not apps:
            return None
        needle = util.normalize_text(hint or "")
        title_norm = util.normalize_text(title or "")
        generic = {"electron", "chromium", "chrome", "chrome browser", "browser", "node", "unknown"}
        hint_tokens = {t for t in needle.split() if t}
        scored: list[tuple[float, AtspiApp]] = []
        for app in apps:
            app_name = util.normalize_text(app.name)
            score = 0.0
            if needle and app_name:
                if needle == app_name:
                    score += 4.0
                elif needle in app_name or app_name in needle:
                    score += 2.5
                elif app_name in generic and (app_name in hint_tokens or any(t in generic for t in hint_tokens)):
                    score += 1.2
                elif "electron" in hint_tokens and app_name in generic:
                    score += 1.2
            if score > 0:
                scored.append((score, app))

        if title_norm:
            # A frame title match is the most reliable signal for Electron,
            # Chromium, Firefox and other multi-window apps. Score every frame
            # instead of returning the first containment hit: "GuidePilot" is a
            # substring of "GuidePilot Demo Application", so first-match order
            # would ground the user's app on GuidePilot's own window.
            best: tuple[int, int, AtspiApp] | None = None
            for app in apps:
                for frame_title, _dest, _path in self._frame_titles(app):
                    frame_norm = util.normalize_text(frame_title)
                    if not frame_norm:
                        continue
                    if frame_norm == title_norm:
                        score, length = 3, len(frame_norm)
                    elif frame_norm in title_norm or title_norm in frame_norm:
                        score, length = 2, len(frame_norm)
                    else:
                        continue
                    if best is None or (score, length) > (best[0], best[1]):
                        best = (score, length, app)
            if best is not None:
                return best[2]
        if scored:
            scored.sort(key=lambda item: item[0], reverse=True)
            return scored[0][1]
        return None

    # -- snapshot ----------------------------------------------------------
    def snapshot(
        self,
        app_hint: str | None = None,
        window_title: str | None = None,
        window_rect: Rect | None = None,
        max_nodes: int | None = None,
        include_values: bool = True,
        retry: bool = True,
    ) -> AtspiSnapshot:
        started = util.now_ms()
        limit = max_nodes or self.max_nodes
        notes: list[str] = []
        if not self._gdbus:
            return AtspiSnapshot(False, notes=["gdbus is not installed"], duration_ms=util.now_ms() - started)
        self.ensure_status_enabled()
        app = self.find_app(app_hint, window_title)
        if app is None:
            return AtspiSnapshot(
                False,
                notes=[
                    "No accessibility application matches the observed window. "
                    "The application may not expose AT-SPI (common for Electron/Chromium apps started "
                    "without accessibility, or Java/Qt apps without the a11y bridge)."
                ],
                duration_ms=util.now_ms() - started,
            )

        elements: list[Element] = []
        node_count = 0
        truncated = False
        frame_title = ""

        def node_meta(dest: str, path: str) -> dict[str, Any]:
            props = self._properties_all(dest, path)
            name = props.get("Name") if isinstance(props.get("Name"), str) else ""
            description = props.get("Description") if isinstance(props.get("Description"), str) else ""
            interfaces = props.get("Interfaces") if isinstance(props.get("Interfaces"), list) else []
            try:
                child_count = int(props.get("ChildCount") or 0)
            except (TypeError, ValueError):
                child_count = 1
            role = self._role(dest, path)
            return {
                "dest": dest,
                "path": path,
                "role": role,
                "name": name or description,
                "description": description,
                "interfaces": set(i for i in interfaces if isinstance(i, str)),
                "child_count": child_count,
            }

        # Breadth-first traversal with concurrent metadata fetches per level.
        frontier: list[tuple[str, str]] = [(app.dest, app.path)]
        depth = 0
        seen: set[str] = set()
        deadline = time.monotonic() + 4.0
        while frontier and node_count < limit and depth < 22:
            if truncated or time.monotonic() > deadline:
                if time.monotonic() > deadline:
                    notes.append("Accessibility traversal hit its time budget; results may be incomplete")
                break
            with ThreadPoolExecutor(max_workers=8) as pool:
                metas = [m for m in pool.map(lambda child: node_meta(*child), frontier) if m is not None]
            next_frontier: list[tuple[str, str]] = []

            def enrich(meta: dict[str, Any]) -> Element | None:
                role = meta["role"]
                name = (meta["name"] or "").strip()
                if name and not util.normalize_text(name):
                    # Labels such as "\n" carry no information.
                    name = ""
                rect = self._extents(meta["dest"], meta["path"])
                value = None
                if include_values and role in {"entry", "password text", "text", "combo box", "spin button"}:
                    # Chromium does not publish an "Interfaces" entry in GetAll, so
                    # the Text interface is attempted directly; unsupported nodes
                    # simply fail the call and yield no value.
                    value = self._text_value(meta["dest"], meta["path"])
                if include_values and role == "combo box":
                    value = self._combo_value(meta["dest"], meta["path"]) or value
                states: list[int] = []
                if role in {"check box", "radio button", "toggle button", "switch"}:
                    states = self._states(meta["dest"], meta["path"])
                if rect is None or rect.is_degenerate():
                    if role and role not in CONTAINER_ROLES:
                        return None
                    rect = rect or Rect(0, 0, 0, 0)
                if not name and not value:
                    if role not in {"frame", "window", "dialog", "alert", "form", "document web"}:
                        # Unnamed structural boxes (panels, toolbars, fillers) add
                        # noise to matching and prompt context without adding
                        # information; they are traversed but not emitted.
                        return None
                attributes: dict[str, Any] = {"bus": meta["dest"], "path": meta["path"]}
                if states:
                    attributes["states"] = states
                    attributes["checked"] = state_set(states, STATE_CHECKED)
                    attributes["enabled"] = state_set(states, STATE_ENABLED)
                    attributes["invalid"] = state_set(states, STATE_INVALID_ENTRY)
                element = Element(
                    id=f"a11y-{meta['dest'].replace(':', '')}-{meta['path'].rsplit('/', 1)[-1]}",
                    name=name.strip(),
                    role=role or "unknown",
                    rect=rect,
                    source=ElementSource.ATSPI,
                    value=value,
                    enabled=attributes.get("enabled", True),
                    confidence=1.0,
                    app=app.name,
                    attributes=attributes,
                )
                if window_rect is not None and not rect.is_degenerate():
                    if not window_rect.inflate(8).intersects(rect):
                        return None
                return element

            with ThreadPoolExecutor(max_workers=8) as pool:
                enriched = list(pool.map(enrich, metas))
            for meta, element in zip(metas, enriched):
                node_count += 1
                if meta["role"] == "frame" and not frame_title and meta["name"]:
                    frame_title = meta["name"]
                if element is not None:
                    if element.role not in _SKIP_ROLES:
                        elements.append(element)
                if meta["child_count"] <= 0:
                    continue
                for child in self._children(meta["dest"], meta["path"]):
                    key = f"{child[0]}{child[1]}"
                    if key in seen:
                        continue
                    seen.add(key)
                    next_frontier.append(child)
                if node_count >= limit:
                    truncated = True
                    break
            frontier = next_frontier
            depth += 1

        if truncated:
            notes.append(f"Accessibility tree truncated at {limit} nodes; refine the target to see more")
        digest_source = "|".join(f"{e.role}:{e.name}:{e.rect.to_list()}" for e in elements)
        tree_hash = hashlib.sha256(digest_source.encode("utf-8", "replace")).hexdigest()[:24]
        if not elements:
            notes.append("Accessibility tree contained no labelled elements for this window")
        values_unavailable = False
        entries = [e for e in elements if e.role in {"entry", "password text"}]
        if include_values and entries and not any((e.value or "").strip() for e in entries):
            # Every entry read back empty. That is either an empty form (normal)
            # or an application that exposes no text at all. Ask the nodes
            # whether they implement Text instead of inferring it from the
            # empty strings, which would be indistinguishable from a blank form.
            support = [
                self._text_supported(e.attributes["bus"], e.attributes["path"])
                for e in entries
                if e.attributes.get("bus") and e.attributes.get("path")
            ]
            if fields_unreadable(support):
                values_unavailable = True
                notes.append(
                    "This application publishes control names but no field text, so values cannot be read back. "
                    "Chromium and Electron do that in basic accessibility mode; restarting the application with "
                    "--force-renderer-accessibility (or enabling accessibility support in its settings) restores them."
                )
        if retry and len(elements) <= 6:
            # Chromium and Electron build their accessibility tree lazily, when
            # the first client asks for it: the first read of a freshly started
            # application sees only the frame and its menus, and a tree that has
            # gone idle can take a moment to come back. Read again a few times
            # with growing pauses instead of reporting an empty screen that is
            # really just an unbuilt one. Each re-read returns a fuller tree, so
            # the wait only costs time when the application really has nothing.
            best = elements
            best_notes: list[str] = []
            for delay in (0.4, 0.8, 1.4):
                time.sleep(delay)
                attempt = self.snapshot(app_hint, window_title, window_rect, max_nodes, include_values, retry=False)
                if len(attempt.elements) > len(best):
                    best = attempt.elements
                    best_notes = attempt.notes
                    values_unavailable = attempt.values_unavailable
                    elements = best
                    if len(best) > 6:
                        break
            if best_notes:
                notes = notes + best_notes
        return AtspiSnapshot(
            available=True,
            values_unavailable=values_unavailable,
            elements=elements,
            app_name=app.name,
            frame_title=frame_title,
            node_count=node_count,
            truncated=truncated,
            duration_ms=util.now_ms() - started,
            notes=notes,
            tree_hash=tree_hash,
        )

    # -- targeted lookups --------------------------------------------------
    def find_element(
        self,
        label: str,
        roles: Iterable[str] | None = None,
        app_hint: str | None = None,
        window_title: str | None = None,
        window_rect: Rect | None = None,
    ) -> Element | None:
        """Fetch one element by label (used by verification and adaptive re-checks)."""
        snapshot = self.snapshot(app_hint=app_hint, window_title=window_title, window_rect=window_rect)
        target = util.normalize_text(label)
        role_filter = set(roles or [])
        best: tuple[float, Element] | None = None
        for element in snapshot.elements:
            if role_filter and element.role not in role_filter:
                continue
            haystack = element.searchable_text()
            if not haystack:
                continue
            if target == haystack:
                score = 1.0
            elif target and target in haystack:
                score = 0.9
            elif haystack in target:
                score = 0.75
            else:
                continue
            if best is None or score > best[0]:
                best = (score, element)
        return best[1] if best else None


def atspi_env_available() -> bool:
    ok, _ = atspi_available()
    return ok
