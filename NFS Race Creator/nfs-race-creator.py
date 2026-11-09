from __future__ import annotations

# --- Standard library ---
import copy, ctypes, heapq, json, logging, math, os, queue, re, struct, subprocess, sys, tempfile, threading, time
import tkinter as tk
import tkinter.font as tkfont
from collections import deque
from ctypes import wintypes
from dataclasses import asdict, dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# --- Third-party ---
import numpy as np
from PIL import Image, ImageTk
from scipy import ndimage

log = logging.getLogger(__name__)

# ============================================================================
# SECTION 1 - WINDOWS & GAME-PROCESS SERVICES
# ============================================================================


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020

DEFAULT_IMAGE_BASE = 0x400000
ADDR_SAVE_HOT_POSITION = 0x1336494
ADDR_GAMEFLOW_MANAGER = 0xDA57B8
ADDR_IS_LOST_FOCUS = 0xDEB99A
ADDR_SAVE_SIGNATURE = 0x870BC0
OFF_SAVE_HOT_POSITION = ADDR_SAVE_HOT_POSITION - DEFAULT_IMAGE_BASE
OFF_GAMEFLOW_MANAGER = ADDR_GAMEFLOW_MANAGER - DEFAULT_IMAGE_BASE
OFF_IS_LOST_FOCUS = ADDR_IS_LOST_FOCUS - DEFAULT_IMAGE_BASE
OFF_SAVE_SIGNATURE = ADDR_SAVE_SIGNATURE - DEFAULT_IMAGE_BASE
SAVE_SIGNATURE = b"\x83\x3d\x94\x64\x33\x01\x00"

HOT_LINE_RE = re.compile(r"HOTPOSITION:\s*([-+]?\d+(?:\.\d+)?)\s*,\s*" r"([-+]?\d+(?:\.\d+)?)\s*,\s*" r"([-+]?\d+(?:\.\d+)?)\s+" r"Angle\s*=\s*(0x[0-9a-fA-F]+|[-+]?\d+(?:\.\d+)?)\s+" r"Speed\s*=\s*([-+]?\d+(?:\.\d+)?)", re.IGNORECASE)

class CaptureError(RuntimeError): pass
class GameNotRunning(CaptureError): pass
class UnsupportedVersion(CaptureError): pass
class NotInGameplay(CaptureError): pass
class CaptureTimeout(CaptureError): pass

@dataclass
class HotPoint:
    x: float
    y: float
    z: float
    rotation: float
    speed: float = 0.0
    slot: int = 0
    source: str = ""

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x, self.y, self.z, self.rotation)

def normalize_angle(deg: float) -> float:
    return ((deg + 180.0) % 360.0) - 180.0

def _parse_angle(raw: str) -> float:
    raw = raw.strip()
    if raw.lower().startswith("0x"):
        deg = int(raw, 16) * 360.0 / 65536.0
    else:
        deg = float(raw)
    return normalize_angle(deg)

def parse_hot_text(text: str, source: str = "", slot: int = 0) -> list[HotPoint]:
    points: list[HotPoint] = []
    for line in text.splitlines():
        match = HOT_LINE_RE.search(line)
        if not match:
            continue
        x, y, z, angle, speed = match.groups()
        points.append(HotPoint(x=float(x), y=float(y), z=float(z), rotation=_parse_angle(angle), speed=float(speed), slot=slot or len(points) + 1, source=source))
    return points

def find_hot_files(game_root: str | Path) -> list[Path]:
    tracks = Path(game_root) / "TRACKS"
    if not tracks.is_dir():
        log.debug("no TRACKS folder under %s", game_root)
        return []
    files = sorted((p for p in tracks.iterdir() if p.is_file() and p.suffix.lower() == ".hot"), key=lambda p: p.name.lower())
    log.debug("found %d .hot file(s) in %s", len(files), tracks)
    return files

def read_hot_file(path: str | Path) -> list[HotPoint]:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = path.read_text(encoding="mbcs", errors="replace")
    points = parse_hot_text(text, source=str(path))
    log.debug("read %d hot position(s) from %s", len(points), path)
    return points

def read_slot(game_root: str | Path, slot: int) -> HotPoint | None:
    slot = max(1, min(5, int(slot)))
    for path in find_hot_files(game_root):
        points = read_hot_file(path)
        if len(points) >= slot:
            log.info("hot slot %d read from %s", slot, path)
            return points[slot - 1]
    log.info("hot slot %d is empty (no matching .hot file)", slot)
    return None

class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

class _MODULEENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("th32ModuleID", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD), ("GlblcntUsage", wintypes.DWORD), ("ProccntUsage", wintypes.DWORD), ("modBaseAddr", ctypes.c_void_p), ("modBaseSize", wintypes.DWORD), ("hModule", wintypes.HMODULE), ("szModule", wintypes.WCHAR * 256), ("szExePath", wintypes.WCHAR * 260)]

kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
kernel32.Process32FirstW.restype = wintypes.BOOL
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
kernel32.Process32NextW.restype = wintypes.BOOL
kernel32.Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MODULEENTRY32W)]
kernel32.Module32FirstW.restype = wintypes.BOOL
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
kernel32.ReadProcessMemory.restype = wintypes.BOOL
kernel32.WriteProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
kernel32.WriteProcessMemory.restype = wintypes.BOOL

def _snapshot_processes():
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return
    entry = _PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            yield entry
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)

def _module_base(pid: int, module_name: str) -> tuple[int, int]:
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return 0, 0
    entry = _MODULEENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        ok = kernel32.Module32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szModule.lower() == module_name.lower():
                return int(entry.modBaseAddr or 0), int(entry.modBaseSize or 0)
            ok = kernel32.Module32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return 0, 0

MODULEENTRY32W = _MODULEENTRY32W
PROCESSENTRY32W = _PROCESSENTRY32W
kernel32.Module32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MODULEENTRY32W)]
kernel32.Module32NextW.restype = wintypes.BOOL

class NfsProcess:
    def __init__(self, pid: int, handle: int, base: int, size: int, exe: str = "nfs.exe"):
        self.pid = pid
        self.handle = handle
        self.base = base
        self.size = size
        self.exe = exe
        self.signature_ok = False
        try:
            self.signature_ok = self.read(base + OFF_SAVE_SIGNATURE, len(SAVE_SIGNATURE)) == SAVE_SIGNATURE
        except CaptureError:
            self.signature_ok = False
        log.debug("Process %d base 0x%08X size 0x%X signature %s", pid, base, size, "OK" if self.signature_ok else "Mismatch",)

    @classmethod
    def find(cls, exe_name: str = "nfs.exe") -> "NfsProcess":
        for entry in _snapshot_processes():
            if entry.szExeFile.lower() != exe_name.lower():
                continue
            pid = int(entry.th32ProcessID)
            base, size = _module_base(pid, exe_name)
            if not base:
                log.debug("skipping PID %d: module %s not enumerated", pid, exe_name)
                continue
            handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_OPERATION | PROCESS_VM_READ | PROCESS_VM_WRITE, False, pid)
            if not handle:
                log.warning("OpenProcess failed for %s PID %d (WinError %s)", exe_name, pid, ctypes.get_last_error())
                raise GameNotRunning(f"Found {exe_name} (PID {pid}) but could not open it. Run Race Creator as administrator.")
            log.info("found %s PID %d, base 0x%08X", exe_name, pid, base)
            return cls(pid, int(handle), base, size, exe_name)
        log.debug("%s is not running", exe_name)
        raise GameNotRunning(f"{exe_name} is not running. Start the game first, then press Attach.")

    def close(self) -> None:
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = 0

    def read(self, address: int, size: int) -> bytes:
        if not self.handle:
            raise GameNotRunning("Game handle is closed.")
        buffer = ctypes.create_string_buffer(size)
        read = ctypes.c_size_t(0)
        ok = kernel32.ReadProcessMemory(self.handle, ctypes.c_void_p(address), buffer, size, ctypes.byref(read))
        if not ok or read.value != size:
            raise CaptureError(f"ReadProcessMemory failed at 0x{address:08X}" f"({ctypes.get_last_error()}).")
        return buffer.raw

    def read_u32(self, address: int) -> int:
        return struct.unpack("<I", self.read(address, 4))[0]

    def read_u8(self, address: int) -> int:
        return self.read(address, 1)[0]

    def write_u32(self, address: int, value: int) -> None:
        if not self.handle:
            raise GameNotRunning("Game handle is closed.")
        log.debug("write 0x%08X = %d", address, int(value) & 0xFFFFFFFF)
        buffer = ctypes.create_string_buffer(struct.pack("<I", int(value) & 0xFFFFFFFF))
        written = ctypes.c_size_t(0)
        ok = kernel32.WriteProcessMemory(self.handle, ctypes.c_void_p(address), buffer, len(buffer), ctypes.byref(written))
        if not ok or written.value != len(buffer):
            raise CaptureError(f"WriteProcessMemory failed at 0x{address:08X}" f"({ctypes.get_last_error()}). Run Race Creator as administrator.")

    def require_supported(self) -> None:
        if not self.signature_ok:
            log.warning("nfs.exe signature mismatch (PID %d, base 0x%08X)", self.pid, self.base)
            raise UnsupportedVersion("This nfs.exe build is not supported. The Race Creator expects NFS Undercover v1.0.0.1 (signature at 0x870BC0 / RVA 0x470BC0).")

    def gameplay_state(self) -> tuple[int, bool]:
        gameflow = self.read_u32(self.base + OFF_GAMEFLOW_MANAGER)
        focused = self.read_u8(self.base + OFF_IS_LOST_FOCUS) == 0
        return gameflow, focused

    def gameplay_ready(self) -> tuple[bool, int, bool]:
        gameflow, focused = self.gameplay_state()
        return gameflow == 6 and focused, gameflow, focused

    def capture(self, slot: int, game_root: str | Path, timeout: float = 3.0) -> HotPoint:
        self.require_supported()
        slot = max(1, min(5, int(slot)))
        ready, gameflow, focused = self.gameplay_ready()
        log.info("capture slot %d: gameflow=%s focused=%s", slot, gameflow, focused)
        if not ready:
            reason = "the game is in a menu or loading screen" if gameflow != 6 else "the game window is not focused"
            log.warning("capture rejected: %s", reason)
            raise NotInGameplay(f"Cannot capture while {reason}. Drive the car, then try again.")
        
        tracks = Path(game_root) / "TRACKS"
        if not tracks.is_dir():
            log.error("capture rejected: TRACKS folder missing under %s", game_root)
            raise CaptureError(f"TRACKS folder not found in '{game_root}'. Set the correct game folder.")

        before: dict[Path, tuple[int, int]] = {}
        for path in find_hot_files(game_root):
            try:
                st = path.stat()
                before[path] = (st.st_mtime_ns, st.st_size)
            except OSError:
                pass

        self.write_u32(self.base + OFF_SAVE_HOT_POSITION, slot)
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                for path in find_hot_files(game_root):
                    try:
                        st = path.stat()
                    except OSError:
                        continue
                    previous = before.get(path)
                    if previous is None or previous != (st.st_mtime_ns, st.st_size):
                        log.debug("hot file changed: %s", path)
                        points = read_hot_file(path)
                        if not points:
                            continue
                        index = slot - 1
                        point = points[index] if index < len(points) else points[-1]
                        point.slot = slot
                        point.source = str(path)
                        log.info("Capture slot %d ok: %.2f, %.2f, %.2f, rot %.2f from %s", slot, point.x, point.y, point.z, point.rotation, path.name)
                        return point
                time.sleep(0.02)
        finally:
            try:
                if self.read_u32(self.base + OFF_SAVE_HOT_POSITION) != 0:
                    self.write_u32(self.base + OFF_SAVE_HOT_POSITION, 0)
            except CaptureError:
                log.debug("could not reset the hot-position flag", exc_info=True)

        log.warning("capture slot %d timed out after %.1fs (no .hot rewrite)", slot, timeout)
        raise CaptureTimeout("The game did not write its hot-position file. Make sure the car is spawned in free-roam or a race and the game is not paused.")

def detect_game_root(app_dir: str | Path) -> Path:
    app_dir = Path(app_dir).resolve()
    candidates = [app_dir.parent, Path(r"D:\Games\Need for Speed Undercover"), Path(r"C:\Program Files (x86)\Steam\steamapps\common\Need for Speed Undercover")]
    for candidate in candidates:
        try:
            if (candidate / "nfs.exe").is_file():
                log.debug("detected game root %s", candidate)
                return candidate
        except OSError:
            continue
    log.debug("could not detect nfs.exe; falling back to %s", app_dir.parent)
    return app_dir.parent

user32 = ctypes.WinDLL("user32", use_last_error=True)

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
PM_NOREMOVE = 0x0000
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL
user32.PeekMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
    wintypes.UINT,
]
user32.PeekMessageW.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostThreadMessageW.restype = wintypes.BOOL
kernel32.GetCurrentThreadId.restype = wintypes.DWORD

HOTKEY_ACTIONS = [
    ("capture_start", "Set start grid", "Ctrl+Alt+S"),
    ("capture_finish", "Set finish line", "Ctrl+Alt+F"),
    ("add_checkpoint", "Add checkpoint", "Ctrl+Alt+C"),
    ("add_bonus", "Add time bonus checkpoint", "Ctrl+Alt+V"),
    ("add_shortcut", "Add shortcut", "Ctrl+Alt+H"),
    ("add_reset", "Set reset trigger", "Ctrl+Alt+R"),
    ("set_map_marker", "Set TrackPosition marker", "Ctrl+Alt+M"),
    ("undo", "Undo last capture", "Ctrl+Alt+Z"),
    ("toggle_hud", "Show/hide in-game HUD", "Ctrl+Alt+O"),
    ("open_editor", "Bring editor to front", "Ctrl+Alt+E"),
    ("export", "Export .nfsms + .end", "Ctrl+Alt+X"),
]

DEFAULT_HOTKEYS = {action: spec for action, _label, spec in HOTKEY_ACTIONS}
HOTKEY_LABELS = {action: label for action, label, _spec in HOTKEY_ACTIONS}

_KEY_NAMES = {
    "space": 0x20,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "backspace": 0x08,
    "insert": 0x2D,
    "ins": 0x2D,
    "delete": 0x2E,
    "del": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pgup": 0x21,
    "pagedown": 0x22,
    "pgdn": 0x22,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "numpad0": 0x60,
    "numpad1": 0x61,
    "numpad2": 0x62,
    "numpad3": 0x63,
    "numpad4": 0x64,
    "numpad5": 0x65,
    "numpad6": 0x66,
    "numpad7": 0x67,
    "numpad8": 0x68,
    "numpad9": 0x69,
    "multiply": 0x6A,
    "add": 0x6B,
    "plus": 0x6B,
    "subtract": 0x6D,
    "minus": 0x6D,
    "decimal": 0x6E,
    "divide": 0x6F,
    "`": 0xC0,
    "-": 0xBD,
    "=": 0xBB,
    "[": 0xDB,
    "]": 0xDD,
    "\\": 0xDC,
    ";": 0xBA,
    "'": 0xDE,
    ",": 0xBC,
    ".": 0xBE,
    "/": 0xBF,
}


_VK_NAMES: dict[int, str] = {}
for _key_name, _key_vk in _KEY_NAMES.items():
    _VK_NAMES.setdefault(_key_vk, _key_name)


def _vk_to_name(vk: int) -> str | None:
    if vk in _VK_NAMES:
        return _VK_NAMES[vk]
    if 0x70 <= vk <= 0x87:
        return f"F{vk - 0x6F}"
    if 0x30 <= vk <= 0x5A and chr(vk).isalnum():
        return chr(vk)
    return None


def alternative_hotkeys(spec: str) -> list[str]:
    try:
        mods, vk = parse_hotkey(spec)
    except ValueError:
        return []
    key_name = _vk_to_name(vk)
    if key_name is None:
        return []
    names: list[str] = []
    if mods & MOD_CONTROL:
        names.append("Ctrl")
    if mods & MOD_ALT:
        names.append("Alt")
    if mods & MOD_SHIFT:
        names.append("Shift")
    if mods & MOD_WIN:
        names.append("Win")
    alternatives: list[str] = []
    for extra in (["Shift"], ["Win"], ["Ctrl", "Shift"], ["Alt", "Shift"]):
        combined = names + [name for name in extra if name not in names]
        if len(combined) == len(names):
            continue
        text = "+".join(combined + [key_name])
        if text not in alternatives:
            alternatives.append(text)
    return alternatives


def parse_hotkey(spec: str) -> tuple[int, int]:
    if not spec or not spec.strip():
        raise ValueError("empty hotkey")
    parts = [part.strip().lower() for part in spec.split("+") if part.strip()]
    mods = 0
    key = None
    for part in parts:
        if part in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif part == "alt":
            mods |= MOD_ALT
        elif part == "shift":
            mods |= MOD_SHIFT
        elif part in ("win", "super", "meta"):
            mods |= MOD_WIN
        else:
            if key is not None:
                raise ValueError(f"more than one key in '{spec}'")
            key = _key_to_vk(part)
    if key is None:
        raise ValueError(f"no key in '{spec}'")
    return mods, key


def _key_to_vk(name: str) -> int:
    if name in _KEY_NAMES:
        return _KEY_NAMES[name]
    if len(name) == 1 and (name.isalnum()):
        return ord(name.upper())
    if name.startswith("f") and name[1:].isdigit():
        number = int(name[1:])
        if 1 <= number <= 24:
            return 0x70 + number - 1
    if name.startswith("vk_"):
        try:
            return int(name[3:], 0)
        except ValueError:
            pass
    raise ValueError(f"unknown key '{name}'")


def hotkey_to_text(spec: str) -> str:
    try:
        parse_hotkey(spec)
        return spec
    except ValueError:
        return f"invalid: {spec}"


class HotkeyManager:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._queue: queue.Queue[str] = queue.Queue()
        self._ready = threading.Event()
        self._bindings: dict[str, str] = {}
        self.errors: list[str] = []
        # (action, spec, WinError) for combos another program already owns.
        # They are reported separately so the caller can try a fallback combo
        # before treating the binding as a real error.
        self.conflicts: list[tuple[str, str, int]] = []

    def start(self, bindings: dict[str, str]) -> list[str]:
        self.stop()
        self._bindings = dict(bindings)
        self.errors = []
        self.conflicts = []
        log.info("registering %d global hotkey(s)", len(self._bindings))
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="nfs-hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(2.0)
        return list(self.errors)

    def _run(self) -> None:
        self._thread_id = int(kernel32.GetCurrentThreadId())
        log.debug("hotkey message thread started (id %d)", self._thread_id)
        message = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(message), None, 0, 0, PM_NOREMOVE)

        id_to_action: dict[int, str] = {}
        registered: list[int] = []
        hotkey_id = 1
        seen: dict[tuple[int, int], str] = {}
        for action, spec in self._bindings.items():
            if not spec or not spec.strip():
                # An empty binding means the action is disabled on purpose.
                log.info("hotkey %s: disabled (empty binding)", action)
                continue
            try:
                mods, vk = parse_hotkey(spec)
            except ValueError as exc:
                self.errors.append(f"{HOTKEY_LABELS.get(action, action)}: {exc}")
                log.warning("hotkey %s: %s", action, exc)
                continue
            signature = (mods, vk)
            if signature in seen:
                problem = (
                    f"{HOTKEY_LABELS.get(action, action)}: same combination as "
                    f"{HOTKEY_LABELS.get(seen[signature], seen[signature])}"
                )
                self.errors.append(problem)
                log.warning("hotkey %s: %s", action, problem)
                continue
            if not user32.RegisterHotKey(None, hotkey_id, mods | MOD_NOREPEAT, vk):
                error = ctypes.get_last_error()
                self.conflicts.append((action, spec, error))
                # DEBUG only: the caller retries with alternative_hotkeys() and
                # logs the outcome at INFO.  A warning here would be misleading
                # when the fallback succeeds.
                log.debug(
                    "hotkey %s (%s) unavailable (WinError %s)", action, spec, error
                )
            else:
                id_to_action[hotkey_id] = action
                registered.append(hotkey_id)
                seen[signature] = action
                log.debug("registered %s = %s (id %d)", action, spec, hotkey_id)
            hotkey_id += 1
        log.info(
            "hotkey thread ready: %d registered, %d conflict(s), %d other error(s)",
            len(registered),
            len(self.conflicts),
            len(self.errors),
        )
        self._ready.set()
        try:
            while True:
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result <= 0:
                    break
                if message.message == WM_HOTKEY:
                    action = id_to_action.get(int(message.wParam))
                    if action:
                        self._queue.put(action)
        finally:
            for registered_id in registered:
                user32.UnregisterHotKey(None, registered_id)
            self._thread_id = 0

    def stop(self) -> None:
        thread = self._thread
        if thread and thread.is_alive():
            if self._thread_id:
                user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            thread.join(1.5)
        self._thread = None
        self._thread_id = 0
        log.debug("hotkey manager stopped")

    def poll(self) -> list[str]:
        actions: list[str] = []
        while True:
            try:
                actions.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return actions

# ------------------------------------------------------------------------
# Source: nfsrc/services/window.py
# ------------------------------------------------------------------------


GWL_STYLE = -16
GWL_EXSTYLE = -20

WS_CAPTION = 0x00C00000
WS_BORDER = 0x00800000
WS_DLGFRAME = 0x00400000
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_SYSMENU = 0x00080000
WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000

WS_EX_DLGMODALFRAME = 0x00000001
WS_EX_WINDOWEDGE = 0x00000100
WS_EX_CLIENTEDGE = 0x00000200
WS_EX_STATICEDGE = 0x00020000
WS_EX_TOOLWINDOW = 0x00000080

SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040
SWP_NOOWNERZORDER = 0x0200

HWND_TOP = 0
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2

MONITOR_DEFAULTTONEAREST = 2

_FRAME_STYLE = (
    WS_CAPTION
    | WS_BORDER
    | WS_DLGFRAME
    | WS_THICKFRAME
    | WS_MINIMIZEBOX
    | WS_MAXIMIZEBOX
    | WS_SYSMENU
)
_FRAME_EXSTYLE = WS_EX_DLGMODALFRAME | WS_EX_WINDOWEDGE | WS_EX_CLIENTEDGE | WS_EX_STATICEDGE


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
user32.SetWindowLongW.restype = ctypes.c_long
user32.SetWindowPos.argtypes = [
    wintypes.HWND,
    wintypes.HWND,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.UINT,
]
user32.SetWindowPos.restype = wintypes.BOOL
user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
user32.MonitorFromWindow.restype = ctypes.c_void_p
user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFO)]
user32.GetMonitorInfoW.restype = wintypes.BOOL

_saved_windows: dict[int, tuple[int, int, tuple[int, int, int, int]]] = {}


def is_window(hwnd: int) -> bool:
    return bool(hwnd) and bool(user32.IsWindow(hwnd))


def find_window(pid: int) -> int:
    """Return the largest visible top-level window owned by *pid*."""
    matches: list[tuple[int, int]] = []

    @_WNDENUMPROC
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid:
            return True
        exstyle = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if exstyle & WS_EX_TOOLWINDOW:
            return True
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width < 200 or height < 150:
            return True
        matches.append((width * height, int(hwnd)))
        return True

    user32.EnumWindows(callback, 0)
    hwnd = max(matches)[1] if matches else 0
    log.debug("find_window(pid=%d) -> hwnd 0x%X (%d candidates)", pid, hwnd, len(matches))
    return hwnd


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = wintypes.RECT()
    if not hwnd or not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return (0, 0, 0, 0)
    return (rect.left, rect.top, rect.right, rect.bottom)


def monitor_rect(hwnd: int) -> tuple[int, int, int, int]:
    monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if not monitor or not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return (0, 0, 0, 0)
    rc = info.rcMonitor
    return (rc.left, rc.top, rc.right, rc.bottom)


def window_has_frame(hwnd: int) -> bool:
    return bool(user32.GetWindowLongW(hwnd, GWL_STYLE) & WS_CAPTION)


def covers_monitor(hwnd: int, tolerance: int = 2) -> bool:
    left, top, right, bottom = window_rect(hwnd)
    mleft, mtop, mright, mbottom = monitor_rect(hwnd)
    return (
        abs(left - mleft) <= tolerance
        and abs(top - mtop) <= tolerance
        and abs(right - mright) <= tolerance
        and abs(bottom - mbottom) <= tolerance
    )


def apply_borderless(hwnd: int) -> bool:
    """Turn a framed game window into a monitor-filling borderless window."""
    if not is_window(hwnd):
        return False
    style = user32.GetWindowLongW(hwnd, GWL_STYLE)
    exstyle = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    if hwnd not in _saved_windows:
        _saved_windows[hwnd] = (style, exstyle, window_rect(hwnd))
    monitor = monitor_rect(hwnd)
    if monitor[2] - monitor[0] < 200:
        return False
    new_style = (style & ~_FRAME_STYLE) | WS_POPUP | (style & WS_VISIBLE)
    new_exstyle = exstyle & ~_FRAME_EXSTYLE
    user32.SetWindowLongW(hwnd, GWL_STYLE, new_style)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, new_exstyle)
    left, top, right, bottom = monitor
    ok = bool(
        user32.SetWindowPos(
            hwnd,
            HWND_TOP,
            left,
            top,
            right - left,
            bottom - top,
            SWP_FRAMECHANGED | SWP_SHOWWINDOW | SWP_NOOWNERZORDER | SWP_NOACTIVATE,
        )
    )
    log.info(
        "borderless %s for hwnd 0x%X at %d,%d %dx%d",
        "applied" if ok else "failed",
        hwnd,
        left,
        top,
        right - left,
        bottom - top,
    )
    return ok


def restore_window(hwnd: int) -> bool:
    saved = _saved_windows.pop(hwnd, None)
    if saved is None or not is_window(hwnd):
        return False
    style, exstyle, rect = saved
    user32.SetWindowLongW(hwnd, GWL_STYLE, style)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, exstyle)
    left, top, right, bottom = rect
    ok = bool(
        user32.SetWindowPos(
            hwnd,
            HWND_NOTOPMOST,
            left,
            top,
            right - left,
            bottom - top,
            SWP_FRAMECHANGED | SWP_SHOWWINDOW | SWP_NOACTIVATE,
        )
    )
    log.info("restored game window hwnd 0x%X (%s)", hwnd, "ok" if ok else "failed")
    return ok


def keep_above(hwnd: int) -> bool:
    """Reassert TOPMOST without moving, resizing or activating the HUD."""
    if not is_window(hwnd):
        return False
    return bool(
        user32.SetWindowPos(
            hwnd,
            HWND_TOPMOST,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )
    )


def launch_windowed(game_root: str | Path) -> subprocess.Popen:
    root = Path(game_root)
    exe = root / "nfs.exe"
    if not exe.is_file():
        log.error("launch cancelled: nfs.exe not found in %s", root)
        raise FileNotFoundError(f"nfs.exe not found in {root}")
    log.info("launching %s -windowed (cwd %s)", exe, root)
    return subprocess.Popen([str(exe), "-windowed"], cwd=str(root))


# ============================================================================
# SECTION 2 - CONFIGURATION, THEME & HUD LAYOUT
# ============================================================================

# ------------------------------------------------------------------------
# Source: nfsrc/config.py
# ------------------------------------------------------------------------
APP_DIR = Path(__file__).resolve().parent
ASSETS_DIR = APP_DIR / "assets"
SETTINGS_PATH = APP_DIR / "settings.json"
THEME_PATH = APP_DIR / "theme.json"
HUD_LAYOUT_PATH = APP_DIR / "hud_layout.json"
PROJECTS_DIR = APP_DIR / "projects"
EXPORTS_DIR = APP_DIR / "exports"
ROUTE_MAP_PATH = ASSETS_DIR / "Map.png"
LOG_DIR = APP_DIR / "logs"
LOG_PATH = LOG_DIR / "race_creator.log"


DEFAULT_SETTINGS = {
    "game_root": str(detect_game_root(APP_DIR)),
    "hot_slot": 5,
    "hotkeys": dict(DEFAULT_HOTKEYS),
    "hud_visible": True,
    "hud_click_through": False,
    "hud_alpha": 0.86,
    "hud_alpha_custom": False,
    "hud_borderless": True,
    "hud_x": 18,
    "hud_y": 13,
    "export_dir": str(EXPORTS_DIR),
    "export_base_auto": True,
    "export_base": "",
    "last_project": "",
    "map_flip_y": False,
}

INT_FIELDS = {
    "level": (0, 18),
    "numLaps": (1, 255),
    "cash_value": (0, None),
    "cash_dominate": (0, None),
    "xp": (0, None),
    "xp_dominate": (0, None),
    "traffic_level": (0, 100),
    "threat_min": (0, 9),
    "threat_max": (0, 9),
    "ai_accel_min": (0, 100),
    "ai_accel_max": (0, 100),
    "ai_handling_min": (0, 100),
    "ai_handling_max": (0, 100),
    "ai_skill_min": (0, 100),
    "ai_skill_max": (0, 100),
    "ai_speed_min": (0, 100),
    "ai_speed_max": (0, 100),
    "track_length": (0, None),
    "race_bin_children": (0, None),
    "track_category_children": (0, None),
    "quick_race_children": (0, None),
    "collectors_quick_race_children": (0, None),
    "career_unlock_children": (0, None),
}

FLOAT_FIELDS = ("domination_time", "track_record", "time_limit")

STRING_FIELDS = (
    "event_id",
    "track_name",
    "event_title",
    "event_title_ru",
    "loc_key",
    "intro_nis",
    "end_english_file",
    "end_russian_file",
    "end_collection",
    "end_section",
)

