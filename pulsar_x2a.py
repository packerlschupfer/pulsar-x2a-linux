#!/usr/bin/env python3
"""
pulsar_x2a.py - Linux configuration tool for the Pulsar X2A Medium Wired mouse

USB protocol reverse-engineered from Wireshark captures of Pulsar Fusion on Windows.
Interface 3, Feature report (wValue=0x0300), 64-byte packets.

Packet format:
  [0]     direction: 0x00=CMD (host→device), 0x01=RSP (device→host)
  [1]     command category
  [2]     register (bit7=0: write, bit7=1: read)
  [3]     sub-register
  [4-5]   always 0x00
  [6]     profile: 0x00=global, 0x01-0x05=profile 1-5
  [7-61]  payload
  [62-63] checksum: little-endian uint16 of sum(bytes[0:62])

Global settings (profile=0): polling rate, debounce, angle snap, ripple, motion sync
Per-profile settings (profile=1-5): DPI stages, LOD, brightness, LED effect, button bindings
"""

import sys
import struct
import argparse
import usb.core
import usb.util

# ── Device constants ─────────────────────────────────────────────────────────
VID          = 0x3710
PID          = 0x1404
INTERFACE    = 3
REPORT_SIZE  = 64
WVALUE       = 0x0300   # HID Feature report, report ID 0

# ── Encoding tables ──────────────────────────────────────────────────────────
POLL_HZ_TO_VAL = {125: 1, 250: 2, 500: 4, 1000: 8}
POLL_VAL_TO_HZ = {v: k for k, v in POLL_HZ_TO_VAL.items()}

LOD_MM_TO_VAL  = {1: 0, 2: 1}
LOD_VAL_TO_MM  = {v: k for k, v in LOD_MM_TO_VAL.items()}

LED_NAME_TO_VAL = {'off': 0, 'steady': 1, 'breath': 2}
LED_VAL_TO_NAME = {v: k for k, v in LED_NAME_TO_VAL.items()}

# ── Button constants ──────────────────────────────────────────────────────────

# Physical button IDs
BUTTONS = {
    'left':   0x01,
    'right':  0x02,
    'wheel':  0x03,
    'thumb1': 0x04,   # left thumb front (default: forward)
    'thumb2': 0x05,   # left thumb back  (default: backward)
    'thumb3': 0x06,   # right thumb front (default: forward)
    'thumb4': 0x07,   # right thumb back  (default: backward)
    'dpi':    0x0b,
}
BUTTON_ID_TO_NAME = {v: k for k, v in BUTTONS.items()}

# Button function types
BTN_TYPE_MOUSE     = 0x01   # standard mouse click
BTN_TYPE_KEYBOARD  = 0x02   # keyboard shortcut
BTN_TYPE_SCROLL    = 0x03   # scroll wheel
BTN_TYPE_PROFILE   = 0x08   # profile switch
BTN_TYPE_DPI       = 0x09   # DPI function
BTN_TYPE_MEDIA     = 0x0d   # HID Consumer Control (multimedia)
BTN_TYPE_XCLICK    = 0x0e   # double-click

# Mouse click actions (type 0x01)
MOUSE_ACTIONS = {
    'left': 0x01, 'right': 0x02, 'wheel': 0x03,
    'forward': 0x04, 'backward': 0x05,
}
MOUSE_ACTION_NAMES = {v: k for k, v in MOUSE_ACTIONS.items()}

# DPI actions (type 0x09)
DPI_ACTIONS = {'dpi+': 0x01, 'dpi-': 0x02, 'dpiloop': 0x03}
DPI_ACTION_NAMES = {v: k for k, v in DPI_ACTIONS.items()}

# Profile actions (type 0x08)
PROFILE_ACTIONS = {'profile+': 0x03, 'profile-': 0x04}
PROFILE_ACTION_NAMES = {v: k for k, v in PROFILE_ACTIONS.items()}

# Scroll actions (type 0x03)
SCROLL_ACTIONS = {'scrollup': 0x01, 'scrolldown': 0xff}
SCROLL_ACTION_NAMES = {0x01: 'scrollup', 0xff: 'scrolldown'}

