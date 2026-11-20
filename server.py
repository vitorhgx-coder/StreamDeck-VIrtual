#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stream Deck do Celular
======================
Servidor local (somente biblioteca padrao do Python, sem pip install) que
transforma o navegador do celular em um painel de controle do PC com Windows:
OBS (via WebSocket), Steam, Epic Games, janelas, sistema e botoes personalizados.

Seguranca: o celular so manda "ids de acao" (ex.: obs.scene). Nenhum comando
bruto vem da rede - comandos personalizados ficam apenas no config.json.
"""
import base64
import hashlib
import hmac
import json
import logging
import logging.handlers
import os
import re
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(APP_DIR, "web")
CONFIG_PATH = os.environ.get("STREAMDECK_CONFIG", os.path.join(APP_DIR, "config.json"))
IS_WIN = os.name == "nt"
WINDOW_TITLE = "Stream Deck do Celular"

LOG = logging.getLogger("streamdeck")


class ActionError(Exception):
    """Erro 'esperado' que e mostrado ao usuario no celular."""


class ObsError(Exception):
    kind = None


class ObsOffline(ObsError):
    pass


class ObsAuthError(ObsError):
    pass


class WSError(Exception):
    pass


# =============================================================================
# Configuracao
# =============================================================================
DEFAULT_CONFIG = {
    "port": 8765,
    "pin": "",
    "secret": "",
    "confirm_dangerous": True,
    "obs": {"host": "127.0.0.1", "port": 4455, "password": "", "path": "", "auto_enable_websocket": True},
    "steam": {"path": ""},
    "epic": {"manifests": "", "launcher_path": "", "libraries": []},
    "chat": {"youtube": "", "twitch": ""},
    "jogos": {"pasta": ""}, "modos": {"discord": "", "droidcam": ""}, "modos_lista": {}, "ajustes": {},
    "custom": [],
}

RAW = {}
CFG = {}
CONFIG_ERROR = None
_CONFIG_MTIME = None
_CONFIG_LOCK = threading.RLock()


def deep_merge(base, over):
    out = {}
    for k, v in base.items():
        out[k] = deep_merge(v, {}) if isinstance(v, dict) else (list(v) if isinstance(v, list) else v)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(first_time=False):
    """Le o config.json. Em recarga, se estiver quebrado, mantem o ultimo bom."""
    global RAW, CFG, CONFIG_ERROR, _CONFIG_MTIME
    with _CONFIG_LOCK:
        try:
            if os.path.exists(CONFIG_PATH):
                with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                    raw = json.load(f)
                if not isinstance(raw, dict):
                    raise ValueError("o arquivo precisa ser um objeto { ... }")
                _CONFIG_MTIME = os.path.getmtime(CONFIG_PATH)
            else:
                raw = {}
                _CONFIG_MTIME = None
            RAW = raw
            CFG = deep_merge(DEFAULT_CONFIG, raw)
            CONFIG_ERROR = None
        except (ValueError, OSError) as e:
            if first_time:
                raise
            CONFIG_ERROR = "config.json com erro: %s (usando a configuracao anterior)" % e
            LOG.warning(CONFIG_ERROR)
            try:
                _CONFIG_MTIME = os.path.getmtime(CONFIG_PATH)
            except OSError:
                pass


def maybe_reload_config():
    """Recarrega o config.json automaticamente quando voce salva o arquivo."""
    try:
        m = os.path.getmtime(CONFIG_PATH)
    except OSError:
        return
    if m != _CONFIG_MTIME:
        old_obs = dict(CFG.get("obs", {}))
        load_config()
        if CFG.get("obs") != old_obs:
            OBS.close()
        _CACHE.clear()
        LOG.info("config.json recarregado")


def save_raw():
    with _CONFIG_LOCK:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(RAW, f, indent=2, ensure_ascii=False)
            f.write("\n")


def ensure_credentials():
    changed = False
    if not str(RAW.get("pin", "")).strip():
        RAW["pin"] = "%06d" % (secrets.randbelow(900000) + 100000)
        changed = True
    if not str(RAW.get("secret", "")).strip():
        RAW["secret"] = secrets.token_hex(24)
        changed = True
    if changed:
        save_raw()
        load_config(first_time=True)


def auth_token():
    key = str(CFG["secret"]).encode("utf-8")
    msg = ("sd1:" + str(CFG["pin"])).encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


# =============================================================================
# Cache simples
# =============================================================================
_CACHE = {}
_CACHE_LOCK = threading.Lock()


def cached(key, ttl, fn):
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _CACHE_LOCK:
        _CACHE[key] = (now, val)
    return val


# =============================================================================
# Processos / URIs
# =============================================================================
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000


def launch_uri(uri):
    """Abre uma URI/arquivo com o programa padrao do Windows (steam://, https://...)."""
    LOG.info("abrindo %s", uri)
    if not IS_WIN:
        raise ActionError("Esta acao so funciona no Windows")
    os.startfile(uri)  # noqa


def spawn_detached(args, cwd=None):
    flags = (DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP) if IS_WIN else 0
    LOG.info("executando %s", args)
    subprocess.Popen(args, cwd=cwd, creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


def run_hidden(args, capture=False, timeout=15, env=None):
    flags = CREATE_NO_WINDOW if IS_WIN else 0
    return subprocess.run(args, creationflags=flags, timeout=timeout,
                          env=dict(os.environ, **env) if env else None,
                          stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL)


def process_running(exe_name):
    if not IS_WIN:
        return False
    try:
        r = run_hidden(["tasklist", "/FI", "IMAGENAME eq " + exe_name, "/NH"], capture=True)
        return exe_name.lower() in r.stdout.decode("cp850", "replace").lower()
    except Exception:
        return False


# =============================================================================
# API do Windows: janelas e teclas (ctypes)
# =============================================================================
if IS_WIN:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
        dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
        dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
    except (OSError, AttributeError):
        dwmapi = None

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.IsZoomed.argtypes = [wintypes.HWND]
    user32.IsZoomed.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
    user32.keybd_event.restype = None
    user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
    user32.MapVirtualKeyW.restype = wintypes.UINT
    user32.LockWorkStation.argtypes = []
    user32.LockWorkStation.restype = wintypes.BOOL
    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

IGNORED_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


def _exe_of(hwnd):
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
    finally:
        kernel32.CloseHandle(h)
    return ""


def list_windows():
    """Janelas abertas visiveis, na ordem do Alt+Tab (mais recente primeiro)."""
    if not IS_WIN:
        return []
    result = []

    def cb(hwnd, lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value.strip()
            if not title or title == WINDOW_TITLE:
                return True
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            if cls.value in IGNORED_CLASSES:
                return True
            if dwmapi is not None:
                cloaked = wintypes.DWORD(0)
                if dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) == 0 \
                        and cloaked.value:
                    return True
            ex = user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
            appwin = bool(ex & 0x00040000)         # WS_EX_APPWINDOW
            if (ex & 0x00000080) and not appwin:   # WS_EX_TOOLWINDOW
                return True
            if user32.GetWindow(hwnd, 4) and not appwin:  # GW_OWNER
                return True
            path = _exe_of(hwnd)
            result.append({"hwnd": int(hwnd), "title": title,
                           "exe": os.path.basename(path).lower(), "path": path})
        except Exception:
            LOG.exception("erro ao ler janela")
        return True

    proc = WNDENUMPROC(cb)
    user32.EnumWindows(proc, 0)
    return result


def find_window(matchers):
    """matchers: lista de funcoes, em ordem de preferencia."""
    wins = list_windows()
    for m in matchers:
        for w in wins:
            try:
                if m(w):
                    return w
            except Exception:
                pass
    return None


def focus_window(hwnd):
    if not IS_WIN:
        raise ActionError("Esta acao so funciona no Windows")
    if not user32.IsWindow(hwnd):
        raise ActionError("Essa janela nao existe mais")
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    if user32.GetForegroundWindow() == hwnd:
        return
    # O Windows as vezes bloqueia: "truque do ALT" + anexar a thread da janela ativa
    _key(0x12, True)
    _key(0x12, False)
    fg = user32.GetForegroundWindow()
    cur = kernel32.GetCurrentThreadId()
    fgt = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = False
    if fgt and fgt != cur:
        attached = bool(user32.AttachThreadInput(cur, fgt, True))
    try:
        user32.BringWindowToTop(hwnd)
        user32.ShowWindow(hwnd, 5)  # SW_SHOW
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(cur, fgt, False)


# ---- Teclas ------------------------------------------------------------------
VK_NAMES = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "windows": 0x5B,
    "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B, "tab": 0x09, "space": 0x20,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "printscreen": 0x2C, "prtsc": 0x2C, "pause": 0x13, "capslock": 0x14, "numlock": 0x90,
    "scrolllock": 0x91, "menu": 0x5D,
    "volumemute": 0xAD, "volumedown": 0xAE, "volumeup": 0xAF,
    "medianext": 0xB0, "mediaprev": 0xB1, "mediastop": 0xB2, "mediaplay": 0xB3,
    "minus": 0xBD, "equals": 0xBB, "plus": 0xBB, "comma": 0xBC, "period": 0xBE,
    "slash": 0xBF, "semicolon": 0xBA, "backslash": 0xDC, "quote": 0xDE, "backtick": 0xC0,
    "lbracket": 0xDB, "rbracket": 0xDD,
}
EXTENDED_VKS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2C, 0x2D, 0x2E, 0x5B, 0x5C,
                0x6F, 0x90, 0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3}


def _vk_for(name):
    n = name.strip().lower()
    if n in VK_NAMES:
        return VK_NAMES[n]
    m = re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", n)
    if m:
        return 0x70 + int(m.group(1)) - 1
    if len(n) == 1 and n.isascii() and n.isalpha():
        return ord(n.upper())
    if len(n) == 1 and n.isdigit():
        return 0x30 + int(n)
    m = re.fullmatch(r"num([0-9])", n)
    if m:
        return 0x60 + int(m.group(1))
    raise ActionError("Tecla desconhecida: '%s'" % name)


def _key(vk, down):
    scan = user32.MapVirtualKeyW(vk, 0) & 0xFF
    flags = 0
    if vk in EXTENDED_VKS:
        flags |= 0x0001  # KEYEVENTF_EXTENDEDKEY
    if not down:
        flags |= 0x0002  # KEYEVENTF_KEYUP
    user32.keybd_event(vk, scan, flags, 0)


def send_keys(combo):
    """Ex.: 'ctrl+shift+m', 'alt+tab', 'win+d', 'f13', 'medianext'."""
    parts = [p for p in re.split(r"\s*\+\s*", str(combo).strip()) if p]
    if not parts:
        raise ActionError("Atalho vazio")
    vks = [_vk_for(p) for p in parts]  # valida tudo antes de apertar qualquer tecla
    if not IS_WIN:
        raise ActionError("Atalhos de teclado so funcionam no Windows")
    for vk in vks:
        _key(vk, True)
        time.sleep(0.015)
    time.sleep(0.03)
    for vk in reversed(vks):
        _key(vk, False)
        time.sleep(0.015)


# =============================================================================
# Cliente WebSocket minimo (so o necessario para o OBS)
# =============================================================================
class WSClient:
    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, host, port, timeout=5.0):
        self.host, self.port, self.timeout = host, int(port), timeout
        self.sock = None
        self.buf = b""

    def connect(self):
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        s.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET / HTTP/1.1\r\n"
               "Host: %s:%d\r\n"
               "Upgrade: websocket\r\n"
               "Connection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n"
               "Sec-WebSocket-Protocol: obswebsocket.json\r\n\r\n") % (self.host, self.port, key)
        try:
            s.sendall(req.encode())
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = s.recv(4096)
                if not chunk:
                    raise WSError("conexao fechada durante o handshake")
                data += chunk
                if len(data) > 65536:
                    raise WSError("resposta de handshake grande demais")
        except (OSError, WSError):
            s.close()
            raise
        head, _, rest = data.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        status = lines[0].split(" ")
        if len(status) < 2 or status[1] != "101":
            s.close()
            raise WSError("handshake recusado: " + lines[0])
        headers = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        expected = base64.b64encode(hashlib.sha1((key + self.GUID).encode()).digest()).decode()
        if headers.get("sec-websocket-accept") != expected:
            s.close()
            raise WSError("handshake invalido (Sec-WebSocket-Accept)")
        self.sock = s
        self.buf = rest

    def _read(self, n):
        while len(self.buf) < n:
            try:
                chunk = self.sock.recv(65536)
            except OSError as e:
                raise WSError(str(e))
            if not chunk:
                raise WSError("conexao encerrada")
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def _send_frame(self, opcode, payload):
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        try:
            self.sock.sendall(bytes(header) + masked)
        except OSError as e:
            raise WSError(str(e))

    def send_text(self, text):
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_text(self):
        msg = b""
        while True:
            b1, b2 = self._read(2)
            fin, op = b1 & 0x80, b1 & 0x0F
            masked, ln = b2 & 0x80, b2 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if masked else None
            payload = self._read(ln)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if op == 0x8:
                raise WSError("conexao fechada pelo servidor")
            if op == 0x9:
                self._send_frame(0xA, payload)
                continue
            if op == 0xA:
                continue
            msg += payload
            if fin:
                return msg.decode("utf-8", "replace")

    def close(self):
        if self.sock:
            try:
                self._send_frame(0x8, b"")
            except Exception:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


# =============================================================================
# OBS WebSocket v5
# =============================================================================
AUTH_MSG = ("Senha do WebSocket do OBS incorreta. No OBS abra Ferramentas > Configuracoes do servidor "
            "WebSocket > Mostrar informacoes de conexao e copie a senha para obs.password no config.json.")


def obs_ws_files():
    """Onde o OBS guarda a config do WebSocket (instalado e portatil)."""
    out = []
    try:
        exe = find_obs_exe()
    except Exception:
        exe = None
    if exe:
        root = os.path.dirname(os.path.dirname(os.path.dirname(exe)))
        if os.path.isfile(os.path.join(root, "portable_mode.txt")):
            out.append(os.path.join(root, "config", "obs-studio", "plugin_config", "obs-websocket", "config.json"))
    appdata = os.environ.get("APPDATA")
    if appdata:
        out.append(os.path.join(appdata, "obs-studio", "plugin_config", "obs-websocket", "config.json"))
    return out