BOOL_FIELDS = (
    "cops_in_race",
    "collectors_edition",
    "master_event",
    "register_race_bin",
    "register_track_category",
    "unlock_quick_race",
    "unlock_collectors_quick_race",
    "register_career_event",
    "end_export_russian",
)


def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def setup_logging(verbose: bool = False) -> None:
    root = logging.getLogger()
    if getattr(root, "_nfs_configured", False):
        if verbose:
            root.setLevel(logging.DEBUG)
        return
    level = logging.DEBUG if verbose else logging.INFO
    root.setLevel(level)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:
        print(f"warning: cannot open log file {LOG_PATH}: {exc}", file=sys.stderr)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)
    root._nfs_configured = True  # type: ignore[attr-defined]
    log.info("logging to %s (level %s)", LOG_PATH, logging.getLevelName(level))


class MemoryLogHandler(logging.Handler):
    def __init__(self, capacity: int = 2000):
        super().__init__()
        self._entries: deque[tuple[int, str]] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._version = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
        except Exception:
            return
        with self._lock:
            self._entries.append((record.levelno, text))
            self._version += 1

    def snapshot(self) -> tuple[int, list[tuple[int, str]]]:
        with self._lock:
            return self._version, list(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._version += 1

def load_settings() -> dict:
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("settings not loaded from %s (%s); using defaults", SETTINGS_PATH, exc)
        return settings
    if isinstance(saved, dict):
        for key, value in saved.items():
            if key == "hotkeys" and isinstance(value, dict):
                settings["hotkeys"].update(value)
            elif key in settings:
                settings[key] = value
    log.info("settings loaded from %s", SETTINGS_PATH)
    return settings


def fmt_number(value) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, float):
        if value == int(value):
            return str(int(value))
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned.strip("._") or "race"


def parse_float_field(raw: str, default: float) -> tuple[float, bool]:
    try:
        return float(str(raw).strip()), True
    except (TypeError, ValueError):
        return default, False


def parse_int_field(raw: str, default: int) -> tuple[int, bool]:
    try:
        return int(float(str(raw).strip())), True
    except (TypeError, ValueError):
        return default, False


def _hot_slot_number(raw: str) -> int:
    try:
        return max(1, min(5, int(raw)))
    except ValueError:
        return 5


def _point_fields(point: Point) -> dict[str, float]:
    return {
        "x": point.x,
        "y": point.y,
        "z": point.z,
        "rotation": point.rotation,
        "dim_x": point.dim_x,
        "dim_y": point.dim_y,
        "dim_z": point.dim_z,
        "flare_spacing": point.flare_spacing,
        "time_bonus": point.time_bonus,
        "width": point.width,
    }

# ------------------------------------------------------------------------
# Source: nfsrc/theme.py
# ------------------------------------------------------------------------

DEFAULT_THEME: dict = {
    "editor": {
        "font_family": "Segoe UI",
        "font_size": 9,
        "mono_family": "Consolas",
        "mono_size": 9,
        "bg": "#1b1d21",
        "bg_light": "#262a30",
        "bg_field": "#2f343c",
        "fg": "#e6e8eb",
        "fg_muted": "#9aa4b2",
        "accent": "#e84545",
        "text_bg": "#14161a",
        "text_fg": "#e6e8eb",
        "panel_border": "#3a414b",
        "panel_title_fg": "#9fd3ff",
        "button_bg": "#303640",
        "button_active": "#3d4551",
        "button_disabled": "#262a30",
        "accent_button_bg": "#8f2626",
        "accent_button_active": "#b03030",
        "accent_button_fg": "#ffffff",
        "combo_bg": "#303640",
        "combo_select_bg": "#8f2626",
        "combo_select_fg": "#ffffff",
        "tab_bg": "#262b33",
        "tab_selected_bg": "#39414d",
        "tab_selected_fg": "#ffffff",
        "tree_selected_bg": "#8f2626",
        "log_error_fg": "#ff6b6b",
        "log_warning_fg": "#ffd23f",
        "window_min_width": 1060,
        "window_min_height": 680,
        "window_max_width": 1320,
        "window_max_height": 880,
    },
    "map": {
        "background": "#0a0d12",
        "grid": "#2f3a46",
        "route_center": "#ff7f00",
        "route_halo": "#ffff00",
        "route_width": 3,
        "route_halo_steps": 3,
        "route_ramp": "#ffffff",
        "spur_center": "#7fff00",
        "spur_halo": "#00ff00",
        "spur_width": 2,
        "marker_outline": "#000000",
        "marker_font_family": "Segoe UI",
        "marker_font_size": 8,
        "overlay_fg": "#9aa4b2",
        "error_fg": "#ff8080",
        "points": {
            "start": "#48d17a",
            "finish": "#ff5b5b",
            "checkpoint": "#ffd23f",
            "bonus": "#ff9f43",
            "shortcut": "#b07cff",
            "reset": "#25d0c0",
            "map": "#ff3dc8",
        },
        "short_labels": {
            "start": "S",
            "finish": "F",
            "checkpoint": "CP",
            "bonus": "TBCP",
            "shortcut": "SC",
            "reset": "R",
            "map": "TRACK POS",
        },
    },
    "hud": {
        "background": "#0c0f14",
        "border": "#39424f",
        "title_fg": "#ff5b5b",
        "status_fg": "#e6e8eb",
        "race_fg": "#9fd3ff",
        "counts_fg": "#c7d0db",
        "action_fg": "#5ef58a",
        "flash_fg": "#ffd23f",
        "hint_fg": "#6f7c8c",
        "font_family": "Consolas",
        "font_size": 9,
        "title_family": "Segoe UI",
        "title_size": 9,
        "hint_size": 8,
    },
}

def _merge(base: dict, override) -> dict:
    result = copy.deepcopy(base)
    if not isinstance(override, dict):
        return result
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result

class Theme:
    def __init__(self, data: dict, path=None):
        self.data = _merge(DEFAULT_THEME, data)
        self.path = path

    def section(self, name: str) -> dict:
        value = self.data.get(name)
        return value if isinstance(value, dict) else {}

    def value(self, section: str, key: str, default=None):
        return self.section(section).get(key, default)

    @property
    def editor(self) -> dict:
        return self.section("editor")

    @property
    def map(self) -> dict:
        return self.section("map")

    @property
    def hud(self) -> dict:
        return self.section("hud")

    def font(self, section: str, family_key: str, size_key: str, **overrides) -> tuple:
        cfg = self.section(section)
        family = cfg.get(family_key) or "Segoe UI"
        size = int(cfg.get(size_key) or 9)
        return (family, size, *overrides)

def load_theme(path=THEME_PATH) -> Theme:
    data = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        log.info("theme loaded from %s", path)
    except FileNotFoundError:
        log.info("theme file not found at %s; using built-in defaults", path)
        try:
            path.write_text(json.dumps(DEFAULT_THEME, indent=2), encoding="utf-8")
            log.info("default theme written to %s", path)
        except OSError as exc:
            log.warning("could not write default theme: %s", exc)
    except (OSError, ValueError) as exc:
        log.warning("theme not loaded from %s (%s); using defaults", path, exc)
    return Theme(data, path)


theme = load_theme()

_EDITOR = theme.editor
_MAP = theme.map
_HUD = theme.hud

BG = _EDITOR["bg"]
BG_LIGHT = _EDITOR["bg_light"]
BG_FIELD = _EDITOR["bg_field"]
FG = _EDITOR["fg"]
FG_MUTED = _EDITOR["fg_muted"]
ACCENT = _EDITOR["accent"]
TEXT_BG = _EDITOR["text_bg"]
TEXT_FG = _EDITOR["text_fg"]

PANEL_BORDER = _EDITOR["panel_border"]
PANEL_TITLE_FG = _EDITOR["panel_title_fg"]
BUTTON_BG = _EDITOR["button_bg"]
BUTTON_ACTIVE = _EDITOR["button_active"]
BUTTON_DISABLED = _EDITOR["button_disabled"]
ACCENT_BUTTON_BG = _EDITOR["accent_button_bg"]
ACCENT_BUTTON_ACTIVE = _EDITOR["accent_button_active"]
ACCENT_BUTTON_FG = _EDITOR["accent_button_fg"]
COMBO_BG = _EDITOR["combo_bg"]
COMBO_SELECT_BG = _EDITOR["combo_select_bg"]
COMBO_SELECT_FG = _EDITOR["combo_select_fg"]
TAB_BG = _EDITOR["tab_bg"]
TAB_SELECTED_BG = _EDITOR["tab_selected_bg"]
TAB_SELECTED_FG = _EDITOR["tab_selected_fg"]
TREE_SELECTED_BG = _EDITOR["tree_selected_bg"]
LOG_ERROR_FG = _EDITOR["log_error_fg"]
LOG_WARNING_FG = _EDITOR["log_warning_fg"]

UI_FONT_FAMILY = _EDITOR["font_family"]
UI_FONT_SIZE = int(_EDITOR["font_size"])
MONO_FONT_FAMILY = _EDITOR["mono_family"]
MONO_FONT_SIZE = int(_EDITOR["mono_size"])
UI_FONT = (UI_FONT_FAMILY, UI_FONT_SIZE)
MONO_FONT = (MONO_FONT_FAMILY, MONO_FONT_SIZE)

TEXT_WIDGET_STYLE = dict(bg=TEXT_BG, fg=TEXT_FG, insertbackground=TEXT_FG, relief="flat")

MAP_POINT_COLORS = dict(_MAP["points"])
MAP_POINT_LABELS = dict(_MAP["short_labels"])

POINT_COLORS = MAP_POINT_COLORS
POINT_SHORT = MAP_POINT_LABELS

HUD_BG = _HUD["background"]
HUD_BORDER = _HUD["border"]
HUD_TITLE_FG = _HUD["title_fg"]
HUD_STATUS_FG = _HUD["status_fg"]
HUD_RACE_FG = _HUD["race_fg"]
HUD_COUNTS_FG = _HUD["counts_fg"]
HUD_ACTION_FG = _HUD["action_fg"]
HUD_FLASH_FG = _HUD["flash_fg"]
HUD_HINT_FG = _HUD["hint_fg"]
HUD_FONT_FAMILY = _HUD["font_family"]
HUD_FONT_SIZE = int(_HUD["font_size"])
HUD_TITLE_FAMILY = _HUD["title_family"]
HUD_TITLE_SIZE = int(_HUD["title_size"])
HUD_HINT_SIZE = int(_HUD["hint_size"])

# ------------------------------------------------------------------------
# Source: nfsrc/hud_layout.py
# ------------------------------------------------------------------------

DEFAULT_HINT = ("Ctrl + Alt:\nS - Start; F - Finish; C - Checkpoint; B - Time Bonus Checkpoint\nH - Shortcut; Z - Undo; O - Hide HUD; E - Open Editor\nCtrl + Alt + Shift:\nR - Reset Player Trigger; M - Event Marker")
_DEFAULT_ROWS = [
    {"id": "title", "visible": True, "text": "NFS UNDERCOVER | RACE CREATOR", "bold": True, "anchor": "n", "justify": "center", "padx": 10, "pady": 5, "fill": "x"},
    {"id": "status", "visible": True, "prefix": "Game: ", "bold": True, "anchor": "n", "justify": "center", "padx": 10, "pady": 0, "fill": "x"},
    {"id": "race", "visible": True, "bold": True, "anchor": "n", "justify": "center", "padx": 10, "pady": 0, "fill": "x"},
    {"id": "counts", "visible": True, "bold": True, "anchor": "n", "justify": "center", "padx": 10, "pady": 0, "fill": "x"},
    {"id": "action", "visible": True, "bold": True, "anchor": "n", "justify": "center", "padx": 10, "pady": 0, "fill": "x"},
    {"id": "hint", "visible": True, "text": DEFAULT_HINT, "bold": True, "anchor": "n", "justify": "center", "padx": 10, "pady": 0, "fill": "x"},
]

DEFAULT_HUD_LAYOUT = {"width": 550, "x": 20, "y": 20, "use_saved_position": False, "alpha": 0.75, "background": HUD_BG, "border": HUD_BORDER, "border_width": 2, "rows": _DEFAULT_ROWS}
ROW_IDS = {row["id"] for row in _DEFAULT_ROWS}

def _default_row(row_id: str) -> dict:
    for row in _DEFAULT_ROWS:
        if row["id"] == row_id:
            return copy.deepcopy(row)
    return {}

def _normalise_rows(raw) -> list[dict]:
    if not isinstance(raw, list):
        return [_default_row(row["id"]) for row in _DEFAULT_ROWS]
    rows: list[dict] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            log.warning("hud_layout: row %r is not an object; skipped", item)
            continue
        row_id = str(item.get("id", "")).strip()
        if row_id not in ROW_IDS:
            log.warning("hud_layout: unknown row id %r; skipped", row_id)
            continue
        if row_id in seen:
            log.warning("hud_layout: duplicate row id %r; skipped", row_id)
            continue
        seen.add(row_id)
        merged = _default_row(row_id)
        merged.update(item)
        rows.append(merged)
    return rows

def load_hud_layout(path=HUD_LAYOUT_PATH) -> dict:
    data: dict = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        log.info("HUD layout loaded from %s", path)
    except FileNotFoundError:
        log.info("HUD layout not found at %s; writing defaults", path)
        try:
            path.write_text(json.dumps(DEFAULT_HUD_LAYOUT, indent=2), encoding="utf-8")
        except OSError as exc:
            log.warning("could not write default HUD layout: %s", exc)
    except (OSError, ValueError) as exc:
        log.warning("HUD layout not loaded from %s (%s); using defaults", path, exc)
    layout = copy.deepcopy(DEFAULT_HUD_LAYOUT)
    if isinstance(data, dict):
        for key, value in data.items():
            if key != "rows":
                layout[key] = value
    defaults_by_id = {row["id"]: row for row in layout["rows"]}
    raw_rows = data.get("rows") if isinstance(data, dict) else None
    if raw_rows is None:
        rows = [_default_row(row_id) for row_id in defaults_by_id]
    else:
        rows = _normalise_rows(raw_rows)
    layout["rows"] = rows
    return layout

def font_for(row: dict, row_id: str = "") -> tuple:
    if row_id == "title":
        family = row.get("font", HUD_TITLE_FAMILY)
        size = int(row.get("size", HUD_TITLE_SIZE))
    elif row_id == "hint":
        family = row.get("font", HUD_FONT_FAMILY)
        size = int(row.get("size", HUD_HINT_SIZE))
    else:
        family = row.get("font", HUD_FONT_FAMILY)
        size = int(row.get("size", HUD_FONT_SIZE))
    if row.get("bold", row_id == "title"):
        return (family, size, "bold")
    return (family, size)


# ============================================================================
# SECTION 3 - DOMAIN MODELS & MAP DATA
# ============================================================================

# ------------------------------------------------------------------------
# Source: nfsrc/models/project.py
# ------------------------------------------------------------------------

REGIONS = ["region_a", "region_b", "region_c"]
REGION_LABELS = {"region_a": "Sunset Hills", "region_b": "Port Crescent", "region_c": "Palm Harbor"}
REGION_FIELD_INDEX = {"region_a": 0, "region_b": 2, "region_c": 1}
EVENT_TYPES = ["sprint", "circuit", "checkpoint"]
EVENT_TYPE_LABELS = {"sprint": "Sprint", "circuit": "Circuit", "checkpoint": "Checkpoint"}
TRACK_DIRS = {"sprint": "sprint", "circuit": "circuit", "checkpoint": "checkpoint"}
EVENT_NODE_TYPES = {"sprint": "p2p", "circuit": "circuit", "checkpoint": "tollboothrace"}
TYPE_PREFIX = {"sprint": "sp", "circuit": "cr", "checkpoint": "cp"}

MAX_CHECKPOINTS = 15
MAX_BONUS_CHECKPOINTS = 15
MAX_SHORTCUTS = 30
MAX_MAP = 3072.0

POINT_KINDS = ("start", "finish", "checkpoint", "bonus", "reset", "shortcut")
POINT_LABELS = {"start": "Start grid", "finish": "Finish line", "checkpoint": "Checkpoint", "bonus": "Time bonus checkpoint", "reset": "Reset player trigger", "shortcut": "Shortcut"}

DEFAULT_DATA = {
    "event_type": "sprint",
    "event_id": "E660",
    "track_name": "",
    "region": "region_a",
    "event_title": "Custom Race",
    "event_title_ru": "Custom Race",
    "loc_key": "",
    "localization": [],
    "level": 0,
    "cash_value": 0,
    "cash_dominate": 0,
    "xp": 0,
    "xp_dominate": 0,
    "num_laps": 1,
    "domination_time": 0.0,
    "track_record": 0.0,
    "time_limit": 0.0,
    "track_length": 0,
    "traffic_level": 0,
    "cops_in_race": False,
    "collectors_edition": True,
    "master_event": False,
    "intro_nis": "Race01",
    "threat_min": 0,
    "threat_max": 0,
    "ai_accel_min": 0,
    "ai_accel_max": 0,
    "ai_handling_min": 0,
    "ai_handling_max": 0,
    "ai_skill_min": 0,
    "ai_skill_max": 0,
    "ai_speed_min": 0,
    "ai_speed_max": 0,
    "track_position": [0.0, 0.0],
    "barriers": [1],
    "rewards_winner": ["0"],
    "rewards_dominate": ["0"],
    "end_english_file": "LANGUAGES\\English_Global.bin",
    "end_russian_file": "LANGUAGES\\Russian_Global.bin",
    "end_collection": "STRBlocks",
    "end_section": "Global",
    "end_export_russian": True,
    "register_race_bin": False,
    "race_bin_children": 0,
    "register_track_category": False,
    "track_category_children": 0,
    "unlock_quick_race": False,
    "quick_race_children": 0,
    "unlock_collectors_quick_race": False,
    "collectors_quick_race_children": 0,
    "register_career_event": False,
    "career_unlock_children": 0,
}

@dataclass
class Point:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    rotation: float = 0.0
    speed: float = 0.0
    dim_x: float = 1.0
    dim_y: float = 80.0
    dim_z: float = 40.0
    flare_spacing: float = 9.0
    time_bonus: float = 0.0
    width: float = 120.0

    @classmethod
    def from_hot(cls, hot) -> "Point":
        return cls(
            x=float(hot.x),
            y=float(hot.y),
            z=float(hot.z),
            rotation=float(hot.rotation),
            speed=float(getattr(hot, "speed", 0.0)),
        )

    @classmethod
    def from_dict(cls, values: dict) -> "Point":
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in values.items() if k in allowed})

    def to_dict(self) -> dict:
        return asdict(self)


def suggest_track_name(event_type: str, region: str, event_id: str) -> str:
    prefix = TYPE_PREFIX.get(event_type, "rc")
    suffix = region[-1] if region else "a"
    number = re.sub(r"\D", "", event_id) or "660"
    return f"rc_{prefix}_{suffix}_{number}"