# HID Consumer Usage codes for multimedia (type 0x0d)
MEDIA_CODES = {
    'play':       0x00cd,
    'next':       0x00b5,
    'prev':       0x00b6,
    'stop':       0x00b7,
    'mute':       0x00e2,
    'vol+':       0x00e9,
    'vol-':       0x00ea,
    'mediaplayer':0x0183,
}
MEDIA_CODE_NAMES = {v: k for k, v in MEDIA_CODES.items()}

# HID keyboard modifier bits (type 0x02, byte[9])
HID_MODS = {
    'ctrl': 0x01, 'lctrl': 0x01,
    'shift': 0x02, 'lshift': 0x02,
    'alt': 0x04, 'lalt': 0x04,
    'gui': 0x08, 'super': 0x08, 'win': 0x08,
    'rctrl': 0x10, 'rshift': 0x20, 'ralt': 0x40, 'rgui': 0x80,
}

# HID keyboard usage codes (type 0x02, byte[10]) — letters a-z, digits, common keys
_alpha = {chr(ord('a') + i): 0x04 + i for i in range(26)}
_digits = {'1': 0x1e, '2': 0x1f, '3': 0x20, '4': 0x21, '5': 0x22,
           '6': 0x23, '7': 0x24, '8': 0x25, '9': 0x26, '0': 0x27}
_special = {
    'enter': 0x28, 'esc': 0x29, 'backspace': 0x2a, 'tab': 0x2b, 'space': 0x2c,
    'minus': 0x2d, 'equal': 0x2e, 'lbracket': 0x2f, 'rbracket': 0x30,
    'backslash': 0x31, 'semicolon': 0x33, 'quote': 0x34, 'grave': 0x35,
    'comma': 0x36, 'dot': 0x37, 'slash': 0x38, 'capslock': 0x39,
    'f1': 0x3a, 'f2': 0x3b, 'f3': 0x3c, 'f4': 0x3d, 'f5': 0x3e,
    'f6': 0x3f, 'f7': 0x40, 'f8': 0x41, 'f9': 0x42, 'f10': 0x43,
    'f11': 0x44, 'f12': 0x45, 'delete': 0x4c, 'home': 0x4a, 'end': 0x4d,
    'pageup': 0x4b, 'pagedown': 0x4e, 'right': 0x4f, 'left': 0x50,
    'down': 0x51, 'up': 0x52, 'insert': 0x49,
}
HID_KEYS = {**_alpha, **_digits, **_special}

# ── Low-level helpers ─────────────────────────────────────────────────────────

def _checksum(data: bytes) -> bytes:
    """Return 2-byte little-endian checksum of first 62 bytes."""
    return struct.pack('<H', sum(data[:62]) & 0xFFFF)


def _build(cat, reg, sub, profile, payload=()):
    """Build a 64-byte command packet."""
    buf = bytearray(64)
    buf[0] = 0x00       # direction: CMD
    buf[1] = cat
    buf[2] = reg        # bit7=0 for write
    buf[3] = sub
    buf[4] = 0x00
    buf[5] = 0x00
    buf[6] = profile
    for i, b in enumerate(payload):
        buf[7 + i] = b
    cs = _checksum(buf)
    buf[62] = cs[0]
    buf[63] = cs[1]
    return bytes(buf)


def _build_read(cat, reg, sub, profile, payload=()):
    """Build a read request (bit7 set in reg)."""
    return _build(cat, reg | 0x80, sub, profile, payload)


def _set_report(dev, data):
    dev.ctrl_transfer(0x21, 0x09, WVALUE, INTERFACE, data)


def _get_report(dev) -> bytes:
    return bytes(dev.ctrl_transfer(0xA1, 0x01, WVALUE, INTERFACE, REPORT_SIZE))


def _cmd(dev, cat, reg, sub, profile, payload=()):
    """Send a write command and read back the acknowledgement."""
    _set_report(dev, _build(cat, reg, sub, profile, payload))
    rsp = _get_report(dev)
    # Device acks writes with 0x01 (most commands) or 0x02 (button bindings)
    if rsp[0] not in (0x01, 0x02):
        raise IOError(f"Unexpected response byte: 0x{rsp[0]:02x}")
    return rsp


