"""
Redragon K596 (and similar) G-key -> F13..F22 Bridge
=====================================================

Turns a keyboard's dedicated macro/G-keys into genuinely independent,
uniquely-identifiable key inputs (F13..F22 by default) that any
application (Discord, OBS, etc.) can bind to directly -- without the
side effects of remapped combos (e.g. Ctrl+Shift+F1) leaking real
modifier keys, and without colliding with ordinary keys.

BACKGROUND / HOW THIS WORKS
----------------------------
Many Redragon keyboards expose their G-keys not as ordinary HID
keyboard keys, but through a vendor-defined HID collection (in
addition to the standard keyboard/consumer-control collections). On
a Redragon VISHNU K596RGB, that vendor collection is usage page
0xFF19 / usage 0xFF19, and each G-key press produces a small report
that looks like:

    byte0 = 0x08   (report id)
    byte1 = 0x11   (event subtype -- observed constant for G-key events)
    byte2 = 0xC0 + (G-key number - 1)   e.g. G1=0xC0, G2=0xC1 ... G10=0xC9

This fires once per physical press (not a down/up pair, and not
repeated on hold), *regardless* of what action Redragon's own
software has assigned to that key -- as long as the key isn't
completely disabled ("Button Off"). This script listens for that
raw HID report directly via the Windows Raw Input API and, on each
qualifying report, synthesizes a clean, real keypress via SendInput
-- entirely independent of Redragon's software.

This was reverse-engineered against one specific unit/firmware
revision. Other Redragon models (or other firmware revisions of the
same model) may use a different report id, subtype byte, or base
offset. All of these are configurable via command-line flags -- see
"Adapting this for a different keyboard" in the README.

REQUIRED SETUP IN REDRAGON'S SOFTWARE
---------------------------------------
Set every G-key you want to use to "Assign a string" with a harmless
one-character placeholder (a backtick, or a digit, works well). This
mode is what makes the vendor HID report fire in the first place.
Do NOT leave the field empty -- on the tested unit, an empty string
silently reverts to whatever was previously saved rather than saving.

Note: this placeholder character will still be typed into whatever
window currently has keyboard focus when you press the key. This is
delivered as a direct text-insertion message to the focused window,
not a real keystroke, so it cannot be globally suppressed by this
script. A space can trigger "scroll page down" in some web views --
picking an inert character avoids that. In practice this is a
non-issue whenever you're not focused in a text field when pressing
a G-key.

USAGE
-----
    python k596_gkey_bridge.py                  # run normally (quiet)
    python k596_gkey_bridge.py --debug           # verbose, see every event
    python k596_gkey_bridge.py --start-key 0x91  # remap to Scroll Lock.. instead of F13..
                                                   # (see --help for all options)

Exit: Ctrl+C, or type 'q' + Enter.
"""

import argparse
import ctypes
from ctypes import wintypes
import struct
import sys
import time
import threading
import datetime

# ---------------------------------------------------------------------------
# ctypes.wintypes compatibility shims (some Python builds are missing these)
# ---------------------------------------------------------------------------
if not hasattr(wintypes, "LRESULT"):
    wintypes.LRESULT = ctypes.c_int64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_int32
if not hasattr(wintypes, "LPARAM"):
    wintypes.LPARAM = ctypes.c_ssize_t
if not hasattr(wintypes, "WPARAM"):
    wintypes.WPARAM = ctypes.c_size_t
if not hasattr(wintypes, "HRAWINPUT"):
    wintypes.HRAWINPUT = wintypes.HANDLE
if not hasattr(wintypes, "ATOM"):
    wintypes.ATOM = wintypes.WORD

if sys.platform != "win32":
    sys.exit("This script uses the Windows API (ctypes.WinDLL) and only runs on Windows.")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ---------------------------------------------------------------------------
# Windows constants
# ---------------------------------------------------------------------------
WM_INPUT = 0x00FF
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_QUIT = 0x0012

RIDEV_INPUTSINK = 0x00000100
RID_INPUT = 0x10000003
RIDI_DEVICENAME = 0x20000007
RIM_TYPEHID = 2

WS_OVERLAPPED = 0x00000000
HWND_MESSAGE = ctypes.c_void_p(-3)
CS_HREDRAW = 0x0002
CS_VREDRAW = 0x0001
CTRL_C_EVENT = 0
CTRL_BREAK_EVENT = 1
CTRL_CLOSE_EVENT = 2

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002

VK_F13 = 0x7C  # F13..F24 are contiguous virtual-key codes: 0x7C..0x87