class RaceProject:
    def __init__(self) -> None:
        self.data = copy.deepcopy(DEFAULT_DATA)
        self.start: Point | None = None
        self.finish: Point | None = None
        self.reset: Point | None = None
        self.checkpoints: list[Point] = []
        self.bonus_checkpoints: list[Point] = []
        self.shortcuts: list[Point] = []
        self.data["track_name"] = suggest_track_name(
            self.data["event_type"], self.data["region"], self.data["event_id"]
        )

    # ------------------------------------------------------------------ data
    @property
    def event_id(self) -> str:
        return str(self.data.get("event_id", "E660")).strip()

    @property
    def track_name(self) -> str:
        return str(self.data.get("track_name", "")).strip()

    @property
    def region(self) -> str:
        region = str(self.data.get("region", "region_a"))
        return region if region in REGIONS else "region_a"

    @property
    def event_type(self) -> str:
        event_type = str(self.data.get("event_type", "sprint"))
        return event_type if event_type in EVENT_TYPES else "sprint"

    @property
    def track_dir(self) -> str:
        return TRACK_DIRS[self.event_type]

    @property
    def track_path(self) -> str:
        return f"tracks/{self.region}/{self.track_dir}/{self.track_name}"

    @property
    def event_path(self) -> str:
        return f"race_bin_career/{self.event_id}"

    @property
    def region_index(self) -> int:
        return REGION_FIELD_INDEX.get(self.region, 0)

    def counts(self) -> dict[str, int]:
        return {
            "start": 1 if self.start else 0,
            "finish": 1 if self.finish else 0,
            "checkpoint": len(self.checkpoints),
            "bonus": len(self.bonus_checkpoints),
            "reset": 1 if self.reset else 0,
            "shortcut": len(self.shortcuts),
            "total_checkpoints": len(self.checkpoints) + len(self.bonus_checkpoints),
        }

    def summary(self) -> str:
        counts = self.counts()
        return (
            f"{self.event_id} | {EVENT_TYPE_LABELS[self.event_type]} | {self.region}\n"
            f"{counts['start']} start, {counts['finish']} finish, "
            f"{counts['checkpoint']} CP, {counts['bonus']} bonus, "
            f"{counts['shortcut']} shortcut, {counts['reset']} reset"
        )

    def _track_position_xy(self) -> tuple[float, float] | None:
        tp = self.data.get("track_position", [0, 0])
        try:
            return float(tp[0]), float(tp[1])
        except (TypeError, ValueError, IndexError):
            return None

    def track_position_gap(self) -> float | None:
        if self.start is None:
            return None

        stx, sty = world_to_trackposition(self.start.x, self.start.y)
        xy = self._track_position_xy()
        if xy is None:
            return None
        tx, ty = xy
        return ((tx - stx) ** 2 + (ty - sty) ** 2) ** 0.5

    # ---------------------------------------------------------------- points
    def apply_hot(self, kind: str, hot) -> str:
        point = Point.from_hot(hot)
        if kind == "start":
            self.start = point
            return "Start grid captured"
        if kind == "finish":
            self.finish = point
            return "Finish line captured"
        if kind == "reset":
            self.reset = point
            return "Reset player trigger captured"
        if kind == "checkpoint":
            if len(self.checkpoints) >= MAX_CHECKPOINTS:
                raise ValueError(f"Checkpoint limit reached ({MAX_CHECKPOINTS}).")
            self.checkpoints.append(point)
            return f"Checkpoint {len(self.checkpoints)} captured"
        if kind == "bonus":
            if len(self.bonus_checkpoints) >= MAX_BONUS_CHECKPOINTS:
                raise ValueError(
                    f"Time bonus checkpoint limit reached ({MAX_BONUS_CHECKPOINTS})."
                )
            self.bonus_checkpoints.append(point)
            return f"Time bonus checkpoint {len(self.bonus_checkpoints)} captured"
        if kind == "shortcut":
            if len(self.shortcuts) >= MAX_SHORTCUTS:
                raise ValueError(f"Shortcut limit reached ({MAX_SHORTCUTS}).")
            self.shortcuts.append(point)
            return f"Shortcut {len(self.shortcuts)} captured"
        if kind == "map":

            tx, ty = world_to_trackposition(float(hot.x), float(hot.y))
            self.data["track_position"] = [tx, ty]
            return "TrackPosition set from the car position"
        raise ValueError(f"Unknown point kind: {kind}")

    def point(self, kind: str, index: int = 0) -> Point | None:
        if kind == "start":
            return self.start
        if kind == "finish":
            return self.finish
        if kind == "reset":
            return self.reset
        if kind == "checkpoint":
            return self.checkpoints[index] if 0 <= index < len(self.checkpoints) else None
        if kind == "bonus":
            return (
                self.bonus_checkpoints[index]
                if 0 <= index < len(self.bonus_checkpoints)
                else None
            )
        if kind == "shortcut":
            return self.shortcuts[index] if 0 <= index < len(self.shortcuts) else None
        return None

    def delete_point(self, kind: str, index: int = 0) -> bool:
        if kind == "start":
            if self.start is None:
                return False
            self.start = None
            return True
        if kind == "finish":
            if self.finish is None:
                return False
            self.finish = None
            return True
        if kind == "reset":
            if self.reset is None:
                return False
            self.reset = None
            return True
        collections = {
            "checkpoint": self.checkpoints,
            "bonus": self.bonus_checkpoints,
            "shortcut": self.shortcuts,
        }
        items = collections.get(kind)
        if items is None or not (0 <= index < len(items)):
            return False
        del items[index]
        return True

    def move_point(self, kind: str, index: int, delta: int) -> bool:
        items = {
            "checkpoint": self.checkpoints,
            "bonus": self.bonus_checkpoints,
            "shortcut": self.shortcuts,
        }.get(kind)
        if items is None:
            return False
        new_index = index + delta
        if not (0 <= index < len(items) and 0 <= new_index < len(items)):
            return False
        items[index], items[new_index] = items[new_index], items[index]
        return True

    def ordered_points(self) -> list[tuple[str, int, str, Point]]:
        rows: list[tuple[str, int, str, Point]] = []
        if self.start:
            rows.append(("start", 0, "Start grid", self.start))
        if self.finish:
            rows.append(("finish", 0, "Finish line", self.finish))
        for i, point in enumerate(self.checkpoints, 1):
            rows.append(("checkpoint", i - 1, f"Checkpoint {i}", point))
        for i, point in enumerate(self.bonus_checkpoints, 1):
            rows.append(("bonus", i - 1, f"Time bonus {i}", point))
        for i, point in enumerate(self.shortcuts, 1):
            rows.append(("shortcut", i - 1, f"Shortcut {i}", point))
        if self.reset:
            rows.append(("reset", 0, "Reset trigger", self.reset))
        return rows

    # ---------------------------------------------------------- persistence
    def snapshot(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    def restore(self, snapshot: str) -> None:
        values = json.loads(snapshot)
        self.load_dict(values)

    def to_dict(self) -> dict:
        return {
            "format": "nfs-uc-race-creator",
            "version": 1,
            "data": self.data,
            "points": {
                "start": self.start.to_dict() if self.start else None,
                "finish": self.finish.to_dict() if self.finish else None,
                "reset": self.reset.to_dict() if self.reset else None,
                "checkpoint": [p.to_dict() for p in self.checkpoints],
                "bonus": [p.to_dict() for p in self.bonus_checkpoints],
                "shortcut": [p.to_dict() for p in self.shortcuts],
            },
        }

    def load_dict(self, values: dict) -> None:
        data = values.get("data", {})
        merged = copy.deepcopy(DEFAULT_DATA)
        for key, value in data.items():
            if key in merged:
                merged[key] = copy.deepcopy(value)
        self.data = merged
        if not self.data.get("track_name"):
            self.data["track_name"] = suggest_track_name(
                self.event_type, self.region, self.event_id
            )
        points = values.get("points", {})
        self.start = self._load_point(points.get("start"))
        self.finish = self._load_point(points.get("finish"))
        self.reset = self._load_point(points.get("reset"))
        self.checkpoints = [self._load_point(p) for p in points.get("checkpoint", [])]
        self.bonus_checkpoints = [self._load_point(p) for p in points.get("bonus", [])]
        self.shortcuts = [self._load_point(p) for p in points.get("shortcut", [])]
        self.checkpoints = [p for p in self.checkpoints if p is not None]
        self.bonus_checkpoints = [p for p in self.bonus_checkpoints if p is not None]
        self.shortcuts = [p for p in self.shortcuts if p is not None]

    @staticmethod
    def _load_point(values) -> Point | None:
        return Point.from_dict(values) if isinstance(values, dict) else None

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info("project saved: %s (%s)", path, self.summary().replace("\n", " / "))

    @classmethod
    def load(cls, path: str | Path) -> "RaceProject":
        path = Path(path)
        project = cls()
        project.load_dict(json.loads(path.read_text(encoding="utf-8")))
        log.info("project loaded: %s (%s)", path, project.summary().replace("\n", " / "))
        return project

    # ----------------------------------------------------------- validation
    def validate(self) -> list[str]:
        messages: list[str] = []
        if not re.fullmatch(r"E\d{3,}", self.event_id):
            messages.append(
                "ERROR: Event ID must look like E660 (letter E followed by at least 3 digits)."
            )
        elif int(self.event_id[1:]) < 660:
            messages.append(
                "WARN: The template expects a new event ID of 660 or greater."
            )
        if not re.fullmatch(r"[A-Za-z0-9_]+", self.track_name):
            messages.append(
                "ERROR: Track name may contain only letters, digits and underscores."
            )
        elif len(self.track_name) > 32:
            messages.append("WARN: Track name is longer than 32 characters.")
        if self.start is None:
            messages.append("ERROR: Start grid is missing.")
        if self.finish is None:
            messages.append("ERROR: Finish line is missing.")
        if self.reset is None:
            messages.append("WARN: Reset player trigger is missing.")
        total = len(self.checkpoints) + len(self.bonus_checkpoints)
        if total == 0:
            messages.append("WARN: No checkpoints captured.")
        if total > 15:
            messages.append("ERROR: More than 15 checkpoints (including time bonus).")
        if len(self.shortcuts) > MAX_SHORTCUTS:
            messages.append("ERROR: More than 30 shortcuts.")
        if not self.data.get("barriers"):
            messages.append("ERROR: At least one barrier is required by the template.")
        xy = self._track_position_xy()
        tx, ty = xy if xy is not None else (0.0, 0.0)
        if not (10 <= tx <= MAX_MAP - 10 and 10 <= ty <= MAX_MAP - 10):
            messages.append(
                "WARN: TrackPosition is outside the 3072x3072 map range. "
                "Pick the event marker on the Map tab."
            )
        if self.event_type == "checkpoint" and float(self.data.get("time_limit", 0)) <= 0:
            messages.append("WARN: Checkpoint races normally need a time limit above 0.")
        if self.finish is not None and (
            self.finish.dim_x <= 0 or self.finish.dim_y <= 0 or self.finish.dim_z <= 0
        ):
            messages.append("WARN: Finish line dimensions should be greater than 0.")
        if not str(self.data.get("event_title", "")).strip():
            messages.append("WARN: Event title is empty (only used by the .end export).")
        if self.data.get("register_race_bin") and self.data.get("race_bin_children", 0) <= 0:
            messages.append(
                "WARN: Set the current race_bin_career children count before registering."
            )
        if (
            self.data.get("register_track_category")
            and self.data.get("track_category_children", 0) <= 0
        ):
            messages.append(
                "WARN: Set the current track-category children count before registering."
            )
        if self.data.get("register_career_event") and not self.data.get("register_race_bin"):
            messages.append(
                "WARN: The career logic trigger unlocks an event that must already be "
                "appended to race_bin_career."
            )
        if (
            self.data.get("register_career_event")
            and self.data.get("career_unlock_children", 0) <= 0
        ):
            messages.append(
                "WARN: Set the current mw2_career/unlock children count before "
                "registering the career trigger."
            )
        section = str(self.data.get("end_section", "Global"))
        if section.upper() == "GLOBAL" and section != "Global":
            messages.append(
                "WARN: Undercover language vaults name their string block 'Global' "
                "(exact case); 'GLOBAL' is the Most Wanted name and Binary rejects it."
            )
        if messages:
            log.debug("validation of %s: %s", self.event_id, "; ".join(messages))
        return messages

# ------------------------------------------------------------------------
# Source: nfsrc/mapview/loader.py
# ------------------------------------------------------------------------

ROAD_RGB = (0, 138, 216)  # #008ad8

_mask_cache: dict = {}


def map_display_name() -> str:
    return ROUTE_MAP_PATH.name


def palette_road_indices(image: Image.Image) -> tuple[int, ...] | None:
    if image.mode != "P":
        return None
    palette = image.getpalette() or []
    hits = [
        index
        for index in range(len(palette) // 3)
        if tuple(palette[index * 3 : index * 3 + 3]) == ROAD_RGB
    ]
    return tuple(hits)


def _source_key(path: Path):
    stat = path.stat()
    return (str(path), stat.st_size, stat.st_mtime_ns)


def load_road_mask(path) -> np.ndarray:
    """Boolean (H, W) mask that is True exactly on road-color pixels."""
    path = Path(path)
    key = _source_key(path)
    cached = _mask_cache.get(key)
    if cached is not None:
        return cached
    with Image.open(path) as image:
        indices = palette_road_indices(image)
        if indices:
            mask = np.isin(np.asarray(image), list(indices))
        else:
            rgb = np.asarray(image.convert("RGB")).astype(np.int16)
            target = np.array(ROAD_RGB, dtype=np.int16)
            mask = (np.abs(rgb - target).sum(axis=2) == 0)
    _mask_cache.clear()
    _mask_cache[key] = mask
    log.info(
        "road mask built from %s: %.2f%% road pixels",
        path.name,
        100.0 * float(mask.mean()),
    )
    return mask


def road_mask_at_grid(path, grid: int, min_coverage: float = 0.25) -> np.ndarray:
    """Downsample the full-res road mask to ``grid`` x ``grid`` booleans."""
    full = load_road_mask(path)
    as_float = Image.fromarray(full.astype(np.float32) * 255.0)
    small = np.asarray(as_float.resize((grid, grid), Image.BILINEAR))
    return small >= 255.0 * min_coverage

# ------------------------------------------------------------------------
# Source: nfsrc/models/route.py
# ------------------------------------------------------------------------

WORLD_MIN_X = -7679.9
WORLD_MAX_X = 4308.9
WORLD_MIN_Y = -5106.1
WORLD_MAX_Y = 6866.7
WORLD_SPAN_X = WORLD_MAX_X - WORLD_MIN_X
WORLD_SPAN_Y = WORLD_MAX_Y - WORLD_MIN_Y

MAP_UV_DX = 81.0 / 3072.0
MAP_UV_DY = -226.0 / 3072.0

GRID_STEP_MAP = 144.0
GRID_X_MAP = 629.33
GRID_Y_MAP = 98.67
TRACKPOS_SPAN = 3072.0
GRID = 1024

_NEIGHBORS = (
    (-1, -1, 1.41421356),
    (0, -1, 1.0),
    (1, -1, 1.41421356),
    (-1, 0, 1.0),
    (1, 0, 1.0),
    (-1, 1, 1.41421356),
    (0, 1, 1.0),
    (1, 1, 1.41421356),
)


def world_to_uv(x: float, y: float) -> tuple[float, float]:
    """World -> TrackPosition uv (the game's 3072 frame)."""
    return ((x - WORLD_MIN_X) / WORLD_SPAN_X, (WORLD_MAX_Y - y) / WORLD_SPAN_Y)

def uv_to_world(u: float, v: float) -> tuple[float, float]:
    return (u * WORLD_SPAN_X + WORLD_MIN_X, WORLD_MAX_Y - v * WORLD_SPAN_Y)

def world_to_map_uv(x: float, y: float) -> tuple[float, float]:
    u, v = world_to_uv(x, y)
    return u + MAP_UV_DX, v + MAP_UV_DY

def map_uv_to_world(u: float, v: float) -> tuple[float, float]:
    return uv_to_world(u - MAP_UV_DX, v - MAP_UV_DY)

def world_to_trackposition(x: float, y: float) -> tuple[float, float]:
    u, v = world_to_uv(x, y)
    return u * TRACKPOS_SPAN, v * TRACKPOS_SPAN

def trackposition_to_world(tx: float, ty: float) -> tuple[float, float]:
    return uv_to_world(tx / TRACKPOS_SPAN, ty / TRACKPOS_SPAN)

def _chaikin(points: list[tuple]) -> list[tuple]:
    if len(points) < 3:
        return points
    out = [points[0]]
    for p, q in zip(points, points[1:]):
        out.append(tuple(0.75 * a + 0.25 * b for a, b in zip(p, q)))
        out.append(tuple(0.25 * a + 0.75 * b for a, b in zip(p, q)))
    out.append(points[-1])
    return out

def _simplify(points: list[tuple], tolerance: float = 0.35):
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    limit = tolerance * tolerance
    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        ax, ay = points[first][0], points[first][1]
        bx, by = points[last][0], points[last][1]
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        best = -1.0
        best_index = None
        for index in range(first + 1, last):
            px, py = points[index][0], points[index][1]
            if length2 == 0:
                distance2 = (px - ax) ** 2 + (py - ay) ** 2
            else:
                t = ((px - ax) * dx + (py - ay) * dy) / length2
                t = max(0.0, min(1.0, t))
                qx, qy = ax + t * dx, ay + t * dy
                distance2 = (px - qx) ** 2 + (py - qy) ** 2
            if distance2 > best:
                best = distance2
                best_index = index
        if best > limit and best_index is not None:
            keep[best_index] = True
            stack.append((first, best_index))
            stack.append((best_index, last))
    return [point for point, kept in zip(points, keep) if kept]

class RoadRouter:
    def __init__(self, image_path: str | Path, grid: int = GRID, min_coverage: float = 0.25, offroad_cost: float = 30.0) -> None:
        self.grid = grid
        self.image_path = Path(image_path)
        self.offroad_cost = offroad_cost
        self.road = road_mask_at_grid(self.image_path, grid, min_coverage)
        _distance, nearest = ndimage.distance_transform_edt(~self.road, return_indices=True)
        self.nearest = nearest
        self.cost = np.where(self.road, 1.0, float(offroad_cost)).astype(np.float32)
        self._cost_list = self.cost.ravel().tolist()
        log.info("road router ready: %s, grid %d, %.1f%% road pixels", self.image_path.name, grid, 100.0 * float(self.road.mean()))

    # ------------------------------------------------------------- coordinates
    def _to_grid(self, u: float, v: float) -> tuple[int, int]:
        limit = self.grid - 1
        x = max(0, min(limit, int(round(u * limit))))
        y = max(0, min(limit, int(round(v * limit))))
        return x, y

    def _to_uv(self, x: float, y: float) -> tuple[float, float]:
        limit = self.grid - 1
        return x / limit, y / limit

    def _snap(self, u: float, v: float) -> tuple[int, int]:
        x, y = self._to_grid(u, v)
        # distance_transform_edt returns (row, column) index pairs.
        row, col = self.nearest[:, y, x]
        return int(col), int(row)

    # --------------------------------------------------------------------- A*
    def _astar(self, src: tuple[int, int], dst: tuple[int, int], max_visits: int = 1_500_000):
        grid = self.grid
        sx, sy = src
        tx, ty = dst
        if (sx, sy) == (tx, ty):
            return [(sx, sy)]
        start = sy * grid + sx
        goal = ty * grid + tx
        gscore = np.full(grid * grid, np.inf, dtype=np.float32)
        gscore[start] = 0.0
        parent = np.full(grid * grid, -1, dtype=np.int32)
        closed = bytearray(grid * grid)
        cost = self._cost_list
        heap = [(0.0, start)]
        visited = 0
        while heap:
            _estimate, current = heapq.heappop(heap)
            if closed[current]:
                continue
            closed[current] = 1
            visited += 1
            if current == goal:
                break
            if visited > max_visits:
                log.warning("road route search gave up after %d cells", visited)
                return []
            cy, cx = divmod(current, grid)
            base = gscore[current]
            for dy, dx, step in _NEIGHBORS:
                ny = cy + dy
                nx = cx + dx
                if ny < 0 or ny >= grid or nx < 0 or nx >= grid:
                    continue
                neighbour = ny * grid + nx
                if closed[neighbour]:
                    continue
                candidate = base + step * cost[neighbour]
                if candidate < gscore[neighbour]:
                    gscore[neighbour] = candidate
                    parent[neighbour] = current
                    heuristic = math.hypot(nx - tx, ny - ty)
                    heapq.heappush(heap, (candidate + heuristic, neighbour))
        if goal != start and parent[goal] == -1:
            return []
        path: list[tuple[int, int]] = []
        current = goal
        while current != -1:
            y, x = divmod(current, grid)
            path.append((int(x), int(y)))
            current = int(parent[current])
        path.reverse()
        return path

    # ------------------------------------------------------------------ routes
    def _leg(self, start: tuple[int, int], end: tuple[int, int]) -> list[tuple[int, int]]:
        path = self._astar(start, end)
        if not path:
            log.warning("no road path between %s and %s; drawing a direct line", start, end)
            return [start, end]
        return path

    def _smooth(self, path: list[tuple]) -> list[tuple]:
        """Grid cells with Z -> smoothed (u, v, z) vertices.

        Z is interpolated between the captured points that anchor each leg;
        the map carries no height data of its own.
        """
        if len(path) < 3:
            return [(*self._to_uv(p[0], p[1]), float(p[2])) for p in path]
        rounded = _chaikin([(float(p[0]), float(p[1]), float(p[2])) for p in path])
        return [
            (*self._to_uv(p[0], p[1]), p[2])
            for p in _simplify(rounded)
        ]

    @staticmethod
    def _xyz(point) -> tuple[float, float, float]:
        z = float(point[2]) if len(point) > 2 else 0.0
        return float(point[0]), float(point[1]), z

    def route(self, world_points) -> list[tuple[float, float, float]]:
        if len(world_points) < 2:
            return []
        anchors = [
            (self._snap(*world_to_map_uv(x, y)), z)
            for x, y, z in (self._xyz(p) for p in world_points)
        ]
        anchors = [a for i, a in enumerate(anchors) if i == 0 or a[0] != anchors[i - 1][0]]
        if len(anchors) < 2:
            return []
        joined: list[tuple[int, int, float]] = []
        for (start, z0), (end, z1) in zip(anchors, anchors[1:]):
            leg = self._leg(start, end)
            count = len(leg)
            for i, (x, y) in enumerate(leg):
                t = i / (count - 1) if count > 1 else 0.0
                joined.append((x, y, z0 + (z1 - z0) * t))
        return self._smooth(joined)

    def shortcuts(
        self,
        main: list[tuple[float, float, float]],
        shortcut_points,
    ) -> list[list[tuple[float, float, float]]]:
        if not main or not shortcut_points:
            return []
        main_grid = [(self._to_grid(u, v), z) for u, v, z in main]
        spurs: list[list[tuple[float, float, float]]] = []
        for point in shortcut_points:
            x, y, z = self._xyz(point)
            target = self._snap(*world_to_map_uv(x, y))
            anchor, anchor_z = min(
                main_grid,
                key=lambda cell: (cell[0][0] - target[0]) ** 2 + (cell[0][1] - target[1]) ** 2,
            )
            leg = self._leg(anchor, target)
            count = len(leg)
            joined = []
            for i, (lx, ly) in enumerate(leg):
                t = i / (count - 1) if count > 1 else 0.0
                joined.append((lx, ly, anchor_z + (z - anchor_z) * t))
            spurs.append(self._smooth(joined))
        return spurs

_router_lock = threading.Lock()
_routers: dict[tuple[str, int], RoadRouter] = {}

def get_router(image_path: str | Path, grid: int = GRID) -> RoadRouter:
    key = (str(image_path), grid)
    with _router_lock:
        router = _routers.get(key)
        if router is None:
            router = RoadRouter(image_path, grid=grid)
            _routers[key] = router
        return router


# ============================================================================
# SECTION 4 - MAP RENDERING
# ============================================================================

# ------------------------------------------------------------------------
# Source: nfsrc/mapview/style.py
# ------------------------------------------------------------------------

MAX_ROUTE_WIDTH = 16

def _clamp_width(value, default: int = 3) -> int:
    try:
        width = int(round(float(value)))
    except (TypeError, ValueError):
        width = default
    return max(1, min(MAX_ROUTE_WIDTH, width))

def _hex_rgb(value) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    text = value.strip().lstrip("#")
    if len(text) != 6:
        return None
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return None

def mix(fg: str, bg: str, t: float) -> str:
    a = _hex_rgb(fg)
    b = _hex_rgb(bg)
    if a is None:
        return fg
    if b is None:
        b = (0, 0, 0)
    t = max(0.0, min(1.0, float(t)))
    channels = tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))
    return "#%02x%02x%02x" % channels

@dataclass
class MapStyle:
    background: str = "#0a0d12"
    grid: str = "#2f3a46"
    route_center: str = "#ff7f00"
    route_halo: str = "#ffff00"
    route_width: int = 3
    halo_steps: int = 3
    ramp: str = "#ffffff"
    spur_center: str = "#7fff00"
    spur_halo: str = "#00ff00"
    spur_width: int = 2
    overlay_fg: str = "#9aa4b2"
    error_fg: str = "#ff8080"
    marker_outline: str = "#000000"
    marker_font: tuple = ("Segoe UI", 8, "bold")
    point_colors: dict = field(default_factory=dict)
    point_labels: dict = field(default_factory=dict)

    @classmethod
    def from_theme(cls, source=theme) -> "MapStyle":
        cfg = source.map
        return cls(
            background=cfg.get("background", "#0a0d12"),
            grid=cfg.get("grid", "#2f3a46"),
            route_center=cfg.get("route_center", "#ff7f00"),
            route_halo=cfg.get("route_halo", "#ffff00"),
            route_width=_clamp_width(cfg.get("route_width"), 3),
            halo_steps=max(0, int(cfg.get("route_halo_steps", 3))),
            ramp=cfg.get("route_ramp", "#ffffff"),
            spur_center=cfg.get("spur_center", "#7fff00"),
            spur_halo=cfg.get("spur_halo", "#00ff00"),
            spur_width=_clamp_width(cfg.get("spur_width"), 2),
            overlay_fg=cfg.get("overlay_fg", "#9aa4b2"),
            error_fg=cfg.get("error_fg", "#ff8080"),
            marker_outline=cfg.get("marker_outline", "#000000"),
            marker_font=(
                cfg.get("marker_font_family", "Segoe UI"),
                int(cfg.get("marker_font_size", 8)),
                "bold",
            ),
            point_colors=dict(cfg.get("points", {})),
            point_labels=dict(cfg.get("short_labels", {})),
        )

    def halo_line(self, center_color: str, halo_color: str, width: int, k: int) -> tuple[str, int]:
        """Halo ring k (1 = outermost) color/width for a stack of halo_steps."""
        steps = max(1, self.halo_steps)
        t = (steps - k + 1) / (steps + 1.0)
        return mix(halo_color, self.background, t), width + (steps - k + 1)

# ------------------------------------------------------------------------
# Source: nfsrc/mapview/view.py
# ------------------------------------------------------------------------

_Z_RAMP = 2.0

_DOT_RADIUS = 4

class MapView(ttk.Frame):
    def __init__(self, master, app: "RaceCreatorApp"):
        super().__init__(master)
        self.app = app
        self.style = MapStyle.from_theme(theme)
        self.canvas = tk.Canvas(
            self,
            bg=self.style.background,
            highlightthickness=0,
            borderwidth=0,
            cursor="crosshair",
        )
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.image: Image.Image | None = None
        self.display_image: Image.Image | None = None
        self.photo: ImageTk.PhotoImage | None = None
        self.W = 3072
        self.H = 3072
        self.scale = 0.25
        self.ox = 0.0
        self.oy = 0.0
        self.fit_mode = True
        self.flip_y = tk.BooleanVar(value=bool(app.settings.get("map_flip_y", False)))
        self.show_grid = tk.BooleanVar(value=bool(app.settings.get("map_show_grid", False)))
        self.rotation = 0
        self._drag_button: int | None = None
        self._drag_start: tuple[int, int] | None = None
        self._drag_origin = (0.0, 0.0)
        self._dragged = False
        self._render_job = None
        self._route: list[tuple[float, float, float]] = []
        self._spurs: list[list[tuple[float, float, float]]] = []
        self._route_status = tk.StringVar(value="Open a project to draw its route.")
        self._route_key = None
        self._route_generation = 0
        self._route_job = None
        self._route_busy = False
        self._route_error = ""
        self._pending_route = None

        self._fit_scale = 0.25

        self._load_image()
        self._build_overlay()

        self.canvas.bind("<Configure>", self._on_resize)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<ButtonPress-3>", self._on_press)
        self.canvas.bind("<B3-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-3>", self._on_release)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Motion>", self._on_motion)
        self.flip_y.trace_add("write", lambda *_: self._on_flip())
        self.show_grid.trace_add("write", lambda *_: self._on_grid_toggle())

    # ------------------------------------------------------------------ images
    def _load_image(self):
        path = ROUTE_MAP_PATH
        try:
            image = Image.open(path).convert("RGB")
            self.image = image
            self.W, self.H = image.size
            log.info("%s loaded for the map view (%dx%d)", path.name, self.W, self.H)
        except Exception as exc:
            self.image = None
            log.error("could not load %s: %s", path, exc)
        self._rebuild_display()

    def _rebuild_display(self):
        if self.image is None:
            self.display_image = None
            return
        display = (
            self.image.transpose(Image.FLIP_TOP_BOTTOM)
            if self.flip_y.get()
            else self.image
        )
        for _ in range(self.rotation):
            display = display.transpose(Image.ROTATE_90)
        self.display_image = display

    def rotate(self, direction: int):
        """Rotate the view 90 deg; -1 clockwise, +1 counter-clockwise."""
        self.rotation = (self.rotation + int(direction)) % 4
        self._rebuild_display()
        self.fit_view()

    # --------------------------------------------------------- display toggles
    def _on_flip(self):
        self._rebuild_display()
        self.fit_view()
        self.app.settings["map_flip_y"] = bool(self.flip_y.get())
        self.app.save_settings()

    def _on_grid_toggle(self):
        self.app.settings["map_show_grid"] = bool(self.show_grid.get())
        self.app.save_settings()
        self._schedule_render()

    # ---------------------------------------------------------------- viewport
    def _on_resize(self, _event=None):
        self._update_overlay()
        self._schedule_render()

    def _schedule_render(self):
        if self._render_job is not None:
            self.after_cancel(self._render_job)
        self._render_job = self.after(40, self.render)

    def fit_view(self):
        self.update_idletasks()
        width = max(1, self.canvas.winfo_width())
        height = max(1, self.canvas.winfo_height())
        if width <= 1 or height <= 1:
            self.after(120, self.fit_view)
            return
        self.scale = min(width / self.W, height / self.H)
        self.ox = (width - self.W * self.scale) / 2.0
        self.oy = (height - self.H * self.scale) / 2.0
        self._fit_scale = self.scale
        self.fit_mode = True
        self.render()

    def zoom_by(self, factor: float):
        width = max(1, self.canvas.winfo_width())
        height = max(1, self.canvas.winfo_height())
        self._zoom_at(width / 2.0, height / 2.0, factor)

    def _zoom_at(self, mx: float, my: float, factor: float):
        low = self._fit_scale
        high = self._fit_scale * 16.0
        new_scale = max(low, min(high, self.scale * factor))
        if new_scale == self.scale:
            return
        ratio = new_scale / self.scale
        self.ox = mx - (mx - self.ox) * ratio
        self.oy = my - (my - self.oy) * ratio
        self.scale = new_scale
        self.fit_mode = False
        self._schedule_render()

    def _on_wheel(self, event):
        self._zoom_at(event.x, event.y, 1.25 if event.delta > 0 else 0.8)

    # ------------------------------------------------------------- coordinates
    def _rotate_uv(self, u: float, v: float) -> tuple[float, float]:
        k = self.rotation
        if k == 1:
            return v, 1.0 - u
        if k == 2:
            return 1.0 - u, 1.0 - v
        if k == 3:
            return 1.0 - v, u
        return u, v

    def _unrotate_uv(self, u: float, v: float) -> tuple[float, float]:
        k = self.rotation
        if k == 1:
            return 1.0 - v, u
        if k == 2:
            return 1.0 - u, 1.0 - v
        if k == 3:
            return v, 1.0 - u
        return u, v

    def uv_to_screen(self, u: float, v: float) -> tuple[float, float]:
        u, v = self._rotate_uv(u, v)
        display_v = (1.0 - v) if self.flip_y.get() else v
        return self.ox + u * self.W * self.scale, self.oy + display_v * self.H * self.scale

    def screen_to_uv(self, sx: float, sy: float) -> tuple[float, float]:
        u = (sx - self.ox) / (self.W * self.scale)
        v = (sy - self.oy) / (self.H * self.scale)
        if self.flip_y.get():
            v = 1.0 - v
        return self._unrotate_uv(u, v)

    def world_to_screen(self, x: float, y: float) -> tuple[float, float]:
        return self.uv_to_screen(*world_to_map_uv(x, y))

    def screen_to_world(self, sx: float, sy: float) -> tuple[float, float]:
        return map_uv_to_world(*self.screen_to_uv(sx, sy))

    def trackposition_to_screen(self, tx: float, ty: float) -> tuple[float, float]:
        return self.world_to_screen(*trackposition_to_world(tx, ty))

    def screen_to_trackposition(self, sx: float, sy: float) -> tuple[float, float]:
        return world_to_trackposition(*self.screen_to_world(sx, sy))

    # ---------------------------------------------------------------- overlay
    def _build_overlay(self):
        font = (self.style.marker_font[0], self.style.marker_font[1])
        self._overlay_world = self.canvas.create_text(
            10, 8, anchor="nw", fill=self.style.overlay_fg, font=font, text=""
        )
        self._overlay_tp = self.canvas.create_text(
            10, 24, anchor="nw", fill=self.style.overlay_fg, font=font, text=""
        )
        self._overlay_hint = self.canvas.create_text(
            10, 8, anchor="sw", fill=self.style.overlay_fg, font=font,
            text="LMB set TrackPosition  |  RMB drag pan  |  wheel zoom 1-16x",
        )
        self._update_overlay()

    def _update_overlay(self):
        height = max(1, self.canvas.winfo_height())
        self.canvas.coords(self._overlay_hint, 10, height - 8)

    # ----------------------------------------------------------------- render
    def render(self):
        self._render_job = None
        self.canvas.delete("map")
        self.canvas.delete("route")
        self.canvas.delete("grid")
        self.canvas.delete("markers")
        if self.display_image is None:
            self.canvas.create_text(
                20,
                40,
                anchor="nw",
                text=f"Map image not found:\n{ROUTE_MAP_PATH}",
                fill=self.style.error_fg,
                font=("Segoe UI", 11),
                tags="map",
            )
            return
        width = max(1, self.canvas.winfo_width())
        height = max(1, self.canvas.winfo_height())

        wx0 = max(0.0, -self.ox / self.scale)
        wy0 = max(0.0, -self.oy / self.scale)
        wx1 = min(float(self.W), (width - self.ox) / self.scale)
        wy1 = min(float(self.H), (height - self.oy) / self.scale)
        if wx1 <= wx0 or wy1 <= wy0:
            return
        ix0 = max(0, int(math.floor(wx0)))
        iy0 = max(0, int(math.floor(wy0)))
        ix1 = min(self.W, int(math.ceil(wx1)))
        iy1 = min(self.H, int(math.ceil(wy1)))
        if ix1 <= ix0 or iy1 <= iy0:
            return
        # Only the visible part of the map is cropped and rescaled; resizing the
        # whole image on every pan/zoom would stall the UI.
        crop = self.display_image.crop((ix0, iy0, ix1, iy1))
        target_w = max(1, int(round((ix1 - ix0) * self.scale)))
        target_h = max(1, int(round((iy1 - iy0) * self.scale)))
        resample = Image.LANCZOS if self.scale < 0.6 else Image.BILINEAR
        resized = crop.resize((target_w, target_h), resample)
        self.photo = ImageTk.PhotoImage(resized)
        self.canvas.create_image(
            self.ox + ix0 * self.scale,
            self.oy + iy0 * self.scale,
            anchor="nw",
            image=self.photo,
            tags="map",
        )
        self._draw_grid()
        self._draw_route()
        self.draw_markers()
        # Overlay readouts are created once at startup; keep them on top.
        for item in (
            self._overlay_world,
            self._overlay_tp,
            self._overlay_hint,
        ):
            self.canvas.tag_raise(item)

    def _line_kwargs(self, color: str, width: int) -> dict:
        return {
            "fill": color,
            "width": width,
            "capstyle": tk.ROUND,
            "joinstyle": tk.ROUND,
        }

    def _halo_stack(self, flat: list[float], center: str, halo: str, width: int):
        """Concentric lines fading from halo color to the background."""
        for k in range(1, self.style.halo_steps + 1):
            color, ring_width = self.style.halo_line(center, halo, width, k)
            self.canvas.create_line(
                *flat, tags="route", **self._line_kwargs(color, ring_width)
            )

    def _route_flat(self, points) -> list[float]:
        flat: list[float] = []
        for u, v, _z in points:
            sx, sy = self.uv_to_screen(u, v)
            flat.extend((sx, sy))
        return flat

    def _z_runs(self, points):
        """Group consecutive vertices; runs with |dz| > _Z_RAMP flag a ramp."""
        if len(points) < 2:
            return [(points, False)]
        runs = []
        start = 0
        ramp = abs(points[1][2] - points[0][2]) > _Z_RAMP
        for index in range(1, len(points) - 1):
            here = abs(points[index + 1][2] - points[index][2]) > _Z_RAMP
            if here != ramp:
                runs.append((points[start : index + 1], ramp))
                start = index
                ramp = here
        runs.append((points[start:], ramp))
        return runs

    def _draw_route(self):
        self.canvas.delete("route")
        if not self._route:
            return
        style = self.style
        flat = self._route_flat(self._route)
        if len(flat) < 4:
            return
        self._halo_stack(flat, style.route_center, style.route_halo, style.route_width)
        for run, ramp in self._z_runs(self._route):
            run_flat = self._route_flat(run)
            if len(run_flat) >= 4:
                self.canvas.create_line(
                    *run_flat,
                    tags="route",
                    **self._line_kwargs(style.ramp if ramp else style.route_center, style.route_width),
                )
        for spur in self._spurs:
            spur_flat = self._route_flat(spur)
            if len(spur_flat) >= 4:
                self._halo_stack(
                    spur_flat, style.spur_center, style.spur_halo, style.spur_width
                )
                self.canvas.create_line(
                    *spur_flat,
                    tags="route",
                    **self._line_kwargs(style.spur_center, style.spur_width),
                )

    def _draw_grid(self):
        """Overlay the in-game reference grid (off unless 'Show grid' is on)."""
        if not self.show_grid.get():
            return
        if GRID_STEP_MAP * (self.W / 8192.0) * self.scale < 8.0:
            return
        for index in range(-5, 60):
            px = GRID_X_MAP + index * GRID_STEP_MAP
            if 0.0 <= px <= 8192.0:
                u = px / 8192.0
                sx, y0 = self.uv_to_screen(u, 0.0)
                _sx, y1 = self.uv_to_screen(u, 1.0)
                self.canvas.create_line(sx, y0, sx, y1, fill=self.style.grid, width=1, tags="grid")
            py = GRID_Y_MAP + index * GRID_STEP_MAP
            if 0.0 <= py <= 8192.0:
                v = py / 8192.0
                x0, sy = self.uv_to_screen(0.0, v)
                x1, _sy = self.uv_to_screen(1.0, v)
                self.canvas.create_line(x0, sy, x1, sy, fill=self.style.grid, width=1, tags="grid")

    # ---------------------------------------------------------------- markers
    def draw_markers(self):
        project = self.app.project
        labels = self.style.point_labels
        colors = self.style.point_colors
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        for kind, index, label, point in project.ordered_points():
            sx, sy = self.world_to_screen(point.x, point.y)
            if not (-8 <= sx <= width + 8 and -8 <= sy <= height + 8):
                continue
            short = {
                "start": labels.get("start", "S"),
                "finish": labels.get("finish", "F"),
                "checkpoint": f"{labels.get('checkpoint', 'CP')}{index + 1}",
                "bonus": f"{labels.get('bonus', 'TBCP')}{index + 1}",
                "shortcut": f"{labels.get('shortcut', 'SC')}{index + 1}",
                "reset": labels.get("reset", "R"),
            }.get(kind, label)
            color = colors.get(kind, "#ffffff")
            radius = _DOT_RADIUS
            self.canvas.create_oval(
                sx - radius,
                sy - radius,
                sx + radius,
                sy + radius,
                fill=color,
                outline=self.style.marker_outline,
                width=1,
                tags="markers",
            )
            if short:
                self.canvas.create_text(
                    sx + radius + 3,
                    sy,
                    anchor="w",
                    text=short,
                    fill=color,
                    font=self.style.marker_font,
                    tags="markers",
                )
        tp = project.data.get("track_position", [0, 0])
        try:
            tx, ty = float(tp[0]), float(tp[1])
        except (TypeError, ValueError, IndexError):
            tx = ty = 0.0
        sx, sy = self.trackposition_to_screen(tx, ty)
        if -8 <= sx <= width + 8 and -8 <= sy <= height + 8:
            color = colors.get("map", "#ff3dc8")
            self.canvas.create_oval(
                sx - 5, sy - 5, sx + 5, sy + 5, outline=color, width=2, tags="markers"
            )
            self.canvas.create_oval(
                sx - 2, sy - 2, sx + 2, sy + 2, fill=color, outline="", tags="markers"
            )
            self.canvas.create_text(
                sx + 8,
                sy,
                anchor="w",
                text=labels.get("map", "TRACK POS"),
                fill=color,
                font=self.style.marker_font,
                tags="markers",
            )

    # ------------------------------------------------------------- interaction
    def _on_press(self, event):
        self._drag_button = event.num
        self._drag_start = (event.x, event.y)
        self._drag_origin = (self.ox, self.oy)
        self._dragged = False

    def _on_drag(self, event):
        if self._drag_start is None:
            return
        dx = event.x - self._drag_start[0]
        dy = event.y - self._drag_start[1]
        if abs(dx) > 4 or abs(dy) > 4:
            self._dragged = True
            self.fit_mode = False
            self.ox = self._drag_origin[0] + dx
            self.oy = self._drag_origin[1] + dy
            self._schedule_render()

    def _on_release(self, event):
        dragged = self._dragged
        button = self._drag_button
        self._drag_start = None
        self._drag_button = None
        if not dragged and button == 1:
            self._place_at(event.x, event.y)

    def _place_at(self, sx, sy):
        x, y = self.screen_to_trackposition(sx, sy)
        if not (0 <= x <= TRACKPOS_SPAN and 0 <= y <= TRACKPOS_SPAN):
            return
        self.app.set_track_position(x, y)
        self.app.hud.flash(f"TrackPosition set to {x:.0f}, {y:.0f}")

    def _on_motion(self, event):
        x, y = self.screen_to_world(event.x, event.y)
        tx, ty = world_to_trackposition(x, y)
        self.canvas.itemconfigure(
            self._overlay_world,
            text=f"WorldMap: X={x:.0f}  Y={y:.0f}",
        )
        self.canvas.itemconfigure(
            self._overlay_tp,
            text=f"TrackPosition: X={tx:.0f}  Y={ty:.0f}",
        )

    # ------------------------------------------------------------------ route
    def _route_inputs(self):
        project = self.app.project
        points: list[tuple[float, float, float]] = []
        if project.start:
            points.append((project.start.x, project.start.y, project.start.z))
        points.extend((p.x, p.y, p.z) for p in project.checkpoints)
        points.extend((p.x, p.y, p.z) for p in project.bonus_checkpoints)
        if project.finish:
            points.append((project.finish.x, project.finish.y, project.finish.z))
        shortcuts = [(p.x, p.y, p.z) for p in project.shortcuts]
        return points, shortcuts

    def request_route(self, force: bool = False):
        points, shortcuts = self._route_inputs()
        if len(points) < 2:
            self._route_generation += 1
            self._route = []
            self._spurs = []
            self._route_key = None
            self._route_busy = False
            self._route_error = ""
            self._route_status.set(
                "Add a start grid and a finish line to draw the route."
            )
            self._schedule_render()
            return
        key = tuple((round(x, 1), round(y, 1)) for x, y, _z in points) + (
            ("sc",) + tuple((round(x, 1), round(y, 1)) for x, y, _z in shortcuts)
        )
        if not force and key == self._route_key and not self._route_error:
            self._schedule_render()
            return
        self._route_key = key
        self._pending_route = (points, shortcuts)
        if self._route_job is not None:
            self.after_cancel(self._route_job)
        self._route_job = self.after(200 if not force else 0, self._start_route)
        self._route_status.set("Calculating route along the roads...")
        self.app._set_status("Calculating route along the roads...")

    def _start_route(self):
        self._route_job = None
        if self._pending_route is None:
            return
        points, shortcuts = self._pending_route
        self._route_generation += 1
        generation = self._route_generation
        self._route_busy = True
        threading.Thread(
            target=self._compute_route,
            args=(generation, points, shortcuts),
            name="nfs-route",
            daemon=True,
        ).start()

    def _compute_route(self, generation: int, points, shortcuts):
        try:
            router = get_router(ROUTE_MAP_PATH)
            main = router.route(points)
            spurs = router.shortcuts(main, shortcuts)
            self.app.events.put(("route_ready", generation, main, spurs, ""))
        except Exception as exc:
            log.exception("route computation failed")
            self.app.events.put(("route_ready", generation, [], [], str(exc)))

    def apply_route(self, generation: int, main, spurs, error: str):
        if generation != self._route_generation:
            return
        self._route_busy = False
        if error:
            self._route = []
            self._spurs = []
            self._route_error = error
            self._route_status.set(f"Route failed: {error}")
            log.warning("route drawing failed: %s", error)
        else:
            self._route = list(main)
            self._spurs = [list(spur) for spur in spurs]
            self._route_error = ""
            self._route_status.set(
                f"Route ready: {len(self._route)} line points, "
                f"{len(self._spurs)} shortcut spur(s)."
            )
            log.info(
                "route ready: %d line points, %d shortcut spur(s)",
                len(self._route),
                len(self._spurs),
            )
        self._schedule_render()

    def refresh_markers(self):
        self.request_route()
        self._schedule_render()

    def use_start_position(self):
        project = self.app.project
        if project is None or project.start is None:
            self.app._set_status("No Start point yet - capture the start grid first.")
            return
        tx, ty = world_to_trackposition(project.start.x, project.start.y)
        self.app.set_track_position(tx, ty)
        log.info(
            "track position synced to start grid: %.1f, %.1f (start %.1f, %.1f)",
            tx,
            ty,
            project.start.x,
            project.start.y,
        )
        self.app._set_status(
            f"TrackPosition placed on the Start grid ({tx:.0f}, {ty:.0f})."
        )

    def route_status_var(self):
        return self._route_status