def _read(dev, cat, reg, sub, profile, payload=()):
    """Send a read request and return the response payload."""
    _set_report(dev, _build_read(cat, reg, sub, profile, payload))
    rsp = _get_report(dev)
    if rsp[0] != 0x01:
        raise IOError(f"Bad response direction byte: 0x{rsp[0]:02x}")
    return rsp

# ── Device open/close ─────────────────────────────────────────────────────────

def open_device():
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        sys.exit("Error: Pulsar X2A not found (VID=0x3710, PID=0x1404). "
                 "Is the mouse plugged in?")
    if dev.is_kernel_driver_active(INTERFACE):
        dev.detach_kernel_driver(INTERFACE)
    usb.util.claim_interface(dev, INTERFACE)
    return dev


def close_device(dev):
    usb.util.release_interface(dev, INTERFACE)
    try:
        dev.attach_kernel_driver(INTERFACE)
    except Exception:
        pass

# ── Polling rate (global) ─────────────────────────────────────────────────────

def get_polling_rate(dev) -> int:
    rsp = _read(dev, 0x01, 0x09, 0x02, 0x00)
    return POLL_VAL_TO_HZ.get(rsp[7], rsp[7] * 125)


def set_polling_rate(dev, hz: int):
    val = POLL_HZ_TO_VAL.get(hz)
    if val is None:
        raise ValueError(f"Polling rate must be one of {sorted(POLL_HZ_TO_VAL)}")
    _cmd(dev, 0x01, 0x09, 0x02, 0x00, [val])

# ── LOD (per-profile) ─────────────────────────────────────────────────────────

def get_lod(dev, profile: int) -> int:
    rsp = _read(dev, 0x07, 0x02, 0x03, profile)
    return LOD_VAL_TO_MM.get(rsp[7], rsp[7])


def set_lod(dev, mm: int, profile: int):
    val = LOD_MM_TO_VAL.get(mm)
    if val is None:
        raise ValueError("LOD must be 1 or 2 (mm)")
    _cmd(dev, 0x07, 0x02, 0x03, profile, [val, val])

# ── Motion sync (global) ──────────────────────────────────────────────────────

def get_motion_sync(dev) -> bool:
    rsp = _read(dev, 0x07, 0x05, 0x02, 0x00)
    return bool(rsp[7])


def set_motion_sync(dev, enabled: bool):
    _cmd(dev, 0x07, 0x05, 0x02, 0x00, [1 if enabled else 0])

# ── Ripple control (global) ───────────────────────────────────────────────────

def get_ripple_control(dev) -> bool:
    rsp = _read(dev, 0x07, 0x03, 0x02, 0x00)
    return bool(rsp[7])


def set_ripple_control(dev, enabled: bool):
    _cmd(dev, 0x07, 0x03, 0x02, 0x00, [1 if enabled else 0])

# ── Angle snap (global) ───────────────────────────────────────────────────────

def get_angle_snap(dev) -> bool:
    rsp = _read(dev, 0x07, 0x04, 0x02, 0x00)
    return bool(rsp[7])


def set_angle_snap(dev, enabled: bool):
    _cmd(dev, 0x07, 0x04, 0x02, 0x00, [1 if enabled else 0])

# ── Debounce (global) ─────────────────────────────────────────────────────────

def get_debounce(dev) -> int:
    rsp = _read(dev, 0x04, 0x03, 0x03, 0x00)
    return rsp[7]


def set_debounce(dev, ms: int):
    if not 0 <= ms <= 20:
        raise ValueError("Debounce must be 0–20 ms")
    _cmd(dev, 0x04, 0x03, 0x03, 0x00, [ms])

# ── Brightness (per-profile) ──────────────────────────────────────────────────

def get_brightness(dev, profile: int) -> int:
    rsp = _read(dev, 0x03, 0x03, 0x03, profile, [0x01])
    return rsp[8]


def set_brightness(dev, value: int, profile: int):
    if not 0 <= value <= 255:
        raise ValueError("Brightness must be 0–255")
    _cmd(dev, 0x03, 0x03, 0x03, profile, [0x01, value])

# ── LED effect (per-profile) ──────────────────────────────────────────────────

