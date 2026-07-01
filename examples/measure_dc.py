#!/usr/bin/env python3
"""Read DC volts from a 34401A a few different ways.

    python examples/measure_dc.py [address]     # default address 22
"""
import sys
import time

from ni_gpib_usb_hs import Agilent34401A

addr = int(sys.argv[1]) if len(sys.argv) > 1 else 22

with Agilent34401A(addr=addr) as dmm:
    print("idn:", dmm.idn())

    # one-shot, autoranged
    print("MEAS DCV (autorange):", dmm.voltage_dc())

    # low-noise repeated reads on a fixed range
    dmm.configure_dc_volts(rng=10, nplc=10)
    print("5x low-noise DCV @ 10 NPLC:")
    for i in range(5):
        print(f"  [{i}] {dmm.read():+.8e} V")
        time.sleep(0.1)
    print("mean of 10:", dmm.read_average(10))

    errs = dmm.errors()
    print("errors:", errs or "none")