# ============================================================================
# SECTION 5 - EXPORT BUILDERS (.NFSMS / .END)
# ============================================================================

# ------------------------------------------------------------------------
# Source: nfsrc/services/nfsms.py
# ------------------------------------------------------------------------

EVENT_ADD_FIELDS = ["0x1c8aa5fa", "0x2fa52304", "AIAccelMax", "AIAccelMin", "AIHandlingMax", "AIHandlingMin", "AISkillMax", "AISkillMin", "AISpeedMax", "AISpeedMin", "CashValue", "CashValueDominate", "CollectorsEditionRace", "CopsInRace", "DominationTime", "EventID", "FinishCamera", "gameplayvault", "IntroNIS", "Level", "MasterEvent", "Name", "NumLaps", "Region", "RewardsForDominate", "RewardsForWinner", "Template", "ThreatRangeMax", "ThreatRangeMin", "TimeLimit", "TrackLayout", "TrackPosition", "TrackRecord", "TrafficLevel", "XP", "XPDominate"]
TRACK_ADD_FIELDS = ["Barriers", "Checkpoint", "Children", "gameplayvault", "Name", "racefinish", "racestart", "Shortcuts", "Template", "TrackLength"]
START_ADD_FIELDS = ["Name", "Position", "Rotation", "Template"]
FINISH_ADD_FIELDS = ["Name", "Dimensions", "FlareSpacing", "Position", "Rotation", "Template"]
RESET_ADD_FIELDS = ["Name", "Position", "Rotation", "Width", "Template"]
CHECKPOINT_ADD_FIELDS = ["Name", "Dimensions", "Position", "Rotation", "Template"]
BONUS_ADD_FIELDS = ["Name", "Dimensions", "FlareSpacing", "Position", "Rotation", "TimeBonus", "Template"]
SHORTCUT_ADD_FIELDS = ["Name", "Position", "Rotation", "Template"]

def num(value) -> str:
    if isinstance(value, bool): return "True" if value else "False"
    if isinstance(value, int): return str(value)
    if isinstance(value, float):
        if value == int(value): return str(int(value))
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return str(value)

def text(value) -> str:
    return str(value).replace("\r", " ").replace("\n", " ").strip()

class _Lines:
    def __init__(self) -> None: self.items: list[str] = []
    def add(self, line: str = "") -> None: self.items.append(line)
    def section(self, title: str) -> None:
        if self.items and self.items[-1] != "": self.add()
        self.add(f"### -- {title} -- ###")
    def command(self, *parts) -> None: self.add(" ".join(str(part) for part in parts))
    def update(self, tree: str, node: str, field: str, value) -> None: self.command("update_field", tree, node, field, value)
    def render(self) -> str:
        while self.items and self.items[0] == "": self.items.pop(0)
        return "\n".join(self.items).rstrip() + "\n"

def _emit_position(out: _Lines, node: str, point: Point) -> None:
    out.update("gameplay", node, "Position X", num(point.x))
    out.update("gameplay", node, "Position Y", num(point.y))
    out.update("gameplay", node, "Position Z", num(point.z))
    out.update("gameplay", node, "Rotation", num(point.rotation))

def _emit_dimensions(out: _Lines, node: str, point: Point) -> None:
    out.update("gameplay", node, "Dimensions X", num(point.dim_x))
    out.update("gameplay", node, "Dimensions Y", num(point.dim_y))
    out.update("gameplay", node, "Dimensions Z", num(point.dim_z))

def _start_section(out: _Lines, project: RaceProject, track: str, vault: str) -> None:
    point = project.start
    if point is None: return
    node = f"{track}/startgrid1"
    out.section("START GRID")
    out.command("add_node", "gameplay", "startgrid", node)
    out.command("change_vault", "gameplay", node, vault)
    for fieldname in START_ADD_FIELDS: out.command("add_field", "gameplay", node, fieldname)
    out.update("gameplay", node, "Name", node)
    _emit_position(out, node, point)

def _finish_section(out: _Lines, project: RaceProject, track: str, vault: str) -> None:
    point = project.finish
    if point is None: return
    node = f"{track}/finishline1"
    out.section("FINISH LINE")
    out.command("add_node", "gameplay", "finishline", node)
    out.command("change_vault", "gameplay", node, vault)
    for fieldname in FINISH_ADD_FIELDS: out.command("add_field", "gameplay", node, fieldname)
    out.update("gameplay", node, "Name", node)
    _emit_dimensions(out, node, point)
    out.update("gameplay", node, "FlareSpacing", num(point.flare_spacing))
    _emit_position(out, node, point)

def _reset_section(out: _Lines, project: RaceProject, track: str, vault: str) -> None:
    point = project.reset
    if point is None: return
    node = f"{track}/resetplayertrigger1"
    out.section("RESET PLAYER TRIGGER")
    out.command("add_node", "gameplay", "resetplayertrigger", node)
    out.command("change_vault", "gameplay", node, vault)
    for fieldname in RESET_ADD_FIELDS: out.command("add_field", "gameplay", node, fieldname)
    out.update("gameplay", node, "Name", node)
    _emit_position(out, node, point)
    out.update("gameplay", node, "Width", num(point.width))

def _checkpoint_sections(out: _Lines, project: RaceProject, track: str, vault: str) -> None:
    for index, point in enumerate(project.checkpoints, 1):
        node = f"{track}/checkpoint{index}"
        out.section(f"CHECKPOINT {index:02d}")
        out.command("add_node", "gameplay", "checkpoint", node)
        out.command("change_vault", "gameplay", node, vault)
        for fieldname in CHECKPOINT_ADD_FIELDS: out.command("add_field", "gameplay", node, fieldname)
        out.update("gameplay", node, "Name", node)
        _emit_dimensions(out, node, point)
        _emit_position(out, node, point)
    for index, point in enumerate(project.bonus_checkpoints, 1):
        node = f"{track}/time_bonus_checkpoint{index}"
        out.section(f"TIME BONUS CHECKPOINT {index:02d}")
        out.command("add_node", "gameplay", "checkpoint", node)
        out.command("change_vault", "gameplay", node, vault)
        for fieldname in BONUS_ADD_FIELDS: out.command("add_field", "gameplay", node, fieldname)
        out.update("gameplay", node, "Name", node)
        _emit_dimensions(out, node, point)
        out.update("gameplay", node, "FlareSpacing", num(point.flare_spacing))
        _emit_position(out, node, point)
        out.update("gameplay", node, "TimeBonus", num(point.time_bonus))

def _shortcut_sections(out: _Lines, project: RaceProject, track: str, vault: str) -> None:
    for index, point in enumerate(project.shortcuts, 1):
        node = f"{track}/shortcut{index}"
        out.section(f"SHORTCUT {index:02d}")
        out.command("add_node", "gameplay", "shortcut", node)
        out.command("change_vault", "gameplay", node, vault)
        for fieldname in SHORTCUT_ADD_FIELDS: out.command("add_field", "gameplay", node, fieldname)
        out.update("gameplay", node, "Name", node)
        _emit_position(out, node, point)

def _array_section(out: _Lines, project: RaceProject, track: str) -> None:
    checkpoint_paths: list[str] = []
    checkpoint_paths += [f"{track}/checkpoint{i}" for i in range(1, len(project.checkpoints) + 1)]
    checkpoint_paths += [f"{track}/time_bonus_checkpoint{i}" for i in range(1, len(project.bonus_checkpoints) + 1)]
    shortcut_paths = [f"{track}/shortcut{i}" for i in range(1, len(project.shortcuts) + 1)]
    children: list[str] = []
    if project.start: children.append(f"{track}/startgrid1")
    if project.finish: children.append(f"{track}/finishline1")
    children += checkpoint_paths
    if project.reset: children.append(f"{track}/resetplayertrigger1")
    children += shortcut_paths
    out.section("CHECKPOINT SETS")
    if checkpoint_paths:
        out.command("resize_field", "gameplay", track, "Checkpoint", len(checkpoint_paths))
        for index, path in enumerate(checkpoint_paths): out.update("gameplay", track, f"Checkpoint[{index}]", path)
    else:
        out.add("### No checkpoints captured; Checkpoint array left untouched.")
    out.section("CHILDREN SETS")
    if children:
        out.command("resize_field", "gameplay", track, "Children", len(children))
        for index, path in enumerate(children): out.update("gameplay", track, f"Children[{index}]", path)
    else:
        out.add("### No child nodes; Children array left untouched.")
    out.section("SHORTCUT SETS")
    if shortcut_paths:
        out.command("resize_field", "gameplay", track, "Shortcuts", len(shortcut_paths))
        for index, path in enumerate(shortcut_paths): out.update("gameplay", track, f"Shortcuts[{index}]", path)
    else:
        out.add("### No shortcuts captured; Shortcuts array left untouched.")
    out.section("BARRIERS")
    barriers = project.data.get("barriers") or [1]
    out.command("resize_field", "gameplay", track, "Barriers", len(barriers))
    for index, barrier in enumerate(barriers): out.update("gameplay", track, f"Barriers[{index}]", num(int(barrier)))

def _registration_section(out: _Lines, project: RaceProject, track: str) -> None:
    data = project.data
    wrote = False
    if data.get("register_race_bin"):
        if not wrote:
            out.section("REGISTER EVENT IN CAREER")
            wrote = True
        index = max(0, int(data.get("race_bin_children", 0)))
        out.command("resize_field", "gameplay", "race_bin_career", "Children", index + 1)
        out.update("gameplay", "race_bin_career", f"Children[{index}]", project.event_path)
    if data.get("register_track_category"):
        if not wrote:
            out.section("REGISTER TRACK IN CATEGORY")
            wrote = True
        parent = f"tracks/{project.region}/{project.track_dir}"
        index = max(0, int(data.get("track_category_children", 0)))
        out.command("resize_field", "gameplay", parent, "Children", index + 1)
        out.update("gameplay", parent, f"Children[{index}]", track)
    if data.get("unlock_quick_race"):
        if not wrote:
            out.section("QUICK RACE UNLOCKS")
            wrote = True
        index = max(0, int(data.get("quick_race_children", 0)))
        out.command("resize_field", "gameplay", "mw2_career/unlock/quick_race/unlock_quickrace", "UnlockEvents", index + 1)
        out.update("gameplay", "mw2_career/unlock/quick_race/unlock_quickrace", f"UnlockEvents[{index}]", project.event_path,)
    if data.get("unlock_collectors_quick_race"):
        if not wrote:
            out.section("COLLECTORS EDITION QUICK RACE UNLOCKS")
            wrote = True
        index = max(0, int(data.get("collectors_quick_race_children", 0)))
        out.command("resize_field", "gameplay", "mw2_career/unlock/collectors_edition/unlock_quickrace", "UnlockEvents", index + 1)
        out.update("gameplay", "mw2_career/unlock/collectors_edition/unlock_quickrace", f"UnlockEvents[{index}]", project.event_path)

def _career_logic_section(out: _Lines, project: RaceProject) -> None:
    data = project.data
    if not data.get("register_career_event"): return
    root = f"mw2_career/unlock/{project.event_id}_Event"
    action = f"{root}/unlock_event"
    gate = f"{root}/and"
    complete = f"{gate}/complete_level"
    unlock_level = max(0, int(data.get("level", 0)))
    out.section(f"CAREER LOGIC TRIGGER ({project.event_id})")
    out.command("add_node", "gameplay", "logic_action_unlock_event", action)
    out.command("add_field", "gameplay", action, "Name")
    out.command("add_field", "gameplay", action, "Template")
    out.command("add_field", "gameplay", action, "UnlockEvents", 1)
    out.update("gameplay", action, "Name", action)
    out.update("gameplay", action, "UnlockEvents[0]", project.event_path)
    out.command("add_node", "gameplay", "logic_node_complete_level", complete)
    out.command("add_field", "gameplay", complete, "Level")
    out.command("add_field", "gameplay", complete, "Name")
    out.command("add_field", "gameplay", complete, "Template")
    out.update("gameplay", complete, "Name", complete)
    out.update("gameplay", complete, "Level", unlock_level)
    out.command("add_node", "gameplay", "logic_and", gate)
    out.command("add_field", "gameplay", gate, "Children", 1)
    out.command("add_field", "gameplay", gate, "Name")
    out.command("add_field", "gameplay", gate, "Template")
    out.update("gameplay", gate, "Children[0]", complete)
    out.update("gameplay", gate, "Name", gate)
    out.command("add_node", "gameplay", "logic_trigger", root)
    out.command("add_field", "gameplay", root, "Children", 2)
    out.command("add_field", "gameplay", root, "Name")
    out.command("add_field", "gameplay", root, "Template")
    out.update("gameplay", root, "Children[0]", action)
    out.update("gameplay", root, "Children[1]", gate)
    out.update("gameplay", root, "Name", root)
    index = max(0, int(data.get("career_unlock_children", 0)))
    if index > 0:
        out.command("resize_field", "gameplay", "mw2_career/unlock", "Children", index + 1)
        out.update("gameplay", "mw2_career/unlock", f"Children[{index}]", root)
    else:
        out.add("### mw2_career/unlock children count not set; trigger left unregistered.")

def build_nfsms(project: RaceProject) -> str:
    counts = project.counts()
    log.info(
        "building .nfsms: %s %s in %s, track %s "
        "(S%d F%d CP%d B%d SC%d R%d)",
        project.event_id,
        project.event_type,
        project.region,
        project.track_name,
        counts["start"],
        counts["finish"],
        counts["checkpoint"],
        counts["bonus"],
        counts["shortcut"],
        counts["reset"],
    )
    out = _Lines()
    data = project.data
    event = project.event_path
    track = project.track_path
    vault = project.track_name
    event_node_type = EVENT_NODE_TYPES[project.event_type]
    out.section("EVENT")
    out.command("add_node", "gameplay", event_node_type, event)
    out.command("change_vault", "gameplay", event, "race_bin_career")
    for fieldname in EVENT_ADD_FIELDS: out.command("add_field", "gameplay", event, fieldname)
    out.update("gameplay", event, "0x1c8aa5fa", 6)
    out.update("gameplay", event, "AIAccelMax", num(int(data["ai_accel_max"])))
    out.update("gameplay", event, "AIAccelMin", num(int(data["ai_accel_min"])))
    out.update("gameplay", event, "AIHandlingMax", num(int(data["ai_handling_max"])))
    out.update("gameplay", event, "AIHandlingMin", num(int(data["ai_handling_min"])))
    out.update("gameplay", event, "AISkillMax", num(int(data["ai_skill_max"])))
    out.update("gameplay", event, "AISkillMin", num(int(data["ai_skill_min"])))
    out.update("gameplay", event, "AISpeedMax", num(int(data["ai_speed_max"])))
    out.update("gameplay", event, "AISpeedMin", num(int(data["ai_speed_min"])))
    out.update("gameplay", event, "CashValue", num(int(data["cash_value"])))
    out.update("gameplay", event, "CashValueDominate", num(int(data["cash_dominate"])))
    out.update("gameplay", event, "CollectorsEditionRace", num(bool(data["collectors_edition"])))
    out.update("gameplay", event, "CopsInRace", num(bool(data["cops_in_race"])))
    out.update("gameplay", event, "DominationTime", num(float(data["domination_time"])))
    out.update("gameplay", event, "EventID", text(project.event_id))
    out.update("gameplay", event, "FinishCamera", "(Automatic)")
    out.update("gameplay", event, "gameplayvault", text(project.event_id))
    out.update("gameplay", event, "IntroNIS", text(data.get("intro_nis") or "0"))
    out.update("gameplay", event, "Level", num(int(data["level"])))
    out.update("gameplay", event, "MasterEvent", num(bool(data["master_event"])))
    out.update("gameplay", event, "Name", event)
    out.update("gameplay", event, "NumLaps", num(int(data["num_laps"])))
    out.update("gameplay", event, "Region", project.region_index)
    for fieldname, key in (("RewardsForDominate", "rewards_dominate"), ("RewardsForWinner", "rewards_winner")):
        rewards = [text(item) for item in (data.get(key) or ["0"]) if text(item)]
        if not rewards: rewards = [""]
        out.command("resize_field", "gameplay", event, fieldname, len(rewards))
        for index, reward in enumerate(rewards): out.update("gameplay", event, f"{fieldname}[{index}]", reward)
    out.update("gameplay", event, "ThreatRangeMax", num(int(data["threat_max"])))
    out.update("gameplay", event, "ThreatRangeMin", num(int(data["threat_min"])))
    out.update("gameplay", event, "TimeLimit", num(float(data["time_limit"])))
    out.update("gameplay", event, "TrackLayout", track)
    tp = data.get("track_position", [0, 0])
    out.update("gameplay", event, "TrackPosition X", num(float(tp[0])))
    out.update("gameplay", event, "TrackPosition Y", num(float(tp[1])))
    out.update("gameplay", event, "TrackRecord", num(float(data["track_record"])))
    out.update("gameplay", event, "TrafficLevel", num(int(data["traffic_level"])))
    out.update("gameplay", event, "XP", num(int(data["xp"])))
    out.update("gameplay", event, "XPDominate", num(int(data["xp_dominate"])))
    out.section("TRACK")
    out.command("add_node", "gameplay", "track_layout", track)
    for fieldname in TRACK_ADD_FIELDS: out.command("add_field", "gameplay", track, fieldname)
    out.update("gameplay", track, "gameplayvault", vault)
    out.update("gameplay", track, "Name", track)
    out.update("gameplay", track, "racefinish", f"{track}/finishline1")
    out.update("gameplay", track, "racestart", f"{track}/startgrid1")
    out.update("gameplay", track, "TrackLength", num(int(data["track_length"])))
    _start_section(out, project, track, vault)
    _finish_section(out, project, track, vault)
    _reset_section(out, project, track, vault)
    _checkpoint_sections(out, project, track, vault)
    _shortcut_sections(out, project, track, vault)
    _array_section(out, project, track)
    _registration_section(out, project, track)
    _career_logic_section(out, project)
    rendered = out.render()
    log.debug(".nfsms built: %d lines", rendered.count("\n"))
    return rendered