def get_led_effect(dev, profile: int) -> str:
    rsp = _read(dev, 0x03, 0x04, 0x0F, profile, [0x01])
    return LED_VAL_TO_NAME.get(rsp[8], f"unknown(0x{rsp[8]:02x})")


def set_led_effect(dev, effect: str, profile: int):
    val = LED_NAME_TO_VAL.get(effect)
    if val is None:
        raise ValueError(f"Effect must be one of {list(LED_NAME_TO_VAL)}")
    _cmd(dev, 0x03, 0x04, 0x0F, profile, [0x01, val])


def get_breath_speed(dev, profile: int) -> int:
    """Breath speed 0–100 (only meaningful when LED effect is 'breath')."""
    rsp = _read(dev, 0x03, 0x04, 0x0F, profile, [0x01])
    return rsp[11]


def set_breath_speed(dev, speed: int, profile: int):
    if not 0 <= speed <= 100:
        raise ValueError("Breath speed must be 0–100")
    _cmd(dev, 0x03, 0x04, 0x0F, profile, [0x01, 0x02, 0x00, 0x00, speed])

# ── DPI stages (per-profile) ──────────────────────────────────────────────────

def get_dpi_stages(dev, profile: int) -> dict:
    """
    Returns dict with:
      'active': int (1-based active stage index)
      'count':  int (number of active stages)
      'stages': list of (dpi_x, dpi_y) tuples, one per stage
    """
    rsp = _read(dev, 0x05, 0x04, 0x15, profile)
    active     = rsp[7]
    num_stages = rsp[8]
    stages = []
    for i in range(num_stages):
        base = 9 + i * 5
        # stage_num = rsp[base]  (1-indexed, matches i+1)
        dpi_x = struct.unpack_from('<H', rsp, base + 1)[0]
        dpi_y = struct.unpack_from('<H', rsp, base + 3)[0]
        stages.append((dpi_x, dpi_y))
    return {'active': active, 'count': num_stages, 'stages': stages}


def set_dpi_stages(dev, stages: list, active: int, profile: int):
    """
    stages: list of int DPI values (1–6 entries, 100–26000)
    active: 1-based index of active stage
    """
    if not 1 <= len(stages) <= 6:
        raise ValueError("Must have 1–6 DPI stages")
    if not 1 <= active <= len(stages):
        raise ValueError(f"Active stage must be 1–{len(stages)}")
    for dpi in stages:
        if not 100 <= dpi <= 26000:
            raise ValueError(f"DPI value {dpi} out of range 100–26000")

    payload = [active, len(stages)]
    for i, dpi in enumerate(stages):
        lo = dpi & 0xFF
        hi = (dpi >> 8) & 0xFF
        payload += [i + 1, lo, hi, lo, hi]   # same value for X and Y axes
    _cmd(dev, 0x05, 0x04, 0x21, profile, payload)


def get_active_dpi_stage(dev, profile: int) -> int:
    rsp = _read(dev, 0x05, 0x01, 0x02, profile)
    return rsp[7]


def set_active_dpi_stage(dev, stage: int, profile: int):
    if not 1 <= stage <= 6:
        raise ValueError("DPI stage must be 1–6")
    _cmd(dev, 0x05, 0x01, 0x02, profile, [stage])

# ── DPI stage color (per-profile) ─────────────────────────────────────────────

def get_stage_color(dev, stage: int, profile: int) -> tuple:
    """Returns (R, G, B) tuple."""
    rsp = _read(dev, 0x05, 0x05, 0x05, profile, [stage])
    return (rsp[8], rsp[9], rsp[10])


def set_stage_color(dev, stage: int, r: int, g: int, b: int, profile: int):
    for val, name in [(r, 'R'), (g, 'G'), (b, 'B')]:
        if not 0 <= val <= 255:
            raise ValueError(f"{name} must be 0–255")
    if not 1 <= stage <= 6:
        raise ValueError("Stage must be 1–6")
    _cmd(dev, 0x05, 0x05, 0x05, profile, [stage, r, g, b])

# ── Button bindings (per-profile) ────────────────────────────────────────────