# ---------------------------------------------------------------------------
# Structures
# ---------------------------------------------------------------------------

class WNDCLASSEX(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT), ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p), ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON),
    ]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [
        ("usUsagePage", ctypes.c_ushort), ("usUsage", ctypes.c_ushort),
        ("dwFlags", wintypes.DWORD), ("hwndTarget", wintypes.HWND),
    ]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [
        ("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD),
        ("hDevice", wintypes.HANDLE), ("wParam", wintypes.WPARAM),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long), ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_ushort), ("wParamH", ctypes.c_ushort),
    ]


class INPUT(ctypes.Structure):
    class _I(ctypes.Union):
        # IMPORTANT: the real Windows INPUT union includes all three
        # variants. MOUSEINPUT is the largest member (32 bytes on 64-bit).
        # Defining the union with only KEYBDINPUT makes ctypes.sizeof(INPUT)
        # come out smaller than the real struct (32 vs 40 bytes on 64-bit),
        # and SendInput silently fails 100% of the time if cbSize doesn't
        # match exactly. This bit us during development -- don't "simplify"
        # this union to just the keyboard member.
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]
    _anonymous_ = ("_i",)
    _fields_ = [("type", ctypes.c_ulong), ("_i", _I)]


WNDPROCTYPE = ctypes.WINFUNCTYPE(
    wintypes.LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)

# ---------------------------------------------------------------------------
# WinAPI prototypes (explicit argtypes/restype -- required for 64-bit safety;
# without these ctypes guesses 32-bit ints for pointer-sized parameters and
# crashes with OverflowError on real pointer-carrying messages)
# ---------------------------------------------------------------------------
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = wintypes.LRESULT
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostQuitMessage.restype = None
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEX)]
user32.RegisterClassExW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RAWINPUTDEVICE), wintypes.UINT, wintypes.UINT]
user32.RegisterRawInputDevices.restype = wintypes.BOOL
user32.GetRawInputData.argtypes = [
    wintypes.HRAWINPUT, wintypes.UINT, ctypes.c_void_p, ctypes.POINTER(wintypes.UINT), wintypes.UINT
]
user32.GetRawInputData.restype = wintypes.UINT
user32.GetRawInputDeviceInfoW.argtypes = [
    wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p, ctypes.POINTER(wintypes.UINT)
]
user32.GetRawInputDeviceInfoW.restype = ctypes.c_int
user32.PeekMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT
]
user32.PeekMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.TranslateMessage.restype = wintypes.BOOL
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = wintypes.LRESULT
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE

# ---------------------------------------------------------------------------
# Runtime config (populated from argparse in main())
# ---------------------------------------------------------------------------
class Config:
    usage_page = 0xFF19
    usage = 0xFF19
    report_id = 0x08
    subtype = 0x11
    base = 0xC0
    count = 10
    start_vk = VK_F13
    debounce_s = 0.12
    hold_ms = 20
    debug = False


cfg = Config()

# ---------------------------------------------------------------------------
# Shutdown coordination
# ---------------------------------------------------------------------------
stop_event = threading.Event()
_hwnd_holder = {"hwnd": None}


def request_stop():
    stop_event.set()
    hwnd = _hwnd_holder.get("hwnd")
    if hwnd:
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


PHANDLER_ROUTINE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)


def _console_ctrl_handler(ctrl_type):
    if ctrl_type in (CTRL_C_EVENT, CTRL_BREAK_EVENT, CTRL_CLOSE_EVENT):
        print("\n[!] Shutdown signal received, closing...")
        request_stop()
        return True
    return False


_console_handler_ref = PHANDLER_ROUTINE(_console_ctrl_handler)
kernel32.SetConsoleCtrlHandler.argtypes = [PHANDLER_ROUTINE, wintypes.BOOL]
kernel32.SetConsoleCtrlHandler.restype = wintypes.BOOL
kernel32.SetConsoleCtrlHandler(_console_handler_ref, True)


def _stdin_watcher():
    while not stop_event.is_set():
        try:
            line = sys.stdin.readline()
        except Exception:
            return
        if not line:
            return
        if line.strip().lower() == "q":
            print("[!] 'q' received, closing...")
            request_stop()
            return


# ---------------------------------------------------------------------------
# Key synthesis
# ---------------------------------------------------------------------------