def export_nfsms(project: RaceProject, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text_value = build_nfsms(project)
    path.write_bytes(text_value.replace("\n", "\r\n").encode("utf-8"))
    log.info("wrote %s (%d bytes)", path, path.stat().st_size)
    return path

# ------------------------------------------------------------------------
# Source: nfsrc/services/endscript.py
# ------------------------------------------------------------------------

def _clean(value) -> str:
    return (str(value).replace("\r", " ").replace("\n", " ").replace('"', "'").strip())

def localization_entries(project: RaceProject) -> list[dict[str, str]]:
    data = project.data
    raw = data.get("localization") or []
    entries: list[dict[str, str]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                key = _clean(item.get("key", ""))
                english = _clean(item.get("en", ""))
                russian = _clean(item.get("ru", ""))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                key = _clean(item[0])
                english = _clean(item[1])
                russian = _clean(item[2]) if len(item) > 2 else english
            else:
                continue
            if key and english:
                entries.append({"key": key, "en": english, "ru": russian or english})

    if not entries:
        track = project.track_name or project.event_id
        title = _clean(data.get("event_title", "")) or track
        russian = _clean(data.get("event_title_ru", "")) or title
        loc_key = _clean(data.get("loc_key", ""))
        entries.append({"key": loc_key or f"TRACKNAME_{track.upper()}", "en": title, "ru": russian})
        if not loc_key:
            entries.append({"key": f"RACE_{project.event_id}", "en": title, "ru": russian})
    return entries

def build_endscript(project: RaceProject) -> str:
    data = project.data
    english_file = _clean(data.get("end_english_file", "LANGUAGES\\English_Global.bin"))
    russian_file = _clean(data.get("end_russian_file", "LANGUAGES\\Russian_Global.bin"))
    collection = _clean(data.get("end_collection", "STRBlocks")) or "STRBlocks"
    section = _clean(data.get("end_section", "Global")) or "Global"

    lines = ["[VERSN2]", "", "unlock_memory all", ""]

    for entry in localization_entries(project):
        key = entry["key"]
        lines.append(f'add_or_update_string {english_file} {collection} {section} AUTO {key} "{entry["en"]}"')
        if data.get("end_export_russian", True) and russian_file:
            lines.append(f'add_or_update_string {russian_file} {collection} {section} AUTO {key} "{entry["ru"]}"')
    lines.append("")
    return "\n".join(lines)

def _vault_path(value) -> str:
    return _clean(value).replace("/", "\\")

def launcher_files(project: RaceProject) -> list[str]:
    data = project.data
    files = [_vault_path(data.get("end_english_file", "LANGUAGES\\English_Global.bin"))]
    if data.get("end_export_russian", True):
        files.append(_vault_path(data.get("end_russian_file", "LANGUAGES\\Russian_Global.bin")))
    unique: list[str] = []
    seen: set[str] = set()
    for item in files:
        if item and item.lower() not in seen:
            seen.add(item.lower())
            unique.append(item)
    return unique

def build_launcher_endscript(project: RaceProject, endscript_name: str, directory: str | None = None) -> str:
    if directory is None:
        directory = project.data.get("launcher_directory", "")
    payload = {
        "Usage": "User",
        "Game": "Undercover",
        "Directory": _clean(directory),
        "Endscript": str(endscript_name).replace("\\", "/"),
        "Files": launcher_files(project),
        "Links": [
            {"LoadType": "Attributes", "PathType": "Absolute", "File": "GLOBAL\\ATTRIBUTES.BIN"},
            {"LoadType": "FeAttrib", "PathType": "Absolute", "File": "GLOBAL\\FE_ATTRIB.BIN"},
            {"LoadType": "Labels", "PathType": "Absolute", "File": "LANGUAGES\\Labels_Global.bin"},
        ],
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False).replace("\\\\", "\\")
    return "[VERSN1]\n\n" + text + "\n"

def export_endscript(project: RaceProject, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(build_endscript(project).replace("\n", "\r\n").encode("utf-8"))
    log.info("wrote %s (%d bytes)", path, path.stat().st_size)
    return path

def launcher_path(end_path: str | Path) -> Path:
    end_path = Path(end_path)
    return end_path.with_name(f"{end_path.stem}_Launcher.end")

def export_endscripts(project: RaceProject, path: str | Path, directory: str | None = None) -> tuple[Path, Path]:
    path = Path(path)
    main_path = export_endscript(project, path)
    launcher = launcher_path(path)
    launcher.write_bytes(build_launcher_endscript(project, main_path.name, directory).replace("\n", "\r\n").encode("utf-8"))
    log.info("wrote %s (%d bytes)", launcher, launcher.stat().st_size)
    return main_path, launcher


# ============================================================================
# SECTION 6 - UI WIDGETS & VIEWS
# ============================================================================

# ------------------------------------------------------------------------
# Source: nfsrc/views/widgets.py
# ------------------------------------------------------------------------
class ScrollFrame(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, bg=BG)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.inner.bind("<Configure>", self._on_inner)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.bind("<Enter>", self._enter)
        self.canvas.bind("<Leave>", self._leave)

    def _on_inner(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, event):
        self.canvas.itemconfigure(self._window, width=event.width)

    def _enter(self, _event=None):
        self.canvas.bind_all("<MouseWheel>", self._wheel)

    def _leave(self, _event=None):
        self.canvas.unbind_all("<MouseWheel>")

    def _wheel(self, event):
        self.canvas.yview_scroll(int(-event.delta / 120), "units")

# ------------------------------------------------------------------------
# Source: nfsrc/views/style.py
# ------------------------------------------------------------------------
def apply_style(root: tk.Misc) -> None:
    """Apply the editor palette, fonts and widget styles to ``root``."""
    for name in (
        "TkDefaultFont",
        "TkTextFont",
        "TkMenuFont",
        "TkHeadingFont",
        "TkCaptionFont",
        "TkTooltipFont",
    ):
        try:
            tkfont.nametofont(name).configure(family=UI_FONT_FAMILY, size=UI_FONT_SIZE)
        except tk.TclError:
            pass
    try:
        tkfont.nametofont("TkFixedFont").configure(
            family=MONO_FONT_FAMILY, size=MONO_FONT_SIZE
        )
    except tk.TclError:
        pass
    root.option_add("*Font", UI_FONT)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(".", background=BG, foreground=FG, fieldbackground=BG_FIELD, font=UI_FONT)
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=FG, font=UI_FONT)
    style.configure("TMuted.TLabel", foreground=FG_MUTED)
    style.configure("TLabelframe", background=BG, bordercolor=PANEL_BORDER)
    style.configure("TLabelframe.Label", background=BG, foreground=PANEL_TITLE_FG)
    style.configure("TButton", background=BUTTON_BG, foreground=FG, padding=(8, 4))
    style.map("TButton", background=[("active", BUTTON_ACTIVE), ("disabled", BUTTON_DISABLED)])
    style.configure("Accent.TButton", background=ACCENT_BUTTON_BG, foreground=ACCENT_BUTTON_FG)
    style.map("Accent.TButton", background=[("active", ACCENT_BUTTON_ACTIVE)])
    style.configure("TEntry", fieldbackground=BG_FIELD, foreground=FG, insertcolor=FG)
    style.configure("TSpinbox", fieldbackground=BG_FIELD, foreground=FG, insertcolor=FG)
    style.configure(
        "TCombobox",
        fieldbackground=BG_FIELD,
        foreground=FG,
        background=COMBO_BG,
        arrowcolor=FG,
        selectbackground=BG_FIELD,
        selectforeground=FG,
        insertcolor=FG,
    )
    style.map(
        "TCombobox",
        fieldbackground=[
            ("readonly", BG_FIELD),
            ("disabled", BG),
            ("active", BG_FIELD),
        ],
        foreground=[
            ("readonly", FG),
            ("disabled", FG_MUTED),
            ("active", FG),
        ],
        selectbackground=[("readonly", BG_FIELD)],
        selectforeground=[("readonly", FG)],
        background=[("readonly", COMBO_BG), ("active", BUTTON_ACTIVE)],
        arrowcolor=[("readonly", FG), ("disabled", FG_MUTED)],
    )
    root.option_add("*TCombobox*Listbox.background", TEXT_BG)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", COMBO_SELECT_BG)
    root.option_add("*TCombobox*Listbox.selectForeground", COMBO_SELECT_FG)
    style.configure("TCheckbutton", background=BG, foreground=FG)
    style.configure("TNotebook", background=BG, bordercolor=PANEL_BORDER)
    style.configure(
        "TNotebook.Tab",
        background=TAB_BG,
        foreground=FG,
        padding=(14, 7),
        bordercolor=PANEL_BORDER,
        font=UI_FONT,
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", TAB_SELECTED_BG)],
        foreground=[("selected", TAB_SELECTED_FG)],
    )
    style.configure("Treeview", background=TEXT_BG, fieldbackground=TEXT_BG, foreground=FG)
    style.configure("Treeview.Heading", background=BUTTON_BG, foreground=FG)
    style.map("Treeview", background=[("selected", TREE_SELECTED_BG)])
    style.configure("TScrollbar", background=BUTTON_BG, troughcolor=BG)
    style.configure("TSeparator", background=PANEL_BORDER)
    style.configure("TScale", background=BG)

# ------------------------------------------------------------------------
# Source: nfsrc/views/hud.py
# ------------------------------------------------------------------------

_ROW_DEFAULT_FG = {"title": HUD_TITLE_FG, "status": HUD_STATUS_FG, "race": HUD_RACE_FG, "counts": HUD_COUNTS_FG, "action": HUD_ACTION_FG, "hint": HUD_HINT_FG}

class HudWindow:
    def __init__(self, app: "RaceCreatorApp", layout: dict | None = None):
        self.app = app
        self.layout = layout if isinstance(layout, dict) else load_hud_layout()
        self.window = tk.Toplevel(app.root)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)

        background = self.layout.get("background") or HUD_BG
        border = self.layout.get("border") or HUD_BORDER
        width = int(self.layout.get("width", 550))
        x, y = self._initial_position()
        alpha = self._initial_alpha()
        self.window.attributes("-alpha", alpha)
        self.window.configure(bg=background)
        self.window.geometry(f"{width}x1+{x}+{y}")

        self.click_through = bool(app.settings.get("hud_click_through", False))
        self.visible = bool(app.settings.get("hud_visible", True))
        self._drag_start = None
        self._flash_job = None
        self.last_action = "Ready"
        self.labels: dict[str, tk.Label] = {}
        self.vars: dict[str, tk.StringVar] = {}
        self._rows: dict[str, dict] = {}

        outer = tk.Frame(self.window, bg=background, highlightbackground=border, highlightthickness=int(self.layout.get("border_width", 1)))
        outer.pack(fill="both", expand=True)
        self.outer = outer

        for row in self.layout.get("rows", []):
            if not isinstance(row, dict) or not row.get("visible", True):
                continue
            row_id = str(row.get("id", ""))
            if row_id not in {"title", "status", "race", "counts", "action", "hint"}:
                continue
            text = self._initial_text(row_id, row)
            var = tk.StringVar(master=self.window, value=text)
            label = tk.Label(outer, textvariable=var, bg=row.get("bg") or background, fg=row.get("fg") or _ROW_DEFAULT_FG.get(row_id, HUD_STATUS_FG), font=font_for(row, row_id), anchor=row.get("anchor", "w"), justify=row.get("justify", "left"), padx=int(row.get("padx", 10)), pady=int(row.get("pady", 0)))
            fill = row.get("fill", "x")
            if fill == "both":
                label.pack(fill="both", expand=True)
            elif fill == "none":
                label.pack()
            else:
                label.pack(fill="x")
            self.labels[row_id] = label
            self.vars[row_id] = var
            self._rows[row_id] = row

        for widget in (outer, *self.labels.values()):
            widget.bind("<ButtonPress-1>", self._drag_begin)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.bind("<ButtonRelease-1>", self._drag_end)

        self.window.update_idletasks()
        height = max(1, self.outer.winfo_reqheight())
        self.window.geometry(f"{width}x{height}+{x}+{y}")

        if not self.visible:
            self.window.withdraw()
        else:
            self.window.deiconify()
        self.window.after(100, self._apply_click_through)
        log.info("HUD built from %s: %d row(s), %dx%d at %d,%d", HUD_LAYOUT_PATH, len(self.labels), width, height, x, y)

    # ------------------------------------------------------------- geometry
    def _initial_position(self) -> tuple[int, int]:
        x = int(self.layout.get("x", 14))
        y = int(self.layout.get("y", 14))
        if self.layout.get("use_saved_position"):
            try:
                sx = int(self.app.settings.get("hud_x"))
                sy = int(self.app.settings.get("hud_y"))
            except (TypeError, ValueError):
                sx = sy = -1
            if sx >= 0:
                x = sx
            if sy >= 0:
                y = sy
        return x, y

    def _initial_alpha(self) -> float:
        if self.app.settings.get("hud_alpha_custom"):
            try:
                return max(0.3, min(1.0, float(self.app.settings.get("hud_alpha", 0.86))))
            except (TypeError, ValueError):
                pass
        return max(0.3, min(1.0, float(self.layout.get("alpha", 0.86))))

    def _initial_text(self, row_id: str, row: dict) -> str:
        if row_id == "title":
            return str(row.get("text", "NFS UNDERCOVER | RACE CREATOR"))
        if row_id == "hint":
            return str(row.get("text", DEFAULT_HINT))
        if row_id == "status":
            return str(row.get("prefix", "Game: ")) + "checking..."
        if row_id == "action":
            return "Ready"
        if row_id == "race":
            return "No race data"
        return ""

    def _drag_begin(self, event):
        if self.click_through:
            return
        self._drag_start = (event.x_root, event.y_root, self.window.winfo_x(), self.window.winfo_y())

    def _drag_move(self, event):
        if self._drag_start is None or self.click_through:
            return
        x0, y0, wx, wy = self._drag_start
        self.window.geometry(f"+{wx + event.x_root - x0}+{wy + event.y_root - y0}")

    def _drag_end(self, _event=None):
        self._drag_start = None

    def _hwnd(self):
        try:
            return int(self.window.wm_frame(), 16)
        except Exception:
            return 0

    def hwnd(self) -> int:
        return self._hwnd()

    def _apply_click_through(self):
        hwnd = self._hwnd()
        if not hwnd:
            self.window.after(300, self._apply_click_through)
            return
        user32 = ctypes.windll.user32
        GWL_EXSTYLE = -20
        WS_EX_TRANSPARENT = 0x00000020
        WS_EX_TOOLWINDOW = 0x00000080
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style |= WS_EX_TOOLWINDOW
        if self.click_through:
            style |= WS_EX_TRANSPARENT
        else:
            style &= ~WS_EX_TRANSPARENT
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
        self.window.attributes("-topmost", True)

    def set_click_through(self, value: bool):
        self.click_through = bool(value)
        self.app.settings["hud_click_through"] = self.click_through
        self._apply_click_through()

    def set_alpha(self, value: float):
        self.window.attributes("-alpha", max(0.3, min(1.0, float(value))))

    # ----------------------------------------------------------------- rows
    def update_content(self, status: str, race: str, counts: str):
        if "status" in self.vars:
            prefix = str(self._rows["status"].get("prefix", "Game: "))
            self.vars["status"].set(prefix + status)
        if "race" in self.vars:
            self.vars["race"].set(race)
        if "counts" in self.vars:
            self.vars["counts"].set(counts)

    def set_last_action(self, message: str):
        self.last_action = message
        if "action" not in self.vars:
            return
        self.vars["action"].set(message)
        self.labels["action"].configure(fg=self._rows["action"].get("fg") or _ROW_DEFAULT_FG["action"])

    def flash(self, message: str, duration: int = 2600):
        if self._flash_job is not None:
            try:
                self.window.after_cancel(self._flash_job)
            except Exception:
                pass
        if "action" in self.vars:
            self.vars["action"].set(message)
            self.labels["action"].configure(fg=self._rows["action"].get("flash_fg") or HUD_FLASH_FG)
        self._flash_job = self.window.after(duration, self._restore_action)

    def _restore_action(self):
        self._flash_job = None
        if "action" not in self.vars:
            return
        self.vars["action"].set(self.last_action)
        self.labels["action"].configure(fg=self._rows["action"].get("fg") or _ROW_DEFAULT_FG["action"])

    # ------------------------------------------------------------- visibility
    def show(self):
        self.visible = True
        self.window.deiconify()
        self.window.attributes("-topmost", True)
        self.app.settings["hud_visible"] = True

    def hide(self):
        self.visible = False
        self.window.withdraw()
        self.app.settings["hud_visible"] = False

    def toggle(self):
        if self.visible:
            self.hide()
        else:
            self.show()

    def geometry(self):
        return (self.window.winfo_x(), self.window.winfo_y(), self.window.winfo_width(), self.window.winfo_height())

    def destroy(self):
        try:
            self.window.destroy()
        except Exception:
            pass

# ------------------------------------------------------------------------
# Source: nfsrc/views/main_view.py
# ------------------------------------------------------------------------
"""Window chrome, menus, shared field widgets and theme application."""





class MainViewMixin:
    def _build_menu(self):
        menu = tk.Menu(self.root)
        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label="New project", command=self.new_project, accelerator="Ctrl+N")
        file_menu.add_command(label="Open project...", command=self.open_project, accelerator="Ctrl+O")
        file_menu.add_command(label="Save project", command=self.save_project, accelerator="Ctrl+S")
        file_menu.add_command(label="Save project as...", command=self.save_project_as)
        file_menu.add_separator()
        file_menu.add_command(label="Export both scripts...", command=self.export_interactive)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.on_close)
        menu.add_cascade(label="File", menu=file_menu)

        game_menu = tk.Menu(menu, tearoff=False)
        game_menu.add_command(label="Attach / refresh game", command=lambda: self.attach_game(True))
        game_menu.add_command(label="Show / hide HUD", command=self.toggle_hud)
        game_menu.add_command(label="Read hot slot", command=self.read_hot_slot)
        game_menu.add_separator()
        game_menu.add_checkbutton(
            label="Borderless game window (HUD over fullscreen)",
            variable=self.hud_borderless_var,
            command=self.toggle_borderless,
        )
        game_menu.add_command(label="Apply borderless now", command=self.apply_borderless_now)
        game_menu.add_command(label="Launch game (windowed)", command=self.launch_game_windowed)
        menu.add_cascade(label="Game", menu=game_menu)

        help_menu = tk.Menu(menu, tearoff=False)
        help_menu.add_command(label="Hotkeys and workflow", command=self.show_help)
        help_menu.add_command(label="Show log (in app)", command=self.show_log_tab)
        help_menu.add_command(label="Open log file", command=self.open_log_file)
        help_menu.add_command(label="About", command=self.show_about)
        menu.add_cascade(label="Help", menu=help_menu)
        self.root.config(menu=menu)
        self.root.bind_all("<Control-n>", lambda _e: self.new_project())
        self.root.bind_all("<Control-o>", lambda _e: self.open_project())
        self.root.bind_all("<Control-s>", lambda _e: self.save_project())

    def _build_toolbar(self):
        bar = ttk.Frame(self.root, padding=(8, 6))
        bar.pack(fill="x")

        capture = ttk.LabelFrame(bar, text="Capture", padding=(6, 3))
        capture.pack(side="left", padx=(0, 8))
        for kind, label in (
            ("start", "Start"),
            ("finish", "Finish"),
            ("checkpoint", "Checkpoint"),
            ("bonus", "Time bonus"),
            ("shortcut", "Shortcut"),
            ("reset", "Reset"),
            ("map", "Map marker"),
        ):
            ttk.Button(
                capture,
                text=label,
                command=lambda k=kind: self.capture_action(k),
                width=11,
            ).pack(side="left", padx=2)

        project = ttk.LabelFrame(bar, text="Project", padding=(6, 3))
        project.pack(side="left", padx=(0, 8))
        ttk.Button(project, text="New", command=self.new_project, width=7).pack(side="left", padx=2)
        ttk.Button(project, text="Open", command=self.open_project, width=7).pack(side="left", padx=2)
        ttk.Button(project, text="Save", command=self.save_project, width=7).pack(side="left", padx=2)
        ttk.Button(project, text="Undo", command=self.undo, width=7).pack(side="left", padx=2)

        view = ttk.LabelFrame(bar, text="View", padding=(6, 3))
        view.pack(side="left")
        ttk.Button(view, text="HUD", command=self.toggle_hud, width=7).pack(side="left", padx=2)
        ttk.Button(view, text="Attach", command=lambda: self.attach_game(True), width=7).pack(
            side="left", padx=2
        )
        ttk.Button(
            view, text="Export", command=self.export_interactive, style="Accent.TButton", width=9
        ).pack(side="left", padx=2)

    # --------------------------------------------------------------- notebook

    def _build_notebook(self):
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        self.race_tab = ttk.Frame(self.notebook)
        self.points_tab = ttk.Frame(self.notebook)
        self.map_tab = ttk.Frame(self.notebook)
        self.export_tab = ttk.Frame(self.notebook)
        self.setup_tab = ttk.Frame(self.notebook)
        self.advanced_tab = ttk.Frame(self.notebook)
        self.log_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.race_tab, text="Race")
        self.notebook.add(self.points_tab, text="Points")
        self.notebook.add(self.map_tab, text="Map")
        self.notebook.add(self.export_tab, text="Export")
        self.notebook.add(self.advanced_tab, text="Advanced")
        self.notebook.add(self.setup_tab, text="Setup")
        self.notebook.add(self.log_tab, text="Log")
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self._build_race_tab()
        self._build_points_tab()
        self._build_map_tab()
        self._build_export_tab()
        self._build_advanced_tab()
        self._build_setup_tab()
        self._build_log_tab()

    def _build_statusbar(self):
        bar = ttk.Frame(self.root, padding=(8, 3))
        bar.pack(fill="x", side="bottom")
        self.status_var = tk.StringVar(value="Ready")
        self.game_status_var = tk.StringVar(value="Game: not running")
        ttk.Label(bar, textvariable=self.status_var, style="TMuted.TLabel").pack(
            side="left", fill="x", expand=True
        )
        ttk.Label(bar, textvariable=self.game_status_var).pack(side="right")

    # ------------------------------------------------------------- variables

    def _mk_var(self, key: str, kind: str = "str") -> tk.Variable:
        if kind == "bool":
            var: tk.Variable = tk.BooleanVar(value=bool(self.project.data.get(key, False)))
        else:
            value = self.project.data.get(key, "")
            if isinstance(value, (list, tuple, dict)):
                value = ""
            var = tk.StringVar(value=fmt_number(value))
        self.field_vars[key] = var
        return var

    def _bind_dirty_traces(self):
        for key, var in self.field_vars.items():
            if key in ("tp_x", "tp_y"):
                continue
            var.trace_add("write", lambda *_a: self.mark_dirty())

    def _add_labeled(
        self, parent, row: int, label: str, widget, column: int = 0, pady: int = 2
    ):
        ttk.Label(parent, text=label).grid(
            row=row, column=column, sticky="w", padx=(6, 8), pady=pady
        )
        widget.grid(row=row, column=column + 1, sticky="ew", padx=(0, 8), pady=pady)
        parent.columnconfigure(column + 1, weight=1)
        return widget

    def _entry(self, parent, key: str, width: int = 16) -> ttk.Entry:
        var = self._mk_var(key)
        return ttk.Entry(parent, textvariable=var, width=width)

    def _spin(self, parent, key: str, minimum: int, maximum: int, width: int = 10):
        var = self._mk_var(key)
        if maximum is None:
            return ttk.Spinbox(parent, textvariable=var, width=width, from_=minimum, to=10**9)
        return ttk.Spinbox(
            parent, textvariable=var, width=width, from_=minimum, to=maximum
        )

    def _check(self, parent, key: str, text: str) -> ttk.Checkbutton:
        var = self._mk_var(key, "bool")
        return ttk.Checkbutton(parent, text=text, variable=var)

    # --------------------------------------------------------------- race tab

    def _on_tab_changed(self, _event=None):
        try:
            current = self.notebook.select()
        except tk.TclError:
            return
        if current == str(self.export_tab):
            self._refresh_validation_and_previews(light=False)
        elif current == str(self.log_tab):
            self._refresh_log_view()
        elif current == str(self.map_tab):
            self.map_view.refresh_markers()

    def _configure_style(self):
        apply_style(self.root)

# ------------------------------------------------------------------------
# Source: nfsrc/views/tabs.py
# ------------------------------------------------------------------------
"""Notebook tab construction."""





