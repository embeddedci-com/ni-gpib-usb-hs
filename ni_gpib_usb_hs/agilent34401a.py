# SPDX-License-Identifier: GPL-2.0-only
# Copyright (C) 2026 Edward Viaene.  Part of ni-gpib-usb-hs (see LICENSE).
"""A convenience wrapper for the Agilent/Keysight/HP 34401A 6.5-digit DMM.

Drives the meter over GPIB through :class:`NIUSBGPIB`. Works with the 34401A and
the pin-compatible HP 34401A / Keysight 34401A (they share the SCPI command set).

    from ni_gpib_usb_hs import Agilent34401A
    with Agilent34401A(addr=22) as dmm:
        print(dmm.idn())
        print(dmm.voltage_dc())          # single autoranged DC reading
        print(dmm.resistance_4wire())
"""
from __future__ import annotations

from .controller import NIUSBGPIB, GpibError

# MEASure/CONFigure subsystem nodes for each supported function
_FUNCTIONS = {
    "voltage_dc": "VOLTage:DC",
    "voltage_ac": "VOLTage:AC",
    "current_dc": "CURRent:DC",
    "current_ac": "CURRent:AC",
    "resistance": "RESistance",        # 2-wire
    "resistance_4wire": "FRESistance",  # 4-wire
    "frequency": "FREQuency",
    "period": "PERiod",
    "diode": "DIODe",
    "continuity": "CONTinuity",
}


class Agilent34401A:
    """34401A digital multimeter over GPIB.

    Args:
        addr: the meter's GPIB primary address (front panel default 22).
        gpib: an existing :class:`NIUSBGPIB` to share; if None, one is opened.
        controller_pad: controller GPIB address when opening our own bus.
        reset: send ``*RST`` + ``*CLS`` on connect (default True).
    """

    def __init__(self, addr: int = 22, gpib: NIUSBGPIB | None = None,
                 controller_pad: int = 0, reset: bool = True):
        self.addr = addr
        self._own_gpib = gpib is None
        self.gpib = gpib if gpib is not None else NIUSBGPIB(my_pad=controller_pad)
        if reset:
            self.reset()
        # Note: unlike RS-232, GPIB has no SCPI "go remote" step — addressing
        # the meter over the bus is enough for it to accept SCPI commands.
        # SYSTem:REMote/SYSTem:LOCal are RS-232-only on the 34401A and raise
        # error 514 ("Command allowed only with RS-232") if sent over GPIB.

    # -- context manager --
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        """Close the underlying GPIB controller, if we opened it ourselves."""
        if self._own_gpib and self.gpib is not None:
            self.gpib.close()
            self.gpib = None

    # -- raw SCPI --
    def write(self, cmd: str):
        """Send a SCPI command (no reply expected)."""
        self.gpib.write(self.addr, cmd)

    def query(self, cmd: str) -> str:
        """Send a SCPI query and return the stripped string reply."""
        return self.gpib.query(self.addr, cmd)

    def query_float(self, cmd: str) -> float:
        s = self.query(cmd)
        try:
            return float(s)
        except ValueError as e:
            raise GpibError(f"expected a number from {cmd!r}, got {s!r}") from e

    # -- common commands --
    def idn(self) -> str:
        return self.query("*IDN?")

    def reset(self):
        """``*RST`` + ``*CLS``."""
        self.write("*RST")
        self.write("*CLS")

    def clear(self):
        """Clear status / error queue (``*CLS``)."""
        self.write("*CLS")

    def errors(self):
        """Drain and return the SCPI error queue as a list of ``(code, text)``."""
        out = []
        for _ in range(32):
            resp = self.query("SYSTem:ERRor?")
            code_str, _, text = resp.partition(",")
            try:
                code = int(code_str)
            except ValueError:
                break
            if code == 0:
                break
            out.append((code, text.strip().strip('"')))
        return out

    # -- one-shot measurements (MEASure? — autoranged, self-configuring) --
    def measure(self, function: str, rng="AUTO", resolution="DEF") -> float:
        """One-shot reading via ``MEASure:<function>?``.

        ``function`` is a key of the supported set (e.g. ``"voltage_dc"``) or a
        raw SCPI node like ``"VOLTage:DC"``. ``rng``/``resolution`` are passed
        through when the function accepts them.
        """
        node = _FUNCTIONS.get(function, function)
        if function in ("diode", "continuity") or node in ("DIODe", "CONTinuity"):
            return self.query_float(f"MEASure:{node}?")
        args = ""
        if rng is not None:
            args = f" {rng}" + (f",{resolution}" if resolution is not None else "")
        return self.query_float(f"MEASure:{node}?{args}")

    def voltage_dc(self, rng="AUTO", resolution="DEF") -> float:
        return self.measure("voltage_dc", rng, resolution)

    def voltage_ac(self, rng="AUTO", resolution="DEF") -> float:
        return self.measure("voltage_ac", rng, resolution)

    def current_dc(self, rng="AUTO", resolution="DEF") -> float:
        return self.measure("current_dc", rng, resolution)

    def current_ac(self, rng="AUTO", resolution="DEF") -> float:
        return self.measure("current_ac", rng, resolution)

    def resistance(self, rng="AUTO", resolution="DEF") -> float:
        return self.measure("resistance", rng, resolution)

    def resistance_4wire(self, rng="AUTO", resolution="DEF") -> float:
        return self.measure("resistance_4wire", rng, resolution)

    def frequency(self, rng="AUTO", resolution="DEF") -> float:
        return self.measure("frequency", rng, resolution)

    def period(self, rng="AUTO", resolution="DEF") -> float:
        return self.measure("period", rng, resolution)

    def diode(self) -> float:
        return self.measure("diode")

    def continuity(self) -> float:
        return self.measure("continuity")

    # -- configured / repeated reads (fast, low-noise) --
    def configure_dc_volts(self, rng=10, nplc=10, autozero=True):
        """Set up fixed-range low-noise DC volts for repeated :meth:`read` calls.

        NPLC (integration in power-line cycles) trades speed for noise: 10 is
        quiet, 100 quieter/slower. Immediate trigger so each ``READ?`` is one-shot.
        """
        self.write(f"CONFigure:VOLTage:DC {rng}")
        self.write(f"VOLTage:DC:NPLC {nplc}")
        self.write(f"ZERO:AUTO {'ON' if autozero else 'OFF'}")
        self.write("TRIGger:SOURce IMMediate")

    def read(self) -> float:
        """Trigger + fetch one reading with the current CONFigure settings."""
        return self.query_float("READ?")

    def read_average(self, samples: int = 1) -> float:
        """Mean of ``samples`` ``READ?`` calls."""
        n = max(1, int(samples))
        return sum(self.read() for _ in range(n)) / n
