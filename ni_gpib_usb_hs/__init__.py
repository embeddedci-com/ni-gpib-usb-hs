# SPDX-License-Identifier: GPL-2.0-only
"""Pure-Python user-space driver for the NI GPIB-USB-HS, with an Agilent 34401A wrapper.

    from ni_gpib_usb_hs import NIUSBGPIB, Agilent34401A, GpibError
"""
from .controller import NIUSBGPIB, GpibError, NI_VID, NI_GPIB_USB_HS_PID
from .agilent34401a import Agilent34401A

__version__ = "0.1.0"
__all__ = ["NIUSBGPIB", "Agilent34401A", "GpibError", "NI_VID", "NI_GPIB_USB_HS_PID"]