def get_button(dev, btn_id: int, profile: int) -> tuple:
    """Returns (type, action1, action2) raw bytes."""
    rsp = _read(dev, 0x04, 0x01, 0x06, profile, [btn_id, 0xff])
    return (rsp[8], rsp[9], rsp[10])


def set_button(dev, btn_id: int, btn_type: int, action1: int, action2: int,
               profile: int):
    _cmd(dev, 0x04, 0x01, 0x06, profile, [btn_id, btn_type, action1, action2])


# Factory default button bindings (from Pulsar Fusion reset capture)
BUTTON_DEFAULTS = {
    0x01: (BTN_TYPE_MOUSE,   0x01, 0x00),  # left   → left click
    0x02: (BTN_TYPE_MOUSE,   0x02, 0x00),  # right  → right click
    0x03: (BTN_TYPE_MOUSE,   0x03, 0x00),  # wheel  → wheel click
    0x04: (BTN_TYPE_MOUSE,   0x04, 0x00),  # thumb1 → forward
    0x05: (BTN_TYPE_MOUSE,   0x05, 0x00),  # thumb2 → backward
    0x06: (BTN_TYPE_MOUSE,   0x04, 0x00),  # thumb3 → forward
    0x07: (BTN_TYPE_MOUSE,   0x05, 0x00),  # thumb4 → backward
    0x0b: (BTN_TYPE_DPI,     0x03, 0x00),  # dpi    → dpiloop
}


def reset_to_defaults(dev, profile: int = 1):
    """
    Reset mouse to factory defaults by sending the two firmware reset commands
    observed in Pulsar Fusion captures (cat=0x02, reg=0x05 then reg=0x06).
    The firmware handles the actual reset internally — the device may briefly
    disconnect/reconnect after the second command.
    """
    _cmd(dev, 0x02, 0x05, 0x01, profile)
    # Second command triggers the actual reset; device may return 0x00
    pkt = _build(0x02, 0x06, 0x01, profile)
    _set_report(dev, pkt)
    try:
        _get_report(dev)   # consume whatever the device sends (may be 0x00)
    except Exception:
        pass  # device may have already reset


def describe_button(btn_type: int, a1: int, a2: int) -> str:
    """Human-readable description of a button binding."""
    if btn_type == BTN_TYPE_MOUSE:
        return MOUSE_ACTION_NAMES.get(a1, f'mouse(0x{a1:02x})')
    if btn_type == BTN_TYPE_KEYBOARD:
        mod_parts = [n for n, v in HID_MODS.items()
                     if v & a1 and n in ('ctrl','shift','alt','gui','rctrl','rshift','ralt','rgui')]
        key_parts = [n for n, v in HID_KEYS.items() if v == a2]
        key = key_parts[0] if key_parts else f'0x{a2:02x}'
        if mod_parts:
            return '+'.join(mod_parts + [key])
        return key
    if btn_type == BTN_TYPE_SCROLL:
        return SCROLL_ACTION_NAMES.get(a1, f'scroll(0x{a1:02x})')
    if btn_type == BTN_TYPE_PROFILE:
        return PROFILE_ACTION_NAMES.get(a1, f'profile(0x{a1:02x})')
    if btn_type == BTN_TYPE_DPI:
        return DPI_ACTION_NAMES.get(a1, f'dpi(0x{a1:02x})')
    if btn_type == BTN_TYPE_MEDIA:
        code = a1 | (a2 << 8)
        return MEDIA_CODE_NAMES.get(code, f'media(0x{code:04x})')
    if btn_type == BTN_TYPE_XCLICK:
        return 'xclick-double'
    return f'type=0x{btn_type:02x} a1=0x{a1:02x} a2=0x{a2:02x}'