def _send_key_event(vk, key_up):
    extra = ctypes.pointer(ctypes.c_ulong(0))
    ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if key_up else 0, 0, extra)
    inp = INPUT(type=INPUT_KEYBOARD, ki=ki)
    ctypes.set_last_error(0)
    sent = user32.SendInput(1, ctypes.pointer(inp), ctypes.sizeof(INPUT))
    if sent != 1:
        err = ctypes.get_last_error()
        print(f"[!] SendInput failed: returned={sent} GetLastError={err} "
              f"({ctypes.WinError(err).strerror if err else 'no error code set'})")
    return sent


def _tap_key(vk):
    _send_key_event(vk, key_up=False)
    if cfg.debug:
        state = user32.GetAsyncKeyState(vk)
        seen_down = bool(state & 0x8000)
        print(f"    [verify] GetAsyncKeyState after keydown: "
              f"{'DOWN (OS registered it)' if seen_down else 'NOT DOWN (!) - injection is not reaching the system'}")
    time.sleep(cfg.hold_ms / 1000.0)
    _send_key_event(vk, key_up=True)


_last_fire = {}  # g_index -> last fire time, for debounce


def handle_gkey_report(hid_data: bytes, source_tag: str):
    if len(hid_data) < 3:
        return
    if hid_data[0] != cfg.report_id or hid_data[1] != cfg.subtype:
        return

    g_index = hid_data[2] - cfg.base
    if not (0 <= g_index < cfg.count):
        return

    now = time.monotonic()
    last = _last_fire.get(g_index, 0)
    if now - last < cfg.debounce_s:
        return
    _last_fire[g_index] = now

    vk = cfg.start_vk + g_index

    if cfg.debug:
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"[{ts}]{source_tag} G{g_index + 1} -> vk=0x{vk:02X}")

    threading.Thread(target=_tap_key, args=(vk,), daemon=True).start()


# ---------------------------------------------------------------------------
# Raw input plumbing
# ---------------------------------------------------------------------------

def get_device_name(hDevice):
    size = wintypes.UINT(0)
    user32.GetRawInputDeviceInfoW(hDevice, RIDI_DEVICENAME, None, ctypes.byref(size))
    if size.value == 0:
        return ""
    buf = ctypes.create_unicode_buffer(size.value)
    user32.GetRawInputDeviceInfoW(hDevice, RIDI_DEVICENAME, buf, ctypes.byref(size))
    return buf.value


def handle_wm_input(lparam):
    size = wintypes.UINT(0)
    header_size = ctypes.sizeof(RAWINPUTHEADER)

    user32.GetRawInputData(ctypes.c_void_p(lparam), RID_INPUT, None, ctypes.byref(size), header_size)
    if size.value == 0:
        return

    buf = ctypes.create_string_buffer(size.value)
    ret = user32.GetRawInputData(ctypes.c_void_p(lparam), RID_INPUT, buf, ctypes.byref(size), header_size)
    if ret != size.value:
        return

    raw_bytes = buf.raw
    header = RAWINPUTHEADER.from_buffer_copy(raw_bytes[:header_size])
    if header.dwType != RIM_TYPEHID:
        return

    offset = header_size
    dwSizeHid, dwCount = struct.unpack_from("<II", raw_bytes, offset)
    offset += 8
    hid_data = raw_bytes[offset: offset + dwSizeHid * dwCount]

    tag = ""
    if cfg.debug:
        device_name = get_device_name(header.hDevice)
        if "Col05" in device_name:
            tag = " [Col05]"
        elif "Col06" in device_name:
            tag = " [Col06]"
        elif device_name:
            tag = f" [{device_name}]"

    handle_gkey_report(hid_data, tag)


def wnd_proc(hwnd, msg, wparam, lparam):
    if msg == WM_INPUT:
        try:
            handle_wm_input(lparam)
        except Exception as e:
            print(f"[!] Error parsing WM_INPUT: {e}")
        return 0
    elif msg in (WM_DESTROY, WM_CLOSE):
        user32.PostQuitMessage(0)
        return 0
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def create_message_window():
    hInstance = kernel32.GetModuleHandleW(None)
    class_name = "K596GKeyBridgeWndClass"
    wnd_proc_c = WNDPROCTYPE(wnd_proc)

    wc = WNDCLASSEX()
    wc.cbSize = ctypes.sizeof(WNDCLASSEX)
    wc.style = CS_HREDRAW | CS_VREDRAW
    wc.lpfnWndProc = ctypes.cast(wnd_proc_c, ctypes.c_void_p)
    wc.hInstance = hInstance
    wc.lpszClassName = class_name

    atom = user32.RegisterClassExW(ctypes.byref(wc))
    if not atom:
        raise ctypes.WinError(ctypes.get_last_error())

    hwnd = user32.CreateWindowExW(
        0, class_name, "K596 G-key Bridge", WS_OVERLAPPED,
        0, 0, 0, 0, HWND_MESSAGE, None, hInstance, None,
    )
    if not hwnd:
        raise ctypes.WinError(ctypes.get_last_error())

    create_message_window._wnd_proc_c = wnd_proc_c
    create_message_window._wc = wc
    return hwnd


