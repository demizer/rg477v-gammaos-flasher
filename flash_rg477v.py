#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "mtkclient @ git+https://github.com/bkerler/mtkclient.git",
#     "cyclopts",
#     "rich",
# ]
# ///
"""
GammaOS Flash Tool for Anbernic RG477V

Flashes GammaOS to the RG477V via the MediaTek BROM interface.
Uses CMD:FLASH-ALL to write all partitions in a single DA session
(same protocol as SP Flash Tool).

Usage:
    uv run flash_rg477v.py flash ./RG477V_GammaOS_Next_Full_v1.2.1
    uv run flash_rg477v.py info --image-dir ./RG477V_GammaOS_Next_Full_v1.2.1
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Monkey-patch: skip the CUSTOM extension in mtkclient's DA upload.
#
# The GammaOS DA_BR.bin has a different instruction layout that crashes
# mtkclient's v6 extension patcher.  We don't need the extension — we use
# the DA's native CMD:FLASH-ALL instead of CMD:WRITE-FLASH.
# ---------------------------------------------------------------------------

from mtkclient.Library.DA.xmlflash import xml_lib as _xml_lib_mod  # noqa: E402

_orig_upload_da = _xml_lib_mod.DAXML.upload_da


def _patched_upload_da(self):
    _orig_reinit = self.reinit

    def _reinit_then_disable_patch(*a, **kw):
        result = _orig_reinit(*a, **kw)
        if getattr(self, "mtk", None) and getattr(self.mtk, "daloader", None):
            self.mtk.daloader.patch = False
        return result

    self.reinit = _reinit_then_disable_patch
    try:
        return _orig_upload_da(self)
    finally:
        self.reinit = _orig_reinit


_xml_lib_mod.DAXML.upload_da = _patched_upload_da

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------

import cyclopts  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.logging import RichHandler  # noqa: E402
from rich.panel import Panel  # noqa: E402
from rich.table import Table  # noqa: E402

from mtkclient.Library.mtk_class import Mtk  # noqa: E402
from mtkclient.config.mtk_config import MtkConfig  # noqa: E402
from mtkclient.Library.DA.mtk_da_handler import DaHandler  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEVICE_NAME = "Anbernic RG477V"
CHIP_NAME = "MT6897 (Dimensity 8300 Ultra)"
EXPECTED_HWCODE = 0x1203

# Preloaders written to UFS boot LUAs.
# LUA1 first, LUA0 last (so device can boot from LUA0 if interrupted).
PRELOADER_MAP: list[tuple[str, str]] = [
    ("lu1", "preloader_b.bin"),
    ("lu0", "preloader_a.bin"),
]

# Regions to format (wipe first 4 MiB of each boot LUA).
# LUA1 first — writing zeros to LUA0 wipes the preloader.
FORMAT_REGIONS: list[tuple[str, int, int]] = [
    ("lu1", 0x0, 0x40_0000),
    ("lu0", 0x0, 0x40_0000),
]

CONFLICTING_MODULES = ["cdc_acm", "option"]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

console = Console()
log = logging.getLogger("rg477v")


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
        force=True,
    )
    for name in ("mtkclient", "Port", "Preloader", "DaHandler", "DAXML", "DAconfig", "XmlFlashExt"):
        logging.getLogger(name).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------


def check_root() -> bool:
    if os.geteuid() != 0:
        log.error("This tool requires root.  Re-run with: sudo uv run flash_rg477v.py ...")
        return False
    return True


def check_kernel_modules() -> None:
    try:
        lsmod = subprocess.check_output(["lsmod"], text=True)
    except FileNotFoundError:
        return
    loaded = [mod for mod in CONFLICTING_MODULES if mod in lsmod]
    if loaded:
        log.warning("Kernel modules that may interfere: %s", ", ".join(loaded))
        log.warning("If connection fails: sudo rmmod %s", " ".join(loaded))


def find_scatter(image_dir: Path) -> Path:
    """Find the scatter XML file (128GB or 256GB variant)."""
    for variant in ("128GB", "256GB"):
        path = image_dir / f"MT6897_Android_scatter_{variant}.xml"
        if path.exists():
            return path
    log.error("No scatter XML found in %s", image_dir)
    sys.exit(1)


def resolve_super_image(image_dir: Path) -> str:
    if (image_dir / "super_full.img").exists():
        return "super_full.img"
    if (image_dir / "super_lite.img").exists():
        return "super_lite.img"
    log.error("No super image found")
    sys.exit(1)


def validate_image_dir(image_dir: Path) -> bool:
    missing: list[str] = []
    da_path = image_dir / "download_agent" / "DA_BR.bin"
    if not da_path.exists():
        missing.append(str(da_path))
    find_scatter(image_dir)  # exits if not found
    for _, fname in PRELOADER_MAP:
        if not (image_dir / fname).exists():
            missing.append(str(image_dir / fname))
    # Check partition images referenced by scatter
    # (the DA will request them; we verify upfront)
    import xml.etree.ElementTree as ET
    scatter = find_scatter(image_dir)
    tree = ET.parse(scatter)
    for pi in tree.getroot().iter("partition_index"):
        dl = pi.find("is_download")
        fn = pi.find("file_name")
        if dl is not None and dl.text == "true" and fn is not None and fn.text:
            if not (image_dir / fn.text).exists():
                missing.append(str(image_dir / fn.text))
    if missing:
        log.error("Missing files:")
        for f in missing:
            log.error("  %s", f)
        return False
    return True


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

CONNECTION_INSTRUCTIONS = (
    "[bold yellow]1.[/] Power off the device completely\n"
    "[bold yellow]2.[/] Hold [bold]Volume Down + Power[/] for 30 seconds\n"
    "[bold yellow]3.[/] Plug in the USB cable while still holding both buttons\n"
    "[bold yellow]4.[/] Release when the tool detects the device"
)


def connect_da(loader: str, preloader: str) -> tuple[Mtk, DaHandler]:
    console.print(Panel(CONNECTION_INSTRUCTIONS, title=f"[bold]{DEVICE_NAME}[/]", border_style="cyan"))
    log.info("Waiting for device in BROM mode ...")

    config = MtkConfig(loglevel=logging.WARNING)
    config.loader = loader
    config.preloader = preloader

    mtk = Mtk(config=config, loglevel=logging.WARNING)
    if not mtk.preloader.init():
        log.error("Failed to connect to BROM")
        sys.exit(1)

    assert mtk.config.hwcode == EXPECTED_HWCODE, (
        f"Unexpected hwcode: {hex(mtk.config.hwcode)} (expected {hex(EXPECTED_HWCODE)})"
    )

    log.info("Device detected")
    log.info("  Chip:     %s", CHIP_NAME)
    log.info("  HW code:  %s", hex(mtk.config.hwcode))
    if mtk.config.meid:
        log.info("  ME_ID:    %s", mtk.config.meid.hex().upper())

    da_handler = DaHandler(mtk, logging.WARNING)
    mtk = da_handler.connect(mtk, directory=".")
    if mtk is None:
        log.error("DA handler connect failed")
        sys.exit(1)

    mtk = da_handler.configure_da(mtk)
    if mtk is None:
        log.error("DA configure failed")
        sys.exit(1)

    log.info("Download Agent loaded")
    return mtk, da_handler


def reconnect(mtk: Mtk, loader: str, preloader: str) -> Mtk:
    """Close USB and reconnect. The device is boot-looping so mtkclient
    catches it on the next BROM cycle."""
    log.info("  Reconnecting ...")
    try:
        mtk.port.close(reset=True)
    except Exception:
        pass
    time.sleep(2)
    mtk, _ = connect_da(loader=loader, preloader=preloader)
    return mtk


# ---------------------------------------------------------------------------
# Flash operations
# ---------------------------------------------------------------------------


def do_format(mtk: Mtk, loader: str, preloader: str) -> Mtk:
    """Format UFS-LUA0 and UFS-LUA1 by writing zeros.
    One write per session — reconnect between each."""
    log.info("Formatting UFS boot regions ...")
    for i, (lutype, addr, length) in enumerate(FORMAT_REGIONS):
        if i > 0:
            mtk = reconnect(mtk, loader, preloader)
        label = lutype.upper().replace("LU", "LUA")
        log.info("  %s  addr=0x%x  length=0x%x", label, addr, length)
        zeros = b"\x00" * length
        ok = mtk.daloader.writeflash(addr=addr, length=length, wdata=zeros, parttype=lutype)
        if not ok:
            log.error("Failed to format %s", label)
            sys.exit(1)
        log.info("  %s formatted", label)
    return mtk


def do_flash_all(mtk: Mtk, image_dir: Path) -> None:
    """Write all partitions using CMD:FLASH-ALL.

    This is the same protocol SP Flash Tool uses.  The DA parses the
    scatter XML, then requests each image file via CMD:DOWNLOAD-FILE.
    All partitions are written in a single session — no reconnects.
    """
    from mtkclient.Library.DA.xmlflash.xml_lib import DwnFile, FileSysOp

    scatter_path = find_scatter(image_dir)
    scatter_data = scatter_path.read_bytes()
    da = mtk.daloader.da  # DAXML instance — direct protocol access

    log.info("Sending CMD:FLASH-ALL (scatter: %s, %s)", scatter_path.name, _human_size(len(scatter_data)))

    # Build the CMD:FLASH-ALL XML.
    # DA disassembly confirms:
    #   - <scatter-file> is a file path/reference — the DA requests it via CMD:DOWNLOAD-FILE
    #   - path_separator: '/' or '\' (checked against chars in scatter file paths)
    # The scatter-file value is used as the "info" field in CMD:DOWNLOAD-FILE.
    # DA disassembly (FUN_40005130) shows it extracts:
    #   da/arg/path_separator — '/' or '\'
    #   da/arg/source_file — scatter file path (MUST contain a '/' or '\')
    #     The DA scans this path backwards to find the directory separator.
    #     If no separator is found, it errors with "Unknow path separator."
    #     SP Flash Tool sends paths like "D:/images/scatter.xml".
    cmd_xml = da.cmd.create_cmd("FLASH-ALL", {
        "arg": [
            "<path_separator>/</path_separator>",
            f"<source_file>./{scatter_path.name}</source_file>",
        ]
    })

    log.debug("CMD XML: %s", cmd_xml[:200])

    # Send the command (noack=True because we handle the response loop ourselves)
    if not da.xsend(data=cmd_xml):
        log.error("Failed to send CMD:FLASH-ALL")
        sys.exit(1)

    # First response should be "OK"
    resp = da.get_response()
    if resp != "OK":
        log.error("CMD:FLASH-ALL rejected: %s", resp)
        sys.exit(1)

    log.info("DA accepted CMD:FLASH-ALL — serving files as requested")

    # The DA now calls its "read host file" function which sends CMD:DOWNLOAD-FILE
    # to request the scatter file from us. We need to handle this properly.
    # Let's read the raw response to see what the DA sends.
    partition_count = 0
    while True:
        raw = da.get_response()
        log.debug("DA raw response: %s", raw[:200] if isinstance(raw, str) and len(raw) > 200 else raw)

        # Re-parse as command result manually
        from mtkclient.Library.DA.xmlflash.xml_lib import get_field, DwnFile, FileSysOp
        cmd = get_field(raw, "command") if isinstance(raw, str) else ""
        log.debug("  Parsed cmd: %s", cmd)

        if cmd == "CMD:DOWNLOAD-FILE":
            checksum = get_field(raw, "checksum")
            info = get_field(raw, "info")
            source_file = get_field(raw, "source_file")
            packet_length_str = get_field(raw, "packet_length")
            packet_length = int(packet_length_str, 16) if packet_length_str else 0x1000
            result = DwnFile(checksum, info, source_file, packet_length)
            da.ack()
            log.debug("  CMD:DOWNLOAD-FILE: info=%s source=%s pkt=%d", info, source_file, packet_length)
        elif cmd == "CMD:END":
            result = get_field(raw, "result")
            log.debug("  CMD:END result=%s", result)
        elif cmd == "CMD:FILE-SYS-OPERATION":
            key = get_field(raw, "key")
            file_path = get_field(raw, "file_path")
            result = FileSysOp(key, file_path)
            # Don't ack here — each handler below sends the full response sequence
        elif cmd == "CMD:START":
            da.ack()
            continue
        elif cmd == "CMD:PROGRESS-REPORT":
            da.ack()
            # Consume progress reports
            pdata = ""
            while pdata != "OK!EOT":
                pdata = da.get_response()
                da.ack()
            continue
        else:
            result = raw

        if cmd == "CMD:END":
            if result == "OK":
                log.info("CMD:FLASH-ALL completed successfully (%d partitions)", partition_count)
            else:
                log.error("CMD:FLASH-ALL failed: %s", result)
                sys.exit(1)
            # ACK the END and wait for START
            da.ack()
            scmd, sresult = da.get_command_result()
            if scmd == "CMD:START":
                pass  # DA is ready for next command
            break

        elif cmd == "CMD:DOWNLOAD-FILE":
            assert isinstance(result, DwnFile), f"Expected DwnFile, got {type(result)}"
            info = result.info
            source = result.source_file

            # Determine which file the DA wants
            if "scatter" in info.lower():
                log.info("  Serving scatter: %s (%s)", scatter_path.name, _human_size(len(scatter_data)))
                file_data = scatter_data
            else:
                filename = _resolve_da_filename(info, source, image_dir)
                filepath = image_dir / filename
                assert filepath.exists(), f"DA requested '{filename}' but file not found: {filepath}"
                fsize = filepath.stat().st_size
                partition_count += 1
                log.info("  [%d] %s (%s)", partition_count, filename, _human_size(fsize))
                file_data = filepath.read_bytes()

            # Upload file data using the DA's protocol:
            # 1. Send length ACK
            # 2. DA sends OK
            # 3. Loop: send ACK(0), DA sends OK, send chunk, DA sends OK
            # 4. Send final ACK
            # (Don't use da.upload() — it expects CMD:END+CMD:START which
            #  FLASH-ALL doesn't send between files)
            pkt_len = result.packet_length
            data_len = len(file_data)

            def _get_ok_response():
                """Read responses, consuming PROGRESS-REPORTs, until OK or error."""
                while True:
                    r = da.get_response()
                    if "PROGRESS-REPORT" in r:
                        da.ack()
                        continue
                    return r

            da.ack_value(data_len)
            resp = _get_ok_response()
            if "OK" not in resp:
                log.error("  Upload rejected: %s", resp)
                sys.exit(1)
            pos = 0
            remaining = data_len
            while remaining > 0:
                da.ack_value(0)
                resp = _get_ok_response()
                if "OK" not in resp:
                    log.error("  Upload error at offset 0x%x: %s", pos, resp)
                    sys.exit(1)
                chunk = file_data[pos:pos + pkt_len]
                da.xsend(data=chunk)
                resp = _get_ok_response()
                if "OK" not in resp:
                    log.error("  Upload error after chunk at 0x%x: %s", pos, resp)
                    sys.exit(1)
                pos += len(chunk)
                remaining -= pkt_len
            # No trailing ACK — FUN_40032140 returns immediately after receiving all data
            log.debug("  Upload complete: %d bytes", data_len)

        elif cmd == "CMD:FILE-SYS-OPERATION":
            assert isinstance(result, FileSysOp), f"Expected FileSysOp, got {type(result)}"
            if result.key == "FILE-SIZE":
                filename = _resolve_da_filename("", result.file_path, image_dir)
                filepath = image_dir / filename
                if filepath.exists():
                    fsize = filepath.stat().st_size
                    log.debug("  FILE-SIZE %s -> %d", filename, fsize)
                    da.ack()  # first OK
                    da.ack_value(fsize)  # second OK@size
                else:
                    log.debug("  FILE-SIZE %s -> 0 (not found)", result.file_path)
                    da.ack()
                    da.ack_value(0)
            elif result.key == "EXISTS":
                # FUN_40032d20 protocol: read OK, then read OK@value
                fpath = result.file_path.lstrip("./")
                filepath = image_dir / fpath
                exists = filepath.exists()
                log.debug("  EXISTS %s -> %s", fpath, exists)
                da.ack()  # first OK
                da.ack_text("EXISTS" if exists else "NOT")  # second OK@value
            else:
                log.warning("  Unknown FILE-SYS-OPERATION: %s %s", result.key, result.file_path)
                da.ack()
                da.ack()

        elif cmd == "CMD:PROGRESS-REPORT":
            # Already handled inside get_command_result
            pass

        elif cmd == "":
            # Empty response — might be an error
            log.error("Unexpected empty response from DA")
            sys.exit(1)

        else:
            log.warning("  Unhandled DA command: %s", cmd)
            da.ack()


def _resolve_da_filename(info: str, source: str, image_dir: Path) -> str:
    """Match a DA file request to a local filename.

    The DA may reference files by their scatter XML filename, or by
    a MEM:// address.  We match against files that exist in image_dir.
    """
    # Try info field first (often contains the filename directly)
    if info:
        # Strip path components — DA might send full paths
        candidate = Path(info).name
        if (image_dir / candidate).exists():
            return candidate

    # Try source_file field — might contain a filename
    if source and "MEM://" not in source:
        candidate = Path(source).name
        if (image_dir / candidate).exists():
            return candidate

    # Try extracting from the source path
    for part in source.replace("\\", "/").split("/"):
        if part.endswith(".img") or part.endswith(".bin") or part.endswith(".xml"):
            if (image_dir / part).exists():
                return part

    # Last resort: return the info as-is
    return info


def do_write_preloaders(mtk: Mtk, image_dir: Path, loader: str, preloader: str) -> Mtk:
    """Write preloader binaries to UFS boot LUAs.
    One write per session — reconnect between each."""
    log.info("Writing preloaders ...")
    for i, (lutype, fname) in enumerate(PRELOADER_MAP):
        if i > 0:
            mtk = reconnect(mtk, loader, preloader)
        filepath = image_dir / fname
        fsize = filepath.stat().st_size
        label = lutype.upper().replace("LU", "LUA")
        log.info("  %s <- %s (%s)", label, fname, _human_size(fsize))
        ok = mtk.daloader.writeflash(addr=0, length=fsize, filename=str(filepath), parttype=lutype)
        if not ok:
            log.error("Failed to write %s to %s", fname, label)
            sys.exit(1)
        log.info("  %s written", label)
    return mtk


def do_reboot(mtk: Mtk) -> None:
    log.info("Disconnecting ...")
    try:
        mtk.port.close(reset=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _human_size(nbytes: int | float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}" if unit != "B" else f"{nbytes} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TiB"


def print_partition_table(image_dir: Path) -> None:
    import xml.etree.ElementTree as ET
    scatter = find_scatter(image_dir)
    tree = ET.parse(scatter)
    table = Table(title=f"Partitions ({scatter.name})")
    table.add_column("Name", style="cyan", min_width=18)
    table.add_column("File", style="white")
    table.add_column("Offset", style="green")
    table.add_column("Size", style="yellow", justify="right")
    table.add_column("Download", style="magenta")
    for pi in tree.getroot().iter("partition_index"):
        name_el = pi.find("partition_name")
        fn_el = pi.find("file_name")
        dl_el = pi.find("is_download")
        addr_el = pi.find("linear_start_addr")
        size_el = pi.find("partition_size")
        if name_el is None:
            continue
        name = name_el.text or ""
        fname = (fn_el.text or "") if fn_el is not None else ""
        dl = (dl_el.text or "false") if dl_el is not None else "false"
        addr = addr_el.text or "0" if addr_el is not None else "0"
        size = int(size_el.text or "0", 16) if size_el is not None else 0
        if dl == "true":
            table.add_row(name, fname, addr, _human_size(size), "yes")
    console.print(table)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

app = cyclopts.App(
    name="flash-rg477v",
    help=f"GammaOS Flash Tool for {DEVICE_NAME}",
    version="2.0.0",
)


@app.command
def flash(
    image_dir: Path,
    *,
    skip_format: bool = False,
    skip_partitions: bool = False,
    skip_preloaders: bool = False,
    verbose: bool = False,
):
    """Flash GammaOS to the RG477V.

    IMAGE_DIR is the path to the extracted GammaOS build directory
    (e.g. RG477V_GammaOS_Next_Full_v1.2.1/).
    """
    setup_logging(verbose)

    if not check_root():
        sys.exit(1)
    check_kernel_modules()

    image_dir = image_dir.resolve()
    if not validate_image_dir(image_dir):
        sys.exit(1)

    log.info("Image directory: %s", image_dir)
    log.info("Scatter file:    %s", find_scatter(image_dir).name)
    log.info("Super image:     %s", resolve_super_image(image_dir))

    loader = str(image_dir / "download_agent" / "DA_BR.bin")
    preloader = str(image_dir / "preloader_a.bin")

    # Connect
    mtk, _ = connect_da(loader=loader, preloader=preloader)

    # Format boot LUAs (1 write per session, 2 reconnects)
    if not skip_format:
        mtk = do_format(mtk, loader=loader, preloader=preloader)
        mtk = reconnect(mtk, loader, preloader)
    else:
        log.info("Skipping format (--skip-format)")

    # Write all partitions in one session via CMD:FLASH-ALL
    if not skip_partitions:
        do_flash_all(mtk, image_dir)
    else:
        log.info("Skipping partitions (--skip-partitions)")

    # Write preloaders (1 write per session, 1 reconnect)
    if not skip_preloaders:
        mtk = reconnect(mtk, loader, preloader)
        mtk = do_write_preloaders(mtk, image_dir, loader=loader, preloader=preloader)
    else:
        log.info("Skipping preloaders (--skip-preloaders)")

    # Reboot
    do_reboot(mtk)

    console.print(Panel(
        "[bold green]Flash complete![/]\n\n"
        "The device will reboot into GammaOS.\n"
        "First boot may take longer than normal.",
        title="Done",
        border_style="green",
    ))


@app.command
def info(
    *,
    image_dir: Path | None = None,
    verbose: bool = False,
):
    """Show partition table from scatter XML (no device needed)."""
    setup_logging(verbose)
    if not image_dir:
        log.error("Provide --image-dir")
        sys.exit(1)
    print_partition_table(image_dir.resolve())


if __name__ == "__main__":
    app()