def obs_ws_file():
    """(caminho, dados) da config do WebSocket do proprio OBS, se existir."""
    for p in obs_ws_files():
        try:
            with open(p, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return p, data
        except (OSError, ValueError):
            continue
    return None, {}


def obs_ws_info():
    return cached(("obsws",), 4, obs_ws_file)


def obs_endpoints():
    """(host, porta, senha) para tentar, do mais para o menos provavel.
    Usa o que voce pôs no config.json e, principalmente, o que o proprio OBS diz."""
    raw = RAW.get("obs") if isinstance(RAW.get("obs"), dict) else {}
    host = CFG["obs"].get("host") or "127.0.0.1"
    _, data = obs_ws_info()
    fport = data.get("server_port")
    fpw = (data.get("server_password") or "") if data.get("auth_required", True) else ""
    cands = [
        (raw.get("port") or fport or 4455, raw.get("password") or fpw),
        (fport or 4455, fpw),
        (raw.get("port") or 4455, raw.get("password") or ""),
        (4455, fpw),
    ]
    out = []
    for port, pw in cands:
        try:
            item = (host, int(port), str(pw or ""))
        except (TypeError, ValueError):
            continue
        if item[1] > 0 and item not in out:
            out.append(item)
    return out


def obs_running_now():
    return bool(find_obs_window()) or process_running("obs64.exe")


def obs_running():
    return cached(("obsrun",), 3, obs_running_now)


def obs_offline_error():
    """Erro 'OBS inacessivel' com o motivo mais provavel (e o tipo, p/ a UI oferecer correcao)."""
    _, data = obs_ws_info()
    if not obs_running():
        e = ObsOffline("O OBS esta fechado. Toque em 'Abrir OBS' e espere ele abrir.")
        e.kind = "closed"
    elif data.get("server_enabled") is False:
        e = ObsOffline("O WebSocket do OBS esta DESATIVADO. No OBS: Ferramentas > Configuracoes do servidor "
                       "WebSocket > marque 'Ativar servidor WebSocket' > OK. (Ou use o botao 'Ativar WebSocket' abaixo.)")
        e.kind = "disabled"
    else:
        ports = ", ".join(sorted({str(p) for _, p, _ in obs_endpoints()}))
        e = ObsOffline("Nao consegui conectar ao WebSocket do OBS (portas testadas: %s). Confira em Ferramentas > "
                       "Configuracoes do servidor WebSocket se ele esta ativado e qual a porta." % ports)
        e.kind = "unreachable"
    return e


class ObsClient:
    def __init__(self):
        self.lock = threading.RLock()
        self.ws = None
        self.down_until = 0.0
        self.down_error = None

    def close(self):
        with self.lock:
            if self.ws:
                self.ws.close()
                self.ws = None

    def _fail(self, err):
        self.down_until = time.time() + 2.5
        self.down_error = err

    def _connect_one(self, host, port, pw):
        ws = WSClient(host, port, timeout=4.0)
        ws.connect()
        authed = False
        try:
            hello = json.loads(ws.recv_text())
            if hello.get("op") != 0:
                raise WSError("resposta inesperada do OBS")
            ident = {"rpcVersion": 1, "eventSubscriptions": 0}
            auth = (hello.get("d") or {}).get("authentication")
            if auth:
                if not pw:
                    raise ObsAuthError(AUTH_MSG)
                secret = base64.b64encode(hashlib.sha256((pw + auth["salt"]).encode()).digest()).decode()
                ident["authentication"] = base64.b64encode(
                    hashlib.sha256((secret + auth["challenge"]).encode()).digest()).decode()
                authed = True
            ws.send_text(json.dumps({"op": 1, "d": ident}))
            resp = json.loads(ws.recv_text())
            if resp.get("op") != 2:
                raise WSError("o OBS recusou a conexao")
        except WSError:
            ws.close()
            if authed:
                raise ObsAuthError(AUTH_MSG)
            raise
        except Exception:
            ws.close()
            raise
        self.ws = ws

    def _connect(self):
        auth_err, last = None, None
        for host, port, pw in obs_endpoints():
            try:
                self._connect_one(host, port, pw)
                return
            except ObsAuthError as e:
                auth_err = e
            except (WSError, OSError, ValueError) as e:
                last = e
        if auth_err:
            raise auth_err
        raise WSError(str(last) if last else "sem endereco para conectar")

    def request(self, rtype, data=None, backoff=False):
        with self.lock:
            if backoff and time.time() < self.down_until and self.down_error is not None:
                raise self.down_error
            last = None
            for _attempt in (0, 1):
                fresh = self.ws is None
                try:
                    if fresh:
                        self._connect()
                    rid = secrets.token_hex(6)
                    msg = {"op": 6, "d": {"requestType": rtype, "requestId": rid}}
                    if data:
                        msg["d"]["requestData"] = data
                    self.ws.send_text(json.dumps(msg))
                    deadline = time.time() + 6
                    while True:
                        m = json.loads(self.ws.recv_text())
                        if m.get("op") == 7 and (m.get("d") or {}).get("requestId") == rid:
                            break
                        if time.time() > deadline:
                            raise WSError("timeout")
                    st = m["d"].get("requestStatus") or {}
                    if not st.get("result"):
                        raise ObsError(st.get("comment") or ("erro %s do OBS" % st.get("code")))
                    self.down_until = 0.0
                    self.down_error = None
                    return m["d"].get("responseData") or {}
                except ObsAuthError as e:
                    self._fail(e)
                    raise
                except ObsError:
                    raise
                except (WSError, OSError, ValueError, KeyError) as e:
                    last = e
                    if self.ws:
                        self.ws.close()
                        self.ws = None
                    if fresh:
                        break
            err = obs_offline_error()
            self._fail(err)
            if str(last) != getattr(self, "_last_log", None):   # evita encher o log a cada 4 s
                self._last_log = str(last)
                LOG.info("OBS inacessivel: %s", last)
            raise err


OBS = ObsClient()


def obs_req(rtype, data=None):
    return OBS.request(rtype, data)


def find_obs_exe():
    cands = []
    p = CFG["obs"].get("path")
    if p:
        cands.append(p)
    try:
        w = find_obs_window()
        if w and w.get("path"):
            cands.append(w["path"])
    except Exception:
        pass
    if IS_WIN:
        try:
            import winreg
            for access in (winreg.KEY_READ | winreg.KEY_WOW64_64KEY, winreg.KEY_READ):
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\OBS Studio", 0, access) as k:
                        d = winreg.QueryValueEx(k, "")[0]
                        cands.append(os.path.join(d, "bin", "64bit", "obs64.exe"))
                except OSError:
                    pass
        except ImportError:
            pass
        for env in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(env)
            if base:
                cands.append(os.path.join(base, "obs-studio", "bin", "64bit", "obs64.exe"))
        local = os.environ.get("LOCALAPPDATA")
        if local:
            cands.append(os.path.join(local, "Programs", "obs-studio", "bin", "64bit", "obs64.exe"))
        try:
            root = steam_root()
            for lib in (steam_libraries(root) if root else []):
                cands.append(os.path.join(lib, "steamapps", "common", "OBS Studio", "bin", "64bit", "obs64.exe"))
        except Exception:
            pass
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def _write_ws_enabled(path, data):
    data = dict(data)
    data["server_enabled"] = True
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
    _CACHE.clear()


def a_obs_enable_ws(_):
    """Liga o servidor WebSocket editando o arquivo do OBS (com o OBS fechado) e reabre o OBS."""
    path, data = obs_ws_file()
    manual = "Ative manualmente em Ferramentas > Configuracoes do servidor WebSocket."
    if not path:
        raise ActionError("Nao achei o arquivo de configuracao do WebSocket do OBS. " + manual)
    if "server_enabled" not in data:
        raise ActionError("O arquivo do OBS tem um formato diferente do esperado. " + manual)
    if data.get("server_enabled"):
        return "O WebSocket ja esta ativado no OBS."
    exe = find_obs_exe()
    if obs_running_now():
        w = find_obs_window()
        if w and IS_WIN:
            user32.PostMessageW(w["hwnd"], 0x0010, 0, 0)  # WM_CLOSE
        for _i in range(40):
            time.sleep(0.5)
            if not obs_running_now():
                break
        if obs_running_now():
            raise ActionError("O OBS nao fechou (ele pode estar pedindo algo na tela do PC, ou estar na bandeja). "
                              "Feche-o por completo e toque de novo.")
    try:
        _write_ws_enabled(path, data)
    except OSError as e:
        raise ActionError("Nao consegui salvar a config do OBS (%s). %s" % (e, manual))
    OBS.close()
    OBS.down_until = 0.0
    if exe:
        spawn_detached([exe], cwd=os.path.dirname(exe))
        return "WebSocket ativado. Reabrindo o OBS..."
    return "WebSocket ativado. Abra o OBS."


def find_obs_window():
    def is_obs(w):
        return w["exe"] in ("obs64.exe", "obs32.exe", "obs.exe")
    return find_window([lambda w: is_obs(w) and w["title"].startswith("OBS"), is_obs])


def obs_snapshot():
    sc = OBS.request("GetSceneList", backoff=True)
    st = OBS.request("GetStreamStatus")
    rc = OBS.request("GetRecordStatus")
    scenes = sorted(sc.get("scenes") or [], key=lambda s: -(s.get("sceneIndex") or 0))
    snap = {
        "scenes": [s.get("sceneName") for s in scenes if s.get("sceneName")],
        "current": sc.get("currentProgramSceneName"),
        "streaming": bool(st.get("outputActive")),
        "recording": bool(rc.get("outputActive")),
        "paused": bool(rc.get("outputPaused")),
        "mic_muted": None,
        "desktop_muted": None,
    }
    try:
        sp = OBS.request("GetSpecialInputs")
        for key, names in (("mic_muted", ("mic1", "mic2", "mic3", "mic4")),
                           ("desktop_muted", ("desktop1", "desktop2"))):
            for n in names:
                if sp.get(n):
                    snap[key] = bool(OBS.request("GetInputMute", {"inputName": sp[n]}).get("inputMuted"))
                    break
    except ObsOffline:
        raise
    except ObsError:
        pass
    return snap


def get_status():
    out = {"obs": False}
    try:
        st = OBS.request("GetStreamStatus", backoff=True)
        rc = OBS.request("GetRecordStatus")
        cur = OBS.request("GetCurrentProgramScene")
        out.update(obs=True, streaming=bool(st.get("outputActive")),
                   recording=bool(rc.get("outputActive")), paused=bool(rc.get("outputPaused")),
                   scene=cur.get("sceneName") or cur.get("currentProgramSceneName") or "")
    except ObsError:
        pass
    return out


# ---- Acoes do OBS --------------------------------------------------------------
def a_obs_open(_):
    w = find_obs_window()
    if w:
        focus_window(w["hwnd"])
        return "OBS ja estava aberto - trazido para frente"
    exe = find_obs_exe()
    if not exe:
        raise ActionError("Nao achei o OBS. Defina obs.path no config.json")
    if process_running(os.path.basename(exe)):
        return "O OBS ja esta rodando (talvez minimizado na bandeja)"
    note = ""
    if CFG["obs"].get("auto_enable_websocket", True):
        path, data = obs_ws_file()
        if path and data.get("server_enabled") is False:   # OBS fechado: seguro editar o arquivo
            try:
                _write_ws_enabled(path, data)
                note = " (WebSocket do OBS ativado automaticamente)"
            except OSError as e:
                LOG.warning("nao consegui ativar o WebSocket do OBS: %s", e)
    spawn_detached([exe], cwd=os.path.dirname(exe))  # o OBS precisa iniciar na propria pasta
    return "Abrindo o OBS..." + note


def a_obs_focus(_):
    w = find_obs_window()
    if not w:
        raise ActionError("Janela do OBS nao encontrada (fechada ou na bandeja)")
    focus_window(w["hwnd"])
    return "Janela do OBS"


def a_obs_scene(arg):
    if not isinstance(arg, str) or not arg:
        raise ActionError("Cena invalida")
    obs_req("SetCurrentProgramScene", {"sceneName": arg})
    return "Cena: " + arg


def a_stream_start(_):
    if obs_req("GetStreamStatus").get("outputActive"):
        return "Ja esta ao vivo"
    obs_req("StartStream")
    return "Iniciando transmissao"


def a_stream_stop(_):
    if not obs_req("GetStreamStatus").get("outputActive"):
        return "A transmissao ja estava parada"
    obs_req("StopStream")
    return "Transmissao encerrada"


def a_rec_start(_):
    if obs_req("GetRecordStatus").get("outputActive"):
        return "Ja esta gravando"
    obs_req("StartRecord")
    return "Gravacao iniciada"


def a_rec_pause(_):
    st = obs_req("GetRecordStatus")
    if not st.get("outputActive"):
        raise ActionError("Nao esta gravando")
    if st.get("outputPaused"):
        return "A gravacao ja esta pausada"
    obs_req("PauseRecord")
    return "Gravacao pausada"


def a_rec_resume(_):
    st = obs_req("GetRecordStatus")
    if not st.get("outputActive"):
        raise ActionError("Nao esta gravando")
    if not st.get("outputPaused"):
        return "A gravacao ja esta em andamento"
    obs_req("ResumeRecord")
    return "Gravacao retomada"


def a_rec_stop(_):
    if not obs_req("GetRecordStatus").get("outputActive"):
        return "Nao estava gravando"
    obs_req("StopRecord")
    return "Gravacao encerrada"


def _special_input(kind):
    sp = obs_req("GetSpecialInputs")
    names = ("mic1", "mic2", "mic3", "mic4") if kind == "mic" else ("desktop1", "desktop2")
    for n in names:
        if sp.get(n):
            return sp[n]
    raise ActionError("Nenhum %s configurado no OBS" % ("microfone" if kind == "mic" else "audio do desktop"))


def _toggle_mute(name, label):
    muted = bool(obs_req("ToggleInputMute", {"inputName": name}).get("inputMuted"))
    return "%s %s" % (label, "mutado" if muted else "ativado")


def a_mute_mic(_):
    return _toggle_mute(_special_input("mic"), "Microfone")


def a_mute_desktop(_):
    return _toggle_mute(_special_input("desktop"), "Audio do desktop")


def a_mute_input(arg):
    if not isinstance(arg, str) or not arg:
        raise ActionError("Entrada invalida")
    return _toggle_mute(arg, arg)


def a_vcam(_):
    on = bool(obs_req("ToggleVirtualCam").get("outputActive"))
    return "Camera virtual " + ("ligada" if on else "desligada")


def a_replay_toggle(_):
    on = bool(obs_req("ToggleReplayBuffer").get("outputActive"))
    return "Replay buffer " + ("ligado" if on else "desligado")


def a_replay_save(_):
    try:
        obs_req("SaveReplayBuffer")
    except ObsOffline:
        raise
    except ObsError:
        raise ActionError("O replay buffer precisa estar ligado")
    return "Replay salvo"


# =============================================================================
# Steam
# =============================================================================
STEAM_HIDE_PREFIXES = ("Steamworks Common", "Steam Linux Runtime", "Proton", "Steam Controller",
                       "Steam Client", "SteamVR Media", "Steamworks Shared")
_VDF_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])')


def _vdf_unescape(s):
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), s)


def parse_vdf(text):
    root = {}
    stack = [root]
    key = None
    for m in _VDF_TOKEN.finditer(text):
        s, brace = m.group(1), m.group(2)
        if brace == "{":
            new = {}
            if key is not None:
                stack[-1][key] = new
            stack.append(new)
            key = None
        elif brace == "}":
            if len(stack) > 1:
                stack.pop()
            key = None
        else:
            s = _vdf_unescape(s)
            if key is None:
                key = s
            else:
                stack[-1][key] = s
                key = None
    return root