def parse_button_function(spec: str) -> tuple:
    """
    Parse a button function string into (type, action1, action2).

    Formats:
      left / right / wheel / forward / backward
      scrollup / scrolldown
      dpi+ / dpi- / dpiloop
      profile+ / profile-
      xclick
      play / next / prev / stop / mute / vol+ / vol- / mediaplayer
      ctrl+c / shift+f5 / alt+tab / ...  (keyboard shortcut)
    """
    s = spec.strip().lower()

    if s in MOUSE_ACTIONS:
        return (BTN_TYPE_MOUSE, MOUSE_ACTIONS[s], 0x00)
    if s in SCROLL_ACTIONS:
        return (BTN_TYPE_SCROLL, SCROLL_ACTIONS[s], 0x00)
    if s in DPI_ACTIONS:
        return (BTN_TYPE_DPI, DPI_ACTIONS[s], 0x00)
    if s in PROFILE_ACTIONS:
        return (BTN_TYPE_PROFILE, PROFILE_ACTIONS[s], 0x00)
    if s == 'xclick':
        return (BTN_TYPE_XCLICK, 0x01, 0x00)
    if s in MEDIA_CODES:
        code = MEDIA_CODES[s]
        return (BTN_TYPE_MEDIA, code & 0xff, (code >> 8) & 0xff)

    # Keyboard shortcut: mod+mod+key
    mod = 0
    key = 0
    for part in s.split('+'):
        if part in HID_MODS:
            mod |= HID_MODS[part]
        elif part in HID_KEYS:
            key = HID_KEYS[part]
        else:
            raise ValueError(
                f"Unknown button function or key '{part}' in '{spec}'.\n"
                f"  Mouse: {', '.join(MOUSE_ACTIONS)}\n"
                f"  Scroll: scrollup, scrolldown\n"
                f"  DPI: dpi+, dpi-, dpiloop\n"
                f"  Profile: profile+, profile-\n"
                f"  Media: {', '.join(MEDIA_CODES)}\n"
                f"  Keyboard: [modifier+]key  e.g. ctrl+c, shift+f5"
            )
    if key:
        return (BTN_TYPE_KEYBOARD, mod, key)

    raise ValueError(f"Cannot parse button function: '{spec}'")


# ── Pretty-print ──────────────────────────────────────────────────────────────

def _on_off(val: bool) -> str:
    return 'on' if val else 'off'


def print_profile(dev, profile: int):
    print(f"\n── Profile {profile} {'─'*47}")
    try:
        info = get_dpi_stages(dev, profile)
        print(f"  DPI stages ({info['count']} active, stage {info['active']} selected):")
        for i, (dx, dy) in enumerate(info['stages'], 1):
            marker = " ◄" if i == info['active'] else ""
            color  = get_stage_color(dev, i, profile)
            print(f"    Stage {i}: {dx} DPI  #{color[0]:02X}{color[1]:02X}{color[2]:02X}{marker}")
    except Exception as e:
        print(f"  DPI:              error ({e})")
    try:
        print(f"  LOD:              {get_lod(dev, profile)} mm")
    except Exception as e:
        print(f"  LOD:              error ({e})")
    try:
        effect = get_led_effect(dev, profile)
        bright = get_brightness(dev, profile)
        speed  = get_breath_speed(dev, profile)
        if effect == 'breath':
            print(f"  LED:              {effect}  speed={speed}/100  brightness={bright}/255")
        else:
            print(f"  LED:              {effect}  brightness={bright}/255")
    except Exception as e:
        print(f"  LED:              error ({e})")
    try:
        print(f"  Buttons:")
        for name, bid in BUTTONS.items():
            t, a1, a2 = get_button(dev, bid, profile)
            print(f"    {name:<8} (0x{bid:02x}): {describe_button(t, a1, a2)}")
    except Exception as e:
        print(f"  Buttons:          error ({e})")


def print_global(dev):
    try:
        print(f"  Polling rate:     {get_polling_rate(dev)} Hz")
    except Exception as e:
        print(f"  Polling rate:     error ({e})")
    try:
        print(f"  Debounce:         {get_debounce(dev)} ms")
    except Exception as e:
        print(f"  Debounce:         error ({e})")
    try:
        print(f"  Angle snap:       {_on_off(get_angle_snap(dev))}")
    except Exception as e:
        print(f"  Angle snap:       error ({e})")
    try:
        print(f"  Ripple control:   {_on_off(get_ripple_control(dev))}")
    except Exception as e:
        print(f"  Ripple control:   error ({e})")
    try:
        print(f"  Motion sync:      {_on_off(get_motion_sync(dev))}")
    except Exception as e:
        print(f"  Motion sync:      error ({e})")