def register_raw_input(hwnd):
    devices = (RAWINPUTDEVICE * 1)()
    devices[0].usUsagePage = cfg.usage_page
    devices[0].usUsage = cfg.usage
    devices[0].dwFlags = RIDEV_INPUTSINK
    devices[0].hwndTarget = hwnd

    ok = user32.RegisterRawInputDevices(devices, 1, ctypes.sizeof(RAWINPUTDEVICE))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())


def _auto_int(s):
    """Parse an int from decimal or 0x-hex on the command line."""
    return int(s, 0)


def parse_args():
    p = argparse.ArgumentParser(
        description="Bridge a Redragon K596 (or similar) keyboard's G-keys to independent F13..F22 key events.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--debug", action="store_true",
                   help="Print every detected G-key event and verify injection via GetAsyncKeyState.")
    p.add_argument("--usage-page", type=_auto_int, default=Config.usage_page,
                   help="Vendor HID usage page to listen on.")
    p.add_argument("--usage", type=_auto_int, default=Config.usage,
                   help="Vendor HID usage to listen on.")
    p.add_argument("--report-id", type=_auto_int, default=Config.report_id,
                   help="Expected report byte 0 for a G-key event.")
    p.add_argument("--subtype", type=_auto_int, default=Config.subtype,
                   help="Expected report byte 1 for a G-key event.")
    p.add_argument("--base", type=_auto_int, default=Config.base,
                   help="Report byte 2 value corresponding to G1. Subsequent G-keys increment from here.")
    p.add_argument("--count", type=int, default=Config.count,
                   help="Number of G-keys to recognize (G1..G<count>).")
    p.add_argument("--start-key", dest="start_vk", type=_auto_int, default=Config.start_vk,
                   help="Virtual-key code to map G1 to (subsequent G-keys increment from here). "
                        "Default is F13 (0x7C). Example: 0x91 for Scroll Lock.")
    p.add_argument("--debounce-ms", type=float, default=Config.debounce_s * 1000,
                   help="Minimum time between accepted events for the same G-key.")
    p.add_argument("--hold-ms", type=float, default=Config.hold_ms,
                   help="How long to hold the synthesized key down before releasing it.")
    return p.parse_args()


def main():
    args = parse_args()
    cfg.usage_page = args.usage_page
    cfg.usage = args.usage
    cfg.report_id = args.report_id
    cfg.subtype = args.subtype
    cfg.base = args.base
    cfg.count = args.count
    cfg.start_vk = args.start_vk
    cfg.debounce_s = args.debounce_ms / 1000.0
    cfg.hold_ms = args.hold_ms
    cfg.debug = args.debug

    if cfg.debug:
        print("K596 G-key bridge (debug mode)")
        print(f"  usage page/usage : 0x{cfg.usage_page:04X} / 0x{cfg.usage:04X}")
        print(f"  report signature : id=0x{cfg.report_id:02X} subtype=0x{cfg.subtype:02X} base=0x{cfg.base:02X} count={cfg.count}")
        print(f"  output keys      : vk 0x{cfg.start_vk:02X}..0x{cfg.start_vk + cfg.count - 1:02X}")
        print(f"  sizeof(INPUT)    : {ctypes.sizeof(INPUT)} bytes (expect 40 on 64-bit, 28 on 32-bit)")
        print("Exit: Ctrl+C, or type 'q' + Enter.\n")

    hwnd = create_message_window()
    _hwnd_holder["hwnd"] = hwnd
    register_raw_input(hwnd)

    watcher = threading.Thread(target=_stdin_watcher, daemon=True)
    watcher.start()

    msg = wintypes.MSG()
    try:
        while not stop_event.is_set():
            has_msg = user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1)
            if has_msg:
                if msg.message == WM_QUIT:
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            else:
                time.sleep(0.01)
    except KeyboardInterrupt:
        if cfg.debug:
            print("\n[!] KeyboardInterrupt, closing...")
    finally:
        if cfg.debug:
            print("Shutting down.")
        try:
            user32.DestroyWindow(hwnd)
        except Exception:
            pass


if __name__ == "__main__":
    main()
