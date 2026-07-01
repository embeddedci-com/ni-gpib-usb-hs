#!/usr/bin/env python3
"""Quickstart: identify whatever instrument sits at a GPIB address.

    python examples/idn.py [address]     # default address 22
"""
import sys

from ni_gpib_usb_hs import NIUSBGPIB

addr = int(sys.argv[1]) if len(sys.argv) > 1 else 22

with NIUSBGPIB() as gpib:
    print(f"adapter serial: {gpib.serial}")
    print(f"GPIB {addr} *IDN? -> {gpib.query(addr, '*IDN?')}")