def print_all(dev):
    print_global(dev)
    for p in range(1, 6):
        print_profile(dev, p)

# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description='Pulsar X2A Linux configuration tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                          # show all settings
  %(prog)s --profile 1              # show profile 1 only

  %(prog)s --poll 1000              # set polling rate to 1000 Hz
  %(prog)s --debounce 3             # set debounce time to 3 ms
  %(prog)s --angle-snap on
  %(prog)s --ripple on
  %(prog)s --motion-sync off

  %(prog)s --profile 1 --lod 1              # set LOD to 1 mm
  %(prog)s --profile 1 --dpi 400,800,1600   # set 3 DPI stages (active=1)
  %(prog)s --profile 1 --dpi 400,800,1600 --active-stage 2
  %(prog)s --profile 1 --brightness 200
  %(prog)s --profile 1 --led steady
  %(prog)s --profile 1 --led breath --breath-speed 50
  %(prog)s --profile 1 --stage-color 1 29 96 cd   # R G B for stage 1

  # Button bindings (require --profile N):
  %(prog)s --profile 1 --button thumb1 dpi+
  %(prog)s --profile 1 --button thumb2 dpi-
  %(prog)s --profile 1 --button thumb3 profile+
  %(prog)s --profile 1 --button dpi    dpiloop
  %(prog)s --profile 1 --button thumb1 ctrl+c
  %(prog)s --profile 1 --button thumb1 vol+
  %(prog)s --profile 1 --button thumb1 scrollup
  %(prog)s --profile 1 --button left   left      # reset one button to default
  %(prog)s --profile 1 --reset                   # reset entire profile to factory defaults

  Button names: left right wheel thumb1 thumb2 thumb3 thumb4 dpi
  Functions:    left right wheel forward backward scrollup scrolldown
                dpi+ dpi- dpiloop  profile+ profile-  xclick
                play next prev stop mute vol+ vol- mediaplayer
                ctrl+c  shift+f5  alt+tab  ...  (any modifier+key combo)
