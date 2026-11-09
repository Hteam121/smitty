# Smitty XIAO ESP32S3 firmware

Bridges the reSpeaker XVF3800's I2S audio to Smitty over Wi-Fi (WebSocket, see
`smitty/audio/websocket.py` for the protocol).

This whole folder is **optional**. Smitty works with the reSpeaker plugged into the Mac over
USB, which is the default and the tested path. The Wi-Fi bridge is experimental: the Mac side
is tested against a fake device (`scripts/ws_fake_device.py`) and playback through the real
board works over Wi-Fi, but the microphone direction has not been proven on real hardware yet.

## Get the XVF3800 firmware images

Seeed's firmware images are not in this repository. Download the two you need from
https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY (folder `xmos_firmwares`) into
`firmware/xvf3800/`:

    firmware/xvf3800/respeaker_xvf3800_usb_dfu_firmware_v2.1.1.bin     # USB audio (the default)
    firmware/xvf3800/respeaker_xvf3800_i2s_slave_v1.0.9_16k.bin        # I2S, for the XIAO bridge

Newer versions are fine; update the two file names at the top of `scripts/flash_xvf3800.sh`.

## One-time: XVF3800 must run the I2S firmware

A new board runs the **USB** firmware. For the XIAO path it needs the I2S image. After this
the board is **no longer a USB audio device**; Smitty must run with `SMITTY_AUDIO=websocket`.
Flashing (XMOS USB-C port, the one near the 3.5 mm jack):

    brew install dfu-util
    dfu-util -l                       # should list "reSpeaker DFU Upgrade"
    scripts/flash_xvf3800.sh i2s

To go back to USB: the I2S firmware has no USB DFU, so put the board in safe mode first (unplug
its USB-C, hold the Mute button, plug it back in while holding, release when the LED blinks
red), then run `scripts/flash_xvf3800.sh usb`.

## Build and flash the XIAO

    cp firmware/smitty_xiao/config.h.example firmware/smitty_xiao/config.h   # fill in Wi-Fi + Mac name or IP (git-ignored)
    arduino-cli core install esp32:esp32
    arduino-cli lib install WebSockets
    arduino-cli compile -b esp32:esp32:XIAO_ESP32S3 firmware/smitty_xiao
    arduino-cli upload  -b esp32:esp32:XIAO_ESP32S3 -p /dev/cu.usbmodem101 firmware/smitty_xiao
    arduino-cli monitor -p /dev/cu.usbmodem101 -c baudrate=115200

## Test

    uv run smitty ws-test            # waits for the XIAO, plays a beep + sentence, records 5 s
    SMITTY_AUDIO=websocket uv run smitty run

Without hardware: `uv run scripts/ws_fake_device.py` plays the part of the XIAO.