def steam_root():
    cands = []
    p = CFG["steam"].get("path")
    if p:
        cands.append(p)
    try:
        w = find_window([lambda x: x["exe"] == "steam.exe"]) if IS_WIN else None
        if w and w.get("path"):
            cands.append(os.path.dirname(w["path"]))
    except Exception:
        pass
    if IS_WIN:
        try:
            import winreg
            for hive, sub, val in (
                    (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath")):
                try:
                    with winreg.OpenKey(hive, sub) as k:
                        cands.append(winreg.QueryValueEx(k, val)[0])
                except OSError:
                    pass
        except ImportError:
            pass
        for env in ("ProgramFiles(x86)", "ProgramFiles"):
            base = os.environ.get(env)
            if base:
                cands.append(os.path.join(base, "Steam"))
    for c in cands:
        c = os.path.normpath(c)
        if os.path.isdir(os.path.join(c, "steamapps")):
            return c
    return None


def steam_libraries(root):
    libs = [root]
    vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
    try:
        with open(vdf, "r", encoding="utf-8", errors="replace") as f:
            lf = parse_vdf(f.read()).get("libraryfolders", {})
    except OSError:
        lf = {}
    for k, v in lf.items():
        if isinstance(v, dict) and v.get("path"):
            libs.append(v["path"])
        elif isinstance(v, str) and k.isdigit():  # formato antigo
            libs.append(v)
    seen, out = set(), []
    for p in libs:
        n = os.path.normpath(p)
        if n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


def _scan_steam():
    root = steam_root()
    if not root:
        return None
    games = {}
    for lib in steam_libraries(root):
        d = os.path.join(lib, "steamapps")
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for fn in names:
            if not (fn.startswith("appmanifest_") and fn.endswith(".acf")):
                continue
            try:
                with open(os.path.join(d, fn), "r", encoding="utf-8", errors="replace") as f:
                    st = parse_vdf(f.read()).get("AppState", {})
            except OSError:
                continue
            appid = str(st.get("appid", ""))
            name = st.get("name", "")
            if not appid.isdigit() or not name:
                continue
            if any(name.startswith(p) for p in STEAM_HIDE_PREFIXES):
                continue
            try:
                last = int(st.get("LastPlayed", 0) or 0)
            except ValueError:
                last = 0
            games[appid] = {"appid": appid, "name": name, "last": last}
    return sorted(games.values(), key=lambda g: g["name"].lower())


def steam_games():
    return cached(("steam", CFG["steam"].get("path", "")), 20, _scan_steam)


STEAM_URIS = {
    "library": ("steam://open/games", "Biblioteca da Steam"),
    "store": ("steam://store", "Loja da Steam"),
    "downloads": ("steam://open/downloads", "Downloads da Steam"),
    "friends": ("steam://open/friends", "Amigos da Steam"),
    "bigpicture": ("steam://open/bigpicture", "Big Picture"),
    "settings": ("steam://open/settings", "Configuracoes da Steam"),
    "exit": ("steam://exit", "Fechando a Steam"),
}


def find_steam_window():
    steam_exes = ("steamwebhelper.exe", "steam.exe")
    return find_window([
        lambda w: w["exe"] in steam_exes and w["title"] == "Steam",
        lambda w: w["exe"] == "steam.exe",
        lambda w: w["exe"] in steam_exes and w["title"].startswith("Steam"),
    ])


def a_steam_open(_):
    launch_uri("steam://open/main")
    return "Abrindo a Steam..."


def a_steam_focus(_):
    w = find_steam_window()
    if w:
        focus_window(w["hwnd"])
        return "Janela da Steam"
    launch_uri("steam://open/main")
    return "Steam sem janela - abrindo..."


def a_steam_uri(arg):
    if arg not in STEAM_URIS:
        raise ActionError("Acao da Steam desconhecida")
    uri, msg = STEAM_URIS[arg]
    launch_uri(uri)
    return msg


def a_steam_launch(arg):
    if not isinstance(arg, str) or not re.fullmatch(r"\d{1,10}", arg):
        raise ActionError("Jogo invalido")
    name = arg
    for g in steam_games() or []:
        if g["appid"] == arg:
            name = g["name"]
            break
    launch_uri("steam://rungameid/" + arg)
    return "Abrindo " + name


# =============================================================================
# Epic Games
# =============================================================================
EPIC_URI_RE = re.compile(r"com\.epicgames\.launcher://apps/([^?\s\"']+)", re.I)
EPIC_SKIP_NS = ("ue",)


def _epic_root():
    return os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"), "Epic")


def _walk_limited(root, depth):
    for base, subs, files in os.walk(root):
        if base[len(root):].count(os.sep) >= depth:
            subs[:] = []
        yield base, subs, files


def _unique_existing(paths, test):
    out, seen = [], set()
    for p in paths:
        n = os.path.normpath(p)
        if n.lower() not in seen and test(n):
            seen.add(n.lower())
            out.append(n)
    return out


def epic_manifest_dirs():
    dirs = []
    if CFG["epic"].get("manifests"):
        dirs.append(CFG["epic"]["manifests"])
    root = _epic_root()
    dirs.append(os.path.join(root, "EpicGamesLauncher", "Data", "Manifests"))
    if IS_WIN:
        try:
            import winreg
            for flag in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Epic Games\EpicGamesLauncher",
                                        0, winreg.KEY_READ | flag) as k:
                        dirs.append(os.path.join(winreg.QueryValueEx(k, "AppDataPath")[0], "Manifests"))
                except OSError:
                    pass
        except ImportError:
            pass
    for base, _subs, _files in _walk_limited(root, 4):   # versoes novas/beta podem mudar a pasta
        if os.path.basename(base).lower() == "manifests":
            dirs.append(base)
    return _unique_existing(dirs, os.path.isdir)


def epic_dat_files():
    root = _epic_root()
    files = [os.path.join(root, "UnrealEngineLauncher", "LauncherInstalled.dat")]
    for base, _subs, names in _walk_limited(root, 4):
        files += [os.path.join(base, n) for n in names if n.lower() == "launcherinstalled.dat"]
    return _unique_existing(files, os.path.isfile)


def epic_shortcut_dirs():
    e = os.environ

    def j(var, *rest):
        return os.path.join(e[var], *rest) if e.get(var) else ""

    cands = [j("USERPROFILE", "Desktop"), j("OneDrive", "Desktop"), j("PUBLIC", "Desktop"),
             j("APPDATA", "Microsoft", "Windows", "Start Menu", "Programs"),
             j("PROGRAMDATA", "Microsoft", "Windows", "Start Menu", "Programs")]
    return _unique_existing([c for c in cands if c], os.path.isdir)


def epic_library_dirs():
    """Pastas onde a Epic instala jogos (cada jogo tem uma subpasta com .egstore)."""
    cands = [str(x) for x in (CFG["epic"].get("libraries") or []) if x]
    rels = ("Epic Games", r"Program Files\Epic Games", r"Program Files (x86)\Epic Games",
            r"Games\Epic Games", r"Games\Epic", "EpicGames", r"Jogos\Epic Games")
    if IS_WIN:
        import string
        for letter in string.ascii_uppercase:
            root = letter + ":\\"
            if os.path.isdir(root):
                cands += [os.path.join(root, r) for r in rels]
    for env in ("ProgramFiles", "ProgramFiles(x86)"):
        if os.environ.get(env):
            cands.append(os.path.join(os.environ[env], "Epic Games"))
    return _unique_existing(cands, os.path.isdir)


def _scan_epic_sources(full=False):
    """Fontes, da mais confiavel p/ a menos: manifests (.item) > LauncherInstalled.dat > atalhos .url.
    As duas ultimas so entram se a anterior nao achou nada (ou full=True, usado no diagnostico)."""
    games = {}
    info = {"manifests": [], "dats": [], "libs": [], "shortcut_dirs": [], "shortcuts": 0}
    for d in epic_manifest_dirs():
        n = 0
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for fn in names:
            if not fn.lower().endswith(".item"):
                continue
            try:
                with open(os.path.join(d, fn), "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            name, app = data.get("DisplayName"), data.get("AppName")
            ns, item = data.get("CatalogNamespace"), data.get("CatalogItemId")
            if not (name and app and ns and item):
                continue
            main = data.get("MainGameAppName")
            if (main and main != app) or data.get("bIsIncompleteInstall") \
                    or str(app).startswith("UE_") or ns in EPIC_SKIP_NS:
                continue
            games[app] = {"app": app, "name": name, "ns": ns, "item": item}
            n += 1
        info["manifests"].append((d, n))
    if full or not games:
        for path in epic_dat_files():
            try:
                with open(path, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            n = 0
            for e in (data.get("InstallationList") or []) if isinstance(data, dict) else []:
                app = e.get("AppName") or e.get("ArtifactId")
                ns, item = e.get("NamespaceId"), e.get("ItemId")
                if not (app and ns and item) or app in games or str(app).startswith("UE_") or ns in EPIC_SKIP_NS:
                    continue
                loc = str(e.get("InstallLocation") or "").rstrip("\\/")
                games[app] = {"app": app, "name": os.path.basename(loc) or app, "ns": ns, "item": item}
                n += 1
            info["dats"].append((path, n))
    if full or not games:
        for lib in epic_library_dirs():
            n = 0
            try:
                entries = os.listdir(lib)
            except OSError:
                continue
            for entry in entries:
                egs = os.path.join(lib, entry, ".egstore")
                try:
                    files = os.listdir(egs)
                except OSError:
                    continue
                for fn in files:
                    if not fn.lower().endswith(".mancpn"):
                        continue
                    try:
                        with open(os.path.join(egs, fn), "r", encoding="utf-8-sig") as f:
                            d = json.load(f)
                    except (OSError, ValueError):
                        continue
                    app, ns, item = d.get("AppName"), d.get("CatalogNamespace"), d.get("CatalogItemId")
                    if not (app and ns and item) or app in games or str(app).startswith("UE_") or ns in EPIC_SKIP_NS:
                        continue
                    games[app] = {"app": app, "name": entry, "ns": ns, "item": item}
                    n += 1
            info["libs"].append((lib, n))
    if full or not games:
        for d in epic_shortcut_dirs():
            info["shortcut_dirs"].append(d)
            for base, _subs, files in _walk_limited(d, 3):
                for fn in files:
                    if not fn.lower().endswith(".url"):
                        continue
                    try:
                        with open(os.path.join(base, fn), "r", encoding="utf-8-sig", errors="replace") as f:
                            text = f.read(4096)
                    except OSError:
                        continue
                    m = EPIC_URI_RE.search(text)
                    parts = urllib.parse.unquote(m.group(1)).split(":") if m else []
                    if len(parts) != 3 or parts[2] in games:
                        continue
                    games[parts[2]] = {"app": parts[2], "name": os.path.splitext(fn)[0],
                                       "ns": parts[0], "item": parts[1]}
                    info["shortcuts"] += 1
    return sorted(games.values(), key=lambda g: g["name"].lower()), info


def _epic_scan():
    return cached(("epic", CFG["epic"].get("manifests", "")), 20, _scan_epic_sources)


def epic_games():
    return _epic_scan()[0]


def find_epic_window():
    skip = ("webhelper", "crash", "online", "helper")
    return find_window([
        lambda w: w["exe"].startswith("epicgameslauncher") and "epic" in w["title"].lower(),
        lambda w: w["exe"].startswith("epicgameslauncher"),
        lambda w: w["title"].lower().startswith("epic games launcher"),
        lambda w: "epic" in w["exe"] and not any(x in w["exe"] for x in skip),
    ])


def a_epic_open(_):
    w = find_epic_window()
    if w:
        focus_window(w["hwnd"])
        return "Epic ja estava aberta - trazida para frente"
    exe = CFG["epic"].get("launcher_path")
    if exe and os.path.isfile(exe):
        spawn_detached([exe], cwd=os.path.dirname(exe))
    else:
        launch_uri("com.epicgames.launcher://")
    return "Abrindo a Epic Games..."


def a_epic_focus(_):
    w = find_epic_window()
    if w:
        focus_window(w["hwnd"])
        return "Janela da Epic"
    return a_epic_open(None)


def a_epic_store(_):
    launch_uri("com.epicgames.launcher://store")
    return "Loja da Epic"


def a_epic_exit(_):
    names = {"EpicGamesLauncher.exe", "EpicWebHelper.exe"}
    w = find_epic_window()
    if w and w.get("path"):
        names.add(os.path.basename(w["path"]))
    for n in names:
        run_hidden(["taskkill", "/IM", n, "/F"])
    return "Epic Games fechada"


def a_epic_launch(arg):
    for g in epic_games() or []:
        if g["app"] == arg:
            q = lambda s: urllib.parse.quote(s, safe="")
            launch_uri("com.epicgames.launcher://apps/%s%%3A%s%%3A%s?action=launch&silent=true"
                       % (q(g["ns"]), q(g["item"]), q(g["app"])))
            return "Abrindo " + g["name"]
    raise ActionError("Jogo da Epic nao encontrado")


# =============================================================================
# Janelas e sistema
# =============================================================================
def a_win_focus(arg):
    try:
        hwnd = int(arg)
    except (TypeError, ValueError):
        raise ActionError("Janela invalida")
    w = next((x for x in list_windows() if x["hwnd"] == hwnd), None)
    if not w:
        raise ActionError("Essa janela nao existe mais")
    focus_window(hwnd)
    return w["title"][:40]


def _foreground():
    if not IS_WIN:
        raise ActionError("Esta acao so funciona no Windows")
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        raise ActionError("Nenhuma janela ativa")
    return hwnd


def a_win_minimize(_):
    user32.ShowWindow(_foreground(), 6)  # SW_MINIMIZE
    return "Janela minimizada"


def a_win_maximize(_):
    h = _foreground()
    if user32.IsZoomed(h):
        user32.ShowWindow(h, 9)  # SW_RESTORE
        return "Janela restaurada"
    user32.ShowWindow(h, 3)  # SW_MAXIMIZE
    return "Janela maximizada"


def a_win_close(_):
    user32.PostMessageW(_foreground(), 0x0010, 0, 0)  # WM_CLOSE
    return "Fechando janela"


def keys_action(combo, msg):
    def fn(_):
        send_keys(combo)
        return msg
    return fn


def a_lock(_):
    if not IS_WIN:
        raise ActionError("Esta acao so funciona no Windows")
    user32.LockWorkStation()
    return "PC bloqueado"


def a_settings(_):
    launch_uri("ms-settings:")
    return "Configuracoes do Windows"


def a_shutdown(_):
    run_hidden(["shutdown", "/s", "/t", "10"])
    return "Desligando em 10 segundos (use 'Cancelar' para abortar)"


def a_restart(_):
    run_hidden(["shutdown", "/r", "/t", "10"])
    return "Reiniciando em 10 segundos (use 'Cancelar' para abortar)"


def a_cancel_shutdown(_):
    run_hidden(["shutdown", "/a"])
    return "Desligamento cancelado"


# =============================================================================
# Botoes personalizados (config.json)
# =============================================================================
def custom_node(path):
    items = CFG.get("custom") or []
    node = None
    try:
        for idx in str(path).split("/"):
            node = items[int(idx)]
            items = node.get("items") or []
    except (ValueError, IndexError, AttributeError, TypeError):
        raise ActionError("Botao personalizado nao existe mais (config.json mudou?)")
    return node


def custom_type(node):
    t = str(node.get("type") or "").lower()
    if not t:
        for k, v in (("steps", "multi"), ("keys", "keys"), ("cmd", "cmd"), ("exe", "focus"), ("target", "open"),
                     ("url", "open"), ("scene", "obs_scene"), ("request", "obs"), ("wait", "wait")):
            if k in node:
                return v
    return t


def exec_custom(node, depth=0):
    if depth > 5:
        raise ActionError("Macro com niveis demais")
    if not isinstance(node, dict):
        raise ActionError("Botao invalido no config.json")
    t = custom_type(node)
    if t == "focus":
        exe = str(node.get("exe") or "").lower()
        title = str(node.get("title") or "").lower()
        w = find_window([lambda x: x["exe"] == exe and (not title or title in x["title"].lower())])
        if w:
            focus_window(w["hwnd"])
            return "Janela: " + w["title"][:40]
        launch = node.get("launch")
        if launch:
            launch_uri(launch)
            return "Abrindo " + str(node.get("label") or exe)
        raise ActionError("%s nao esta aberto" % str(node.get("label") or exe))
    if t == "wait":
        time.sleep(max(0.0, min(float(node.get("wait", 0)), 30.0)))
        return ""
    if t == "kill":
        exe = str(node.get("exe") or "").lower()
        if not exe or exe in PROTECTED_EXES:
            raise ActionError("Programa invalido")
        if not IS_WIN:
            raise ActionError("Esta acao so funciona no Windows")
        if not process_running(exe):
            raise ActionError("%s nao esta aberto" % exe)
        run_hidden(["taskkill", "/IM", exe, "/T", "/F"])
        return "Encerrado: " + exe
    if t == "appkey":
        app = str(node.get("app") or "")
        if app == "obs":
            return a_obs_open(None)
        if app in APP_SPECS:
            return open_or_focus(app)
        raise ActionError("Programa desconhecido")
    if t in ("open", "url", "app"):
        target = node.get("target") or node.get("url")
        if not target:
            raise ActionError("Falta 'target' no botao")
        args = node.get("args")
        if args and os.path.isfile(target):
            spawn_detached('"%s" %s' % (target, args), cwd=node.get("cwd") or os.path.dirname(target))
        else:
            launch_uri(target)
        return "Abrindo " + str(node.get("label") or target)
    if t == "keys":
        send_keys(node.get("keys", ""))
        return "Atalho enviado: " + str(node.get("keys"))
    if t == "cmd":
        cmd = node.get("cmd")
        if not cmd:
            raise ActionError("Falta 'cmd' no botao")
        flags = CREATE_NO_WINDOW if IS_WIN else 0
        subprocess.Popen(cmd, shell=True, creationflags=flags, cwd=node.get("cwd") or None,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return "Comando executado"
    if t == "obs_scene":
        return a_obs_scene(node.get("scene"))
    if t == "obs":
        obs_req(str(node.get("request", "")), node.get("data"))
        return "OBS: " + str(node.get("request"))
    if t == "multi":
        for step in node.get("steps") or []:
            exec_custom(step, depth + 1)
        return "Macro executada: " + str(node.get("label") or "")
    raise ActionError("Tipo de botao desconhecido: '%s'" % t)


def a_custom(arg):
    return exec_custom(custom_node(arg)) or "OK"


# =============================================================================
# Registro de acoes
# =============================================================================
ACTIONS = {
    "obs.open": a_obs_open, "obs.focus": a_obs_focus, "obs.scene": a_obs_scene,
    "obs.stream_start": a_stream_start, "obs.stream_stop": a_stream_stop,
    "obs.rec_start": a_rec_start, "obs.rec_pause": a_rec_pause,
    "obs.rec_resume": a_rec_resume, "obs.rec_stop": a_rec_stop,
    "obs.mute_mic": a_mute_mic, "obs.mute_desktop": a_mute_desktop, "obs.mute": a_mute_input,
    "obs.vcam": a_vcam, "obs.replay_toggle": a_replay_toggle, "obs.replay_save": a_replay_save,
    "steam.open": a_steam_open, "steam.focus": a_steam_focus, "steam.uri": a_steam_uri,
    "steam.launch": a_steam_launch,
    "epic.open": a_epic_open, "epic.focus": a_epic_focus, "epic.store": a_epic_store,
    "epic.exit": a_epic_exit, "epic.launch": a_epic_launch,
    "win.focus": a_win_focus, "win.minimize": a_win_minimize, "win.maximize": a_win_maximize,
    "win.close": a_win_close,
    "win.alttab": keys_action("alt+tab", "Alt+Tab"),
    "win.taskview": keys_action("win+tab", "Visao de tarefas"),
    "win.desktop": keys_action("win+d", "Mostrar area de trabalho"),
    "win.snap_left": keys_action("win+left", "Janela para a esquerda"),
    "win.snap_right": keys_action("win+right", "Janela para a direita"),
    "win.altf4": keys_action("alt+f4", "Alt+F4 enviado"),
    "br.close_tab": keys_action("ctrl+w", "Aba fechada"),
    "br.new_tab": keys_action("ctrl+t", "Nova aba"),
    "br.reopen_tab": keys_action("ctrl+shift+t", "Aba reaberta"),
    "br.next_tab": keys_action("ctrl+tab", "Proxima aba"),
    "br.prev_tab": keys_action("ctrl+shift+tab", "Aba anterior"),
    "br.back": keys_action("alt+left", "Voltar"),
    "br.forward": keys_action("alt+right", "Avancar"),
    "br.reload": keys_action("f5", "Pagina atualizada"),
    "br.fullscreen": keys_action("f11", "Tela cheia (F11)"),
    "br.address": keys_action("ctrl+l", "Barra de endereco"),
    "br.new_window": keys_action("ctrl+n", "Nova janela"),
    "sys.vol_up": keys_action("volumeup", "Volume +"),
    "sys.vol_down": keys_action("volumedown", "Volume -"),
    "sys.mute": keys_action("volumemute", "Mudo alternado"),
    "sys.play": keys_action("mediaplay", "Play/Pause"),
    "sys.next": keys_action("medianext", "Proxima faixa"),
    "sys.prev": keys_action("mediaprev", "Faixa anterior"),
    "sys.explorer": keys_action("win+e", "Explorador de Arquivos"),
    "sys.taskmgr": keys_action("ctrl+shift+esc", "Gerenciador de Tarefas"),
    "sys.screenshot": keys_action("win+printscreen", "Captura salva em Imagens > Capturas de tela"),
    "sys.lock": a_lock, "sys.settings": a_settings,
    "sys.shutdown": a_shutdown, "sys.restart": a_restart, "sys.cancel_shutdown": a_cancel_shutdown,
    "obs.enable_ws": a_obs_enable_ws,
    "lnk.open": lambda arg: a_lnk_open(arg),
    "custom.run": a_custom,
}


# =============================================================================
# Icones: logos de marcas e icones de interface ficam embutidos no painel (web/icons.js);
# icones reais de programas/atalhos sao extraidos aqui (PowerShell) e guardados em icon_cache/.
# =============================================================================
ICON_SRC = {}   # id -> caminho de arquivo. O celular nunca envia caminhos, so ids que o servidor criou.
ICON_CACHE_DIR = os.path.join(APP_DIR, "icon_cache")
ICON_PS = os.path.join(APP_DIR, "icon_extract.ps1")
ICON_SEM = threading.Semaphore(2)
ICON_LOCKS = {}
ICON_FAIL = {}
IMG_MIME = {".png": "image/png", ".ico": "image/x-icon", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".webp": "image/webp"}


def icon_url(path):
    iid = hashlib.sha1(os.path.normcase(os.path.abspath(path)).encode("utf-8", "replace")).hexdigest()[:16]
    ICON_SRC[iid] = path
    return "/api/icon?id=" + iid


def extract_icon(src, out):
    """Chama o PowerShell para salvar o icone real de src em out (PNG). Retorna (ok, mensagem)."""
    if not IS_WIN:
        return False, "so funciona no Windows"
    if not os.path.isfile(ICON_PS):
        return False, "icon_extract.ps1 nao encontrado"
    env = dict(os.environ, SD_SRC=src, SD_OUT=out, SD_SIZE="128")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-File", ICON_PS], env=env, creationflags=CREATE_NO_WINDOW, timeout=40,
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    msg = (r.stdout + r.stderr).decode("utf-8", "replace").strip()[:300]
    ok = r.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 0
    if not ok:
        LOG.info("icone nao extraido (%s): %s", src, msg)
    return ok, msg


def icon_bytes(iid):
    """(bytes, mime) ou None."""
    path = ICON_SRC.get(iid)
    if not path or not os.path.isfile(path):
        return None
    ext = os.path.splitext(path)[1].lower()
    if ext in IMG_MIME:
        try:
            with open(path, "rb") as f:
                return f.read(), IMG_MIME[ext]
        except OSError:
            return None
    out = os.path.join(ICON_CACHE_DIR, iid + ".png")

    def cached_png():
        try:
            if os.path.isfile(out) and os.path.getmtime(out) >= os.path.getmtime(path):
                with open(out, "rb") as f:
                    return f.read(), "image/png"
        except OSError:
            pass
        return None

    hit = cached_png()
    if hit or not IS_WIN:
        return hit
    if time.time() - ICON_FAIL.get(iid, 0) < 600:
        return None
    with _CACHE_LOCK:
        lock = ICON_LOCKS.setdefault(iid, threading.Lock())
    with lock:
        hit = cached_png()
        if hit:
            return hit
        try:
            os.makedirs(ICON_CACHE_DIR, exist_ok=True)
        except OSError:
            return None
        with ICON_SEM:
            ok, _msg = extract_icon(path, out)
        if ok:
            return cached_png()
        ICON_FAIL[iid] = time.time()
        return None


KNOWN_SITES = {
    "youtube.com": "youtube", "youtu.be": "youtube", "whatsapp.com": "whatsapp", "twitch.tv": "twitch",
    "netflix.com": "netflix", "spotify.com": "spotify", "instagram.com": "instagram", "x.com": "x",
    "twitter.com": "x", "github.com": "github", "reddit.com": "reddit", "discord.com": "discord",
    "telegram.org": "telegram", "mail.google.com": "gmail", "drive.google.com": "googledrive",
    "facebook.com": "facebook", "tiktok.com": "tiktok", "notion.so": "notion", "kick.com": "kick",
    "soundcloud.com": "soundcloud", "pinterest.com": "pinterest", "linkedin.com": "linkedin",
    "slack.com": "slack", "meet.google.com": "googlemeet", "maps.google.com": "googlemaps",
    "photos.google.com": "googlephotos", "calendar.google.com": "googlecalendar", "claude.ai": "claude",
    "wikipedia.org": "wikipedia", "steampowered.com": "steam", "epicgames.com": "epicgames",
    "bsky.app": "bluesky", "threads.net": "threads", "zoom.us": "zoom", "google.com": "google",
}


def site_icon(url):
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    for dom, brand in KNOWN_SITES.items():
        if host == dom or host.endswith("." + dom):
            return "brand:" + brand
    return "fav:" + host if host else "ui:globe"


NODE_ICONS = {"keys": "ui:keyboard", "cmd": "ui:terminal", "obs_scene": "ui:clapperboard", "obs": "ui:clapperboard",
              "multi": "ui:zap", "focus": "ui:app-window", "wait": "ui:clock", "kill": "ui:power",
              "appkey": "ui:rocket"}


def item_icon(node):
    """Icone de um botao seu. Emoji antigo e ignorado: usa logo da marca, favicon ou icone real do programa."""
    logo = str(node.get("logo") or "").lower()
    if re.fullmatch(r"[a-z0-9]{1,40}", logo):
        return "brand:" + logo
    ic = str(node.get("icon") or "")
    if re.match(r"(ui|brand):[a-z0-9-]{1,40}$", ic):
        return ic
    if isinstance(node.get("items"), list):
        return "ui:folder"
    img = node.get("img")
    if isinstance(img, str) and img and os.path.isfile(img):
        return "img:" + icon_url(img)
    t = custom_type(node)
    if t in ("open", "url", "app"):
        target = str(node.get("target") or node.get("url") or "")
        if target.lower().startswith(("http://", "https://")):
            return site_icon(target)
        if os.path.isfile(target):
            return "img:" + icon_url(target)
        return "ui:app-window"
    return NODE_ICONS.get(t, "ui:zap")


# =============================================================================
# Pastas (o que aparece na tela do celular)
# =============================================================================
def btn(label, icon, action, arg=None, **kw):
    d = {"type": "button", "label": label, "icon": icon, "action": action}
    if arg is not None:
        d["arg"] = arg
    d.update(kw)
    return d


def fld(label, icon, path, **kw):
    d = {"type": "folder", "label": label, "icon": icon, "path": path}
    d.update(kw)
    return d


def hdr(label):
    return {"type": "header", "label": label}


def risky(text):
    return {"tone": "danger", "confirm": text if CFG.get("confirm_dangerous", True) else None}


def steam_cover_urls(appid):
    return ["https://cdn.cloudflare.steamstatic.com/steam/apps/%s/header.jpg" % appid,
            "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/%s/header.jpg" % appid]


def steam_item(g):
    return btn(g["name"], "ui:gamepad-2", "steam.launch", g["appid"], kind="game", imgs=steam_cover_urls(g["appid"]))


def epic_item(g):
    return btn(g["name"], "ui:gamepad-2", "epic.launch", g["app"], kind="game")


def custom_item(node, idxpath):
    label = str(node.get("label", "?"))
    p = "/".join(str(i) for i in idxpath)
    if isinstance(node.get("items"), list):
        it = fld(label, item_icon(node), "custom/" + p, accent="custom")
    else:
        it = btn(label, item_icon(node), "custom.run", p, accent="custom")
        conf = node.get("confirm")
        if conf:
            it["confirm"] = conf if isinstance(conf, str) else "Confirmar: %s?" % label
            it["tone"] = "danger"
        if node.get("tone"):
            it["tone"] = str(node["tone"])
    it["cid"] = p          # id do botao no config.json (usado pelo modo editar)
    return it


BUILTIN_FOLDERS = ("obs", "steam", "epic", "jogos", "modes", "browser", "windows", "system")


def _custom_home(node):
    """Em qual pasta padrao o botao personalizado aparece ('' = tela inicial)."""
    p = str(node.get("pasta") or node.get("folder") or "").strip().lower()
    return p if p in BUILTIN_FOLDERS else ""


def custom_for(home):
    out = []
    for i, node in enumerate(CFG.get("custom") or []):
        if isinstance(node, dict) and _custom_home(node) == home:
            out.append(custom_item(node, [i]))
    return out


def window_items():
    items = []
    for w in list_windows():
        title = w["title"] if len(w["title"]) <= 48 else w["title"][:47] + "…"
        icon = "img:" + icon_url(w["path"]) if w.get("path") else "ui:app-window"
        h = str(w["hwnd"])
        menu = [
            {"label": "Trazer para frente", "icon": "ui:monitor-play", "action": "win.focus", "arg": h},
            {"label": "Minimizar", "icon": "ui:minus", "action": "win.minimize_w", "arg": h},
            {"label": "Fechar janela", "icon": "ui:circle-x", "action": "win.close_w", "arg": h},
            {"label": "Encerrar programa (completo)", "icon": "ui:power", "action": "win.kill_w", "arg": h,
             "tone": "danger", "confirm": "Encerrar %s por completo? Trabalho nao salvo pode ser perdido." % w["exe"]},
        ]
        items.append(btn(title, icon, "win.focus", h, sub=w["exe"].replace(".exe", ""), kind="window", menu=menu))
    return items


def desktop_item():
    return btn("Area de trabalho", "ui:monitor", "win.desktop", sub="mostrar tudo", kind="window", pin=True)


def windows_section():
    """Itens da secao 'Janelas abertas': Area de trabalho fixa na esquerda + janelas."""
    wins = window_items()
    items = [hdr("Janelas abertas (%d) - segure para opcoes" % len(wins)), desktop_item()]
    return items + wins


def folder_root():
    items = [
        fld("OBS", "brand:obsstudio", "obs", accent="obs"),
        fld("Steam", "brand:steam", "steam", accent="steam"),
        fld("Epic Games", "brand:epicgames", "epic", accent="epic"),
        fld("Jogos", "ui:gamepad-2", "jogos", accent="jogos"),
        fld("Modos", "ui:sparkles", "modes", accent="modes"),
        fld("Navegador", "ui:globe", "browser", accent="browser"),
        fld("Janelas", "ui:app-window", "windows", accent="windows"),
        fld("Sistema", "ui:settings", "system", accent="system"),
    ]
    items += custom_for("")
    if IS_WIN:
        items += windows_section()
    return {"title": "Stream Deck", "items": items, "notice": CONFIG_ERROR, "live": True}


def folder_obs(sub):
    if sub == ["more"]:
        return folder_obs_more()
    if sub:
        raise ActionError("Pasta nao encontrada")
    notice, snap, kind = None, None, None
    try:
        snap = obs_snapshot()
    except ObsError as e:
        notice, kind = str(e), getattr(e, "kind", None)
    s = snap or {}
    items = [
        hdr("Programa"),
        btn("Abrir OBS", "brand:obsstudio", "obs.open"),
        btn("Janela do OBS", "ui:app-window", "obs.focus"),
        hdr("Transmissao"),
        btn("Iniciar stream", "ui:radio", "obs.stream_start", on=bool(s.get("streaming")), tone="live"),
        btn("Encerrar stream", "ui:square", "obs.stream_stop", **risky("Encerrar a transmissao?")),
        btn("Ver chat", "ui:message-circle", "chat.open"),
        hdr("Gravacao"),
        btn("Iniciar gravacao", "ui:circle-dot", "obs.rec_start", on=bool(s.get("recording")), tone="live"),
        btn("Pausar gravacao", "ui:pause", "obs.rec_pause", on=bool(s.get("paused"))),
        btn("Retomar gravacao", "ui:play", "obs.rec_resume"),
        btn("Encerrar gravacao", "ui:square", "obs.rec_stop", **risky("Encerrar a gravacao?")),
        hdr("Audio"),
        btn("Mutar microfone", "ui:mic-off" if s.get("mic_muted") else "ui:mic", "obs.mute_mic",
            on=bool(s.get("mic_muted"))),
        btn("Mutar desktop", "ui:volume-x" if s.get("desktop_muted") else "ui:volume-2", "obs.mute_desktop",
            on=bool(s.get("desktop_muted"))),
        fld("Mais opcoes", "ui:sliders-horizontal", "obs/more"),
        hdr("Cenas"),
    ]
    if kind == "disabled":
        items[:0] = [hdr("Corrigir"),
                     btn("Ativar WebSocket", "ui:wrench", "obs.enable_ws", tone="danger",
                         confirm="Isso fecha e reabre o OBS. Nao faca durante uma live ou gravacao. Continuar?")]
    for name in s.get("scenes") or []:
        items.append(btn(name, "ui:clapperboard", "obs.scene", name, on=(name == s.get("current"))))
    if snap is not None and not snap["scenes"]:
        notice = "O OBS nao tem nenhuma cena."
    return {"title": "OBS", "items": items, "notice": notice, "live": True}


def folder_obs_more():
    notice = None
    items = []
    try:
        data = obs_req("GetInputList")
        inputs = [i for i in (data.get("inputs") or [])
                  if "wasapi" in str(i.get("inputKind", "")) or "audio" in str(i.get("inputKind", ""))]
        if inputs:
            items.append(hdr("Entradas de audio (toque para mutar)"))
        for i in inputs[:24]:
            name = i.get("inputName")
            try:
                muted = bool(obs_req("GetInputMute", {"inputName": name}).get("inputMuted"))
            except ObsOffline:
                raise
            except ObsError:
                muted = False
            items.append(btn(name, "ui:volume-x" if muted else "ui:sliders-horizontal", "obs.mute", name, on=muted))
    except ObsError as e:
        notice = str(e)
    items += [
        hdr("Extras"),
        btn("Camera virtual", "ui:camera", "obs.vcam"),
        btn("Replay buffer liga/desliga", "ui:repeat", "obs.replay_toggle"),
        btn("Salvar replay", "ui:save", "obs.replay_save"),
    ]
    return {"title": "OBS - mais opcoes", "items": items, "notice": notice, "live": True, "accent": "obs"}


def folder_steam(sub):
    if sub == ["games"]:
        games = steam_games() or []
        return {"title": "Todos os jogos (%d)" % len(games), "search": True,
                "items": [steam_item(g) for g in games],
                "notice": None if games else "Nenhum jogo instalado encontrado.", "accent": "steam"}
    if sub:
        raise ActionError("Pasta nao encontrada")
    games = steam_games()
    items = [
        hdr("Cliente"),
        btn("Abrir Steam", "brand:steam", "steam.open"),
        btn("Janela da Steam", "ui:app-window", "steam.focus"),
        btn("Biblioteca", "ui:library", "steam.uri", "library"),
        btn("Loja", "ui:shopping-bag", "steam.uri", "store"),
        btn("Downloads", "ui:download", "steam.uri", "downloads"),
        btn("Amigos", "ui:users", "steam.uri", "friends"),
        btn("Big Picture", "ui:tv", "steam.uri", "bigpicture"),
        btn("Configuracoes", "ui:settings", "steam.uri", "settings"),
        btn("Fechar Steam", "ui:power", "steam.uri", "exit", **risky("Fechar a Steam?")),
    ]
    notice = None
    if games is None:
        notice = "Steam nao encontrada. Se estiver instalada em outro lugar, defina steam.path no config.json"
    else:
        recent = sorted([g for g in games if g["last"] > 0], key=lambda g: -g["last"])[:6]
        items.append(hdr("Jogos recentes" if recent else "Jogos"))
        items += [steam_item(g) for g in recent]
        items.append(fld("Todos os jogos (%d)" % len(games), "ui:library", "steam/games", wide=True))
    return {"title": "Steam", "items": items, "notice": notice}


def folder_epic(sub):
    if sub:
        raise ActionError("Pasta nao encontrada")
    games = epic_games()
    items = [
        hdr("Launcher"),
        btn("Abrir Epic", "brand:epicgames", "epic.open"),
        btn("Janela da Epic", "ui:app-window", "epic.focus"),
        btn("Loja", "ui:shopping-bag", "epic.store"),
        btn("Fechar Epic", "ui:power", "epic.exit", **risky("Fechar a Epic Games?")),
    ]
    notice = None
    if games:
        items.append(hdr("Jogos (%d)" % len(games)))
        items += [epic_item(g) for g in games]
    else:
        notice = ("Nao consegui listar seus jogos da Epic. Alternativa: coloque os atalhos deles na pasta JOGOS "
                  "da area de trabalho e abra pela aba Jogos.")
    return {"title": "Epic Games", "items": items, "notice": notice, "search": True}


# ---- Jogos: atalhos da pasta JOGOS da area de trabalho --------------------------------------
SHORTCUT_EXT = (".lnk", ".url", ".exe")


def desktop_dirs():
    out = []
    if IS_WIN:
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(520)
            for csidl in (0x0010, 0x0019):   # DESKTOPDIRECTORY e COMMON_DESKTOPDIRECTORY
                if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0:
                    out.append(buf.value)
        except Exception:
            pass
    e = os.environ
    if e.get("USERPROFILE"):
        out.append(os.path.join(e["USERPROFILE"], "Desktop"))
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        if e.get(var):
            out.append(os.path.join(e[var], "Desktop"))
    if e.get("PUBLIC"):
        out.append(os.path.join(e["PUBLIC"], "Desktop"))
    return _unique_existing(out, os.path.isdir)


def jogos_dirs():
    cfg = str((CFG.get("jogos") or {}).get("pasta") or "").strip()
    if cfg and os.path.isdir(cfg):
        return [os.path.normpath(cfg)]
    found = []
    for d in desktop_dirs():
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for n in names:
            if n.lower() in ("jogos", "games") and os.path.isdir(os.path.join(d, n)):
                found.append(os.path.join(d, n))
    return _unique_existing(found, os.path.isdir)


def read_url_file(path):
    d = {}
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            for line in f.read(8192).splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    d[k.strip().lower()] = v.strip()
    except OSError:
        pass
    return d


def shortcut_item(full, base):
    rel = os.path.relpath(full, base).replace("\\", "/")
    name = os.path.splitext(os.path.basename(full))[0]
    it = btn(name, "ui:gamepad-2", "lnk.open", rel, kind="game")
    if full.lower().endswith(".url"):
        d = read_url_file(full)
        m = re.match(r"steam://rungameid/(\d+)", d.get("url", ""), re.I)
        if m:
            it["imgs"] = steam_cover_urls(m.group(1))
        icon = d.get("iconfile")
        if icon and os.path.isfile(icon):
            it["img"] = icon_url(icon)
    else:
        it["img"] = icon_url(full)
    return it


def resolve_shortcut(rel):
    parts = [p for p in str(rel).replace("\\", "/").split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        raise ActionError("Atalho invalido")
    for base in jogos_dirs():
        path = os.path.normpath(os.path.join(base, *parts))
        try:
            inside = os.path.commonpath([os.path.normcase(path), os.path.normcase(base)]) == os.path.normcase(base)
        except ValueError:
            inside = False
        if inside and os.path.isfile(path) and path.lower().endswith(SHORTCUT_EXT):
            return path
    raise ActionError("Atalho nao encontrado (a pasta JOGOS mudou?)")


def a_lnk_open(arg):
    path = resolve_shortcut(arg)
    launch_uri(path)
    return "Abrindo " + os.path.splitext(os.path.basename(path))[0]


def folder_jogos(sub):
    if any(not x or x in (".", "..") or "/" in x or "\\" in x for x in sub):
        raise ActionError("Pasta nao encontrada")
    bases = jogos_dirs()
    folders, games = {}, []
    for base in bases:
        d = os.path.join(base, *sub)
        if not os.path.isdir(d):
            continue
        try:
            names = sorted(os.listdir(d), key=str.lower)
        except OSError:
            continue
        for name in names:
            full = os.path.join(d, name)
            if name.startswith(".") or name.lower() == "desktop.ini":
                continue
            if os.path.isdir(full):
                folders[name] = True
            elif name.lower().endswith(SHORTCUT_EXT):
                games.append(shortcut_item(full, base))
    items = []
    if folders:
        items.append(hdr("Pastas"))
        items += [fld(n, "ui:folder", "/".join(["jogos"] + sub + [n])) for n in sorted(folders, key=str.lower)]
    if games:
        items.append(hdr("Jogos (%d)" % len(games)))
        items += games
    notice = None
    if not bases:
        notice = ("Nao achei a pasta JOGOS na area de trabalho. Crie uma pasta chamada JOGOS la e coloque os "
                  "atalhos dos seus jogos (ou defina jogos.pasta no config.json).")
    elif not folders and not games:
        notice = "A pasta JOGOS esta vazia."
    if not sub:
        items += [hdr("Plataformas"),
                  fld("Steam", "brand:steam", "steam", accent="steam"),
                  fld("Epic Games", "brand:epicgames", "epic", accent="epic")]
    return {"title": sub[-1] if sub else "Jogos", "items": items, "notice": notice, "search": True,
            "accent": "jogos"}


def folder_windows(sub):
    if sub:
        raise ActionError("Pasta nao encontrada")
    items = [
        hdr("Atalhos"),
        btn("Alt + Tab", "ui:repeat", "win.alttab"),
        btn("Visao de tarefas", "ui:layout-dashboard", "win.taskview"),
        btn("Area de trabalho", "ui:monitor", "win.desktop"),
        btn("Janela a esquerda", "ui:panel-left", "win.snap_left"),
        btn("Janela a direita", "ui:panel-right", "win.snap_right"),
        btn("Minimizar atual", "ui:minus", "win.minimize"),
        btn("Maximizar atual", "ui:maximize", "win.maximize"),
        btn("Fechar aba (Ctrl+W)", "ui:x", "br.close_tab"),
        btn("Fechar janela", "ui:circle-x", "win.close", **risky("Fechar a janela ativa do PC?")),
        btn("Alt + F4", "ui:ban", "win.altf4", **risky("Enviar Alt+F4 para a janela ativa?")),
    ]
    notice = None
    if not IS_WIN:
        notice = "A lista de janelas so funciona no Windows."
    else:
        items += windows_section()
    return {"title": "Janelas", "items": items, "notice": notice, "live": True}


def folder_system(sub):
    if sub:
        raise ActionError("Pasta nao encontrada")
    items = [
        hdr("Audio e midia"),
        btn("Volume -", "ui:volume-1", "sys.vol_down"),
        btn("Volume +", "ui:volume-2", "sys.vol_up"),
        btn("Mudo", "ui:volume-x", "sys.mute"),
        btn("Play / Pause", "ui:circle-play", "sys.play"),
        btn("Proxima", "ui:skip-forward", "sys.next"),
        btn("Anterior", "ui:skip-back", "sys.prev"),
        hdr("PC"),
        btn("Explorador", "ui:folder-open", "sys.explorer"),
        btn("Gerenc. de tarefas", "ui:activity", "sys.taskmgr"),
        btn("Captura de tela", "ui:camera", "sys.screenshot"),
        btn("Configuracoes", "ui:settings", "sys.settings"),
        btn("Bloquear PC", "ui:lock", "sys.lock"),
        hdr("Energia"),
        btn("Desligar", "ui:power", "sys.shutdown", **risky("Desligar o PC em 10 segundos?")),
        btn("Reiniciar", "ui:rotate-cw", "sys.restart", **risky("Reiniciar o PC em 10 segundos?")),
        btn("Cancelar", "ui:ban", "sys.cancel_shutdown"),
    ]
    return {"title": "Sistema", "items": items}


def folder_browser(sub):
    if sub:
        raise ActionError("Pasta nao encontrada")
    items = [
        hdr("Abas"),
        btn("Fechar aba", "ui:x", "br.close_tab"),
        btn("Nova aba", "ui:plus", "br.new_tab"),
        btn("Reabrir aba", "ui:undo-2", "br.reopen_tab"),
        btn("Proxima aba", "ui:arrow-right", "br.next_tab"),
        btn("Aba anterior", "ui:arrow-left", "br.prev_tab"),
        hdr("Pagina"),
        btn("Voltar", "ui:chevron-left", "br.back"),
        btn("Avancar", "ui:chevron-right", "br.forward"),
        btn("Atualizar", "ui:refresh-cw", "br.reload"),
        btn("Tela cheia (F11)", "ui:maximize", "br.fullscreen"),
        btn("Barra de endereco", "ui:link", "br.address"),
        hdr("Janela"),
        btn("Nova janela", "ui:app-window", "br.new_window"),
        btn("Fechar janela", "ui:circle-x", "win.close", **risky("Fechar a janela ativa do PC?")),
    ]
    return {"title": "Navegador", "items": items}


def folder_custom(sub):
    items_cfg = CFG.get("custom") or []
    node = None
    idx = []
    try:
        for s_ in sub:
            i = int(s_)
            node = items_cfg[i]
            idx.append(i)
            items_cfg = node.get("items") or []
    except (ValueError, IndexError, AttributeError, TypeError):
        raise ActionError("Pasta nao encontrada")
    if node is None:
        raise ActionError("Pasta nao encontrada")
    items = [custom_item(n, idx + [i]) for i, n in enumerate(items_cfg) if isinstance(n, dict)]
    out = {"title": str(node.get("label", "Pasta")), "items": items, "accent": "custom",
           "notice": None if items else "Pasta vazia. Toque em + para adicionar botoes aqui."}
    if len(sub) == 1:
        out["parent"] = _custom_home(node)
    return out


# ---- Abas ------------------------------------------------------------------------------
# =============================================================================
# Janelas: minimizar / fechar / encerrar uma janela especifica (segurar na lista)
# =============================================================================
PROTECTED_EXES = {"explorer.exe", "winlogon.exe", "csrss.exe", "dwm.exe", "python.exe", "pythonw.exe", "py.exe"}


def _win_by_arg(arg):
    try:
        hwnd = int(arg)
    except (TypeError, ValueError):
        raise ActionError("Janela invalida")
    w = next((x for x in list_windows() if x["hwnd"] == hwnd), None)
    if not w:
        raise ActionError("Essa janela nao existe mais")
    return w


def a_win_minimize_w(arg):
    w = _win_by_arg(arg)
    user32.ShowWindow(w["hwnd"], 6)
    return "Minimizada: " + w["title"][:30]


def a_win_close_w(arg):
    w = _win_by_arg(arg)
    user32.PostMessageW(w["hwnd"], 0x0010, 0, 0)   # WM_CLOSE (como clicar no X)
    return "Fechando: " + w["title"][:30]


def a_win_kill_w(arg):
    w = _win_by_arg(arg)
    if w["exe"] in PROTECTED_EXES:
        raise ActionError("Por seguranca nao encerro %s por aqui. Use Fechar janela." % w["exe"])
    import ctypes as _ct
    pid = _ct.c_ulong(0)
    user32.GetWindowThreadProcessId(w["hwnd"], _ct.byref(pid))
    if not pid.value:
        raise ActionError("Nao achei o processo dessa janela")
    run_hidden(["taskkill", "/PID", str(pid.value), "/T", "/F"])
    return "Encerrado: " + w["exe"]


# =============================================================================
# Modos (ex.: Modo Call): abre Discord + DroidCam e pergunta onde vai jogar
# =============================================================================
APP_SPECS = {
    "discord": {"name": "Discord", "exe": "discord.exe", "short": "discord"},
    "droidcam": {"name": "DroidCam", "exe": "droidcamapp.exe", "short": "droidcam"},
}


def find_app_launch(key):
    """(comando/alvo, args) para abrir o app, ou None."""
    spec = APP_SPECS[key]
    cfgp = str((CFG.get("modos") or {}).get(key) or "").strip().strip('"')
    if cfgp and os.path.exists(cfgp):
        return cfgp, None
    e = os.environ
    cands = []
    if key == "discord":
        for base in (e.get("LOCALAPPDATA"),):
            if base:
                up = os.path.join(base, "Discord", "Update.exe")
                if os.path.isfile(up):
                    return up, "--processStart Discord.exe"
    else:
        for base in (e.get("ProgramFiles(x86)"), e.get("ProgramFiles"), e.get("ProgramW6432")):
            if base:
                cands.append(os.path.join(base, "DroidCam", "DroidCamApp.exe"))
    for c in cands:
        if os.path.isfile(c):
            return c, None
    try:
        for name, full in cached(("apps",), 30, scan_app_shortcuts):
            if spec["short"] in name.lower():
                return full, None
    except Exception:
        pass
    return None


def open_or_focus(key):
    """Abre o app (ou traz para frente se ja estiver aberto). Retorna texto de status."""
    spec = APP_SPECS[key]
    w = find_window([lambda x: x["exe"] == spec["exe"]])
    if w:
        focus_window(w["hwnd"])
        return spec["name"] + " ja aberto"
    found = find_app_launch(key)
    if not found:
        raise ActionError("Nao achei o %s. Informe o caminho em modos.%s no config.json" % (spec["name"], key))
    target, args = found
    if args:
        spawn_detached('"%s" %s' % (target, args), cwd=os.path.dirname(target))
    else:
        launch_uri(target)
    return "Abrindo " + spec["name"]


def a_app_open(arg):
    if arg not in APP_SPECS:
        raise ActionError("App desconhecido")
    return open_or_focus(arg)


def a_app_focus(arg):
    if arg not in APP_SPECS:
        raise ActionError("App desconhecido")
    spec = APP_SPECS[arg]
    w = find_window([lambda x: x["exe"] == spec["exe"]])
    if not w:
        raise ActionError("%s nao esta aberto" % spec["name"])
    focus_window(w["hwnd"])
    return spec["name"]


def _discord_keys(combo, msg):
    def fn(_):
        w = find_window([lambda x: x["exe"] == "discord.exe"])
        if not w:
            raise ActionError("Discord nao esta aberto")
        focus_window(w["hwnd"])
        time.sleep(0.35)
        send_keys(combo)
        return msg
    return fn


def list_mics():
    """Microfones ativos no Windows (leitura do registro, sem instalar nada)."""
    if not IS_WIN:
        return []
    try:
        import winreg
    except ImportError:
        return []
    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture"
    out = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base)
    except OSError:
        return []
    i = 0
    while True:
        try:
            sub = winreg.EnumKey(root, i)
        except OSError:
            break
        i += 1
        try:
            with winreg.OpenKey(root, sub) as k:
                if winreg.QueryValueEx(k, "DeviceState")[0] != 1:
                    continue
            with winreg.OpenKey(root, sub + r"\Properties") as pk:
                def val(name):
                    try:
                        return str(winreg.QueryValueEx(pk, name)[0])
                    except OSError:
                        return ""
                dev = val("{a45c254e-df1c-4efd-8020-67d146a850e0},2")
                adp = val("{b3f8fa53-0004-438e-9003-51a46e139bfc},6")
            name = dev if (not adp or adp in dev) else "%s (%s)" % (dev, adp)
            if name:
                out.append({"name": name, "droidcam": "droidcam" in name.lower()})
        except OSError:
            continue
    return sorted(out, key=lambda m: (not m["droidcam"], m["name"].lower()))


def mic_summary():
    mics = list_mics()
    if not mics:
        return "Nenhum microfone ativo encontrado"
    return "Microfones: " + ", ".join(m["name"] for m in mics)


def a_mode_call(_):
    msgs, errs = [], []
    for key in ("discord", "droidcam"):
        try:
            msgs.append(open_or_focus(key))
        except ActionError as e:
            errs.append(str(e))
    if errs and not msgs:
        raise ActionError(" | ".join(errs))
    return "; ".join(msgs + errs)


def a_mic_info(arg):
    return str(arg or "")


def a_sound_settings(_):
    launch_uri("ms-settings:sound")
    return "Configuracoes de som"


def a_sound_recording(_):
    if not IS_WIN:
        raise ActionError("Esta acao so funciona no Windows")
    spawn_detached("control mmsys.cpl,,1")
    return "Painel de gravacao"


def a_mic_privacy(_):
    launch_uri("ms-settings:privacy-microphone")
    return "Privacidade do microfone"


def a_discord_settings(_):
    return _discord_keys("ctrl+comma", "Configuracoes do Discord")(None)


ACTIONS.update({
    "win.minimize_w": a_win_minimize_w, "win.close_w": a_win_close_w, "win.kill_w": a_win_kill_w,
    "app.open": a_app_open, "app.focus": a_app_focus, "mode.call": a_mode_call,
    "discord.mute": _discord_keys("ctrl+shift+m", "Mutar/desmutar enviado ao Discord"),
    "discord.deafen": _discord_keys("ctrl+shift+d", "Ensurdecer/desensurdecer enviado ao Discord"),
    "discord.settings": a_discord_settings,
    "mic.info": a_mic_info, "sound.settings": a_sound_settings, "sound.recording": a_sound_recording,
    "mic.privacy": a_mic_privacy,
})


def folder_modes(sub):
    if not sub:
        items = [hdr("Modos")]
        for m in modes_effective():
            asks = [{"i": i, "text": st["ask"]} for i, st in enumerate(m.get("steps") or []) if st.get("ask")]
            n = len(m.get("steps") or [])
            it = btn(m["label"], mode_icon(m), "mode.run", m["id"], accent="modes", mid=m["id"],
                     sub="%d passo%s" % (n, "" if n == 1 else "s"))
            if m.get("then"):
                it["go"] = m["then"]
            if asks:
                it["asks"] = asks
            items.append(it)
        return {"title": "Modos", "items": items, "notice": None}
    if sub != ["call"]:
        raise ActionError("Pasta nao encontrada")
    mics = list_mics()
    notice = None
    if not IS_WIN:
        notice = "Modo Call so funciona no Windows."
    elif not mics:
        notice = "Nao achei nenhum microfone ativo. Veja as configuracoes de som abaixo."
    elif not any(m["droidcam"] for m in mics):
        notice = "O microfone do DroidCam ainda nao apareceu. Abra o DroidCam e conecte o celular."
    items = [
        hdr("Onde vai jogar?"),
        fld("Steam", "brand:steam", "steam", accent="steam"),
        fld("Epic Games", "brand:epicgames", "epic", accent="epic"),
        fld("Jogo avulso", "ui:gamepad-2", "jogos", accent="jogos", sub="pasta JOGOS"),
        hdr("Discord"),
        btn("Abrir Discord", "brand:discord", "app.open", "discord"),
        btn("Janela do Discord", "ui:app-window", "app.focus", "discord"),
        btn("Mutar microfone", "ui:mic-off", "discord.mute", sub="Ctrl+Shift+M"),
        btn("Ensurdecer", "ui:headphones", "discord.deafen", sub="Ctrl+Shift+D"),
        btn("Configurar Discord", "ui:settings", "discord.settings", sub="abre as configuracoes"),
        hdr("Camera"),
        btn("Abrir DroidCam", "ui:camera", "app.open", "droidcam"),
        btn("Janela do DroidCam", "ui:app-window", "app.focus", "droidcam"),
        hdr("Microfones (%d)" % len(mics)),
    ]
    for m in mics:
        items.append(btn(m["name"], "ui:mic", "mic.info", m["name"], sub="DroidCam" if m["droidcam"] else None,
                         on=m["droidcam"]))
    items += [
        btn("Configuracoes de som", "ui:sliders-horizontal", "sound.settings", sub="escolher microfone"),
        btn("Painel de gravacao", "ui:mic", "sound.recording", sub="clássico"),
        btn("Privacidade do mic", "ui:lock", "mic.privacy"),
    ]
    return {"title": "Modo Call", "items": items, "notice": notice, "live": True, "parent": "modes", "accent": "modes"}


BUILTIN_TABS = {
    "inicio": ("Inicio", "ui:house", ""),
    "modos": ("Modos", "ui:sparkles", "modes"),
    "jogos": ("Jogos", "ui:gamepad-2", "jogos"),
    "obs": ("OBS", "brand:obsstudio", "obs"),
    "steam": ("Steam", "brand:steam", "steam"),
    "epic": ("Epic", "brand:epicgames", "epic"),
    "navegador": ("Navegador", "ui:globe", "browser"),
    "janelas": ("Janelas", "ui:app-window", "windows"),
    "sistema": ("Sistema", "ui:settings", "system"),
}
DEFAULT_TABS = ["inicio", "modos", "jogos", "obs", "navegador", "sistema"]


def tabs_list():
    ids = CFG.get("abas") if isinstance(CFG.get("abas"), list) else DEFAULT_TABS
    out = []
    for tid in ids:
        if tid in BUILTIN_TABS:
            label, icon, path = BUILTIN_TABS[tid]
            o = (CFG.get("ajustes") or {}).get("f:" + path)
            if isinstance(o, dict):
                label = str(o.get("label") or label)[:14]
                icon = ("brand:" + o["logo"]) if o.get("logo") else (o.get("icon") or icon)
            out.append({"id": tid, "label": label, "icon": icon, "path": path})
    for i, node in enumerate(CFG.get("custom") or []):
        if isinstance(node, dict) and isinstance(node.get("items"), list) and node.get("aba"):
            out.append({"id": "c%d" % i, "label": str(node.get("label", "Aba"))[:14],
                        "icon": item_icon(node), "path": "custom/%d" % i})
    return out


def active_tab(parts, tabs):
    paths = {t["path"] for t in tabs}
    for n in range(len(parts), 0, -1):
        pref = "/".join(parts[:n])
        if pref in paths:
            return pref
    if parts and parts[0] in ("steam", "epic") and "jogos" in paths:
        return "jogos"
    if parts and parts[0] == "custom" and len(parts) > 1:
        try:
            home = _custom_home(CFG["custom"][int(parts[1])])
        except (ValueError, IndexError, TypeError, KeyError):
            home = ""
        m = {"windows": "windows", "system": "system", "browser": "browser"}.get(home, home)
        if m and m in paths:
            return m
    return "" if "" in paths else None


def build_folder(path, edit=False):
    parts = [p for p in str(path).strip("/").split("/") if p]
    top = parts[0] if parts else ""
    builders = {"obs": folder_obs, "steam": folder_steam, "epic": folder_epic, "jogos": folder_jogos, "modes": folder_modes,
                "browser": folder_browser, "windows": folder_windows, "system": folder_system,
                "custom": folder_custom}
    if not top:
        data = folder_root()
    elif top in builders:
        data = builders[top](parts[1:])
        if top in BUILTIN_FOLDERS and len(parts) == 1:
            data["items"] = list(data["items"]) + custom_for(top)   # botoes seus misturados aos padrao
    else:
        raise ActionError("Pasta nao encontrada")
    data.setdefault("accent", top)
    data["path"] = "/".join(parts)
    if "parent" not in data:
        data["parent"] = "" if len(parts) <= 1 else "/".join(parts[:-1])
    apply_ajustes("/".join(parts), data, edit)
    data["tabs"] = tabs_list()
    data["tab"] = active_tab(parts, data["tabs"])
    return data


# =============================================================================
# Editor: adicionar / editar / apagar / mover botoes pelo celular (grava no config.json)
# =============================================================================
EDIT_APPS = {}   # id -> caminho de atalho/programa escolhido na lista


def _custom_list():
    lst = RAW.get("custom")
    if not isinstance(lst, list):
        lst = []
        RAW["custom"] = lst
    return lst


def _locate(cid):
    try:
        idxs = [int(x) for x in str(cid).split("/") if x != ""]
        lst = _custom_list()
        for i in idxs[:-1]:
            lst = lst[i]["items"]
        if not isinstance(lst, list) or not idxs:
            raise ValueError
        lst[idxs[-1]]
        return lst, idxs[-1]
    except (ValueError, IndexError, KeyError, TypeError):
        raise ActionError("Botao nao encontrado (o config.json mudou?)")


def _container_for(where):
    """(lista onde o item entra, pasta padrao a gravar em 'pasta' ou None)."""
    parts = [p for p in str(where or "").strip("/").split("/") if p]
    if not parts:
        return _custom_list(), None
    if parts[0] == "custom" and len(parts) >= 2:
        try:
            lst = _custom_list()
            for sidx in parts[1:]:
                node = lst[int(sidx)]
                if not isinstance(node.get("items"), list):
                    raise ValueError
                lst = node["items"]
            return lst, None
        except (ValueError, IndexError, KeyError, TypeError):
            raise ActionError("Pasta de destino invalida")
    if parts[0] in BUILTIN_FOLDERS:
        return _custom_list(), parts[0]
    raise ActionError("Nao e possivel adicionar botoes aqui")


def sanitize_item(src):
    """Valida o que veio do celular. Comandos livres (cmd/macros) so existem editando o config.json."""
    if not isinstance(src, dict):
        raise ActionError("Botao invalido")
    kind = str(src.get("kind") or "")
    label = str(src.get("label") or "").strip()
    if not label and kind == "wait":
        label = "Esperar %ss" % _wait_secs(src)
    if not label and kind == "appkey" and str(src.get("app") or "") in APP_LABELS:
        label = APP_LABELS[str(src["app"])]
    if not label:
        raise ActionError("De um nome ao botao")
    out = {"label": label[:40]}
    logo = str(src.get("logo") or "").lower()
    if logo:
        if not re.fullmatch(r"[a-z0-9]{1,40}", logo):
            raise ActionError("Logo invalido")
        out["logo"] = logo
    icon = str(src.get("icon") or "")
    if icon:
        if not re.fullmatch(r"ui:[a-z0-9-]{1,40}", icon):
            raise ActionError("Icone invalido")
        out["icon"] = icon
    if kind == "site":
        url = str(src.get("url") or "").strip()
        if not re.match(r"https?://", url, re.I):
            if re.fullmatch(r"[\w.-]+\.[A-Za-z]{2,}([/?#].*)?", url):
                url = "https://" + url
            else:
                raise ActionError("Endereco do site invalido")
        out.update(type="open", target=url)
    elif kind == "app":
        path = EDIT_APPS.get(str(src.get("app") or "")) or str(src.get("path") or "").strip().strip('"')
        if not path or not os.path.exists(path):
            raise ActionError("Programa nao encontrado")
        out.update(type="open", target=path, img=path)
    elif kind == "keys":
        parts = [p for p in re.split(r"\s*\+\s*", str(src.get("keys") or "").strip().lower()) if p]
        if not parts:
            raise ActionError("Informe o atalho de teclado")
        for p in parts:
            _vk_for(p)
        out.update(type="keys", keys="+".join(parts))
    elif kind == "scene":
        scene = str(src.get("scene") or "").strip()
        if not scene:
            raise ActionError("Escolha a cena")
        out.update(type="obs_scene", scene=scene[:120])
    elif kind == "focus":
        exe = str(src.get("exe") or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9._ -]{1,60}\.exe", exe):
            raise ActionError("Programa invalido")
        out.update(type="focus", exe=exe)
        wpath = str(src.get("path") or "")
        if wpath and os.path.isfile(wpath) and os.path.basename(wpath).lower() == exe:
            out.update(launch=wpath, img=wpath)
        elif EDIT_APPS.get(str(src.get("app") or "")):
            p = EDIT_APPS[str(src["app"])]
            out.update(launch=p, img=p)
    elif kind == "kill":
        exe = str(src.get("exe") or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9._ -]{1,60}\.exe", exe):
            raise ActionError("Informe o nome do programa (ex.: medal.exe)")
        if exe in PROTECTED_EXES:
            raise ActionError("Por seguranca nao da para encerrar %s" % exe)
        out.update(type="kill", exe=exe, confirm="Encerrar %s por completo?" % exe)
    elif kind == "appkey":
        app = str(src.get("app") or "")
        if app not in APP_LABELS:
            raise ActionError("Escolha o programa")
        out.update(type="appkey", app=app)
    elif kind == "wait":
        out.update(type="wait", wait=_wait_secs(src))
    elif kind == "folder":
        out["items"] = []
        if src.get("aba"):
            out["aba"] = True
    else:
        raise ActionError("Tipo de botao desconhecido")
    return out


APP_LABELS = {"obs": "OBS", "discord": "Discord", "droidcam": "DroidCam"}


def _wait_secs(src):
    try:
        n = int(float(src.get("sec", src.get("wait", 2))))
    except (TypeError, ValueError):
        raise ActionError("Tempo de espera invalido")
    if not 1 <= n <= 30:
        raise ActionError("A espera vai de 1 a 30 segundos")
    return n


def lnk_target(path):
    """Programa (.exe) para o qual um atalho .lnk aponta (so Windows)."""
    if path.lower().endswith(".exe"):
        return path
    if not (IS_WIN and path.lower().endswith(".lnk")):
        return ""
    try:
        r = run_hidden(["powershell", "-NoProfile", "-Command",
                        "(New-Object -ComObject WScript.Shell).CreateShortcut($env:SD_LNK).TargetPath"],
                       capture=True, timeout=20, env={"SD_LNK": path})
        t = r.stdout.decode("utf-8", "replace").strip()
        return t if t.lower().endswith(".exe") else ""
    except Exception:
        return ""


def build_appkit(src):
    """Pasta pronta para um programa: Iniciar / Trazer janela / Fechar (completo)."""
    name = str(src.get("label") or "").strip()
    if not name:
        raise ActionError("De um nome ao painel")
    path = EDIT_APPS.get(str(src.get("app") or "")) or str(src.get("path") or "").strip().strip('"')
    wpath = str(src.get("wpath") or "")
    if not path and wpath and os.path.isfile(wpath):
        path = wpath
    if not path or not os.path.exists(path):
        raise ActionError("Escolha o programa na lista ou informe o caminho")
    exe = str(src.get("exe") or "").strip().lower()
    if not exe:
        exe = os.path.basename(lnk_target(path)).lower()
    if not re.fullmatch(r"[a-z0-9._ -]{1,60}\.exe", exe):
        raise ActionError("Informe o nome do programa (.exe), usado para fechar e trazer a janela")
    if exe in PROTECTED_EXES:
        raise ActionError("Por seguranca nao da para criar painel de %s" % exe)
    node = sanitize_item({"kind": "folder", "label": name, "logo": src.get("logo"), "icon": src.get("icon"),
                          "aba": bool(src.get("aba"))})
    if "logo" not in node and "icon" not in node:
        node["img"] = path
    node["items"] = [
        {"label": "Iniciar", "icon": "ui:rocket", "type": "open", "target": path, "img": path},
        {"label": "Trazer janela", "icon": "ui:app-window", "type": "focus", "exe": exe, "launch": path},
        {"label": "Fechar (completo)", "icon": "ui:power", "type": "kill", "exe": exe,
         "confirm": "Encerrar %s por completo?" % name},
    ]
    return node


def _save_and_reload():
    save_raw()
    load_config()
    _CACHE.clear()


def api_custom_add(body):
    with _CONFIG_LOCK:
        where = str(body.get("where") or "").strip("/")
        lst, home = _container_for(body.get("where"))
        src = body.get("item")
        if isinstance(src, dict) and src.get("kind") == "appkit":
            item = build_appkit(src)
        else:
            item = sanitize_item(src)
        if home:
            item["pasta"] = home
        lst.append(item)
        _save_and_reload()
    base = where if where.startswith("custom/") else "custom"
    return {"path": "%s/%d" % (base, len(lst) - 1)} if isinstance(item.get("items"), list) else {}


def api_custom_update(body):
    with _CONFIG_LOCK:
        lst, idx = _locate(body.get("cid"))
        old = lst[idx]
        if body.get("item") is None:     # so renomear / trocar logo / mudar de lugar (botoes cmd/multi do config.json)
            new = dict(old)
            if str(body.get("label") or "").strip():
                new["label"] = str(body["label"]).strip()[:40]
            body = dict(body, item={"aba": bool(old.get("aba"))} if isinstance(old.get("items"), list) else {})
        else:
            new = sanitize_item(body.get("item"))
        if isinstance(old.get("items"), list):
            new["items"] = old["items"]
            if "aba" in body.get("item", {}):
                if body["item"].get("aba"):
                    new["aba"] = True
                else:
                    new.pop("aba", None)
            elif old.get("aba"):
                new["aba"] = True
        if old.get("pasta"):
            new["pasta"] = old["pasta"]
        where = body.get("where")
        if where is not None:
            dst, home = _container_for(where)
            cur_home = str(old.get("pasta") or "") or None
            if dst is not lst or (home or None) != cur_home:
                cid = str(body.get("cid"))
                w = str(where).strip("/")
                if isinstance(new.get("items"), list) and (w + "/").startswith("custom/" + cid + "/"):
                    raise ActionError("Nao da para mover uma pasta para dentro dela mesma")
                lst.pop(idx)
                new.pop("pasta", None)
                if home:
                    new["pasta"] = home
                dst.append(new)
                _save_and_reload()
                return {}
        lst[idx] = new
        _save_and_reload()
    return {}


def api_custom_delete(body):
    with _CONFIG_LOCK:
        lst, idx = _locate(body.get("cid"))
        lst.pop(idx)
        _save_and_reload()
    return {}


def api_custom_move(body):
    with _CONFIG_LOCK:
        lst, idx = _locate(body.get("cid"))
        delta = -1 if int(body.get("delta", 0)) < 0 else 1
        home = _custom_home(lst[idx]) if isinstance(lst[idx], dict) else ""
        sibs = [i for i, n in enumerate(lst) if isinstance(n, dict) and _custom_home(n) == home]
        pos = sibs.index(idx)
        if 0 <= pos + delta < len(sibs):
            other = sibs[pos + delta]
            lst[idx], lst[other] = lst[other], lst[idx]
            _save_and_reload()
    return {}


def node_to_form(n):
    ic = str(n.get("icon") or "")
    f = {"label": str(n.get("label", "")), "logo": str(n.get("logo") or ""), "editable": True,
         "icon": ic if re.fullmatch(r"ui:[a-z0-9-]{1,40}", ic) else ""}
    t = custom_type(n)
    if isinstance(n.get("items"), list):
        f.update(kind="folder", aba=bool(n.get("aba")))
    elif t == "open" and str(n.get("target") or n.get("url") or "").lower().startswith(("http://", "https://")):
        f.update(kind="site", url=str(n.get("target") or n.get("url")))
    elif t == "open" and not n.get("args"):
        f.update(kind="app", path=str(n.get("target") or ""))
    elif t == "keys":
        f.update(kind="keys", keys=str(n.get("keys") or ""))
    elif t == "obs_scene":
        f.update(kind="scene", scene=str(n.get("scene") or ""))
    elif t == "focus":
        f.update(kind="focus", exe=str(n.get("exe") or ""), path=str(n.get("launch") or ""))
    elif t == "kill":
        f.update(kind="kill", exe=str(n.get("exe") or ""))
    elif t == "appkey":
        f.update(kind="appkey", app=str(n.get("app") or ""))
    elif t == "wait":
        f.update(kind="wait", sec=int(n.get("wait") or 2))
    else:
        f.update(kind="other", editable=False)
    return f


def api_custom_get(body):
    """Devolve o botao em forma de formulario (para o celular preencher a tela de edicao)."""
    lst, idx = _locate(body.get("cid"))
    return {"form": node_to_form(lst[idx])}


def okey_of(path, it):
    if it.get("type") == "folder":
        return "f:" + str(it.get("path"))
    return "b:%s|%s|%s" % (path, it.get("action"), it.get("arg", ""))


def apply_ajustes(path, data, edit):
    """Aplica renomear/logo/ocultar nos botoes padrao (e marca 'okey' para o modo editar)."""
    aj = CFG.get("ajustes") or {}
    out = []
    for it in data["items"]:
        if it.get("type") not in ("button", "folder") or it.get("cid") or it.get("mid") or it.get("kind") == "window":
            out.append(it)
            continue
        it = dict(it)
        key = okey_of(path, it)
        it["okey"] = key
        o = aj.get(key) if isinstance(aj.get(key), dict) else None
        if o:
            it["edited"] = True
            if o.get("label"):
                it["label"] = str(o["label"])[:40]
            if o.get("logo"):
                it["icon"] = "brand:" + str(o["logo"])
            elif o.get("icon"):
                it["icon"] = str(o["icon"])
            if o.get("hidden"):
                if not edit:
                    continue
                it["hidden"] = True
        out.append(it)
    # cabecalhos sem nenhum botao depois deles (tudo oculto) somem
    clean = []
    for i, it in enumerate(out):
        if it.get("type") == "header":
            nxt = out[i + 1] if i + 1 < len(out) else None
            if nxt is None or nxt.get("type") == "header":
                continue
        clean.append(it)
    data["items"] = clean


def api_custom_override(body):
    key = str(body.get("okey") or "")
    if not re.match(r"^[bf]:", key) or len(key) > 300:
        raise ActionError("Botao invalido")
    with _CONFIG_LOCK:
        aj = RAW.setdefault("ajustes", {})
        if not isinstance(aj, dict):
            aj = RAW["ajustes"] = {}
        if body.get("reset"):
            aj.pop(key, None)
        else:
            o = {}
            label = str(body.get("label") or "").strip()
            if label:
                o["label"] = label[:40]
            logo = str(body.get("logo") or "").lower()
            if logo:
                if not re.fullmatch(r"[a-z0-9]{1,40}", logo):
                    raise ActionError("Logo invalido")
                o["logo"] = logo
            icon = str(body.get("icon") or "")
            if icon:
                if not re.fullmatch(r"ui:[a-z0-9-]{1,40}", icon):
                    raise ActionError("Icone invalido")
                o["icon"] = icon
            if body.get("hidden"):
                o["hidden"] = True
            if o:
                aj[key] = o
            else:
                aj.pop(key, None)
        _save_and_reload()
    return {}


CUSTOM_API = {"override": api_custom_override, "get": api_custom_get, "add": api_custom_add, "update": api_custom_update, "delete": api_custom_delete,
              "move": api_custom_move}



DEFAULT_MODES = [
    {"id": "call", "label": "Modo Call com amigos", "logo": "discord", "then": "modes/call",
     "steps": [{"label": "Discord", "type": "appkey", "app": "discord"},
               {"label": "DroidCam", "type": "appkey", "app": "droidcam"}]},
    {"id": "stream", "label": "Modo Stream", "logo": "obsstudio", "then": "obs",
     "steps": [{"label": "OBS", "type": "appkey", "app": "obs"},
               {"label": "DroidCam", "type": "appkey", "app": "droidcam", "ask": "Quer abrir o DroidCam tambem?"}]},
]
MODE_GOTO_EXTRA = [{"path": "modes/call", "label": "Modo Call › Onde vai jogar?"}]


def modes_effective():
    saved = CFG.get("modos_lista") if isinstance(CFG.get("modos_lista"), dict) else {}
    out = []
    for dm in DEFAULT_MODES:
        o = saved.get(dm["id"])
        if isinstance(o, dict) and o.get("deleted"):
            continue
        m = dict(o) if isinstance(o, dict) and o.get("steps") is not None else dict(dm)
        m.update(id=dm["id"], default=True, edited=isinstance(o, dict) and not o.get("deleted"))
        out.append(m)
    defaults = {d["id"] for d in DEFAULT_MODES}
    for mid, o in saved.items():
        if mid not in defaults and isinstance(o, dict):
            out.append(dict(o, id=mid, default=False, edited=False))
    return out


def find_mode(mid):
    return next((m for m in modes_effective() if m["id"] == str(mid)), None)


def mode_icon(m):
    return item_icon({"logo": m.get("logo"), "icon": m.get("icon")}) if (m.get("logo") or m.get("icon")) else "ui:sparkles"


def a_mode_run(arg):
    yes = set()
    if isinstance(arg, dict):
        mid = arg.get("id")
        yes = {int(i) for i in (arg.get("yes") or []) if str(i).lstrip("-").isdigit()}
    else:
        mid = arg
    m = find_mode(mid)
    if not m:
        raise ActionError("Modo nao encontrado")
    done, errs = [], []
    for i, step in enumerate(m.get("steps") or []):
        if step.get("ask") and i not in yes:
            continue
        try:
            exec_custom(step)
            done.append(str(step.get("label") or step.get("type")))
        except (ActionError, ObsError) as e:
            errs.append("%s: %s" % (step.get("label") or step.get("type"), e))
    if errs and not done:
        raise ActionError(" | ".join(errs))
    return (m["label"] + ": " + ", ".join(done) if done else m["label"]) + ((" | " + " | ".join(errs)) if errs else "")


ACTIONS["mode.run"] = a_mode_run


def mode_goto_paths():
    return {l["path"] for l in editor_locations()} | {x["path"] for x in MODE_GOTO_EXTRA}


def sanitize_mode(src):
    if not isinstance(src, dict):
        raise ActionError("Modo invalido")
    label = str(src.get("label") or "").strip()
    if not label:
        raise ActionError("De um nome ao modo")
    out = {"label": label[:40]}
    node = sanitize_item({"kind": "wait", "label": "x", "logo": src.get("logo"), "icon": src.get("icon")})
    for k in ("logo", "icon"):
        if node.get(k):
            out[k] = node[k]
    then = str(src.get("then") or "").strip("/")
    if then and then not in mode_goto_paths():
        raise ActionError("Tela de destino invalida")
    out["then"] = then
    steps = src.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ActionError("Coloque pelo menos um passo no modo")
    if len(steps) > 12:
        raise ActionError("No maximo 12 passos")
    out["steps"] = []
    for st in steps:
        node = sanitize_item(st)
        node.pop("confirm", None)
        ask = str(st.get("ask") or "").strip() if isinstance(st, dict) else ""
        if ask:
            node["ask"] = ask[:80]
        out["steps"].append(node)
    return out


def api_mode_get(body):
    m = find_mode(body.get("id"))
    if not m:
        raise ActionError("Modo nao encontrado")
    steps = []
    for n in m.get("steps") or []:
        f = node_to_form(n)
        f["ask"] = str(n.get("ask") or "")
        steps.append(f)
    return {"mode": {"id": m["id"], "label": m["label"], "logo": m.get("logo") or "", "icon": m.get("icon") or "",
                     "then": m.get("then") or "", "default": m["default"], "edited": m["edited"], "steps": steps},
            "goto": editor_locations() + MODE_GOTO_EXTRA}


def api_mode_save(body):
    mode = sanitize_mode(body.get("mode"))
    mid = str(body.get("id") or "")
    with _CONFIG_LOCK:
        saved = RAW.get("modos_lista")
        if not isinstance(saved, dict):
            saved = RAW["modos_lista"] = {}
        if mid:
            if not find_mode(mid):
                raise ActionError("Modo nao encontrado")
        else:
            base = re.sub(r"[^a-z0-9]+", "-", mode["label"].lower()).strip("-")[:20] or "modo"
            mid, n = base, 2
            while mid in saved or any(d["id"] == mid for d in DEFAULT_MODES):
                mid = "%s-%d" % (base, n)
                n += 1
        saved[mid] = mode
        _save_and_reload()
    return {"id": mid}


def api_mode_delete(body):
    mid = str(body.get("id") or "")
    m = find_mode(mid)
    if not m:
        raise ActionError("Modo nao encontrado")
    with _CONFIG_LOCK:
        saved = RAW.setdefault("modos_lista", {})
        if m["default"]:
            saved[mid] = {"deleted": True}
        else:
            saved.pop(mid, None)
        _save_and_reload()
    return {}


def api_mode_reset(body):
    mid = str(body.get("id") or "")
    if not any(d["id"] == mid for d in DEFAULT_MODES):
        raise ActionError("So os modos padrao podem ser restaurados")
    with _CONFIG_LOCK:
        (RAW.setdefault("modos_lista", {})).pop(mid, None)
        _save_and_reload()
    return {}


CUSTOM_API.update({"mode_get": api_mode_get, "mode_save": api_mode_save, "mode_delete": api_mode_delete,
                   "mode_reset": api_mode_reset})


def seed_defaults():
    """Primeira vez nesta versao: cria botoes de YouTube e WhatsApp Web na tela inicial."""
    if int(RAW.get("seed", 0) or 0) >= 3:
        return
    lst = _custom_list()
    have = json.dumps(lst, ensure_ascii=False).lower()
    for label, url, logo in (("YouTube", "https://www.youtube.com", "youtube"),
                             ("WhatsApp Web", "https://web.whatsapp.com", "whatsapp")):
        if urllib.parse.urlparse(url).hostname.replace("www.", "") in have:
            continue   # voce ja tem um botao desse site
        lst.append({"label": label, "type": "open", "target": url, "logo": logo})
    RAW["seed"] = 3
    save_raw()
    load_config(first_time=True)


def editor_locations():
    out = [{"path": "", "label": "Tela inicial"}]
    for p, label in (("obs", "OBS"), ("steam", "Steam"), ("epic", "Epic Games"), ("jogos", "Jogos"), ("modes", "Modos"),
                     ("browser", "Navegador"), ("windows", "Janelas"), ("system", "Sistema")):
        out.append({"path": p, "label": label})

    def walk(lst, prefix, label_prefix):
        for i, node in enumerate(lst):
            if isinstance(node, dict) and isinstance(node.get("items"), list):
                p = prefix + [str(i)]
                label = label_prefix + str(node.get("label", "Pasta"))
                out.append({"path": "custom/" + "/".join(p), "label": label})
                walk(node["items"], p, label + " › ")

    walk(CFG.get("custom") or [], [], "Pasta: ")
    return out


def editor_options():
    scenes = []
    try:
        data = OBS.request("GetSceneList", backoff=True)
        scenes = [x.get("sceneName") for x in sorted(data.get("scenes") or [], key=lambda x: -(x.get("sceneIndex") or 0))
                  if x.get("sceneName")]
    except ObsError:
        pass
    wins, seen = [], set()
    for w in list_windows():
        if w["exe"] in seen:
            continue
        seen.add(w["exe"])
        wins.append({"exe": w["exe"], "title": w["title"][:60], "path": w.get("path") or "",
                     "img": icon_url(w["path"]) if w.get("path") else ""})
    gone = [{"id": d["id"], "label": d["label"]} for d in DEFAULT_MODES
            if isinstance((CFG.get("modos_lista") or {}).get(d["id"]), dict)
            and (CFG["modos_lista"][d["id"]]).get("deleted")]
    return {"scenes": scenes, "windows": wins, "locations": editor_locations(), "deleted_modes": gone}


def scan_app_shortcuts():
    roots = [(d, 2) for d in desktop_dirs()]
    for var in ("APPDATA", "PROGRAMDATA"):
        if os.environ.get(var):
            roots.append((os.path.join(os.environ[var], "Microsoft", "Windows", "Start Menu", "Programs"), 3))
    seen, out = set(), []
    for base, depth in roots:
        if not os.path.isdir(base):
            continue
        for b, _subs, files in _walk_limited(base, depth):
            for fn in files:
                if not fn.lower().endswith((".lnk", ".url")):
                    continue
                name = os.path.splitext(fn)[0]
                low = name.lower()
                if low in seen or "uninstall" in low or "desinstal" in low:
                    continue
                seen.add(low)
                out.append((name, os.path.join(b, fn)))
    out.sort(key=lambda x: x[0].lower())
    return out[:900]


def editor_apps(q=""):
    q = q.strip().lower()
    items = []
    for name, full in cached(("apps",), 30, scan_app_shortcuts):
        if q and q not in name.lower():
            continue
        aid = hashlib.sha1(full.encode("utf-8", "replace")).hexdigest()[:12]
        EDIT_APPS[aid] = full
        items.append({"id": aid, "name": name, "img": icon_url(full)})
        if len(items) >= 80:
            break
    return {"apps": items}


# =============================================================================
# Chat da live (YouTube: o servidor le o chat; Twitch: o celular le direto)
# =============================================================================
import collections
import urllib.request

CHAT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def chat_clean_twitch(v):
    v = str(v or "").strip()
    m = re.search(r"twitch\.tv/(?:popout/|embed/|moderator/)?([A-Za-z0-9_]{2,25})", v)
    if m:
        v = m.group(1)
    v = v.lstrip("@#")
    return v.lower() if re.fullmatch(r"[A-Za-z0-9_]{2,25}", v) else ""


def chat_clean_youtube(v):
    """Aceita link da live, ID do video (11 letras), @canal, link do canal ou ID UC... Devolve texto limpo ou ''."""
    v = str(v or "").strip()
    if not v:
        return ""
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", v) and not v.startswith("@"):
        return v
    m = re.search(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|live/|embed/|live_chat\?(?:.*&)?v=)|youtu\.be/)([A-Za-z0-9_-]{11})", v)
    if m:
        return m.group(1)
    m = re.search(r"youtube\.com/(@[A-Za-z0-9._%-]{2,60})", v)
    if m:
        return m.group(1)
    m = re.search(r"youtube\.com/channel/(UC[A-Za-z0-9_-]{20,24})", v)
    if m:
        return m.group(1)
    if re.fullmatch(r"@[A-Za-z0-9._%-]{2,60}", v):
        return v
    if re.fullmatch(r"UC[A-Za-z0-9_-]{20,24}", v):
        return v
    if re.fullmatch(r"[A-Za-z0-9._-]{3,60}", v):
        return "@" + v
    return ""


def _yt_get(url, data=None, timeout=12):
    hdrs = {"User-Agent": CHAT_UA, "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8", "Cookie": "SOCS=CAI; CONSENT=YES+1"}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
        data = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def yt_resolve_video(ref, fetch=_yt_get):
    """ref (limpo) -> (video_id, aviso)."""
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", ref) and not ref.startswith("@"):
        return ref, None
    base = "https://www.youtube.com/channel/%s/live" % ref if ref.startswith("UC") else "https://www.youtube.com/%s/live" % ref
    html = fetch(base)
    m = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([A-Za-z0-9_-]{11})"', html)
    if m and '"isLive":true' in html.replace(" ", ""):
        return m.group(1), None
    if m and ("isLiveNow" in html or '"liveBroadcastDetails"' in html):
        return m.group(1), None
    return None, "Sem live no ar nesse canal agora."


def yt_page_info(html):
    """Da pagina live_chat: (api_key, client_version, continuation, acoes_iniciais)."""
    key = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
    ver = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', html) or re.search(r'"clientVersion":"([^"]+)"', html)
    data = None
    m = re.search(r'(?:window\["ytInitialData"\]|var ytInitialData)\s*=\s*(\{.*?\});\s*</script>', html, re.S)
    if m:
        try:
            data = json.loads(m.group(1))
        except ValueError:
            data = None
    cont, acts = None, []
    lc = ((data or {}).get("contents") or {}).get("liveChatRenderer") or {}
    acts = lc.get("actions") or []
    cont = yt_next_continuation(lc)[0]
    return (key.group(1) if key else None, ver.group(1) if ver else "2.20240101.00.00", cont, acts, lc)


def yt_next_continuation(lc):
    """-> (continuation, timeout_ms)"""
    for c in (lc.get("continuations") or []):
        for k in ("invalidationContinuationData", "timedContinuationData", "reloadContinuationData"):
            d = c.get(k)
            if d and d.get("continuation"):
                return d["continuation"], int(d.get("timeoutMs") or 3000)
    return None, 3000


def _yt_runs(msg):
    text, parts = [], []
    for r in (msg or {}).get("runs") or []:
        if "text" in r:
            text.append(r["text"])
            parts.append({"t": r["text"]})
        elif "emoji" in r:
            e = r["emoji"]
            th = ((e.get("image") or {}).get("thumbnails") or [{}])
            sc = (e.get("shortcuts") or [""])[0]
            text.append(sc)
            if th and th[0].get("url"):
                parts.append({"img": th[0]["url"], "t": sc})
            else:
                parts.append({"t": sc})
    return "".join(text), parts


def yt_parse_actions(actions):
    out = []
    for a in actions or []:
        item = ((a.get("addChatItemAction") or {}).get("item")) or {}
        kind = next(iter(item), None)
        r = item.get(kind) or {}
        if kind not in ("liveChatTextMessageRenderer", "liveChatPaidMessageRenderer", "liveChatMembershipItemRenderer"):
            continue
        name = ((r.get("authorName") or {}).get("simpleText")) or "?"
        text, parts = _yt_runs(r.get("message"))
        badges = set()
        for b in r.get("authorBadges") or []:
            t = ((b.get("liveChatAuthorBadgeRenderer") or {}).get("tooltip")) or ""
            tl = t.lower()
            if "owner" in tl or "propriet" in tl:
                badges.add("owner")
            elif "moderator" in tl or "moderador" in tl:
                badges.add("mod")
            elif "member" in tl or "membro" in tl:
                badges.add("member")
        m = {"id": r.get("id") or "", "p": "yt", "u": name, "x": text, "parts": parts, "b": sorted(badges)}
        if kind == "liveChatPaidMessageRenderer":
            m["super"] = ((r.get("purchaseAmountText") or {}).get("simpleText")) or "Super Chat"
        elif kind == "liveChatMembershipItemRenderer":
            m["super"] = "Novo membro"
            if not text:
                m["x"] = "entrou como membro"
                m["parts"] = [{"t": m["x"]}]
        out.append(m)
    return out


class YTChat:
    """Le o chat de uma live do YouTube enquanto o celular estiver pedindo mensagens."""
    def __init__(self, fetch=_yt_get):
        self.fetch = fetch
        self.lock = threading.Lock()
        self.msgs = collections.deque(maxlen=300)
        self.seq = 0
        self.seen = collections.deque(maxlen=800)
        self.ref = ""
        self.status = ""
        self.thread = None
        self.last_poll = 0.0
        self.gen = 0

    def configure(self, ref):
        with self.lock:
            if ref != self.ref:
                self.ref = ref
                self.gen += 1
                self.msgs.clear()
                self.seen.clear()
                self.status = "" if not ref else "Conectando ao chat do YouTube..."

    def _add(self, items):
        with self.lock:
            for m in items:
                key = m.get("id") or (m["u"], m["x"])
                if key in self.seen:
                    continue
                self.seen.append(key)
                self.seq += 1
                self.msgs.append(dict(m, n=self.seq))

    def poll(self, since):
        self.last_poll = time.time()
        with self.lock:
            ref = self.ref
        if ref and (self.thread is None or not self.thread.is_alive()):
            self.thread = threading.Thread(target=self._run, args=(self.gen,), daemon=True, name="yt-chat")
            self.thread.start()
        with self.lock:
            msgs = [m for m in self.msgs if m["n"] > since]
            return {"seq": self.seq, "msgs": msgs, "status": self.status if ref else "", "on": bool(ref)}

    def _stale(self, gen):
        return gen != self.gen or time.time() - self.last_poll > 60

    def _sleep(self, gen, secs):
        end = time.time() + secs
        while time.time() < end:
            if self._stale(gen):
                return False
            time.sleep(0.25)
        return True

    def _run(self, gen):
        while not self._stale(gen):
            try:
                with self.lock:
                    ref = self.ref
                vid, warn = yt_resolve_video(ref, self.fetch)
                if not vid:
                    self.status = warn
                    if not self._sleep(gen, 20):
                        return
                    continue
                html = self.fetch("https://www.youtube.com/live_chat?is_popout=1&v=%s" % vid)
                key, ver, cont, acts, _lc = yt_page_info(html)
                if not key or not cont:
                    self.status = "Esse video nao tem chat ao vivo (ou o chat esta desligado)."
                    if not self._sleep(gen, 20):
                        return
                    continue
                self.status = ""
                self._add(yt_parse_actions(acts)[-30:])
                fails = 0
                while not self._stale(gen) and cont:
                    try:
                        raw = self.fetch("https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?key=%s&prettyPrint=false" % key,
                                         {"context": {"client": {"clientName": "WEB", "clientVersion": ver, "hl": "pt-BR"}},
                                          "continuation": cont})
                        lc = (json.loads(raw).get("continuationContents") or {}).get("liveChatContinuation") or {}
                        self._add(yt_parse_actions(lc.get("actions")))
                        cont, tmo = yt_next_continuation(lc)
                        fails = 0
                        self.status = ""
                    except Exception as e:  # noqa
                        fails += 1
                        tmo = 4000
                        if fails >= 4:
                            self.status = "Chat do YouTube caiu; tentando de novo..."
                            break
                    if not self._sleep(gen, max(1.0, min(tmo, 8000) / 1000.0)):
                        return
                if cont is None and not fails:
                    self.status = "A live terminou."
            except Exception as e:  # noqa
                LOG.info("chat YouTube: %s", e)
                self.status = "Nao consegui ler o chat do YouTube (sem internet?)."
            if not self._sleep(gen, 10):
                return


YT = YTChat()


def chat_config():
    c = CFG.get("chat") if isinstance(CFG.get("chat"), dict) else {}
    return {"youtube": chat_clean_youtube(c.get("youtube")), "twitch": chat_clean_twitch(c.get("twitch"))}


def api_chat_get(_body):
    c = chat_config()
    raw = RAW.get("chat") if isinstance(RAW.get("chat"), dict) else {}
    return {"youtube": c["youtube"], "twitch": c["twitch"], "youtube_raw": str(raw.get("youtube") or "")}


def api_chat_save(body):
    yt_in, tw_in = str(body.get("youtube") or "").strip(), str(body.get("twitch") or "").strip()
    yt, tw = chat_clean_youtube(yt_in), chat_clean_twitch(tw_in)
    if yt_in and not yt:
        raise ActionError("Nao entendi o link/canal do YouTube. Use o link da live, o @canal ou o ID do video.")
    if tw_in and not tw:
        raise ActionError("Nao entendi o canal da Twitch. Use o nome do canal ou o link.")
    with _CONFIG_LOCK:
        RAW["chat"] = {"youtube": yt, "twitch": tw}
        _save_and_reload()
    return {"youtube": yt, "twitch": tw}


def api_chat_poll(since):
    YT.configure(chat_config()["youtube"])
    return YT.poll(since)


CUSTOM_API.update({"chat_get": api_chat_get, "chat_save": api_chat_save})
ACTIONS["chat.open"] = lambda _a: "Chat"


# =============================================================================
# Servidor HTTP
# =============================================================================
FAILS = {}
FAILS_LOCK = threading.Lock()
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
    ".webmanifest": "application/manifest+json", ".json": "application/json",
    ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
}