""")

    p.add_argument('--profile', type=int, metavar='N',
                   help='Profile to read/write (1-5, default: all for read)')

    # Global settings
    g = p.add_argument_group('global settings (shared across all profiles)')
    g.add_argument('--poll', type=int, metavar='HZ',
                   help='Polling rate: 125, 250, 500, 1000')
    g.add_argument('--debounce', type=int, metavar='MS',
                   help='Debounce time in ms (0-20)')
    g.add_argument('--angle-snap', metavar='on|off')
    g.add_argument('--ripple', metavar='on|off')
    g.add_argument('--motion-sync', metavar='on|off')

    # Per-profile settings
    pp = p.add_argument_group('per-profile settings (require --profile N)')
    pp.add_argument('--lod', type=int, metavar='MM',
                    help='Lift-off distance in mm: 1 or 2')
    pp.add_argument('--dpi', metavar='D1[,D2,...]',
                    help='Comma-separated DPI stage values (1-6 values, 100-26000)')
    pp.add_argument('--active-stage', type=int, metavar='N',
                    help='Active DPI stage index (1-6), used with --dpi or alone')
    pp.add_argument('--brightness', type=int, metavar='0-255')
    pp.add_argument('--led', metavar='steady|breath',
                    help='LED effect')
    pp.add_argument('--breath-speed', type=int, metavar='0-100')
    pp.add_argument('--stage-color', nargs=4, metavar=('STAGE', 'R', 'G', 'B'),
                    type=int, help='Set DPI stage LED color (RGB 0-255)')
    pp.add_argument('--button', nargs=2, metavar=('BTN', 'FUNC'),
                    help='Remap a button: BTN is left/right/wheel/thumb1-4/dpi, '
                         'FUNC is the function (see examples below)')
    pp.add_argument('--reset', action='store_true',
                    help='Reset profile to factory defaults (firmware reset, '
                         'matches Pulsar Fusion behaviour)')

    return p.parse_args()


def _parse_bool(s, name):
    if s in ('on', '1', 'yes', 'true'):
        return True
    if s in ('off', '0', 'no', 'false'):
        return False
    raise ValueError(f"--{name}: expected on or off, got '{s}'")


def main():
    args = parse_args()

    write_ops = any([
        args.poll, args.debounce,
        args.angle_snap, args.ripple, args.motion_sync,
        args.lod, args.dpi, args.active_stage,
        args.brightness, args.led, args.breath_speed,
        args.stage_color, args.button, args.reset,
    ])

    profile_required = any([
        args.lod, args.dpi, args.active_stage,
        args.brightness, args.led, args.breath_speed,
        args.stage_color, args.button, args.reset,
    ])

    if profile_required and args.profile is None:
        sys.exit("Error: --profile N is required for per-profile settings")

    if args.profile is not None and not 1 <= args.profile <= 5:
        sys.exit("Error: --profile must be 1–5")

    dev = open_device()
    try:
        if write_ops:
            # ── Global writes ─────────────────────────────────────────────
            if args.poll is not None:
                set_polling_rate(dev, args.poll)
                print(f"Polling rate set to {args.poll} Hz")

            if args.debounce is not None:
                set_debounce(dev, args.debounce)
                print(f"Debounce set to {args.debounce} ms")

            if args.angle_snap is not None:
                v = _parse_bool(args.angle_snap, 'angle-snap')
                set_angle_snap(dev, v)
                print(f"Angle snap: {_on_off(v)}")

            if args.ripple is not None:
                v = _parse_bool(args.ripple, 'ripple')
                set_ripple_control(dev, v)
                print(f"Ripple control: {_on_off(v)}")

            if args.motion_sync is not None:
                v = _parse_bool(args.motion_sync, 'motion-sync')
                set_motion_sync(dev, v)
                print(f"Motion sync: {_on_off(v)}")

            # ── Per-profile writes ────────────────────────────────────────
            prof = args.profile  # validated above if needed
            if args.lod is not None:
                set_lod(dev, args.lod, prof)
                print(f"Profile {prof} LOD set to {args.lod} mm")

            if args.dpi is not None:
                stages = [int(x.strip()) for x in args.dpi.split(',')]
                active = args.active_stage if args.active_stage else 1
                set_dpi_stages(dev, stages, active, prof)
                print(f"Profile {prof} DPI stages: {stages}  active={active}")
            elif args.active_stage is not None:
                set_active_dpi_stage(dev, args.active_stage, prof)
                print(f"Profile {prof} active DPI stage: {args.active_stage}")

            if args.brightness is not None:
                set_brightness(dev, args.brightness, prof)
                print(f"Profile {prof} brightness: {args.brightness}/255")

            if args.led is not None:
                set_led_effect(dev, args.led, prof)
                print(f"Profile {prof} LED effect: {args.led}")

            if args.breath_speed is not None:
                set_breath_speed(dev, args.breath_speed, prof)
                print(f"Profile {prof} breath speed: {args.breath_speed}/100")

            if args.stage_color is not None:
                stage, r, g, b = args.stage_color
                set_stage_color(dev, stage, r, g, b, prof)
                print(f"Profile {prof} stage {stage} color: #{r:02X}{g:02X}{b:02X}")

            if args.button is not None:
                btn_name, func_spec = args.button
                btn_id = BUTTONS.get(btn_name.lower())
                if btn_id is None:
                    sys.exit(f"Unknown button '{btn_name}'. "
                             f"Use: {', '.join(BUTTONS)}")
                try:
                    t, a1, a2 = parse_button_function(func_spec)
                except ValueError as e:
                    sys.exit(str(e))
                set_button(dev, btn_id, t, a1, a2, prof)
                print(f"Profile {prof} {btn_name} → {describe_button(t, a1, a2)}")

            if args.reset:
                reset_to_defaults(dev, prof)
                print(f"Profile {prof} reset to factory defaults")
        else:
            # ── Read mode ─────────────────────────────────────────────────
            print("Pulsar X2A — current settings")
            print("══════════════════════════════════════════════════════")
            print()
            print("Global:")
            if args.profile is None:
                print_all(dev)
            else:
                try:
                    print_global(dev)
                except Exception as e:
                    print(f"  (error reading global settings: {e})")
                print_profile(dev, args.profile)
            print()
    finally:
        close_device(dev)


if __name__ == '__main__':
    main()
