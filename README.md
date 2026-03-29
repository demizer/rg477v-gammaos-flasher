#  GammaOS Flasher for Anbernic RG477V

> [!caution]
> **USE AT YOUR OWN RISK.** The script and instructions in this document interact directly with your device's flash storage via the MediaTek BROM interface. Incorrect use can **permanently brick your device**. I take no responsibility for any damage, data loss, or bricked devices resulting from following these instructions. Proceed with an abundance of caution. Make sure you have the [stock firmware unbricker](https://github.com/TheGammaSqueeze/Anbernic_RG477V_Unbricker) downloaded before you start.
>
> This tool depends on [mtkclient](https://github.com/bkerler/mtkclient), a community-driven effort to reverse-engineer proprietary MediaTek protocols. It is a work in progress with known bugs and incomplete support for newer chipsets (including the MT6897 used in this device). The flash script works around these limitations, but upstream changes could break compatibility at any time.

The os that came with the device has a bunch of crap on it. I want OnionOS experience but OnionOS does not support this device.

This guide and the `flash_rg477v.py` tool were created with the assistance of [Claude Code](https://claude.ai/claude-code). The `CMD:FLASH-ALL` protocol was reverse-engineered from the DA binary using Ghidra.

Written and tested on Arch Linux — the script should be portable to any system with Python 3.12+ and [uv](https://docs.astral.sh/uv/).

## Reference

* [GammaOS Installation Guide (SP Flash Tool, Full and Lite Builds)](https://github.com/TheGammaSqueeze/GammaOSNext/wiki/GammaOS-Installation-Guide-for-Anbernic-RG557,-RG477M,-and-RG477V-(SP-Flash-Tool,-Full-and-Lite-Builds)) — official wiki (Windows-only)
* [GammaOS Next - Install guide video](https://www.youtube.com/watch?v=u_NH83XWAAU)
* [Stock firmware unbricker (RG477V)](https://github.com/TheGammaSqueeze/Anbernic_RG477V_Unbricker)
* [mtkclient](https://github.com/bkerler/mtkclient) — open-source MediaTek BROM/DA client (used as a library by the flash script)
* [GammaOSNext releases](https://github.com/TheGammaSqueeze/GammaOSNext) — ROM source and releases
* [Ghidra](https://ghidra-sre.org/) — used to reverse-engineer the DA binary's `CMD:FLASH-ALL` protocol

## Determine Storage Size

> [!warning]
> These devices come in **128GB** and **256GB** variants. You **must** know which one you have — the flash script selects the correct scatter file based on this. Flashing with the wrong size can brick the device.

1. Boot into stock Android
2. Go to **Settings > Storage**
3. Note the total storage size

## Installation

### 1. Extract the Release

The release comes as a multi-part 7z archive with a password (Patreon early access, public after 60 days).

```bash
7z x RG477V_GammaOS_Next_v1.2.1.7z.001
```

After extraction you should have:
- `RG477V_GammaOS_Next_Full_v1.2.1/`
- `RG477V_GammaOS_Next_Lite_v1.2.1/`

### 2. Flash

The script at `flash_rg477v.py` handles the entire flash process in one command using [mtkclient](https://github.com/bkerler/mtkclient). It communicates directly with the MediaTek BROM via libusb — no kernel module blacklisting or driver setup needed. Dependencies are managed inline by `uv`.

```bash
sudo uv run flash_rg477v.py flash ./RG477V_GammaOS_Next_Full_v1.2.1
```

The script will prompt you to connect the device. The sequence is:

1. Power off the device completely
2. Hold **Volume Down + Power** for 30 seconds
3. Plug in the USB cable while still holding both buttons
4. Release when the tool detects the device

**What the script does (in order):**

1. Connects to the MT6897 BROM and uploads the Download Agent (`DA_BR.bin`)
2. Formats UFS-LUA1 and UFS-LUA0 boot regions (first 4 MiB of each, with reconnect between)
3. Sends `CMD:FLASH-ALL` with the scatter XML — the DA writes all 12 partitions in a single session (misc, vbmeta_a/b, metadata, boot_a/b, vendor_boot_a/b, init_boot_a/b, super, userdata)
4. Writes `preloader_b.bin` to UFS-LUA1 and `preloader_a.bin` to UFS-LUA0 (with reconnect between)
5. Hold **Power + Volume Down** for 10 seconds to reboot into GammaOS

Individual steps can be skipped with `--skip-format`, `--skip-partitions`, `--skip-preloaders`.

> [!note]
> The script uses `CMD:FLASH-ALL` (the same protocol SP Flash Tool uses) to write all partitions in one session. This was discovered by reverse-engineering the DA binary in Ghidra — mtkclient doesn't implement this command. The DA's CUSTOM extension is skipped (incompatible binary signatures) but isn't needed for FLASH-ALL.

### 3. Done

First boot may take longer than normal.

### flash_rg477v.py

Source: [`flash_rg477v.py`](flash_rg477v.py)

## Thermal Throttling Patch

GammaOS ships a thermal safety protection Magisk module that lowers thermal thresholds for stability. Two flavors:

- **Maximum stability** (recommended): `thermal_safety_protection.zip`
- **Maximum safe performance** (testing): `thermal_safety_protection_2.zip`

### Option A: Install via Magisk (post-flash)

1. Copy the zip to device internal storage
2. Open Magisk app (cancel any update prompts)
3. Go to **Modules** tab > **Install from storage**
4. Select the zip, reboot

### Option B: Bake into super image (pre-flash)

This patches the vendor partition inside `super_full.img` so the thermal scripts are built-in — no Magisk module needed.

Requires: `lpunpack`, `lpmake`, `e2fsck` (from `android-tools` on Arch)

```bash
# Extract vendor_a from super image
mkdir -p /tmp/gammaos_work
lpunpack -p vendor_a super_full.img /tmp/gammaos_work/

# Mount the vendor image
mkdir -p /tmp/gammaos_work/vendor_mount
sudo mount -o loop,rw /tmp/gammaos_work/vendor_a.img /tmp/gammaos_work/vendor_mount

# Extract the thermal module zip
unzip thermal_safety_protection.zip -d /tmp/thermal_patch

# Copy patched scripts into vendor
sudo cp /tmp/thermal_patch/system/vendor/bin/thermal_safety_guard.sh \
        /tmp/thermal_patch/system/vendor/bin/customizationload.sh \
        /tmp/thermal_patch/system/vendor/bin/setclock_max.sh \
        /tmp/gammaos_work/vendor_mount/bin/
sudo chmod 755 /tmp/gammaos_work/vendor_mount/bin/thermal_safety_guard.sh \
               /tmp/gammaos_work/vendor_mount/bin/customizationload.sh \
               /tmp/gammaos_work/vendor_mount/bin/setclock_max.sh

# Unmount
sudo umount /tmp/gammaos_work/vendor_mount

# Write patched vendor back into super image at the correct offset
# vendor_a starts at sector 8204288 in the super image (from lpdump)
dd if=/tmp/gammaos_work/vendor_a.img of=super_full.img bs=512 seek=8204288 conv=notrunc

# Verify the super image size matches the scatter file (9,663,676,416 bytes for 128GB)
truncate -s 9663676416 super_full.img
```

## Troubleshooting

**Device not detected:**
1. Make sure the device is **powered off**
2. Hold **Power + Volume Down** for at least 60 seconds
3. While still holding, plug in the USB cable
4. Detection can take 30+ seconds — be patient

It is also possible to just hold down the "Power + Volume Down" keys to perform a reboot, then unplug and plugin the USB cable.

# Bootloader unlock (Doesn't work)

> [!warning]
> I was unable to use the instructions below. The volume up and down keys do not work here. According to GammaOS the bootloader cannot be unlocked on these devices!

Steps to Unlock Bootloader.

1. **Enable Developer Options:** Go to **Settings > About Device** and click on the "Build Number" 7 times.
2. **Enable OEM Unlocking:** Go to **Settings > System > Developer Options** and toggle on **OEM Unlocking** and **USB Debugging**.
3. **Boot to Fastboot:** Connect the device to your PC. Open a command prompt/terminal on your PC and run:
    `adb reboot bootloader`
    _(Alternatively, hold Power + Volume Down while turning on)._
4. **Unlock Command:** Once the device is in fastboot mode (usually showing a black screen with an Anbernic logo or a prompt), run the following command:
    `fastboot flashing unlock`
5. **Confirm on Device:** The screen will ask you to confirm. Use the volume keys to highlight "Yes" or "Unlock" and press the power button to confirm.
6. **Reboot:** Once complete, run `fastboot reboot`.