def login_blocked_for(ip):
    with FAILS_LOCK:
        _, until = FAILS.get(ip, (0, 0.0))
        return max(0, int(until - time.time())) if until > time.time() else 0


def register_fail(ip):
    with FAILS_LOCK:
        n, until = FAILS.get(ip, (0, 0.0))
        n += 1
        if n >= 5:
            FAILS[ip] = (0, time.time() + 60)
        else:
            FAILS[ip] = (n, until)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "StreamDeck/1.0"
    timeout = 60

    def log_message(self, fmt, *args):
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    # ---- helpers
    def _send(self, code, body, ctype, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if "Cache-Control" not in (extra or {}):
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _token_ok(self, tok):
        return bool(tok) and hmac.compare_digest(str(tok), auth_token())

    def _authed(self):
        return self._token_ok(self.headers.get("X-Token", ""))

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 65536:
            return {}
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except ValueError:
            return {}

    def _static(self, name):
        if name in ("", "index"):
            name = "index.html"
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name) or name.startswith("."):
            return self._json(404, {"ok": False, "error": "nao encontrado"})
        fp = os.path.join(WEB_DIR, name)
        ext = os.path.splitext(name)[1].lower()
        if not os.path.isfile(fp) or ext not in CONTENT_TYPES:
            return self._json(404, {"ok": False, "error": "nao encontrado"})
        with open(fp, "rb") as f:
            self._send(200, f.read(), CONTENT_TYPES[ext])

    # ---- rotas
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            u = urllib.parse.urlparse(self.path)
            if u.path.startswith("/api/"):
                maybe_reload_config()
                if u.path == "/api/ping":
                    return self._json(200, {"ok": True, "name": "streamdeck"})
                q = urllib.parse.parse_qs(u.query)
                if not self._authed() and not (u.path == "/api/icon" and self._token_ok((q.get("t") or [""])[0])):
                    return self._json(401, {"ok": False, "error": "login necessario"})
                if u.path == "/api/status":
                    return self._json(200, get_status())
                if u.path == "/api/icon":
                    res = icon_bytes((q.get("id") or [""])[0])
                    if not res:
                        return self._json(404, {"ok": False, "error": "sem icone"})
                    return self._send(200, res[0], res[1], {"Cache-Control": "private, max-age=86400"})
                if u.path == "/api/chat/poll":
                    try:
                        since = int((q.get("since") or ["0"])[0])
                    except ValueError:
                        since = 0
                    return self._json(200, dict(api_chat_poll(since), ok=True))
                if u.path == "/api/editor/options":
                    return self._json(200, editor_options())
                if u.path == "/api/editor/apps":
                    return self._json(200, editor_apps((q.get("q") or [""])[0]))
                if u.path == "/api/folder":
                    q = urllib.parse.parse_qs(u.query)
                    try:
                        return self._json(200, build_folder((q.get("path") or [""])[0], edit=(q.get("edit") or [""])[0] == "1"))
                    except ActionError as e:
                        return self._json(404, {"ok": False, "error": str(e)})
                return self._json(404, {"ok": False, "error": "rota desconhecida"})
            return self._static(u.path.lstrip("/"))
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            LOG.exception("erro no GET %s", self.path)
            try:
                self._json(500, {"ok": False, "error": "erro interno do servidor"})
            except Exception:
                pass

    def do_POST(self):
        try:
            u = urllib.parse.urlparse(self.path)
            maybe_reload_config()
            body = self._body()
            ip = self.client_address[0]
            if u.path == "/api/login":
                wait = login_blocked_for(ip)
                if wait:
                    return self._json(429, {"ok": False, "error": "Muitas tentativas. Aguarde %ds." % wait})
                pin = str(body.get("pin", ""))
                if hmac.compare_digest(pin.encode("utf-8"), str(CFG["pin"]).encode("utf-8")):
                    with FAILS_LOCK:
                        FAILS.pop(ip, None)
                    return self._json(200, {"ok": True, "token": auth_token()})
                register_fail(ip)
                return self._json(403, {"ok": False, "error": "PIN incorreto"})
            if not self._authed():
                return self._json(401, {"ok": False, "error": "login necessario"})
            if u.path.startswith("/api/custom/"):
                fn = CUSTOM_API.get(u.path.rsplit("/", 1)[1])
                if fn is None:
                    return self._json(404, {"ok": False, "error": "rota desconhecida"})
                try:
                    res = fn(body)
                    return self._json(200, dict(res, ok=True))
                except (ActionError, ValueError) as e:
                    return self._json(400, {"ok": False, "error": str(e)})
                except OSError as e:
                    return self._json(500, {"ok": False, "error": "Nao consegui salvar o config.json: %s" % e})
            if u.path == "/api/run":
                action = body.get("action")
                fn = ACTIONS.get(action) if isinstance(action, str) else None
                if fn is None:
                    return self._json(400, {"ok": False, "error": "acao desconhecida"})
                try:
                    msg = fn(body.get("arg"))
                    return self._json(200, {"ok": True, "msg": msg or "OK"})
                except (ActionError, ObsError) as e:
                    return self._json(400, {"ok": False, "error": str(e)})
                except OSError as e:
                    LOG.warning("falha na acao %s: %s", action, e)
                    return self._json(400, {"ok": False, "error": "Nao foi possivel executar: %s" % e})
                except Exception as e:
                    LOG.exception("erro na acao %s", action)
                    return self._json(500, {"ok": False, "error": "Erro interno: %s" % e})
            return self._json(404, {"ok": False, "error": "rota desconhecida"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            LOG.exception("erro no POST %s", self.path)
            try:
                self._json(500, {"ok": False, "error": "erro interno do servidor"})
            except Exception:
                pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = not IS_WIN  # no Windows, reuse permitiria 2 servidores na mesma porta


def make_server(port=None, host="0.0.0.0"):
    return Server((host, int(port if port is not None else CFG["port"])), Handler)


def local_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return ips


def setup_logging():
    LOG.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    try:
        fh = logging.handlers.RotatingFileHandler(os.path.join(APP_DIR, "streamdeck.log"),
                                                  maxBytes=500000, backupCount=1, encoding="utf-8")
        fh.setFormatter(fmt)
        LOG.addHandler(fh)
    except OSError:
        pass
    if sys.stdout is not None:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        LOG.addHandler(sh)


def diagnose():
    L = []
    add = L.append
    add("STREAM DECK - DIAGNOSTICO (%s)" % time.strftime("%Y-%m-%d %H:%M:%S"))
    add("Python %s | Windows: %s" % (sys.version.split()[0], "sim" if IS_WIN else "nao"))
    add("Pasta: %s" % APP_DIR)
    add("config.json: %s" % ("encontrado" if os.path.exists(CONFIG_PATH) else "nao existe (usando padroes)"))
    add("")
    add("[OBS]")
    exe = find_obs_exe()
    add("  programa: %s" % (exe or "NAO ENCONTRADO (defina obs.path no config.json)"))
    add("  aberto agora: %s" % ("sim" if obs_running_now() else "nao"))
    path, data = obs_ws_file()
    if path:
        pw = data.get("server_password") or ""
        add("  arquivo do WebSocket: %s" % path)
        add("    server_enabled=%s  server_port=%s  auth_required=%s  senha=%s" % (
            data.get("server_enabled"), data.get("server_port"), data.get("auth_required"),
            ("definida (%d caracteres)" % len(pw)) if pw else "vazia"))
    else:
        add("  arquivo do WebSocket: nao encontrado (procurado em: %s)" % ("; ".join(obs_ws_files()) or "-"))
    add("  portas testadas: %s" % ", ".join(sorted({str(p) for _, p, _ in obs_endpoints()})))
    try:
        OBS.close()
        OBS.down_until = 0.0
        v = OBS.request("GetVersion")
        add("  conexao: OK (OBS %s, WebSocket %s)" % (v.get("obsVersion"), v.get("obsWebSocketVersion")))
    except ObsError as e:
        add("  conexao: FALHOU - %s" % e)
    add("")
    add("[STEAM]")
    root = steam_root()
    if root:
        add("  pasta: %s" % root)
        add("  bibliotecas: %s" % "; ".join(steam_libraries(root)))
        add("  jogos instalados: %d" % len(steam_games() or []))
    else:
        add("  NAO ENCONTRADA (defina steam.path no config.json)")
    add("")
    add("[EPIC]")
    games, info = _scan_epic_sources(full=True)
    add("  pasta Epic em ProgramData: %s" % (_epic_root() if os.path.isdir(_epic_root()) else "NAO EXISTE"))
    for d, n in info["manifests"]:
        add("  manifests: %s -> %d jogo(s)" % (d, n))
    if not info["manifests"]:
        add("  manifests: nenhuma pasta encontrada")
    for p, n in info["dats"]:
        add("  LauncherInstalled.dat: %s -> %d jogo(s)" % (p, n))
    for lib, n in info["libs"]:
        add("  pasta de jogos: %s -> %d jogo(s)" % (lib, n))
        try:
            add("    subpastas: %s" % ", ".join(sorted(os.listdir(lib))[:25]))
        except OSError:
            pass
    if not info["libs"]:
        add("  pasta de jogos Epic: nenhuma encontrada (use epic.libraries no config.json)")
    add("  atalhos .url da Epic: %d (em %d pasta(s))" % (info["shortcuts"], len(info["shortcut_dirs"])))
    add("  pastas com 'epic' no nome:")
    for var in ("PROGRAMDATA", "LOCALAPPDATA", "APPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(var)
        try:
            hits = [n for n in os.listdir(base) if "epic" in n.lower()] if base else []
        except OSError:
            hits = []
        if hits:
            add("    %s: %s" % (base, ", ".join(hits)))
    add("  jogos detectados: %d" % len(games))
    for g in games[:40]:
        add("    - %s" % g["name"])
    for label, base in (("ProgramData\\Epic", _epic_root()),
                        ("LocalAppData\\EpicGamesLauncher", os.path.join(os.environ.get("LOCALAPPDATA", ""), "EpicGamesLauncher"))):
        if base and os.path.isdir(base):
            add("  pastas em %s:" % label)
            for b, subs, _f in _walk_limited(base, 2):
                if b != base:
                    add("    %s" % os.path.relpath(b, base))
    add("")
    add("[JOGOS - pasta JOGOS da area de trabalho]")
    jd = jogos_dirs()
    add("  desktops: %s" % ("; ".join(desktop_dirs()) or "nenhum"))
    if jd:
        for d in jd:
            try:
                n = len([x for x in os.listdir(d) if x.lower().endswith(SHORTCUT_EXT)])
            except OSError:
                n = 0
            add("  pasta: %s -> %d atalho(s) na raiz" % (d, n))
    else:
        add("  NENHUMA pasta JOGOS/Games encontrada nos desktops acima")
    add("")
    add("[ICONES REAIS]")
    if IS_WIN:
        test = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "explorer.exe")
        out = os.path.join(ICON_CACHE_DIR, "_teste.png")
        try:
            os.makedirs(ICON_CACHE_DIR, exist_ok=True)
        except OSError:
            pass
        ok, msg = extract_icon(test, out)
        add("  extrair icone de explorer.exe: %s %s" % ("OK" if ok else "FALHOU", msg))
        if ok:
            add("  tamanho do PNG: %d bytes" % os.path.getsize(out))
    else:
        add("  (so no Windows)")
    add("")
    add("[JANELAS ABERTAS RELEVANTES]")
    found = False
    for w in list_windows():
        t = (w["exe"] + " " + w["title"]).lower()
        if any(k in t for k in ("obs", "steam", "epic")):
            add("  exe=%s | titulo=%s | caminho=%s" % (w["exe"], w["title"][:60], w.get("path")))
            found = True
    if not found:
        add("  (nenhuma de OBS/Steam/Epic aberta)")
    return L


def run_diag_cli():
    try:
        load_config(first_time=True)
    except (ValueError, OSError) as e:
        print("config.json invalido: %s" % e)
        CFG.clear()
        CFG.update(deep_merge(DEFAULT_CONFIG, {}))
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    text = "\n".join(diagnose())
    print(text)
    try:
        with open(os.path.join(APP_DIR, "diagnostico.txt"), "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print("\n(Resultado salvo em diagnostico.txt)")
    except OSError:
        pass


def main():
    if "--diag" in sys.argv:
        run_diag_cli()
        return
    setup_logging()
    if IS_WIN:
        try:
            ctypes.windll.kernel32.SetConsoleTitleW(WINDOW_TITLE)
        except Exception:
            pass
    try:
        load_config(first_time=True)
    except (ValueError, OSError) as e:
        LOG.error("Nao consegui ler o config.json: %s", e)
        LOG.error("Dica: em caminhos do Windows use barra normal (C:/Pasta/app.exe) ou barra dupla (C:\\\\Pasta\\\\app.exe).")
        sys.exit(1)
    ensure_credentials()
    try:
        seed_defaults()
    except OSError as e:
        LOG.warning("nao consegui criar os botoes iniciais: %s", e)
    try:
        srv = make_server()
    except OSError as e:
        LOG.error("Nao consegui abrir a porta %s: %s", CFG["port"], e)
        LOG.error("Ja existe outro Stream Deck aberto? Feche-o ou mude 'port' no config.json.")
        sys.exit(1)
    port = CFG["port"]
    LOG.info("=" * 56)
    LOG.info(" STREAM DECK DO CELULAR - rodando")
    LOG.info("=" * 56)
    LOG.info(" No celular (mesmo Wi-Fi), abra um destes enderecos:")
    for ip in local_ips() or ["IP-DO-PC"]:
        LOG.info("     http://%s:%s", ip, port)
    LOG.info(" PIN de acesso: %s", CFG["pin"])
    LOG.info(" (para mudar o PIN, edite config.json)")
    LOG.info(" Deixe esta janela aberta. Ctrl+C para encerrar.")
    LOG.info("=" * 56)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        OBS.close()
        srv.server_close()


if __name__ == "__main__":
    main()
