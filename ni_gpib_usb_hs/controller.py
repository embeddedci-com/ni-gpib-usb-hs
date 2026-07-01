# SPDX-License-Identifier: GPL-2.0-only
#
# Pure-Python user-space driver for the National Instruments GPIB-USB-HS.
#
# Copyright (C) 2026 Edward Viaene
#
# This is a port of the NI-USB protocol implemented by the linux-gpib project's
# ni_usb_gpib kernel driver (drivers/gpib/ni_usb/ni_usb_gpib.[ch]),
#   Copyright (C) 2004 Frank Mori Hess, and contributors incl. Dave Penkler.
# Register/command layouts are derived from that GPL-2.0 code, so this port is
# likewise licensed GPL-2.0-only. See the LICENSE file and README credits.
#
# This program is free software; you can redistribute it and/or modify it under
# the terms of the GNU General Public License version 2 as published by the Free
# Software Foundation.
"""User-space GPIB controller over an NI GPIB-USB-HS (USB id 3923:709b).

Talks to the adapter's Cypress FX2 (USB) + NI TNT4882 (GPIB controller) directly
via libusb — no kernel driver, no NI software, no linux-gpib. Works on macOS
(incl. Apple Silicon, where NI ships no driver), Linux, and anywhere pyusb +
libusb run.

It uses synchronous bulk request->response transfers and does NOT use the
adapter's interrupt endpoint. That keeps it simple and, in practice, robust on
hosts where the linux-gpib kernel driver's interrupt-status path misbehaves.

Scope: a single GPIB controller driving one addressed instrument at a time
(command / write / read / query). No serial poll, SRQ, or parallel poll. That is
enough to drive the vast majority of SCPI bench instruments.
"""
from __future__ import annotations

import usb.core
import usb.util

NI_VID = 0x3923
NI_GPIB_USB_HS_PID = 0x709b

# bulk endpoints (NI-USB-HS)
_EP_OUT, _EP_IN = 0x02, 0x84
_BMREQ_VENDOR_IN = 0xC0

# bulk instruction / block ids (ni_usb_gpib.h)
_REG_WRITE_ID = 0x09
_TERM_ID = 0x04
_IBCAC_ID = 0x01           # take control
_CMD_ID = 0x0c             # send command bytes (ATN asserted)
_WRITE_ID = 0x0d           # send data bytes
_READ_ID = 0x0a            # receive data bytes
_IBRD_DATA_ID = 0x36
_IBRD_EXT_ID = 0x37

# TNT4882 subdevice ids used by the adapter's register protocol
_TNT, _UNK2, _UNK3 = 1, 2, 3

_ERR_NAMES = {0: "NO_ERROR", 1: "ABORTED", 2: "ATN_STATE", 3: "ADDRESSING",
              4: "NO_LISTENER", 5: "TIMEOUT", 6: "EOSMODE", 7: "NO_BUS"}

# GPIB universal command bytes
_UNL = 0x3f  # unlisten


def _lad(addr: int) -> int:
    """Listen-address byte for a primary GPIB address."""
    return 0x20 + addr


def _tad(addr: int) -> int:
    """Talk-address byte for a primary GPIB address."""
    return 0x40 + addr


class GpibError(RuntimeError):
    """Raised on any GPIB/USB error (device not found, timeout, addressing, ...)."""


def _timeout_code(usec: int) -> int:
    """Map a timeout in microseconds to the TNT timeout code byte."""
    table = [(10, 0xf1), (30, 0xf2), (100, 0xf3), (300, 0xf4), (1000, 0xf5),
             (3000, 0xf6), (10000, 0xf7), (30000, 0xf8), (100000, 0xf9),
             (300000, 0xfa), (1000000, 0xfb), (3000000, 0xfc), (10000000, 0xfd)]
    if usec == 0:
        return 0xf0
    for limit, code in table:
        if usec <= limit:
            return code
    return 0xfe


