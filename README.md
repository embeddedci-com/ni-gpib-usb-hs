# ni-gpib-usb-hs

A **pure-Python, user-space driver for the National Instruments GPIB-USB-HS
adapter itself** — it works with **any GPIB instrument** at any address, not
just one model. No NI-488.2. No `linux-gpib`. No kernel module. Just
[pyusb](https://github.com/pyusb/pyusb) + [libusb](https://libusb.info/)
talking to the adapter directly.

```python
from ni_gpib_usb_hs import NIUSBGPIB

with NIUSBGPIB() as gpib:
    print(gpib.query(22, "*IDN?"))    # or any instrument at any address
```

A ready-to-use wrapper for the **Agilent/HP/Keysight 34401A** multimeter ships
alongside the driver as a worked example — swap in your own instrument's SCPI
commands and it works the same way (see [Quickstart](#quickstart)):

```python
from ni_gpib_usb_hs import Agilent34401A

with Agilent34401A(addr=22) as dmm:
    print(dmm.idn())            # HEWLETT-PACKARD,34401A,0,10-5-2
    print(dmm.voltage_dc())     # 0.0000334
```

## Why this exists

The GPIB-USB-HS is a great, cheap way to get a modern host talking to old GPIB
bench gear — except:

- **NI ships no driver for Apple Silicon.** NI-488.2/NI-VISA for macOS is
  Intel-only, and the kernel extension doesn't even load on macOS 13+.
- **`linux-gpib`** is the usual fallback on Linux/Raspberry Pi, but it can be a
  pain to build (out-of-tree kernel module, autotools, Python-binding install
  paths that vary by distro) — and on the hardware this was built against, a
  genuine, boxed NI GPIB-USB-HS reproducibly failed every *addressed* GPIB
  transfer under `linux-gpib`'s `ni_usb` kernel driver (deterministic
  `NIUSB_ADDRESSING_ERROR` / USB `-110` timeouts), even against `git master` and
  after ruling out cabling, the instrument, USB autosuspend, and every config
  knob.

The interesting part: the *same adapter*, driven directly over `libusb` with a
from-scratch reimplementation of NI's wire protocol, worked immediately — clean
register reads/writes, clean addressed transfers, real `*IDN?` and `READ?`
replies from a 34401A. The fault was in the kernel driver's interrupt-endpoint
handling on that particular host, not the hardware. This project is that
reimplementation, cleaned up: synchronous bulk request/response only, no
interrupt-endpoint monitoring, no kernel module — which means it also runs
identically on **macOS (incl. Apple Silicon) and Linux**.

## Install

```bash
# libusb (native library) — pyusb needs this
brew install libusb          # macOS
# sudo apt install libusb-1.0-0   # Debian/Ubuntu/Raspberry Pi OS

pip install ni-gpib-usb-hs
```

<sub>Want the latest unreleased code instead? Install from source — see [Development](#development) below.</sub>

### Linux permissions

On Linux, plain USB device nodes are root-owned by default. Install the udev
rule so your user can access the adapter without `sudo`:

```bash
sudo cp udev/99-ni-gpib-usb-hs.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
# log out/in (or add yourself to the "plugdev" group) for group membership to apply
```

macOS needs no special permissions.

## Quickstart

```bash
python examples/idn.py 22          # *IDN? whatever is at GPIB address 22
python examples/measure_dc.py 22   # a few different ways to read DC volts
```

### Talk to any GPIB instrument

`NIUSBGPIB` is a thin, general-purpose GPIB controller — it just addresses a
device and moves bytes, so it works with anything on the bus (SCPI or not):

```python
from ni_gpib_usb_hs import NIUSBGPIB

with NIUSBGPIB() as gpib:
    print(gpib.query(22, "*IDN?"))
    gpib.write(5, "OUTP ON")
    print(gpib.query(5, "MEAS:VOLT?"))
```

### The bundled 34401A wrapper

`Agilent34401A` is a convenience wrapper around `NIUSBGPIB` for one specific
instrument's command set (also works with HP 34401A / Keysight 34401A, which
share it):

```python
from ni_gpib_usb_hs import Agilent34401A

with Agilent34401A(addr=22) as dmm:
    # one-shot, autoranged
    print(dmm.voltage_dc())
    print(dmm.resistance_4wire())
    print(dmm.frequency())

    # fixed-range, low-noise, repeated reads (faster than MEASure? each time)
    dmm.configure_dc_volts(rng=10, nplc=10)
    for _ in range(5):
        print(dmm.read())
    print("mean of 10:", dmm.read_average(10))

    print(dmm.errors())   # drains SYSTem:ERRor? queue
```

### Writing a wrapper for your own instrument

If you have a different instrument, [`ni_gpib_usb_hs/agilent34401a.py`](ni_gpib_usb_hs/agilent34401a.py)
is a ~180-line template: a small class holding a GPIB address and an
`NIUSBGPIB`, with methods that just call `self.write(...)`/`self.query(...)`
with your instrument's SCPI commands. Or skip the wrapper entirely and use
`NIUSBGPIB` directly, as above — plenty of use cases don't need one.

## Hardware

- Tested against a genuine, Hungary-made **NI GPIB-USB-HS** (USB ID `3923:709b`),
  talking to an **HP/Agilent 34401A**.
- Should work with any instrument on the bus, since the driver just implements
  the standard NI-USB wire protocol (register read/write, command, addressed
  write/read) — the 34401A wrapper is a convenience layer on top.
- Not tested against the **GPIB-USB-HS+** (different USB PID) or the older
  **GPIB-USB-B**; PRs welcome if you have one.

## How it works

The adapter exposes a Cypress FX2 USB front-end in front of an NI TNT4882 GPIB
controller chip. `NIUSBGPIB`:

1. Performs the vendor control-transfer "ready" handshake (serial number +
   poll-ready).
2. Bulk-writes a 26-entry TNT4882 register init sequence (reset, handshake mode,
   T1 delay, interrupt-mask setup, controller address, system-controller bit).
3. Issues GPIB **command** bytes (UNL/talk-address/listen-address) over a bulk
   OUT/IN request-response pair to address instruments.
4. Issues **write**/**read** bulk transfers for the actual data, with EOI
   handling and NI's chunked read-response framing.

Every operation is a synchronous bulk OUT then bulk IN — there is no async
interrupt-endpoint status monitoring, which keeps the implementation small and
sidesteps whatever the kernel driver's interrupt path was tripping over.

See [`ni_gpib_usb_hs/controller.py`](ni_gpib_usb_hs/controller.py) for the full
implementation (well-commented, ~300 lines).

## Scope / limitations

- One controller, one instrument addressed at a time. No serial poll, no SRQ,
  no parallel poll.
- No secondary GPIB addressing.
- This is enough to drive the vast majority of SCPI bench instruments (anything
  you'd script with `*IDN?` / `MEAS?` / `READ?` style commands), but if you need
  interrupt-driven SRQ handling or multi-controller bus sharing, use
  `linux-gpib` or NI-488.2 instead.

## Development

```bash
git clone https://github.com/embeddedci-com/ni-gpib-usb-hs
cd ni-gpib-usb-hs
python -m venv .venv && source .venv/bin/activate
pip install -e .
python examples/idn.py
```

## Publishing (maintainers)

This repo publishes to PyPI via [Trusted Publishing](https://docs.pypi.org/trusted-publishers/)
(OIDC) — no API tokens stored in CI. One-time setup, then every GitHub Release
publishes automatically.

**One-time setup:**

1. Create the project on PyPI (either publish once manually, or use PyPI's
   ["pending publisher"](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
   flow to register the name before any upload exists).
2. On PyPI → your project → **Settings → Publishing**, add a trusted publisher:
   - Owner: `embeddedci-com` (or your GitHub org/user)
   - Repository name: `ni-gpib-usb-hs`
   - Workflow name: `publish.yml`
   - Environment name: `pypi`
3. On GitHub → repo → **Settings → Environments**, create an environment named
   `pypi` (matches the workflow). Optionally add required reviewers for extra
   safety before a publish runs.

**Every release:**

1. Bump `version` in `pyproject.toml` and `ni_gpib_usb_hs/__init__.py`.
2. Commit, tag, and push:
   ```bash
   git commit -am "release: v0.1.0"
   git tag v0.1.0
   git push && git push --tags
   ```
3. On GitHub, **Releases → Draft a new release**, pick the tag, publish it.
   The `Publish to PyPI` workflow (`.github/workflows/publish.yml`) runs
   automatically and uploads the build to PyPI.

To test the packaging without publishing, run the `build` job locally:

```bash
python -m pip install build
python -m build          # produces dist/*.whl and dist/*.tar.gz
```

Or use `workflow_dispatch` to trigger `publish.yml` manually from the Actions
tab if you ever need to re-run a publish.

## Credits

The GPIB-USB-HS wire protocol implemented here (register layout, command
framing, TNT4882 init sequence) is derived from the
[linux-gpib](https://linux-gpib.sourceforge.io/) project's `ni_usb` kernel
driver (`drivers/gpib/ni_usb/ni_usb_gpib.c`), © Frank Mori Hess and
contributors, licensed GPL-2.0. This project is a from-scratch, user-space
reimplementation of that protocol in Python — no code was copied — released
under the same license (GPL-2.0-only) in respect of that heritage.

## License

GPL-2.0-only. See [LICENSE](LICENSE).