class TabsMixin:
    def _build_race_tab(self):
        scroll = ScrollFrame(self.race_tab)
        scroll.pack(fill="both", expand=True)
        body = scroll.inner
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        left = ttk.Frame(body)
        right = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(4, 8), pady=4)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 4), pady=4)
        left.columnconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        identity = ttk.LabelFrame(left, text="Identity", padding=(6, 6))
        identity.pack(fill="x", pady=(0, 8))
        self.event_type_var = tk.StringVar(
            value=EVENT_TYPE_LABELS[self.project.event_type]
        )
        self.region_var = tk.StringVar(value=REGION_LABELS[self.project.region])
        self.field_vars["event_type"] = self.event_type_var
        self.field_vars["region"] = self.region_var

        type_box = ttk.Combobox(
            identity,
            textvariable=self.event_type_var,
            values=[EVENT_TYPE_LABELS[t] for t in EVENT_TYPES],
            state="readonly",
            width=24,
        )
        self._add_labeled(identity, 0, "Event type", type_box)
        type_box.bind("<<ComboboxSelected>>", lambda _e: self._on_identity_change())
        self._add_labeled(identity, 1, "Event ID", self._entry(identity, "event_id"))

        region_box = ttk.Combobox(
            identity,
            textvariable=self.region_var,
            values=[REGION_LABELS[r] for r in REGIONS],
            state="readonly",
            width=24,
        )
        self._add_labeled(identity, 2, "Region", region_box)
        region_box.bind("<<ComboboxSelected>>", lambda _e: self._on_identity_change())

        track_row = ttk.Frame(identity)
        track_entry = self._entry(track_row, "track_name", 20)
        track_entry.pack(side="left", fill="x", expand=True)
        track_entry.bind("<KeyRelease>", lambda _e: setattr(self, "track_name_auto", False))
        ttk.Button(
            track_row, text="Suggest", command=self._suggest_track_name, width=8
        ).pack(side="left", padx=(4, 0))
        self._add_labeled(identity, 3, "Track / vault name", track_row)

        self._add_labeled(identity, 4, "Event title (English)", self._entry(identity, "event_title"))
        self._add_labeled(identity, 5, "Event title (Russian)", self._entry(identity, "event_title_ru"))
        self._add_labeled(identity, 6, "Localization key", self._entry(identity, "loc_key"))
        ttk.Label(
            identity,
            text="The .end export writes these titles into the language vaults.",
            style="TMuted.TLabel",
        ).grid(row=7, column=0, columnspan=2, sticky="w", padx=6, pady=(2, 0))

        progression = ttk.LabelFrame(left, text="Progression and rewards", padding=(6, 6))
        progression.pack(fill="x", pady=(0, 8))
        self._add_labeled(progression, 0, "Unlock level (0-18)", self._spin(progression, "level", 0, 18))
        self._add_labeled(progression, 1, "Cash", self._spin(progression, "cash_value", 0, None))
        self._add_labeled(
            progression, 2, "Cash on dominate", self._spin(progression, "cash_dominate", 0, None)
        )
        self._add_labeled(progression, 3, "XP", self._spin(progression, "xp", 0, None))
        self._add_labeled(
            progression, 4, "XP on dominate", self._spin(progression, "xp_dominate", 0, None)
        )
        self._add_labeled(
            progression, 5, "Domination time (s)", self._entry(progression, "domination_time")
        )
        self._add_labeled(
            progression, 6, "Track record (s)", self._entry(progression, "track_record")
        )
        self._add_labeled(
            progression, 7, "Time limit (checkpoint)", self._entry(progression, "time_limit")
        )
        self._add_labeled(
            progression, 8, "Traffic level (0-100)", self._spin(progression, "traffic_level", 0, 100)
        )
        self._add_labeled(
            progression, 9, "Track length", self._spin(progression, "track_length", 0, None)
        )

        flags = ttk.LabelFrame(left, text="Flags", padding=(6, 6))
        flags.pack(fill="x")
        self._check(flags, "cops_in_race", "Cops in race").grid(
            row=0, column=0, columnspan=2, sticky="w", padx=6
        )
        self._check(flags, "collectors_edition", "Collectors Edition race").grid(
            row=1, column=0, columnspan=2, sticky="w", padx=6
        )
        self._check(flags, "master_event", "Master event").grid(
            row=2, column=0, columnspan=2, sticky="w", padx=6
        )
        self._add_labeled(flags, 3, "Intro NIS", self._entry(flags, "intro_nis"))

        ai = ttk.LabelFrame(right, text="AI racers (0-100)", padding=(6, 6))
        ai.pack(fill="x", pady=(0, 8))
        ai_fields = [
            ("Accel min", "ai_accel_min", 0),
            ("Accel max", "ai_accel_max", 1),
            ("Handling min", "ai_handling_min", 2),
            ("Handling max", "ai_handling_max", 3),
            ("Skill min", "ai_skill_min", 4),
            ("Skill max", "ai_skill_max", 5),
            ("Speed min", "ai_speed_min", 6),
            ("Speed max", "ai_speed_max", 7),
        ]
        for label, key, row in ai_fields:
            self._add_labeled(ai, row, label, self._spin(ai, key, 0, 100, 12))
        for row in range(4):
            ai.rowconfigure(row, weight=1)

        pursuit = ttk.LabelFrame(right, text="Pursuit", padding=(6, 6))
        pursuit.pack(fill="x", pady=(0, 8))
        self._add_labeled(pursuit, 0, "Threat range min", self._spin(pursuit, "threat_min", 0, 9))
        self._add_labeled(pursuit, 1, "Threat range max", self._spin(pursuit, "threat_max", 0, 9))
        ttk.Label(
            pursuit,
            text="Region index is derived from the selected region (A=0, B=1, C=2).",
            style="TMuted.TLabel",
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=6, pady=(4, 0))

        tp = ttk.LabelFrame(right, text="TrackPosition (map marker)", padding=(6, 6))
        tp.pack(fill="x")
        tp_x = tk.StringVar(value="0")
        tp_y = tk.StringVar(value="0")
        self.field_vars["tp_x"] = tp_x
        self.field_vars["tp_y"] = tp_y
        self._add_labeled(tp, 0, "Map X", ttk.Entry(tp, textvariable=tp_x, width=14))
        self._add_labeled(tp, 1, "Map Y", ttk.Entry(tp, textvariable=tp_y, width=14))
        tp_buttons = ttk.Frame(tp)
        tp_buttons.grid(row=2, column=0, columnspan=2, sticky="ew", padx=6, pady=(4, 0))
        ttk.Button(
            tp_buttons, text="Open map", command=lambda: self.notebook.select(self.map_tab)
        ).pack(side="left")
        ttk.Button(tp_buttons, text="Use car position", command=lambda: self.capture_action("map")).pack(
            side="left", padx=4
        )
        ttk.Label(
            tp,
            text="Click the Map tab (or right-click) to place the race marker.",
            style="TMuted.TLabel",
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=6, pady=(4, 0))

        self.race_summary_var = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.race_summary_var, style="TMuted.TLabel").pack(
            fill="x", padx=6, pady=(8, 0)
        )

    def _on_identity_change(self):
        if self.track_name_auto:
            self._suggest_track_name()

    def _suggest_track_name(self):
        event_type = self._current_event_type()
        region = self._current_region()
        event_id = str(self.field_vars["event_id"].get())
        name = suggest_track_name(event_type, region, event_id)
        self._loading = True
        self.field_vars["track_name"].set(name)
        self._loading = False
        self.track_name_auto = True

    def _current_event_type(self) -> str:
        label = str(self.event_type_var.get())
        for key, value in EVENT_TYPE_LABELS.items():
            if value == label:
                return key
        return label if label in EVENT_TYPES else "sprint"

    def _current_region(self) -> str:
        label = str(self.region_var.get())
        for key, value in REGION_LABELS.items():
            if value == label:
                return key
        return label if label in REGIONS else "region_a"

    # ------------------------------------------------------------- points tab

    def _build_points_tab(self):
        panes = ttk.PanedWindow(self.points_tab, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=6, pady=6)
        left = ttk.Frame(panes)
        right = ttk.Frame(panes)
        panes.add(left, weight=3)
        panes.add(right, weight=2)

        columns = ("type", "x", "y", "z", "rot", "extra")
        self.points_tree = ttk.Treeview(left, columns=columns, show="headings", height=18)
        headings = {
            "type": "Point",
            "x": "X",
            "y": "Y",
            "z": "Z",
            "rot": "Rotation",
            "extra": "Extra",
        }
        widths = {"type": 170, "x": 80, "y": 80, "z": 70, "rot": 80, "extra": 150}
        for column in columns:
            self.points_tree.heading(column, text=headings[column])
            self.points_tree.column(
                column, width=widths[column], anchor="w" if column == "type" else "e"
            )
        scrollbar = ttk.Scrollbar(left, orient="vertical", command=self.points_tree.yview)
        self.points_tree.configure(yscrollcommand=scrollbar.set)
        self.points_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self.points_tree.bind("<<TreeviewSelect>>", self._on_point_selected)

        actions = ttk.Frame(left)
        actions.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Button(actions, text="Delete", command=self.delete_selected_point).pack(side="left")
        ttk.Button(actions, text="Move up", command=lambda: self.move_selected_point(-1)).pack(
            side="left", padx=4
        )
        ttk.Button(actions, text="Move down", command=lambda: self.move_selected_point(1)).pack(
            side="left"
        )
        ttk.Label(
            actions,
            text="Capture buttons are in the top toolbar.",
            style="TMuted.TLabel",
        ).pack(side="right")

        editor = ttk.LabelFrame(right, text="Selected point", padding=(8, 8))
        editor.pack(fill="both", expand=True)
        for key, label in (
            ("x", "X"),
            ("y", "Y"),
            ("z", "Z"),
            ("rotation", "Rotation"),
            ("dim_x", "Dimensions X"),
            ("dim_y", "Dimensions Y"),
            ("dim_z", "Dimensions Z"),
            ("flare_spacing", "Flare spacing"),
            ("time_bonus", "Time bonus (s)"),
            ("width", "Reset width"),
        ):
            var = tk.StringVar(value="0")
            self.point_editor_vars[key] = var
            ttk.Label(editor, text=label).grid(
                row=len(self.point_editor_vars) - 1, column=0, sticky="w", padx=(4, 8), pady=3
            )
            ttk.Entry(editor, textvariable=var, width=14).grid(
                row=len(self.point_editor_vars) - 1,
                column=1,
                sticky="ew",
                padx=(0, 4),
                pady=3,
            )
        editor.columnconfigure(1, weight=1)
        self.point_kind_var = tk.StringVar(value="No point selected")
        ttk.Label(editor, textvariable=self.point_kind_var, foreground=PANEL_TITLE_FG).grid(
            row=10, column=0, columnspan=2, sticky="w", padx=4, pady=(8, 2)
        )
        buttons = ttk.Frame(editor)
        buttons.grid(row=11, column=0, columnspan=2, sticky="ew", padx=4, pady=(4, 0))
        ttk.Button(buttons, text="Apply", command=self.apply_point_edit).pack(side="left")
        ttk.Button(buttons, text="Use last capture", command=self.use_last_for_point).pack(
            side="left", padx=4
        )
        ttk.Button(buttons, text="Clear fields", command=self._clear_point_editor).pack(side="left")
        ttk.Label(
            editor,
            text="Dimensions and flare spacing apply to finish and checkpoints;\n"
            "time bonus applies to bonus checkpoints; reset width to the reset trigger.",
            style="TMuted.TLabel",
        ).grid(row=12, column=0, columnspan=2, sticky="w", padx=4, pady=(8, 0))

    # ---------------------------------------------------------------- map tab

    def _build_map_tab(self):

        frame = ttk.Frame(self.map_tab)
        frame.pack(fill="both", expand=True)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.map_view = MapView(frame, self)
        self.map_view.grid(row=0, column=0, sticky="nsew", padx=(6, 0), pady=6)

        side = ttk.Frame(frame, padding=(8, 6))
        side.grid(row=0, column=1, sticky="ns")
        ttk.Label(
            side, text="MAP", foreground=PANEL_TITLE_FG,
            font=(UI_FONT_FAMILY, UI_FONT_SIZE + 1, "bold")
        ).pack(anchor="w")
        ttk.Label(
            side,
            text=f"{map_display_name()} road map (8192 x 8192) with the route,\n"
            "captured points and the TrackPosition marker\n"
            "in one clean view.",
            style="TMuted.TLabel",
            justify="left",
        ).pack(anchor="w", pady=(2, 6))
        ttk.Label(
            side,
            textvariable=self.map_view.route_status_var(),
            style="TMuted.TLabel",
            wraplength=250,
            justify="left",
        ).pack(anchor="w", pady=(0, 4))
        ttk.Button(
            side,
            text="Recalculate route",
            command=lambda: self.map_view.request_route(force=True),
        ).pack(fill="x", pady=2)
        ttk.Button(side, text="Fit map", command=self.map_view.fit_view).pack(fill="x", pady=2)
        zoom = ttk.Frame(side)
        zoom.pack(fill="x", pady=2)
        ttk.Button(zoom, text="-", width=4, command=lambda: self.map_view.zoom_by(0.8)).pack(
            side="left", expand=True, fill="x"
        )
        ttk.Button(zoom, text="+", width=4, command=lambda: self.map_view.zoom_by(1.25)).pack(
            side="left", expand=True, fill="x", padx=4
        )
        rotate = ttk.Frame(side)
        rotate.pack(fill="x", pady=2)
        ttk.Button(
            rotate, text="Rotate left", command=lambda: self.map_view.rotate(-1)
        ).pack(side="left", expand=True, fill="x")
        ttk.Button(
            rotate, text="Rotate right", command=lambda: self.map_view.rotate(1)
        ).pack(side="left", expand=True, fill="x", padx=4)
        ttk.Checkbutton(side, text="Flip Y for display", variable=self.map_view.flip_y).pack(
            anchor="w", pady=(6, 2)
        )
        ttk.Checkbutton(side, text="Show grid", variable=self.map_view.show_grid).pack(
            anchor="w", pady=2
        )

        ttk.Separator(side).pack(fill="x", pady=6)
        ttk.Label(
            side, text="TRACKPOSITION", foreground=PANEL_TITLE_FG
        ).pack(anchor="w")
        ttk.Button(
            side,
            text="Use car position",
            command=lambda: self.capture_action("map"),
        ).pack(fill="x", pady=2)
        ttk.Button(
            side,
            text="Use start position",
            command=self.map_view.use_start_position,
        ).pack(fill="x", pady=2)
        ttk.Label(
            side,
            text=(
                "Marker X / Y fields are on the Race tab.\n\n"
                "Left click sets TrackPosition (0..3072).\n"
                "Right-drag pans; the wheel zooms 1-16x.\n\n"
                "RouteRoad: center line with a fading halo,\n"
                "fixed width, shortest Start -> CP/TBCP ->\n"
                "Finish path. Lime spurs are shortcuts.\n"
                "White route segments change road level (Z)."
            ),
            style="TMuted.TLabel",
            justify="left",
        ).pack(anchor="w", pady=(6, 0))

    # ------------------------------------------------------------- export tab

    def _build_export_tab(self):
        top = ttk.Frame(self.export_tab, padding=(8, 6))
        top.pack(fill="x")
        ttk.Label(top, text="Output folder").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.export_dir_var = tk.StringVar(value=self.settings.get("export_dir", str(EXPORTS_DIR)))
        ttk.Entry(top, textvariable=self.export_dir_var, width=70).grid(
            row=0, column=1, sticky="ew"
        )
        ttk.Button(top, text="Browse...", command=self._browse_export_dir).grid(
            row=0, column=2, padx=(4, 0)
        )
        ttk.Label(top, text="Base file name").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(4, 0))
        self.export_base_var = tk.StringVar(value=self.settings.get("export_base", ""))
        base_entry = ttk.Entry(top, textvariable=self.export_base_var, width=40)
        base_entry.grid(row=1, column=1, sticky="w", pady=(4, 0))
        base_entry.bind("<KeyRelease>", lambda _e: setattr(self, "base_name_auto", False))
        ttk.Button(top, text="Auto name", command=self._auto_export_base).grid(
            row=1, column=2, padx=(4, 0), pady=(4, 0)
        )
        top.columnconfigure(1, weight=1)

        buttons = ttk.Frame(self.export_tab, padding=(8, 0))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Refresh preview", command=self.refresh_previews).pack(side="left")
        ttk.Button(buttons, text="Save .nfsms", command=lambda: self.export_files(False)).pack(
            side="left", padx=4
        )
        ttk.Button(
            buttons, text="Save .end (V1+V2)", command=lambda: self.export_files(True)
        ).pack(side="left")
        ttk.Button(buttons, text="Save both", command=self.export_interactive).pack(side="left", padx=4)
        ttk.Button(buttons, text="Open folder", command=self._open_export_dir).pack(side="left")
        ttk.Button(buttons, text="Check game data", command=self.check_applied_vault).pack(
            side="left", padx=(4, 0)
        )
        self.export_status_var = tk.StringVar(value="")
        ttk.Label(buttons, textvariable=self.export_status_var, style="TMuted.TLabel").pack(
            side="right"
        )

        validation = ttk.LabelFrame(self.export_tab, text="Validation", padding=(4, 4))
        validation.pack(fill="x", padx=8, pady=(6, 0))
        self.validation_text = tk.Text(
            validation,
            height=4,
            **TEXT_WIDGET_STYLE,
            font=MONO_FONT,
            wrap="word",
        )
        self.validation_text.pack(fill="x")
        self.validation_text.tag_configure("error", foreground=LOG_ERROR_FG)
        self.validation_text.tag_configure("warn", foreground=LOG_WARNING_FG)
        self.validation_text.configure(state="disabled")

        panes = ttk.PanedWindow(self.export_tab, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=8, pady=6)
        nfsms_frame = ttk.LabelFrame(panes, text=".nfsms preview (VltEd)", padding=(4, 4))
        end_frame = ttk.LabelFrame(panes, text=".end preview (Binary Endscript)", padding=(4, 4))
        panes.add(nfsms_frame, weight=3)
        panes.add(end_frame, weight=2)

        self.nfsms_preview = self._preview_text(nfsms_frame)
        end_tabs = ttk.Notebook(end_frame)
        end_tabs.pack(fill="both", expand=True)
        main_tab = ttk.Frame(end_tabs)
        launcher_tab = ttk.Frame(end_tabs)
        end_tabs.add(main_tab, text="VERSN2 main")
        end_tabs.add(launcher_tab, text="VERSN1 launcher")
        self.end_preview = self._preview_text(main_tab)
        self.launcher_preview = self._preview_text(launcher_tab)

    def _preview_text(self, parent) -> tk.Text:
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True)
        text = tk.Text(
            frame,
            **TEXT_WIDGET_STYLE,
            font=MONO_FONT,
            wrap="none",
        )
        y_scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        x_scroll = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
        text.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        text.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        text.configure(state="disabled")
        return text

    # ----------------------------------------------------------- advanced tab

    def _adv_text(self, parent, height: int = 1, width: int = 78) -> tk.Text:
        widget = tk.Text(
            parent,
            height=height,
            width=width,
            **TEXT_WIDGET_STYLE,
            font=MONO_FONT,
            wrap="none",
            padx=4,
            pady=2,
        )
        widget.bind("<KeyRelease>", lambda _e: self.mark_dirty())
        return widget

    def _build_advanced_tab(self):
        scroll = ScrollFrame(self.advanced_tab)
        scroll.pack(fill="both", expand=True)
        body = scroll.inner
        body.columnconfigure(0, weight=1)

        arrays = ttk.LabelFrame(body, text="Track arrays and rewards", padding=(8, 8))
        arrays.pack(fill="x", padx=6, pady=(6, 8))
        arrays.columnconfigure(1, weight=1)
        for row, (key, label) in enumerate(
            (
                ("barriers", "Barriers (comma separated IDs)"),
                ("rewards_winner", "Rewards for winner (comma separated nodes)"),
                ("rewards_dominate", "Rewards for dominate"),
            )
        ):
            ttk.Label(arrays, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
            self.adv_texts[key] = self._adv_text(arrays, height=1)
            self.adv_texts[key].grid(row=row, column=1, sticky="ew", pady=2)

        localization = ttk.LabelFrame(
            body, text="Localization entries (KEY | English | Russian)", padding=(8, 8)
        )
        localization.pack(fill="x", padx=6, pady=(0, 8))
        localization.columnconfigure(0, weight=1)
        ttk.Label(
            localization,
            text="One line per key. Leave empty to auto-generate TRACKNAME_<track> and RACE_<event>.",
            style="TMuted.TLabel",
        ).grid(row=0, column=0, sticky="w", pady=(0, 4))
        loc_row = ttk.Frame(localization)
        loc_row.grid(row=1, column=0, sticky="ew")
        loc_row.columnconfigure(0, weight=1)
        self.adv_texts["localization"] = self._adv_text(loc_row, height=5)
        self.adv_texts["localization"].grid(row=0, column=0, sticky="ew")
        loc_scroll = ttk.Scrollbar(loc_row, orient="vertical", command=self.adv_texts["localization"].yview)
        loc_scroll.grid(row=0, column=1, sticky="ns")
        self.adv_texts["localization"].configure(yscrollcommand=loc_scroll.set)

        end_box = ttk.LabelFrame(body, text="Endscript (.end) settings", padding=(8, 8))
        end_box.pack(fill="x", padx=6, pady=(0, 8))
        end_box.columnconfigure(1, weight=1)
        for row, (key, label) in enumerate(
            (
                ("end_english_file", "English language vault"),
                ("end_russian_file", "Russian language vault"),
                ("end_collection", "String collection"),
                ("end_section", "String block (Undercover: Global)"),
            )
        ):
            ttk.Label(end_box, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
            self._entry(end_box, key, 52).grid(row=row, column=1, sticky="ew", pady=2)
        self._check(end_box, "end_export_russian", "Also write Russian strings").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )

        registration = ttk.LabelFrame(body, text="Registration and unlocks", padding=(8, 8))
        registration.pack(fill="x", padx=6, pady=(0, 8))
        ttk.Label(
            registration,
            text="Off by default. Enter the CURRENT number of children in the target array; "
            "VltEd resizes to current+1. A wrong count can overwrite existing entries.",
            style="TMuted.TLabel",
            wraplength=820,
            justify="left",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        for row, (flag, label, count_key) in enumerate(
            (
                ("register_race_bin", "Append event to race_bin_career", "race_bin_children"),
                (
                    "register_track_category",
                    "Append track to tracks/<region>/<type>",
                    "track_category_children",
                ),
                ("unlock_quick_race", "Unlock in quick race", "quick_race_children"),
                (
                    "unlock_collectors_quick_race",
                    "Unlock in Collectors Edition quick race",
                    "collectors_quick_race_children",
                ),
            ),
            start=1,
        ):
            self._check(registration, flag, label).grid(row=row, column=0, sticky="w", pady=2)
            ttk.Label(registration, text="current Children count").grid(
                row=row, column=1, sticky="e", padx=(24, 6), pady=2
            )
            self._spin(registration, count_key, 0, None, 8).grid(
                row=row, column=2, sticky="w", pady=2
            )

        self._check(
            registration,
            "register_career_event",
            "Add career logic trigger (map marker)",
        ).grid(row=5, column=0, sticky="w", pady=(6, 0))
        ttk.Label(registration, text="current mw2_career/unlock count").grid(
            row=5, column=1, sticky="e", padx=(24, 6), pady=(6, 0)
        )
        self._spin(registration, "career_unlock_children", 0, None, 8).grid(
            row=5, column=2, sticky="w", pady=(6, 0)
        )
        ttk.Label(
            registration,
            text="Creates mw2_career/unlock/<Event>_Event, registers it in "
            "mw2_career/unlock Children, and unlocks the event at the event Level. "
            "Needs the event registered in race_bin_career.",
            style="TMuted.TLabel",
            wraplength=820,
            justify="left",
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(2, 0))

        ttk.Button(
            body, text="Refresh preview", command=self.refresh_previews
        ).pack(anchor="w", padx=14, pady=(0, 8))

    # -------------------------------------------------------------- setup tab

    def _build_setup_tab(self):
        scroll = ScrollFrame(self.setup_tab)
        scroll.pack(fill="both", expand=True)
        body = scroll.inner
        body.columnconfigure(0, weight=1)

        game = ttk.LabelFrame(body, text="Game connection", padding=(8, 8))
        game.pack(fill="x", padx=6, pady=(6, 8))
        ttk.Label(game, text="Game folder").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.game_root_var = tk.StringVar(value=self.settings.get("game_root", ""))
        ttk.Entry(game, textvariable=self.game_root_var, width=80).grid(row=0, column=1, sticky="ew")
        ttk.Button(game, text="Browse...", command=self._browse_game_root).grid(
            row=0, column=2, padx=4
        )
        game.columnconfigure(1, weight=1)

        ttk.Label(game, text="Hot slot (1-5)").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(6, 0))
        self.hot_slot_var = tk.StringVar(value=str(self.settings.get("hot_slot", 5)))
        ttk.Spinbox(game, from_=1, to=5, width=5, textvariable=self.hot_slot_var).grid(
            row=1, column=1, sticky="w", pady=(6, 0)
        )
        ttk.Label(
            game,
            text="Race Creator writes the game's own hot-position slot. Slot 5 is used by default.",
            style="TMuted.TLabel",
            wraplength=760,
            justify="left",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))

        row = ttk.Frame(game)
        row.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Button(row, text="Attach / refresh", command=lambda: self.attach_game(True)).pack(side="left")
        ttk.Button(row, text="Read hot slot now", command=self.read_hot_slot).pack(side="left", padx=6)
        self.setup_game_var = tk.StringVar(value="Not attached")
        ttk.Label(row, textvariable=self.setup_game_var, style="TMuted.TLabel").pack(side="left", padx=8)

        hotkeys = ttk.LabelFrame(body, text="Global hotkeys", padding=(8, 8))
        hotkeys.pack(fill="x", padx=6, pady=(0, 8))
        self.hotkey_vars: dict[str, tk.StringVar] = {}
        for index, (action, label, _default) in enumerate(HOTKEY_ACTIONS):
            var = tk.StringVar(value=self.settings["hotkeys"].get(action, DEFAULT_HOTKEYS[action]))
            self.hotkey_vars[action] = var
            ttk.Label(hotkeys, text=label).grid(
                row=index, column=0, sticky="w", padx=(0, 10), pady=2
            )
            ttk.Entry(hotkeys, textvariable=var, width=22).grid(
                row=index, column=1, sticky="w", pady=2
            )
        hotkeys.columnconfigure(1, weight=1)
        ttk.Button(hotkeys, text="Apply hotkeys", command=lambda: self._apply_hotkeys(False)).grid(
            row=len(HOTKEY_ACTIONS), column=0, columnspan=2, sticky="w", pady=(6, 0)
        )
        ttk.Label(
            hotkeys,
            text="Combinations must include at least one modifier and be unused by other software. "
            "If a combo is already taken, the tool retries with Shift/Win added and logs the "
            "change.",
            style="TMuted.TLabel",
            wraplength=800,
            justify="left",
        ).grid(row=len(HOTKEY_ACTIONS) + 1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        hud = ttk.LabelFrame(body, text="In-game HUD", padding=(8, 8))
        hud.pack(fill="x", padx=6, pady=(0, 8))
        self.hud_visible_var = tk.BooleanVar(value=bool(self.settings.get("hud_visible", True)))
        self.hud_click_var = tk.BooleanVar(
            value=bool(self.settings.get("hud_click_through", False))
        )
        self.hud_alpha_var = tk.DoubleVar(value=float(self.settings.get("hud_alpha", 0.86)))
        ttk.Checkbutton(hud, text="Show HUD", variable=self.hud_visible_var).pack(anchor="w")
        ttk.Checkbutton(
            hud, text="Click-through (mouse passes to the game)", variable=self.hud_click_var
        ).pack(anchor="w")
        ttk.Checkbutton(
            hud,
            text="Borderless game window (required for the HUD over a fullscreen game)",
            variable=self.hud_borderless_var,
            command=self.toggle_borderless,
        ).pack(anchor="w")
        alpha_row = ttk.Frame(hud)
        alpha_row.pack(anchor="w", fill="x", pady=(4, 0))
        ttk.Label(alpha_row, text="Opacity").pack(side="left")
        ttk.Scale(
            alpha_row,
            from_=0.35,
            to=1.0,
            variable=self.hud_alpha_var,
            orient="horizontal",
            length=220,
        ).pack(side="left", padx=8)
        window_row = ttk.Frame(hud)
        window_row.pack(anchor="w", pady=(6, 0))
        ttk.Button(
            window_row, text="Apply borderless now", command=self.apply_borderless_now
        ).pack(side="left")
        ttk.Button(
            window_row, text="Launch game (windowed)", command=self.launch_game_windowed
        ).pack(side="left", padx=6)
        ttk.Button(hud, text="Apply HUD settings", command=self._apply_hud_settings).pack(
            anchor="w", pady=(6, 0)
        )
        ttk.Label(
            hud,
            text="Direct3D exclusive fullscreen hides every normal window. Borderless mode "
            "keeps the fullscreen look while letting the HUD stay on top. If the game is "
            "already running fullscreen, close it and use 'Launch game (windowed)'. "
            "Set the in-game resolution to your monitor size for a crisp image.",
            style="TMuted.TLabel",
            wraplength=800,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

        about = ttk.LabelFrame(body, text="Workflow", padding=(8, 8))
        about.pack(fill="x", padx=6, pady=(0, 8))
        ttk.Label(
            about,
            text=(
                "1. Start NFS Undercover and drive to the start grid.\n"
                "2. Press Ctrl+Alt+S, then drive to the finish and press Ctrl+Alt+F.\n"
                "3. Capture checkpoints, time bonus checkpoints and shortcuts in order.\n"
                "4. Open the Map tab, choose TrackPosition view and press 'Use start\n"
                "   position' so the event marker matches the car's spawn point.\n"
                "   Route view draws the captured points and the road route.\n"
                "5. Export, apply the .nfsms in VltEd, then import the *_Launcher.end\n"
                "   (VERSN1) in Binary; it loads the main VERSN2 .end automatically."
            ),
            justify="left",
        ).pack(anchor="w")
        ttk.Button(about, text="Open full help", command=self.show_help).pack(anchor="w", pady=(6, 0))

    # ---------------------------------------------------------------- log tab

    def _build_log_tab(self):
        bar = ttk.Frame(self.log_tab, padding=(6, 6))
        bar.pack(fill="x")
        ttk.Button(bar, text="Refresh", command=lambda: self._refresh_log_view(reset=True)).pack(
            side="left"
        )
        ttk.Button(bar, text="Clear view", command=self._clear_log_view).pack(side="left", padx=6)
        ttk.Button(bar, text="Open log file", command=self.open_log_file).pack(side="left")
        self.log_follow_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="Follow new lines", variable=self.log_follow_var).pack(
            side="left", padx=12
        )
        self.log_count_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.log_count_var, style="TMuted.TLabel").pack(side="right")

        frame = ttk.Frame(self.log_tab, padding=(6, 0, 6, 6))
        frame.pack(fill="both", expand=True)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(
            frame,
            **TEXT_WIDGET_STYLE,
            wrap="none",
            font=MONO_FONT,
            padx=8,
            pady=6,
            state="disabled",
        )
        y_scroll = ttk.Scrollbar(frame, orient="vertical", command=self.log_text.yview)
        x_scroll = ttk.Scrollbar(frame, orient="horizontal", command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        self.log_text.tag_configure("ERROR", foreground=LOG_ERROR_FG)
        self.log_text.tag_configure("WARNING", foreground=LOG_WARNING_FG)
        self.log_text.tag_configure("INFO", foreground=TEXT_FG)
        self.log_text.tag_configure("DEBUG", foreground=FG_MUTED)
        self._refresh_log_view(reset=True)

    def _refresh_log_view(self, reset: bool = False):
        if not hasattr(self, "log_text"):
            return
        version, entries = self.log_handler.snapshot()
        if not reset and version == self._log_version:
            return
        self._log_version = version
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        for level, text in entries:
            tag = "ERROR" if level >= logging.ERROR else logging.getLevelName(level)
            if tag in ("ERROR", "WARNING", "INFO", "DEBUG"):
                self.log_text.insert("end", text + "\n", tag)
            else:
                self.log_text.insert("end", text + "\n")
        self.log_text.configure(state="disabled")
        self.log_count_var.set(f"{len(entries)} recent line(s)")
        if self.log_follow_var.get():
            self.log_text.see("end")

    def _clear_log_view(self):
        self.log_handler.clear()
        self._refresh_log_view(reset=True)
        self._set_status("Log view cleared. The log file itself is unchanged.")

    def show_log_tab(self):
        self._refresh_log_view(reset=True)
        self.notebook.select(self.log_tab)
        self._set_status(f"Recent log lines; full file: {LOG_PATH}")

    # ----------------------------------------------------------- populate/sync


# ============================================================================
# SECTION 7 - CONTROLLERS (APPLICATION LOGIC)
# ============================================================================

# ------------------------------------------------------------------------
# Source: nfsrc/controllers/points.py
# ------------------------------------------------------------------------

class PointsMixin:
    def _on_point_selected(self, _event=None):
        selection = self.points_tree.selection()
        if not selection:
            return
        iid = selection[0]
        try:
            kind, index_text = iid.split(":", 1)
            index = int(index_text)
        except ValueError:
            return
        point = self.project.point(kind, index)
        if point is None:
            return
        self.selected_point = (kind, index)
        self.point_kind_var.set(
            f"{POINT_LABELS.get(kind, kind)}  [{kind}:{index}]"
        )
        values = _point_fields(point)
        for key, value in values.items():
            self.point_editor_vars[key].set(fmt_number(value))

    def _clear_point_editor(self):
        for var in self.point_editor_vars.values():
            var.set("")
        self.point_kind_var.set("No point selected")

    def _refresh_point_views(self):
        self._populate_points()
        self.map_view.refresh_markers()
        self._refresh_race_summary()
        self._refresh_validation_and_previews(light=True)

    def apply_point_edit(self):
        if not self.selected_point:
            return
        kind, index = self.selected_point
        point = self.project.point(kind, index)
        if point is None:
            return
        current = _point_fields(point)
        parsed = {}
        for key, value in current.items():
            parsed[key], ok = parse_float_field(str(self.point_editor_vars[key].get()), value)
            if not ok:
                self._set_status(f"{key} is not a number; kept {value}.")
                parsed[key] = value
        self._push_undo()
        for key, value in parsed.items():
            setattr(point, key, value)
        self.mark_dirty()
        self._refresh_point_views()
        self._set_status("Point updated.")

    def use_last_for_point(self):
        if self.last_hot is None:
            self._set_status("No captured position yet.")
            return
        if not self.selected_point:
            self._set_status("Select a point first.")
            return
        self.point_editor_vars["x"].set(fmt_number(self.last_hot.x))
        self.point_editor_vars["y"].set(fmt_number(self.last_hot.y))
        self.point_editor_vars["z"].set(fmt_number(self.last_hot.z))
        self.point_editor_vars["rotation"].set(fmt_number(self.last_hot.rotation))
        self.apply_point_edit()

    def delete_selected_point(self):
        if not self.selected_point:
            return
        kind, index = self.selected_point
        self._push_undo()
        if self.project.delete_point(kind, index):
            self.selected_point = None
            self._clear_point_editor()
            self.mark_dirty()
            self._refresh_point_views()
            self._set_status("Point deleted.")

    def move_selected_point(self, delta: int):
        if not self.selected_point:
            return
        kind, index = self.selected_point
        self._push_undo()
        if self.project.move_point(kind, index, delta):
            self.selected_point = (kind, index + delta)
            self.mark_dirty()
            self._populate_points()
            self.map_view.refresh_markers()
            self._refresh_validation_and_previews(light=True)
            self._set_status("Point order changed.")

    # ---------------------------------------------------------------- capture

    def set_track_position(self, x: float, y: float):
        self.project.data["track_position"] = [float(x), float(y)]
        self._set_tp_vars_from_project()
        self.map_view.refresh_markers()
        self.mark_dirty()
        self._refresh_race_summary()
        self._refresh_validation_and_previews(light=True)

    def undo(self):
        if not self.undo_stack:
            self._set_status("Nothing to undo.")
            return
        snapshot = self.undo_stack.pop()
        self.project.restore(snapshot)
        self.last_hot = None
        self.selected_point = None
        self._clear_point_editor()
        self._populate_fields()
        self._populate_advanced()
        self._refresh_point_views()
        self.mark_dirty()
        self._set_status("Undo.")

    def _push_undo(self):
        self.undo_stack.append(self.project.snapshot())
        if len(self.undo_stack) > 80:
            self.undo_stack.pop(0)

    # ------------------------------------------------------------------- game

# ------------------------------------------------------------------------
# Source: nfsrc/controllers/capture.py
# ------------------------------------------------------------------------
"""Hot-position capture actions and the UI event queue."""





class CaptureMixin:
    def capture_action(self, kind: str):
        if self.capture_in_progress:
            self._set_status("A capture is already running.")
            return
        if not self.attach_game(False):
            log.warning("capture %s blocked: game is not attached", kind)
            self._set_status("Game not running. Start nfs.exe and press Attach.")
            self.hud.flash("Game not running - start nfs.exe first")
            return
        assert self.proc is not None
        if not self.proc.signature_ok:
            log.warning("capture %s blocked: unsupported nfs.exe build", kind)
            self._set_status("Unsupported nfs.exe build; capture disabled.")
            self.hud.flash("Unsupported nfs.exe build")
            return
        slot = _hot_slot_number(self.hot_slot_var.get())
        game_root = self.game_root_var.get().strip() or str(detect_game_root(APP_DIR))
        label = POINT_LABELS.get(kind, "TrackPosition")
        log.info("capture %s requested (slot %d, game_root %s)", kind, slot, game_root)
        self.capture_in_progress = True
        self._set_status(f"Capturing {label.lower()}...")
        self.hud.flash(f"Capturing {label.lower()}...")
        proc = self.proc

        # The capture blocks in ReadProcessMemory/polling loops, so it runs on
        # a worker thread and posts its result to self.events.  _poll_events
        # then applies it on the Tk thread (Tk widgets are not thread-safe).
        def worker():
            try:
                point = proc.capture(slot, game_root, timeout=3.5)
            except Exception as exc:
                log.warning("capture %s failed: %s", kind, exc)
                self.events.put(("capture_error", kind, str(exc)))
            else:
                log.info(
                    "capture %s ok: x=%.2f y=%.2f z=%.2f rot=%.2f source=%s",
                    kind,
                    point.x,
                    point.y,
                    point.z,
                    point.rotation,
                    point.source,
                )
                self.events.put(("capture_ok", kind, point))

        threading.Thread(target=worker, name="nfs-capture", daemon=True).start()

    def _poll_events(self):
        if self._closing:
            return
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event[0] == "capture_ok":
                self.capture_in_progress = False
                kind, point = event[1], event[2]
                self.last_hot = point
                self._push_undo()
                try:
                    message = self.project.apply_hot(kind, point)
                except Exception as exc:
                    self._set_status(str(exc))
                    self.hud.flash(str(exc)[:100])
                    continue
                self.mark_dirty()
                self._populate_points()
                self._set_tp_vars_from_project()
                self.map_view.refresh_markers()
                self._refresh_race_summary()
                self._refresh_validation_and_previews(light=True)
                coords = f"{point.x:.1f}, {point.y:.1f}, {point.z:.1f}"
                self.hud.set_last_action(f"{message}: {coords}")
                self.hud.flash(f"{message}: {coords}")
                status = f"{message} at {coords}."
                if kind == "start":
                    gap = self.project.track_position_gap()
                    if gap is not None and gap > 64:
                        log.info("start saved but TrackPosition is %.0f units away", gap)
                        status += " Map marker is far from the start - use 'Use start position'."
                self._set_status(status)
                if self.project_path is not None:
                    try:
                        self.project.save(self.project_path)
                        self.dirty = False
                        self._update_title()
                    except OSError:
                        pass
            elif event[0] == "capture_error":
                self.capture_in_progress = False
                kind, error = event[1], event[2]
                self._set_status(error)
                self.hud.flash(error[:120], duration=4200)
                self.hud.set_last_action(f"Capture failed: {POINT_LABELS.get(kind, kind)}")
            elif event[0] == "route_ready":
                self.map_view.apply_route(*event[1:])
        self.root.after(60, self._poll_events)

# ------------------------------------------------------------------------
# Source: nfsrc/controllers/game.py
# ------------------------------------------------------------------------
"""Game process, HUD window, periodic tick and global hotkeys."""





class GameMixin:
    def attach_game(self, announce: bool) -> bool:
        if self.capture_in_progress:
            return self.proc is not None
        if self.proc is not None:
            try:
                self.proc.gameplay_state()
                return True
            except CaptureError:
                log.info("lost connection to nfs.exe PID %s", self.proc.pid)
                self.proc.close()
                self.proc = None
        try:
            self.proc = NfsProcess.find()
        except CaptureError as exc:
            # _tick retries every 3 s while the game is closed, so a missing
            # process is only DEBUG; explicit Attach clicks still announce it.
            log.debug("attach failed: %s", exc)
            self.proc = None
            self._update_game_labels(str(exc))
            if announce:
                messagebox.showerror("NFS Undercover", str(exc), parent=self.root)
            return False
        if self.proc.signature_ok:
            message = (
                f"Attached to nfs.exe PID {self.proc.pid}, base 0x{self.proc.base:08X}"
            )
            log.info("%s (signature ok)", message)
        else:
            message = (
                "Attached, but this nfs.exe build is not supported. "
                "Expected NFS Undercover v1.0.0.1."
            )
            log.warning("%s (PID %s, base 0x%08X)", message, self.proc.pid, self.proc.base)
        self._update_game_labels(message)
        self.hud.flash("Game attached" if self.proc.signature_ok else "Unsupported game build")
        return True

    def _update_game_labels(self, message: str):
        self.game_status_var.set(f"Game: {message}")
        self.setup_game_var.set(message)

    def _game_status_short(self) -> str:
        if self.proc is None:
            return "not running"
        if not self.proc.signature_ok:
            return "unsupported build"
        try:
            gameflow, focused = self.proc.gameplay_state()
        except CaptureError:
            return "disconnected"
        labels = {3: "front-end", 4: "loading", 5: "loading", 6: "gameplay"}
        state = labels.get(gameflow, f"state {gameflow}")
        return state + (" (focused)" if focused else " (unfocused)")

    def read_hot_slot(self):
        slot = _hot_slot_number(self.hot_slot_var.get())
        root = self.game_root_var.get().strip()
        point = read_slot(root, slot)
        if point is None:
            self._set_status(f"No saved position found in hot slot {slot}.")
            return
        self.last_hot = point
        log.info(
            "hot slot %d read from %s: %.2f, %.2f, %.2f, rot %.2f",
            slot,
            point.source,
            point.x,
            point.y,
            point.z,
            point.rotation,
        )
        self._set_status(
            f"Hot slot {slot}: {point.x:.2f}, {point.y:.2f}, {point.z:.2f}, "
            f"rotation {point.rotation:.2f}."
        )
        self.hud.flash(
            f"Slot {slot}: {point.x:.1f}, {point.y:.1f}, {point.z:.1f}"
        )

    # ------------------------------------------------------------------- HUD

    def toggle_hud(self):
        self.hud.toggle()
        self.hud_visible_var.set(self.hud.visible)
        self.save_settings()

    def toggle_borderless(self):
        enabled = bool(self.hud_borderless_var.get())
        self.settings["hud_borderless"] = enabled
        self.save_settings()
        if enabled:
            self.apply_borderless_now(quiet=True)
        else:
            self.restore_game_window()

    def apply_borderless_now(self, quiet: bool = False) -> bool:
        if not self.attach_game(False):
            if not quiet:
                self._set_status("Game not running. Start nfs.exe first.")
                self.hud.flash("Game not running - start nfs.exe first")
            return False
        assert self.proc is not None
        hwnd = find_window(self.proc.pid)
        if not hwnd:
            if not quiet:
                self._set_status("Game window not found yet.")
            return False
        if self._borderless_hwnd == hwnd:
            if not quiet:
                self.hud.flash("Game window is already borderless")
            return True
        if window_has_frame(hwnd):
            if apply_borderless(hwnd):
                self._borderless_hwnd = hwnd
                self._borderless_warned = False
                self._set_status("Game window set to borderless; the HUD will stay on top.")
                self.hud.flash("Game window: borderless fullscreen")
                return True
            if not quiet:
                self._set_status("Could not change the game window style.")
            return False
        if not covers_monitor(hwnd):
            if not quiet:
                self.hud.flash("Game window is already borderless")
            return True
        self._borderless_warned = True
        message = (
            "The game window has no frame and covers the monitor, so it is either "
            "already borderless or running in exclusive fullscreen.\n\nIf the HUD is "
            "not visible, the game is in exclusive fullscreen: close it and use "
            "Game > Launch game (windowed), or start nfs.exe with -windowed. The HUD "
            "then stays above the borderless game window."
        )
        if quiet:
            self.hud.flash(
                "If the HUD is hidden: use Game > Launch game (windowed)", duration=6000
            )
        else:
            messagebox.showinfo("Game window mode", message, parent=self.root)
        return False

    def restore_game_window(self):
        hwnd = self._borderless_hwnd
        self._borderless_hwnd = 0
        if hwnd and restore_window(hwnd):
            self._set_status("Game window restored.")
            self.hud.flash("Game window restored")

    def launch_game_windowed(self):
        if self.proc is not None:
            messagebox.showinfo(
                "Game already running",
                "Close the running game first, then launch it windowed from here.",
                parent=self.root,
            )
            return
        root = Path(self.game_root_var.get().strip() or str(detect_game_root(APP_DIR)))
        try:
            self._game_process = launch_windowed(root)
        except (OSError, FileNotFoundError) as exc:
            messagebox.showerror("Launch game", str(exc), parent=self.root)
            return
        self._borderless_hwnd = 0
        self._borderless_warned = False
        self._last_attach_try = 0.0
        self._set_status("Launched nfs.exe -windowed. Waiting for the game window...")
        self.hud.flash("Game launched windowed - waiting for the window", duration=5000)

    def _maintain_game_window(self):
        if self.proc is None or not self.hud.visible:
            return
        hwnd = find_window(self.proc.pid)
        if hwnd and self.hud_borderless_var.get():
            if window_has_frame(hwnd):
                if apply_borderless(hwnd):
                    self._borderless_hwnd = hwnd
                    self._borderless_warned = False
                    self._set_status(
                        "Game window set to borderless; the HUD will stay on top."
                    )
                    self.hud.flash("Game window: borderless fullscreen")
            elif self._borderless_hwnd != hwnd and not self._borderless_warned:
                self._borderless_warned = True
                self._set_status(
                    "HUD cannot draw over exclusive fullscreen. "
                    "Use Game > Launch game (windowed)."
                )
                self.hud.flash(
                    "If the HUD is hidden: use Game > Launch game (windowed)",
                    duration=6000,
                )
        hud_hwnd = self.hud.hwnd()
        if hud_hwnd:
            keep_above(hud_hwnd)

    def _apply_hud_settings(self):
        self.hud.set_click_through(bool(self.hud_click_var.get()))
        self.hud.set_alpha(float(self.hud_alpha_var.get()))
        if self.hud_visible_var.get():
            self.hud.show()
        else:
            self.hud.hide()
        self.settings["hud_visible"] = self.hud.visible
        self.settings["hud_click_through"] = self.hud.click_through
        self.settings["hud_alpha"] = float(self.hud_alpha_var.get())
        self.settings["hud_alpha_custom"] = True
        self.settings["hud_borderless"] = bool(self.hud_borderless_var.get())
        if self.hud_borderless_var.get():
            self.apply_borderless_now(quiet=True)
        self.save_settings()
        self._set_status("HUD settings applied.")

    def _update_hud(self):
        status = self._game_status_short()
        counts = self.project.counts()
        race = (
            f"{self.project.event_id} | {EVENT_TYPE_LABELS[self.project.event_type]} | "
            f"{self.project.region}"
        )
        point_counts = (
            f"S: {counts['start']}; F: {counts['finish']}; CP: {counts['checkpoint']}; "
            f"TBCP: {counts['bonus']}; SC: {counts['shortcut']}; R: {counts['reset']} | "
            f"TP: {fmt_number(self.project.data.get('track_position', [0, 0])[0])}, "
            f"{fmt_number(self.project.data.get('track_position', [0, 0])[1])}"
        )
        self.hud.update_content(status, race, point_counts)
        self.game_status_var.set(f"Game: {status}")

    def _tick(self):
        if self._closing:
            return
        now = time.monotonic()
        if self.proc is None and now - self._last_attach_try > 3.0 and not self.capture_in_progress:
            self._last_attach_try = now
            self.attach_game(False)
        elif self.proc is not None and not self.capture_in_progress:
            try:
                self.proc.gameplay_state()
            except CaptureError:
                log.info("game process disappeared; detaching")
                self.proc.close()
                self.proc = None
                self._update_game_labels("Disconnected")
        self._update_hud()
        self._maintain_game_window()
        if self.notebook.select() == str(self.log_tab):
            self._refresh_log_view()
        self.root.after(600, self._tick)

    def _poll_hotkeys(self):
        if self._closing:
            return
        for action in self.hotkeys.poll():
            try:
                self._handle_hotkey(action)
            except Exception as exc:
                self._set_status(f"Hotkey action failed: {exc}")
        self.root.after(50, self._poll_hotkeys)

    def _handle_hotkey(self, action: str):
        if action == "undo":
            self.undo()
        elif action == "toggle_hud":
            self.toggle_hud()
        elif action == "open_editor":
            self.show_editor()
        elif action == "export":
            self.export_quick()
        elif action == "set_map_marker":
            self.capture_action("map")
        elif action == "capture_start":
            self.capture_action("start")
        elif action == "capture_finish":
            self.capture_action("finish")
        elif action == "add_checkpoint":
            self.capture_action("checkpoint")
        elif action == "add_bonus":
            self.capture_action("bonus")
        elif action == "add_shortcut":
            self.capture_action("shortcut")
        elif action == "add_reset":
            self.capture_action("reset")

    def show_editor(self):
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except tk.TclError:
            pass

    def _rebind_conflicts(
        self,
        bindings: dict[str, str],
        original: dict[str, str],
        tried: dict[str, set[str]],
    ) -> list[str]:
        """Try alternative combos for hotkeys owned by another program.

        Updates *bindings* in place and returns messages for conflicts that
        cannot be resolved.  Only WinError 1409 (already registered) is worth
        retrying; other failures are reported straight away.
        """
        unresolved: list[str] = []
        used = {spec.lower() for spec in bindings.values()}
        for action, spec, winerror in self.hotkeys.conflicts:
            label = HOTKEY_LABELS.get(action, action)
            if winerror != 1409:
                unresolved.append(
                    f"{label} ({spec}): registration failed (WinError {winerror})."
                )
                continue
            chosen = None
            for candidate in alternative_hotkeys(original[action]):
                if candidate.lower() in used or candidate.lower() in tried[action]:
                    continue
                chosen = candidate
                break
            if chosen is None:
                unresolved.append(
                    f"{label} ({spec}): used by another program and no fallback was free."
                )
                continue
            tried[action].add(chosen.lower())
            used.discard(spec.lower())
            used.add(chosen.lower())
            bindings[action] = chosen
        return unresolved

    def _apply_hotkeys(self, initial: bool):
        bindings = {action: var.get().strip() for action, var in self.hotkey_vars.items()}
        original = dict(bindings)
        tried: dict[str, set[str]] = {action: set() for action in bindings}
        problems: list[str] = []
        for attempt in range(5):
            for message in self.hotkeys.start(bindings):
                if message not in problems:
                    problems.append(message)
            if not self.hotkeys.conflicts:
                break
            unresolved = self._rebind_conflicts(bindings, original, tried)
            if unresolved:
                problems.extend(unresolved)
                break
            if attempt == 4:
                for action, spec, winerror in self.hotkeys.conflicts:
                    problems.append(
                        f"{HOTKEY_LABELS.get(action, action)} ({spec}): still "
                        f"unavailable (WinError {winerror}); pick another key in Setup."
                    )
                break

        rebound: list[str] = []
        for action, spec in bindings.items():
            if spec != original[action]:
                self.hotkey_vars[action].set(spec)
                rebound.append(
                    f"{HOTKEY_LABELS.get(action, action)}: {original[action]} -> {spec}"
                )
        for line in rebound:
            log.info("hotkey fallback: %s", line)
        self.settings["hotkeys"] = dict(bindings)
        self.hotkey_errors = problems
        self.save_settings()
        if problems:
            detail = "\n".join(problems)
            log.warning(
                "hotkey registration: %d unresolved problem(s): %s",
                len(problems),
                detail,
            )
            self._set_status("Some hotkeys could not be registered; see the Log tab.")
            if not initial:
                messagebox.showwarning(
                    "Hotkeys",
                    "Some hotkeys could not be registered:\n\n" + detail,
                    parent=self.root,
                )
        else:
            log.info(
                "registered %d global hotkeys; %d rebound to free combinations",
                len(bindings),
                len(rebound),
            )
            summary = f"Global hotkeys registered ({len(bindings)})."
            if rebound:
                summary += (
                    f" {len(rebound)} rebound because the original combo is taken; "
                    "see Setup or the Log tab."
                )
            self._set_status(summary)

    # --------------------------------------------------------------- project

    def _take_screenshot(self, path: str):
        try:

            self.root.update_idletasks()
            self.root.lift()
            self.root.update()
            time.sleep(0.2)
            self.root.update()

            hwnd = int(self.root.wm_frame(), 16)
            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            width = int(rect.right - rect.left)
            height = int(rect.bottom - rect.top)
            if width <= 0 or height <= 0:
                raise ValueError("invalid window size")

            window_dc = user32.GetWindowDC(hwnd)
            mem_dc = gdi32.CreateCompatibleDC(window_dc)
            bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
            old = gdi32.SelectObject(mem_dc, bitmap)
            try:
                if not user32.PrintWindow(hwnd, mem_dc, 2):
                    raise ValueError("PrintWindow failed")

                class BITMAPINFOHEADER(ctypes.Structure):
                    _fields_ = [
                        ("biSize", wintypes.DWORD),
                        ("biWidth", wintypes.LONG),
                        ("biHeight", wintypes.LONG),
                        ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD),
                        ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD),
                        ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG),
                        ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD),
                    ]

                header = BITMAPINFOHEADER()
                header.biSize = ctypes.sizeof(header)
                header.biWidth = width
                header.biHeight = -height
                header.biPlanes = 1
                header.biBitCount = 32
                buffer = ctypes.create_string_buffer(width * height * 4)
                gdi32.GetDIBits(
                    mem_dc, bitmap, 0, height, buffer, ctypes.byref(header), 0
                )
                Image.frombuffer(
                    "RGBA", (width, height), buffer, "raw", "BGRA", 0, 1
                ).convert("RGB").save(path)
                return
            finally:
                gdi32.SelectObject(mem_dc, old)
                gdi32.DeleteObject(bitmap)
                gdi32.DeleteDC(mem_dc)
                user32.ReleaseDC(hwnd, window_dc)
        except Exception:
            pass
        try:
            from PIL import ImageGrab
            x, y = self.root.winfo_rootx(), self.root.winfo_rooty()
            w, h = self.root.winfo_width(), self.root.winfo_height()
            ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True).save(path)
        except Exception:
            pass