def _init_writes(pad: int, master: bool):
    """The TNT4882 bring-up register sequence (ported from ni_usb_setup_init).

    t1 handshake delay 2000 ns, secondary address disabled, non-binary EOS.
    """
    A = 0x0a  # nec7210->tnt4882 offset of AUXMR (2*5)
    return [
        (_UNK3, 0x10, 0x00),
        (_TNT, 0x1c, 0x22),                      # CMDR SOFT_RESET
        (_TNT, A, 0x81),                         # AUXMR AUXRA | HR_HLDA
        (_TNT, 0x06, 0x81),                      # AUXCR
        (_TNT, 0x0d, 0x01),                      # HSSEL TNT_ONE_CHIP_BIT
        (_TNT, A, 0x02),                         # AUXMR AUX_CR (chip reset)
        (_TNT, 0x1d, 0x80),                      # IMR0 ALWAYS bit
        (_TNT, 0x02, 0x00),                      # IMR1
        (_TNT, 0x04, 0x00),                      # IMR2
        (_TNT, 0x12, 0x00),                      # IMR3
        (_TNT, A, 0x51),                         # AUXMR AUX_HLDI
        (_TNT, A, 0xe1),                         # t1 delay: AUXRI | SISB
        (_TNT, A, 0xa0),                         # t1 delay: AUXRB
        (_TNT, 0x17, 0x00),                      # t1 delay: KEYREG
        (_TNT, A, 0x48),                         # AUXMR AUXRG | NTNL_BIT
        (_TNT, 0x1c, 0x03 if master else 0x02),  # CMDR SETSC / CLRSC
        (_TNT, A, 0x16),                         # AUXMR AUX_CIFC (clear IFC)
        (_TNT, 0x0c, pad),                       # ADR = controller primary address
        (_UNK2, 0x00, pad),
        (_TNT, 0x0c, 0xe0),                      # ADR: disable secondary address
        (_TNT, 0x08, 0x31),                      # ADMR
        (_UNK2, 0x01, 0x00),
        (_UNK2, 0x02, 0xfd),
        (_TNT, 0x0f, 0x11),
        (_TNT, A, 0x00),                         # AUXMR AUX_PON
        (_TNT, A, 0x01),                         # AUXMR AUX_CPPF
    ]


class NIUSBGPIB:
    """A GPIB controller backed by an NI GPIB-USB-HS.

    Example::

        from ni_gpib_usb_hs import NIUSBGPIB
        with NIUSBGPIB() as gpib:
            print(gpib.query(22, "*IDN?"))

    Args:
        my_pad: the controller's own GPIB primary address (default 0).
        master: act as system controller (default True).
        timeout_usec: GPIB transfer timeout in microseconds (default 3 s).
        usb_timeout_ms: libusb transfer timeout in milliseconds.
        serial: optional adapter serial-number string to select a specific
            adapter when several are attached.
    """

    def __init__(self, my_pad: int = 0, master: bool = True,
                 timeout_usec: int = 3_000_000, usb_timeout_ms: int = 5000,
                 serial: str | None = None):
        self.my_pad = my_pad
        self.master = master
        self._tcode = _timeout_code(timeout_usec)
        self._usb_ms = usb_timeout_ms

        matches = list(usb.core.find(find_all=True, idVendor=NI_VID,
                                     idProduct=NI_GPIB_USB_HS_PID))
        if not matches:
            raise GpibError("no NI GPIB-USB-HS (3923:709b) found on this host")
        if serial is not None:
            matches = [d for d in matches
                       if usb.util.get_string(d, d.iSerialNumber) == serial]
            if not matches:
                raise GpibError(f"no NI GPIB-USB-HS with serial {serial!r} found")
        self.dev = matches[0]
        try:
            self.serial = usb.util.get_string(self.dev, self.dev.iSerialNumber)
        except Exception:  # noqa: BLE001
            self.serial = None
        try:
            self.dev.set_configuration()
        except Exception:  # noqa: BLE001  (already configured, e.g. on macOS)
            pass
        self._wait_ready()
        self._init_chip()

    # -- context manager --
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        """Release the USB device."""
        if getattr(self, "dev", None) is not None:
            usb.util.dispose_resources(self.dev)
            self.dev = None

    # -- low-level usb --
    def _out(self, data):
        n = self.dev.write(_EP_OUT, bytes(data), timeout=self._usb_ms)
        if n != len(data):
            raise GpibError(f"short bulk OUT {n}/{len(data)}")

    def _in(self, length):
        return bytes(self.dev.read(_EP_IN, length, timeout=self._usb_ms))

    @staticmethod
    def _pad_term(buf):
        while len(buf) % 4:
            buf.append(0x00)
        buf += [_TERM_ID, 0, 0, 0]
        return buf

    def _status12(self, tag):
        resp = self._in(0x10)
        if len(resp) != 12:
            raise GpibError(f"{tag}: expected 12-byte status, got {len(resp)}")
        return resp[0], (resp[1] << 8) | resp[2], resp[3]  # id, ibsta, error_code

    # -- init --
    def _wait_ready(self):
        ser = self.dev.ctrl_transfer(_BMREQ_VENDOR_IN, 0x41, 0, 0, 16, timeout=self._usb_ms)
        rdy = self.dev.ctrl_transfer(_BMREQ_VENDOR_IN, 0x40, 0, 0, 16, timeout=self._usb_ms)
        if ser[0] != 0x41 or rdy[0] != 0x40:
            raise GpibError("adapter failed the ready handshake")

    def _write_registers(self, writes):
        buf = [_REG_WRITE_ID, len(writes), 0x00]
        for dev, addr, val in writes:
            buf += [dev, addr & 0xff, val & 0xff]
        self._pad_term(buf)
        self._out(buf)
        resp = self._in(0x20)
        sid, err, completed = resp[0], resp[3], (resp[8] if len(resp) > 8 else -1)
        if sid != _REG_WRITE_ID or err or completed != len(writes):
            raise GpibError(f"register write failed (id=0x{sid:02x} "
                            f"err={_ERR_NAMES.get(err, err)} completed={completed})")

    def _init_chip(self):
        self._write_registers(_init_writes(self.my_pad, self.master))
        # take control (assert ATN); NO_BUS here is benign when idle
        self._out(self._pad_term([_IBCAC_ID, 1, 0, 0]))
        self._status12("take_control")

    # -- gpib primitives --
    def command(self, cmd_bytes):
        """Send up to 16 GPIB command bytes (ATN asserted)."""
        if len(cmd_bytes) > 16:
            raise GpibError("command chunk > 16 bytes")
        cc = (~(len(cmd_bytes) - 1)) & 0xff
        buf = [_CMD_ID, cc, 0x00, self._tcode] + list(cmd_bytes)
        self._pad_term(buf)
        self._out(buf)
        _, _, err = self._status12("command")
        if err:
            raise GpibError(f"command error {_ERR_NAMES.get(err, err)}")

    def write(self, addr: int, data, eoi: bool = True):
        """Address instrument ``addr`` as listener and send ``data`` (EOI on last byte)."""
        if isinstance(data, str):
            data = data.encode()
        self.command([_UNL, _tad(self.my_pad), _lad(addr)])
        cc = (~(len(data) - 1)) & 0xffff
        buf = [_WRITE_ID, cc & 0xff, (cc >> 8) & 0xff, self._tcode, 0, 0,
               0x8 if eoi else 0, 0] + list(data)
        self._pad_term(buf)
        self._out(buf)
        _, _, err = self._status12("write")
        if err:
            raise GpibError(f"write error {_ERR_NAMES.get(err, err)}")

    def read(self, addr: int, length: int = 256) -> bytes:
        """Address instrument ``addr`` as talker and read up to ``length`` bytes."""
        self.command([_UNL, _lad(self.my_pad), _tad(addr)])
        nc = (~(length - 1)) & 0xffff
        buf = [_READ_ID, 0x00, 0x00, self._tcode, nc & 0xff, (nc >> 8) & 0xff, 0, 0]
        # the read instruction embeds two aux register writes (AUX_HLDI, AUX_CLEAR_END)
        buf += [_REG_WRITE_ID, 2, 0x00, _TNT, 0x0a, 0x51, _TNT, 0x0a, 0x55]
        self._pad_term(buf)
        self._out(buf)
        resp = self._in((length // 30 + 2) * 0x20)
        return self._parse_read(resp)

    def query(self, addr: int, cmd, length: int = 256) -> str:
        """Write ``cmd`` then read the reply, returned as a stripped ``str``."""
        self.write(addr, cmd)
        return self.read(addr, length).decode(errors="replace").strip()

    @staticmethod
    def _parse_read(resp) -> bytes:
        i, data, n_blocks, blocklen = 0, bytearray(), 0, 15
        while i < len(resp) and resp[i] in (_IBRD_DATA_ID, _IBRD_EXT_ID):
            if resp[i] == _IBRD_DATA_ID:
                blocklen, i = 15, i + 1
            else:
                blocklen, i = 30, i + 2  # 0x37 then a pad byte
            for _ in range(blocklen):
                if i < len(resp):
                    data.append(resp[i])
                    i += 1
            n_blocks += 1
        err = resp[i + 3] if i + 3 < len(resp) else 0
        i += 8 + 1  # 8-byte status block + separator
        if n_blocks:
            last = resp[i] if i < len(resp) else 0
            actual = (n_blocks - 1) * blocklen + last
        else:
            actual = 0
        if err:
            raise GpibError(f"read error {_ERR_NAMES.get(err, err)}")
        return bytes(data[:actual])