# ------------------------------------------------------------------------
# Source: nfsrc/controllers/project.py
# ------------------------------------------------------------------------
"""Project files, folders and export actions."""





class ProjectMixin:
    def _reset_and_reload(self):
        self.undo_stack.clear()
        self.last_hot = None
        self.selected_point = None
        self.dirty = False
        self._loading = True
        try:
            self._populate_all()
        finally:
            self._loading = False
        self._update_title()

    def new_project(self):
        if not self._confirm_discard():
            return
        self.project = RaceProject()
        self.project_path = None
        self.track_name_auto = True
        self._reset_and_reload()
        self._set_status("New project.")
        log.info("new project")

    def open_project(self):
        if not self._confirm_discard():
            return
        path = filedialog.askopenfilename(
            parent=self.root,
            title="Open race project",
            initialdir=str(PROJECTS_DIR),
            filetypes=[("Race Creator project", "*.nfsrace"), ("JSON", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            project = RaceProject.load(path)
        except Exception as exc:
            messagebox.showerror("Open project", f"Could not open the project:\n{exc}", parent=self.root)
            return
        self.project = project
        self.project_path = Path(path)
        self.settings["last_project"] = str(self.project_path)
        self._reset_and_reload()
        self._set_status(f"Opened {self.project_path.name}.")
        log.info("opened project %s", self.project_path)

    def save_project(self) -> bool:
        if self.project_path is None:
            return self.save_project_as()
        self._sync_all()
        try:
            self.project.save(self.project_path)
        except OSError as exc:
            messagebox.showerror("Save project", str(exc), parent=self.root)
            return False
        self.settings["last_project"] = str(self.project_path)
        self.save_settings()
        self.dirty = False
        self._update_title()
        self._set_status(f"Saved {self.project_path.name}.")
        log.info("saved project %s", self.project_path)
        return True

    def save_project_as(self) -> bool:
        default = self._auto_export_base_text() + ".nfsrace"
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Save race project",
            initialdir=str(PROJECTS_DIR),
            initialfile=default,
            defaultextension=".nfsrace",
            filetypes=[("Race Creator project", "*.nfsrace"), ("JSON", "*.json")],
        )
        if not path:
            return False
        self.project_path = Path(path)
        return self.save_project()

    def _confirm_discard(self) -> bool:
        if not self.dirty:
            return True
        answer = messagebox.askyesnocancel(
            "Unsaved changes",
            "Save the current project before continuing?",
            parent=self.root,
        )
        if answer is None:
            return False
        if answer:
            return self.save_project()
        return True

    def _browse_export_dir(self):
        path = filedialog.askdirectory(
            parent=self.root, title="Select export folder", initialdir=self.export_dir_var.get()
        )
        if path:
            self.export_dir_var.set(path)
            self.settings["export_dir"] = path
            self.save_settings()

    def _browse_game_root(self):
        path = filedialog.askdirectory(
            parent=self.root, title="Select NFS Undercover folder", initialdir=self.game_root_var.get()
        )
        if path:
            self.game_root_var.set(path)
            self.settings["game_root"] = path
            self.proc = None
            self.attach_game(False)
            self.save_settings()

    def check_applied_vault(self):
        """Report whether the project's values are inside the game vault.

        VltEd writes GLOBAL/gameplay.* only when the user imports a .nfsms and
        saves the vault; the exported script alone is not read by the game.
        Positions are stored as little-endian float32, so the exported numbers
        can be searched byte-for-byte.
        """
        root = Path(self.game_root_var.get().strip() or detect_game_root(APP_DIR))
        files = [root / "GLOBAL" / name for name in ("gameplay.lzc", "gameplay.bin")]
        start = self.project.start
        tp = self.project.data.get("track_position", [0, 0])
        patterns: list[tuple[str, tuple[float, ...]]] = []
        if start is not None:
            patterns.append(("Start grid", (start.x, start.y, start.z)))
        try:
            patterns.append(("TrackPosition", (float(tp[0]), float(tp[1]))))
        except (TypeError, ValueError):
            pass
        checkpoints = [(p.x, p.y, p.z) for p in self.project.checkpoints]

        def packed(values) -> bytes:
            # The .nfsms writes 6 decimals, so match that before VltEd turns
            # the text into a 32-bit float.
            return b"".join(struct.pack("<f", float(f"{float(v):.6f}")) for v in values)

        lines: list[str] = []
        start_found = False
        tp_found = False
        any_file = False
        for path in files:
            if not path.exists():
                lines.append(f"{path.name}: not found in {path.parent}")
                continue
            any_file = True
            try:
                data = path.read_bytes()
            except OSError as exc:
                lines.append(f"{path.name}: cannot read ({exc})")
                continue
            saved = time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime))
            lines.append(f"{path.name} (saved {saved}):")
            for label, values in patterns:
                hit = data.find(packed(values)) >= 0
                lines.append(f"  {label}: {'FOUND' if hit else 'NOT FOUND'}")
                if hit and label == "Start grid":
                    start_found = True
                if hit and label == "TrackPosition":
                    tp_found = True
            hits = sum(1 for values in checkpoints if data.find(packed(values)) >= 0)
            lines.append(f"  Checkpoints found: {hits}/{len(checkpoints)}")

        if not any_file:
            summary = (
                f"No gameplay vault found under {root}\\GLOBAL. "
                "Set the correct game folder in Setup."
            )
        elif start_found and tp_found:
            summary = (
                "The game vault contains the current Start grid and TrackPosition. "
                "Restart the game so it reloads the gameplay data."
            )
        else:
            summary = (
                "The game vault does NOT contain the current export. Import the "
                ".nfsms in VltEd, save the gameplay vault, then restart the game."
            )
        log.info("game data check: %s\n%s", summary, "\n".join(lines))
        self._set_status(summary)
        messagebox.showinfo("Game data check", summary + "\n\n" + "\n".join(lines), parent=self.root)

    def _open_export_dir(self):
        path = Path(self.export_dir_var.get())
        path.mkdir(parents=True, exist_ok=True)
        try:

            os.startfile(str(path))
        except OSError as exc:
            self._set_status(str(exc))

    def export_files(self, end_only: bool = False) -> bool:
        self._sync_all()
        messages = self.project.validate()
        errors = [m for m in messages if m.startswith("ERROR")]
        log.info(
            "export requested: %s (%d error(s), %d note(s))",
            self.project.event_id,
            len(errors),
            len(messages) - len(errors),
        )
        if errors:
            log.warning("export has validation errors: %s", "; ".join(errors[:8]))
            answer = messagebox.askyesno(
                "Validation errors",
                "The project has validation errors:\n\n"
                + "\n".join(errors[:8])
                + "\n\nExport anyway?",
                parent=self.root,
            )
            if not answer:
                log.info("export cancelled by the user")
                return False
        nfsms_path, end_path = self._output_paths()
        written: list[Path] = []
        try:
            if not end_only:
                written.append(export_nfsms(self.project, nfsms_path))
            written.extend(
                export_endscripts(
                    self.project, end_path, self.settings.get("game_root") or None
                )
            )
        except OSError as exc:
            log.error("export failed: %s", exc)
            messagebox.showerror("Export", str(exc), parent=self.root)
            return False
        self.settings["export_dir"] = str(nfsms_path.parent)
        self.settings["export_base"] = self.export_base_var.get()
        self.settings["export_base_auto"] = self.base_name_auto
        self.save_settings()
        names = ", ".join(path.name for path in written)
        self.export_status_var.set(f"Wrote {names}")
        self._set_status(f"Exported {names}.")
        log.info("export wrote %s in %s", names, nfsms_path.parent)
        self.hud.flash(f"Exported {names}", duration=4000)
        self._refresh_validation_and_previews(light=False)
        return True

    def export_interactive(self):
        self.export_files(False)

    def export_quick(self):
        self._sync_all()
        nfsms_path, end_path = self._output_paths()
        try:
            export_nfsms(self.project, nfsms_path)
            end_files = export_endscripts(
                self.project, end_path, self.settings.get("game_root") or None
            )
        except OSError as exc:
            log.error("quick export failed: %s", exc)
            self.hud.flash(f"Export failed: {exc}"[:120], duration=5000)
            self._set_status(f"Export failed: {exc}")
            return
        names = ", ".join(path.name for path in (nfsms_path, *end_files))
        self.hud.flash(f"Exported {names}", duration=5000)
        self._set_status(f"Exported to {nfsms_path.parent}.")
        log.info("quick export wrote %s in %s", names, nfsms_path.parent)

    # ------------------------------------------------------------------ misc

# ------------------------------------------------------------------------
# Source: nfsrc/controllers/sync.py
# ------------------------------------------------------------------------
"""Model-to-view synchronisation and previews."""





class SyncMixin:
    def _populate_all(self):
        self._loading = True
        try:
            self._populate_fields()
            self._populate_advanced()
            self._populate_points()
            self.map_view.fit_view()
            self.map_view.refresh_markers()
            self._refresh_race_summary()
            self._auto_export_base(force=True)
            self._refresh_validation_and_previews(light=True)
        finally:
            self._loading = False

    def _populate_fields(self):
        project = self.project
        data = project.data
        self.event_type_var.set(EVENT_TYPE_LABELS[project.event_type])
        self.region_var.set(REGION_LABELS[project.region])
        for key in STRING_FIELDS:
            if key in self.field_vars:
                self.field_vars[key].set(str(data.get(key, "")))
        for key in INT_FIELDS:
            if key in self.field_vars:
                self.field_vars[key].set(str(int(data.get(key, 0))))
        for key in FLOAT_FIELDS:
            if key in self.field_vars:
                self.field_vars[key].set(fmt_number(float(data.get(key, 0.0))))
        for key in BOOL_FIELDS:
            if key in self.field_vars:
                self.field_vars[key].set(bool(data.get(key, False)))
        tp = data.get("track_position", [0, 0])
        self.field_vars["tp_x"].set(fmt_number(float(tp[0])))
        self.field_vars["tp_y"].set(fmt_number(float(tp[1])))
        self.track_name_auto = self.project.track_name == suggest_track_name(
            self.project.event_type, self.project.region, self.project.event_id
        )

    def _populate_advanced(self):
        if not self.adv_texts:
            return
        self.adv_texts["barriers"].delete("1.0", "end")
        self.adv_texts["barriers"].insert(
            "1.0", ", ".join(str(int(v)) for v in self.project.data.get("barriers", [1]))
        )
        for key, data_key in (
            ("rewards_winner", "rewards_winner"),
            ("rewards_dominate", "rewards_dominate"),
        ):
            self.adv_texts[key].delete("1.0", "end")
            self.adv_texts[key].insert(
                "1.0", ", ".join(str(v) for v in self.project.data.get(data_key, ["0"]))
            )
        self.adv_texts["localization"].delete("1.0", "end")
        lines = []
        for entry in self.project.data.get("localization", []):
            if isinstance(entry, dict):
                lines.append(
                    f"{entry.get('key', '')} | {entry.get('en', '')} | {entry.get('ru', '')}"
                )
            elif isinstance(entry, (list, tuple)):
                lines.append(" | ".join(str(v) for v in entry))
        self.adv_texts["localization"].insert("1.0", "\n".join(lines))

    def _sync_race_fields(self):
        self.sync_errors = []
        data = self.project.data
        data["event_type"] = self._current_event_type()
        data["region"] = self._current_region()
        for key in STRING_FIELDS:
            if key in self.field_vars:
                data[key] = str(self.field_vars[key].get()).strip()
        for key, (minimum, maximum) in INT_FIELDS.items():
            if key not in self.field_vars:
                continue
            current = int(data.get(key, 0))
            value, ok = parse_int_field(str(self.field_vars[key].get()), current)
            if not ok:
                self.sync_errors.append(f"WARN: {key} is not a number; kept {current}.")
                value = current
            if minimum is not None and value < minimum:
                self.sync_errors.append(f"WARN: {key} raised to {minimum}.")
                value = minimum
            if maximum is not None and value > maximum:
                self.sync_errors.append(f"WARN: {key} limited to {maximum}.")
                value = maximum
            data[key] = value
        for key in FLOAT_FIELDS:
            if key not in self.field_vars:
                continue
            current = float(data.get(key, 0.0))
            value, ok = parse_float_field(str(self.field_vars[key].get()), current)
            if not ok:
                self.sync_errors.append(f"WARN: {key} is not a number; kept {current}.")
                value = current
            data[key] = value
        for key in BOOL_FIELDS:
            if key in self.field_vars:
                data[key] = bool(self.field_vars[key].get())
        tx, okx = parse_float_field(
            str(self.field_vars["tp_x"].get()), float(data.get("track_position", [0, 0])[0])
        )
        ty, oky = parse_float_field(
            str(self.field_vars["tp_y"].get()), float(data.get("track_position", [0, 0])[1])
        )
        if not okx or not oky:
            self.sync_errors.append("WARN: TrackPosition is not numeric; kept previous value.")
        data["track_position"] = [tx, ty]
        if str(data.get("track_name", "")).strip() == "":
            self.sync_errors.append("WARN: Track name is empty.")
        if self.base_name_auto:
            self._auto_export_base(force=True)

    def _sync_advanced(self):
        if not self.adv_texts:
            return
        raw = self.adv_texts["barriers"].get("1.0", "end")
        barriers: list[int] = []
        for token in re.split(r"[,\s]+", raw.strip()):
            if not token:
                continue
            try:
                barriers.append(int(float(token)))
            except ValueError:
                self.sync_errors.append(f"WARN: barrier '{token}' is not a number; ignored.")
        self.project.data["barriers"] = barriers or [1]

        for key in ("rewards_winner", "rewards_dominate"):
            raw = self.adv_texts[key].get("1.0", "end")
            items = [item.strip() for item in raw.split(",") if item.strip()]
            self.project.data[key] = items or ["0"]

        localization: list[dict[str, str]] = []
        raw = self.adv_texts["localization"].get("1.0", "end")
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [part.strip() for part in line.split("|")]
            if len(parts) < 2 or not parts[0]:
                self.sync_errors.append(f"WARN: localization line ignored: {line}")
                continue
            localization.append(
                {
                    "key": parts[0],
                    "en": parts[1],
                    "ru": parts[2] if len(parts) > 2 and parts[2] else parts[1],
                }
            )
        self.project.data["localization"] = localization

    def _populate_points(self):
        for item in self.points_tree.get_children():
            self.points_tree.delete(item)
        for kind, index, label, point in self.project.ordered_points():
            extra = ""
            if kind in ("finish", "checkpoint", "bonus"):
                extra = f"D {fmt_number(point.dim_x)}/{fmt_number(point.dim_y)}/{fmt_number(point.dim_z)}"
            if kind == "bonus":
                extra += f"  T {fmt_number(point.time_bonus)}"
            if kind == "reset":
                extra = f"W {fmt_number(point.width)}"
            self.points_tree.insert(
                "",
                "end",
                iid=f"{kind}:{index}",
                values=(
                    label,
                    fmt_number(point.x),
                    fmt_number(point.y),
                    fmt_number(point.z),
                    fmt_number(point.rotation),
                    extra,
                ),
            )
        if self.selected_point:
            iid = f"{self.selected_point[0]}:{self.selected_point[1]}"
            if self.points_tree.exists(iid):
                self.points_tree.selection_set(iid)
            else:
                self.selected_point = None
                self._clear_point_editor()

    def _refresh_race_summary(self):
        counts = self.project.counts()
        summary = (
            f"{self.project.event_id}  |  {EVENT_TYPE_LABELS[self.project.event_type]}  |  "
            f"{self.project.region}\n"
            f"TP {fmt_number(self.project.data.get('track_position', [0, 0])[0])}, "
            f"{fmt_number(self.project.data.get('track_position', [0, 0])[1])}  |  "
            f"S{counts['start']} F{counts['finish']} C{counts['checkpoint']} "
            f"B{counts['bonus']} SC{counts['shortcut']} R{counts['reset']}"
        )
        self.race_summary_var.set(summary)

    # ----------------------------------------------------------- points edit

    def _set_tp_vars_from_project(self):
        tp = self.project.data.get("track_position", [0, 0])
        self._loading = True
        try:
            self.field_vars["tp_x"].set(fmt_number(float(tp[0])))
            self.field_vars["tp_y"].set(fmt_number(float(tp[1])))
        finally:
            self._loading = False

    def _apply_tp_entries(self):
        tx, okx = parse_float_field(
            str(self.field_vars["tp_x"].get()),
            float(self.project.data.get("track_position", [0, 0])[0]),
        )
        ty, oky = parse_float_field(
            str(self.field_vars["tp_y"].get()),
            float(self.project.data.get("track_position", [0, 0])[1]),
        )
        if not okx or not oky:
            self._set_status("TrackPosition must be numeric.")
            return
        self.set_track_position(tx, ty)
        self._set_status(f"TrackPosition set to {tx:.0f}, {ty:.0f}.")

    def _sync_all(self):
        self._sync_race_fields()
        self._sync_advanced()
        return list(self.sync_errors)

    def mark_dirty(self):
        if self._loading:
            return
        if not self.dirty:
            self.dirty = True
            self._update_title()

    # ---------------------------------------------------------------- export

    def _update_title(self):
        name = self.project_path.name if self.project_path else "Untitled"
        star = "*" if self.dirty else ""
        self.root.title(f"{name}{star} - NFS Undercover Race Creator")

    def _auto_export_base_text(self) -> str:
        return safe_name(f"{self.project.event_id}_{self.project.track_name or 'race'}")

    def _auto_export_base(self, force: bool = False):
        if not force and not self.base_name_auto:
            return
        if not hasattr(self, "export_base_var"):
            return
        self._loading = True
        try:
            self.export_base_var.set(self._auto_export_base_text())
        finally:
            self._loading = False
        self.base_name_auto = True

    def _refresh_validation_and_previews(self, light: bool = False):
        if not hasattr(self, "validation_text"):
            return
        self._sync_all()
        messages = self.project.validate() + self.sync_errors
        if messages:
            log.debug("validation produced %d message(s)", len(messages))
        self.validation_text.configure(state="normal")
        self.validation_text.delete("1.0", "end")
        if not messages:
            self.validation_text.insert("end", "No validation problems.", "warn")
        for message in messages:
            tag = "error" if message.startswith("ERROR") else "warn"
            self.validation_text.insert("end", message + "\n", tag)
        self.validation_text.configure(state="disabled")
        if not light:
            _nfsms_path, end_path = self._output_paths()
            self._set_preview_text(self.nfsms_preview, build_nfsms(self.project))
            self._set_preview_text(self.end_preview, build_endscript(self.project))
            self._set_preview_text(
                self.launcher_preview,
                build_launcher_endscript(
                    self.project, end_path.name, self.settings.get("game_root") or None
                ),
            )

    def _set_preview_text(self, widget: tk.Text, content: str):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", content)
        widget.configure(state="disabled")

    def refresh_previews(self):
        self._sync_all()
        self._refresh_validation_and_previews(light=False)
        self._set_status("Preview refreshed.")

    def _output_paths(self) -> tuple[Path, Path]:
        self._sync_all()
        base = safe_name(str(self.export_base_var.get()) or self._auto_export_base_text())
        folder = Path(self.export_dir_var.get() or EXPORTS_DIR)
        return folder / f"{base}.nfsms", folder / f"{base}.end"

    def save_settings(self):
        try:
            self.settings["game_root"] = self.game_root_var.get()
            self.settings["hot_slot"] = int(float(self.hot_slot_var.get()))
            self.settings["export_dir"] = self.export_dir_var.get()
            self.settings["export_base"] = self.export_base_var.get()
            self.settings["export_base_auto"] = self.base_name_auto
            self.settings["hud_visible"] = self.hud.visible
            self.settings["hud_click_through"] = self.hud.click_through
            self.settings["hud_alpha"] = float(self.hud_alpha_var.get())
            self.settings["hud_borderless"] = bool(self.hud_borderless_var.get())
            x, y, _w, _h = self.hud.geometry()
            if x > -3000 and y > -3000:
                self.settings["hud_x"] = x
                self.settings["hud_y"] = y
            SETTINGS_PATH.write_text(
                json.dumps(self.settings, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            log.debug("settings saved to %s", SETTINGS_PATH)
        except Exception as exc:
            log.warning("settings could not be saved: %s", exc)

# ------------------------------------------------------------------------
# Source: nfsrc/controllers/app.py
# ------------------------------------------------------------------------
"""Application controller: composes the mixins and owns window lifetime."""






class RaceCreatorApp(MainViewMixin, TabsMixin, SyncMixin, PointsMixin, CaptureMixin, GameMixin, ProjectMixin):
    def __init__(self, register_hotkeys: bool = True):
        # Attach the in-memory handler first so the Log tab also shows the
        # startup lines (settings load, attach attempt, hotkey registration).
        self.log_handler = MemoryLogHandler()
        self.log_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
        )
        logging.getLogger().addHandler(self.log_handler)
        self._log_version = -1
        self.settings = load_settings()
        log.info(
            "starting Race Creator: game_root=%s hot_slot=%s export_dir=%s",
            self.settings.get("game_root"),
            self.settings.get("hot_slot"),
            self.settings.get("export_dir"),
        )
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
        self.project = RaceProject()
        self.project_path: Path | None = None
        self.dirty = False
        # Set while widgets are filled from the model: the StringVar write
        # traces call mark_dirty(), and without this guard loading a project
        # would immediately mark it as modified.
        self._loading = False
        self._closing = False
        self.undo_stack: list[str] = []
        self.last_hot: HotPoint | None = None
        self.proc: NfsProcess | None = None
        self.capture_in_progress = False
        self.events: queue.Queue = queue.Queue()
        self.hotkeys = HotkeyManager()
        self.hotkey_errors: list[str] = []
        self.track_name_auto = True
        self.base_name_auto = bool(self.settings.get("export_base_auto", True))
        self.sync_errors: list[str] = []
        self._last_attach_try = 0.0
        self.field_vars: dict[str, tk.Variable] = {}
        self.adv_texts: dict[str, tk.Text] = {}
        self.point_editor_vars: dict[str, tk.StringVar] = {}
        self.selected_point: tuple[str, int] | None = None
        self._borderless_hwnd = 0
        self._borderless_warned = False
        self._game_process: subprocess.Popen | None = None

        self.root = tk.Tk()
        self.root.title("NFS Undercover Race Creator")
        # Unhandled exceptions inside Tk callbacks die silently in pythonw;
        # route them to the log and the status bar instead.
        self.root.report_callback_exception = self._report_callback_exception
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        editor = theme.editor
        min_w = int(editor.get("window_min_width", 1060))
        min_h = int(editor.get("window_min_height", 680))
        max_w = int(editor.get("window_max_width", 1320))
        max_h = int(editor.get("window_max_height", 880))
        width = min(max_w, max(min_w, screen_w - 120))
        height = min(max_h, max(min_h, screen_h - 140))
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min_w, min_h)
        self.root.configure(bg=BG)
        self._configure_style()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.hud_borderless_var = tk.BooleanVar(
            value=bool(self.settings.get("hud_borderless", True))
        )

        self._build_menu()
        self._build_toolbar()
        self._build_statusbar()
        self._build_notebook()

        self.hud = HudWindow(self)
        self._populate_all()
        self._bind_dirty_traces()
        self._update_title()
        self._set_status("Drive in free-roam or a race, then capture points with the hotkeys.")
        self.attach_game(announce=False)
        if register_hotkeys:
            self._apply_hotkeys(initial=True)
        self.root.after(500, self._tick)
        self.root.after(60, self._poll_events)
        self.root.after(50, self._poll_hotkeys)
        log.info("UI ready; global hotkeys %s", "enabled" if register_hotkeys else "disabled")

    def _report_callback_exception(self, exc_type, exc_value, exc_tb):
        log.error("unhandled UI callback error", exc_info=(exc_type, exc_value, exc_tb))
        try:
            self._set_status(f"Internal error: {exc_value} (see Help > Open log file)")
        except Exception:
            pass

    # --------------------------------------------------------------- styling

    def _set_status(self, message: str):
        self.status_var.set(message)

    def open_log_file(self):
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            LOG_PATH.touch(exist_ok=True)

            os.startfile(str(LOG_PATH))
        except OSError as exc:
            log.warning("could not open the log file: %s", exc)
            self._set_status(f"Could not open the log file: {exc}")

    def show_help(self):
        window = tk.Toplevel(self.root)
        window.title("Hotkeys and workflow")
        window.geometry("780x640")
        window.configure(bg=BG)
        text = tk.Text(
            window,
            **TEXT_WIDGET_STYLE,
            wrap="word",
            font=("Segoe UI", 10),
            padx=14,
            pady=14,
        )
        text.pack(fill="both", expand=True)
        help_text = (
            "NFS UNDERCOVER RACE CREATOR\n"
            "===========================\n\n"
            "WORKFLOW\n"
            "1. Start NFS Undercover and load a save. Drive the car in free-roam or a race.\n"
            "2. Press Attach in the toolbar. The game must be NFS Undercover v1.0.0.1.\n"
            "3. Capture the start grid, finish line and checkpoints with the hotkeys below.\n"
            "4. Open the Map tab. Left-click the map to set TrackPosition (0..3072),\n"
            "   or use 'Use start position' / 'Use car position'. The marker X/Y\n"
            "   fields are on the Race tab.\n"
            "5. The same view draws the captured points (S, F, CPn, TBCPn, SC, R) and\n"
            "   the road route from Start through CP/TBCP to Finish, with one lime\n"
            "   spur per shortcut point.\n"
            "6. Fill the race settings on the Race tab and export on the Export tab.\n"
            "7. Apply the .nfsms file in VltEd and save the gameplay vaults. Use the\n"
            "   Export tab's 'Check game data' to confirm GLOBAL/gameplay.lzc has\n"
            "   the new Start grid and TrackPosition; an exported script alone is\n"
            "   not read by the game.\n"
            "8. In Binary use 'Import Endscript' and select the *_Launcher.end file.\n"
            "   That VERSN1 launcher points to the main VERSN2 .end, which adds the\n"
            "   localized race names to English_Global.bin / Russian_Global.bin.\n\n"
            "HUD OVER FULLSCREEN\n"
            "Direct3D exclusive fullscreen hides all normal windows, including the\n"
            "HUD. Use Game > Launch game (windowed) (starts nfs.exe -windowed) and\n"
            "keep 'Borderless game window' enabled. The tool then makes the game\n"
            "window borderless and keeps the HUD above it, so the game still fills\n"
            "the screen. If the game is already running fullscreen, close it first.\n\n"
            "DEFAULT HOTKEYS\n"
            + "\n".join(
                f"  {spec:<14} {label}" for _action, label, spec in HOTKEY_ACTIONS
            )
            + "\n\nCAPTURE DETAILS\n"
            "The tool writes the game's own hot-position slot flag and then reads\n"
            "TRACKS\\HotPosition*.hot. Coordinates in that file are already in VltEd\n"
            "space, so X/Y/Z are copied directly. Rotation is the 16-bit game angle\n"
            "converted to degrees (-180..180).\n\n"
            "Slot 5 is used by default. Change it in Setup if you need to preserve it.\n"
            "The game must be focused and in gameplay (not menu/loading).\n\n"
            "ARRAYS AND REGISTRATION\n"
            "The script creates the event, track layout and all child nodes. A new node\n"
            "is not automatically attached to existing Children arrays. If you enable\n"
            "registration in Advanced, enter the CURRENT array size; VltEd will resize\n"
            "to current+1 and put the new node at that index. If the count is wrong,\n"
            "existing entries can be overwritten, which is why registration is off by\n"
            "default.\n\n"
            "FILES\n"
            "  .nfsms           VltEd script (gameplay tree)\n"
            "  .end             Binary Endscript [VERSN2] (language strings)\n"
            "  *_Launcher.end   Binary launcher [VERSN1] that loads the VERSN2 file\n\n"
            "MAP VIEW\n"
            "One unified view draws the map image, the traced road network, the\n"
            "route, the captured points and the TrackPosition marker together.\n"
            "  Left click    set TrackPosition (writes X/Y in the 0..3072 frame)\n"
            "  Right drag    pan the map\n"
            "  Mouse wheel   zoom 1x..16x over fit\n"
            "  Rotate buttons turn the whole view in 90 deg steps; 'Flip Y for\n"
            "  display' mirrors it vertically. Neither changes the stored map.\n"
            "The RouteRoad is a vector line with a fixed on-screen width: an orange\n"
            "center with a fading yellow halo for the main route, lime with a green\n"
            "halo for shortcut spurs. White center segments mark places where the\n"
            "route changes road level (Z); the map has no height data of its own,\n"
            "so Z is interpolated between the captured points that anchor each leg.\n"
            "Only pixels of the exact road color #008ad8 count as roads; anything\n"
            "else is invalid and is neither rendered as a road nor routed on.\n"
            "Directional markers (start, finish, reset) rotate with the point's\n"
            "in-game rotation; all markers keep a fixed 48 px size at any zoom.\n\n"
            "CUSTOMIZING THE UI\n"
            "Two JSON files next to race_creator.py are read at startup; restart the\n"
            "editor after editing them.\n"
            f"  {THEME_PATH.name}\n"
            "      Editor colors and fonts, window size limits, map background,\n"
            "      grid and road color, RouteRoad center/halo colors (route and\n"
            "      shortcut spur), route width (clamped to 16 px), halo steps,\n"
            "      Z-ramp color, point colors and labels, and default HUD colors/\n"
            "      fonts.\n"
            f"  {HUD_LAYOUT_PATH.name}\n"
            "      HUD width, position, opacity, border and row order. Row ids are\n"
            "      title, status, race, counts, action, hint. Set \"visible\": false\n"
            "      to hide a row. fg, bg, font and size are optional; when omitted\n"
            "      they inherit theme.json, and a row without bg inherits the HUD\n"
            "      window background. Set \"use_saved_position\": true to use the\n"
            "      position saved from dragging instead of the x/y values here.\n"
            "      The Setup opacity slider saves hud_alpha_custom in settings.json;\n"
            "      while that is true the slider alpha wins over \"alpha\" here.\n\n"
            "TROUBLESHOOTING / LOG\n"
            f"Every run is written to {LOG_PATH}\n"
            "The Log tab shows the latest 2000 lines from this session; red lines are\n"
            "errors and yellow lines are warnings. Use Help > Open log file for the\n"
            "complete file. Run 'py -3 race_creator.py --diagnostics' for a PASS/FAIL\n"
            "report without opening the GUI; add --verbose for DEBUG detail (every\n"
            "process read and hot-file poll).\n"
        )
        text.insert("1.0", help_text)
        text.configure(state="disabled")

    def show_about(self):
        messagebox.showinfo(
            "About",
            "NFS Undercover Race Creator\n\n"
            "Hotkey capture, point editor, map route preview and .nfsms/.end exporters.\n"
            "Built around assets/TEMPLATE.nfsms and the UC custom-race examples.\n\n"
            f"Log file: {LOG_PATH}",
            parent=self.root,
        )

    def on_close(self):
        if self._closing:
            return
        if self.dirty:
            answer = messagebox.askyesnocancel(
                "Unsaved changes",
                "Save the current project before exiting?",
                parent=self.root,
            )
            if answer is None:
                return
            if answer and not self.save_project():
                return
        self._closing = True
        log.info("closing Race Creator")
        self.save_settings()
        try:
            self.hotkeys.stop()
        except Exception:
            log.warning("hotkey manager did not stop cleanly", exc_info=True)
        if self.proc is not None:
            self.proc.close()
            self.proc = None
        logging.getLogger().removeHandler(self.log_handler)
        self.hud.destroy()
        self.root.destroy()

    # ------------------------------------------------------------- self-test

    def _force_close(self):
        self.dirty = False
        self.on_close()


# ============================================================================
# SECTION 8 - DIAGNOSTICS & ENTRY POINT
# ============================================================================

# ------------------------------------------------------------------------
# Source: nfsrc/diagnostics.py
# ------------------------------------------------------------------------
"""Non-GUI self-check used by ``race_creator.py --diagnostics``."""






def run_diagnostics() -> int:
    """Exercise the non-GUI pipeline and print a PASS/FAIL report.

    This proves the installation works without starting the GUI or writing
    anything into the game folder: it parses a synthetic hot-position file,
    round-trips a project, builds and writes both exporters into a temporary
    folder, re-exports the newest saved project, and reports the live game
    state read-only when nfs.exe is running.
    """

    setup_logging("--verbose" in sys.argv)
    print("NFS Undercover Race Creator - diagnostics")
    print(f"Python {sys.version.split()[0]}  |  log: {LOG_PATH}")
    print("-" * 78)

    failures = 0

    def check(name, func, critical=True):
        nonlocal failures
        try:
            detail = func()
        except Exception as exc:
            if critical:
                failures += 1
                log.exception("diagnostic failed: %s", name)
                print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
            else:
                log.warning("diagnostic warning: %s: %s", name, exc)
                print(f"[WARN] {name}: {type(exc).__name__}: {exc}")
            return False
        print(f"[PASS] {name}: {detail}")
        log.info("diagnostic %s: %s", name, detail)
        return True

    def assets():
        template = ASSETS_DIR / "TEMPLATE.nfsms"
        if not template.is_file():
            raise FileNotFoundError(template)

        details = [f"TEMPLATE.nfsms {template.stat().st_size} bytes"]
        if not ROUTE_MAP_PATH.is_file():
            raise FileNotFoundError(ROUTE_MAP_PATH)
        with Image.open(ROUTE_MAP_PATH) as image:
            size = image.size

            indices = palette_road_indices(image)
        if indices is None and not _has_road_color_pixels():
            raise ValueError(f"{ROUTE_MAP_PATH.name} has no road pixels of {ROAD_RGB}")
        details.append(f"{ROUTE_MAP_PATH.name} {size[0]}x{size[1]}, road #008ad8 ok")
        return ", ".join(details)

    def _has_road_color_pixels() -> bool:
        import numpy as _np

        with Image.open(ROUTE_MAP_PATH) as image:
            rgb = _np.asarray(image.convert("RGB"))
        return bool((_np.abs(rgb.astype(int) - (0, 138, 216)).sum(axis=2) == 0).any())

    def settings_check():
        settings = load_settings()
        root = Path(str(settings.get("game_root", "")))
        found = "nfs.exe found" if (root / "nfs.exe").is_file() else "nfs.exe NOT found"
        tracks = "TRACKS ok" if (root / "TRACKS").is_dir() else "TRACKS missing"
        return (
            f"game_root={root} ({found}, {tracks}), "
            f"hot_slot={settings.get('hot_slot')}, export_dir={settings.get('export_dir')}"
        )

    def parser():
        sample = (
            "HOTPOSITION: 1.5, -2.25, 3.0 Angle = 0x4000 Speed = 42\n"
            "HOTPOSITION: 10, 20, 30 Angle = -270 Speed = 0\n"
        )
        points = parse_hot_text(sample, source="<diagnostic>")
        if len(points) != 2:
            raise AssertionError(f"expected 2 points, parsed {len(points)}")
        if abs(points[0].rotation - 90.0) > 1e-6:
            raise AssertionError(f"hex angle parsed as {points[0].rotation}")
        if abs(points[1].rotation - 90.0) > 1e-6:
            raise AssertionError(f"wrapped degree angle parsed as {points[1].rotation}")
        if points[0].x != 1.5 or points[0].speed != 42.0:
            raise AssertionError("coordinates/speed mismatch")
        return "2 lines, hex angle 0x4000 and -270 deg both normalize to 90"

    def hot_files():
        files = find_hot_files(load_settings().get("game_root", ""))
        if not files:
            return "no .hot files yet (the game writes one after a slot is saved)"
        return "; ".join(f"{p.name}: {len(read_hot_file(p))} slots" for p in files)

    def hotkeys_check():
        for _action, spec in DEFAULT_HOTKEYS.items():
            parse_hotkey(spec)
        return f"{len(DEFAULT_HOTKEYS)} default bindings parse"

    def project_roundtrip():
        project = RaceProject()
        project.data.update(
            {
                "event_id": "E777",
                "event_title": "Diagnostic Race",
                "event_title_ru": "Diagnostic Race",
                "track_position": [1536.0, 1536.0],
            }
        )
        project.data["track_name"] = suggest_track_name(
            project.event_type, project.region, project.event_id
        )
        project.start = Point(x=1, y=2, z=3, rotation=45)
        project.finish = Point(x=4, y=5, z=6, rotation=90)
        project.checkpoints = [Point(x=10 + i, y=20, z=30) for i in range(3)]
        project.bonus_checkpoints = [Point(x=40, y=50, z=60, time_bonus=15)]
        project.reset = Point(x=7, y=8, z=9, width=60)
        notes = project.validate()
        errors = [m for m in notes if m.startswith("ERROR")]
        if errors:
            raise AssertionError("; ".join(errors))
        restored = RaceProject()
        restored.restore(project.snapshot())
        if restored.counts() != project.counts():
            raise AssertionError(f"counts differ: {restored.counts()} != {project.counts()}")
        with tempfile.TemporaryDirectory(prefix="nfsrc-diag-") as tmp:
            path = Path(tmp) / "roundtrip.nfsrace"
            project.save(path)
            again = RaceProject.load(path)
        return (
            f"E777 | {again.counts()['checkpoint']} CP, {again.counts()['bonus']} bonus | "
            f"{len(notes)} validator notes | save/load ok"
        )

    def diag_project():
        project = RaceProject()
        project.data["event_id"] = "E777"
        project.start = Point(x=1, y=2, z=3, rotation=45)
        project.finish = Point(x=4, y=5, z=6, rotation=90)
        project.checkpoints = [Point(x=10, y=20, z=30)]
        return project

    def exporters():
        project = diag_project()
        nfsms = build_nfsms(project)
        end = build_endscript(project)
        launcher = build_launcher_endscript(project, "diag.end", None)
        if "add_node gameplay" not in nfsms or "race_bin_career/E777" not in nfsms:
            raise AssertionError(".nfsms is missing the event node")
        if "[VERSN2]" not in end or "add_or_update_string" not in end:
            raise AssertionError(".end is missing [VERSN2] commands")
        if not launcher.startswith("[VERSN1]") or '"Endscript": "diag.end"' not in launcher:
            raise AssertionError("launcher is malformed")
        return (
            f"nfsms {len(nfsms.splitlines())} lines, end {len(end.splitlines())} lines, "
            "launcher ok"
        )

    def write_exporters():
        project = diag_project()
        with tempfile.TemporaryDirectory(prefix="nfsrc-diag-") as tmp:
            folder = Path(tmp)
            nfsms_path = export_nfsms(project, folder / "diag.nfsms")
            main_path, launcher_path_value = export_endscripts(
                project, folder / "diag.end", None
            )
            written = (nfsms_path, main_path, launcher_path_value)
            for path in written:
                if path.stat().st_size == 0:
                    raise AssertionError(f"{path.name} is empty")
            return ", ".join(f"{p.name} {p.stat().st_size} B" for p in written)

    def real_project():
        projects = list(PROJECTS_DIR.glob("*.nfsrace"))
        if not projects:
            return "no saved projects yet"
        newest = max(projects, key=lambda p: p.stat().st_mtime)
        project = RaceProject.load(newest)
        errors = [m for m in project.validate() if m.startswith("ERROR")]
        with tempfile.TemporaryDirectory(prefix="nfsrc-diag-") as tmp:
            base = safe_name(f"{project.event_id}_{project.track_name or 'race'}")
            nfsms_path = export_nfsms(project, Path(tmp) / f"{base}.nfsms")
            export_endscripts(project, Path(tmp) / f"{base}.end", None)
            # Inspect the files while the temporary folder still exists.
            nfsms_size = nfsms_path.stat().st_size
        if nfsms_size == 0:
            raise AssertionError("re-exported .nfsms is empty")
        return (
            f"{newest.name}: {project.event_id}, "
            f"{project.counts()['total_checkpoints']} checkpoints, "
            f"{len(errors)} errors, re-export ok"
        )

    def live_game():
        try:
            proc = NfsProcess.find()
        except CaptureError as exc:
            return f"skipped ({exc})"
        try:
            gameflow, focused = proc.gameplay_state()
            return (
                f"PID {proc.pid}, base 0x{proc.base:08X}, "
                f"signature={'ok' if proc.signature_ok else 'mismatch'}, "
                f"gameflow={gameflow}, focused={focused}"
            )
        finally:
            proc.close()

    check("assets", assets)
    check("settings", settings_check)
    check("hot-position parser", parser)
    check("game hot files (read-only)", hot_files, critical=False)
    check("hotkey parser", hotkeys_check)
    check("project model + validation", project_roundtrip)
    check("export builders (in memory)", exporters)
    check("export writers (temp folder)", write_exporters)
    check("saved project re-export", real_project)
    check("live game (read-only)", live_game, critical=False)

    print("-" * 78)
    if failures:
        print(f"RESULT: FAIL ({failures} critical check(s) failed)")
        print(f"See {LOG_PATH} for details.")
        log.error("diagnostics finished with %d critical failure(s)", failures)
        return 1
    print("RESULT: PASS")
    print(f"Log: {LOG_PATH}")
    log.info("diagnostics finished: PASS")
    return 0

# ------------------------------------------------------------------------
# Source: nfsrc/main.py
# ------------------------------------------------------------------------

def main() -> None:
    if "--diagnostics" in sys.argv:
        sys.exit(run_diagnostics())
    enable_dpi_awareness()
    verbose = "--verbose" in sys.argv
    setup_logging(verbose)
    self_test = "--self-test" in sys.argv or "--screenshot" in sys.argv
    register_hotkeys = "--no-hotkeys" not in sys.argv and not self_test
    log.info("launching GUI (self_test=%s, hotkeys=%s, verbose=%s)", self_test, register_hotkeys, verbose)
    try:
        app = RaceCreatorApp(register_hotkeys=register_hotkeys)
        if self_test:
            screenshot_path = None
            if "--screenshot" in sys.argv:
                index = sys.argv.index("--screenshot")
                if index + 1 < len(sys.argv):
                    screenshot_path = sys.argv[index + 1]
            if screenshot_path:
                app.root.after(1400, lambda: app._take_screenshot(screenshot_path))
            app.root.after(2200, app._force_close)
        app.root.mainloop()
    except Exception:
        log.exception("fatal error")
        raise
    log.info("GUI closed")


if __name__ == "__main__":
    main()
