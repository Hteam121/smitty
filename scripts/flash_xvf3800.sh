#!/bin/zsh
# Switch the reSpeaker XVF3800 between its two firmwares.
#   scripts/flash_xvf3800.sh usb    -> USB audio device (Smitty over the cable: `uv run smitty run`)
#   scripts/flash_xvf3800.sh i2s    -> I2S for the XIAO ESP32S3 (Smitty over Wi-Fi: SMITTY_AUDIO=websocket)
#
# The board must be visible to dfu-util. On USB firmware it always is. On I2S firmware it is not:
# put it in SAFE MODE first -> unplug the board's USB-C (the port next to the 3.5 mm jack), hold the
# Mute button, plug it back in while holding, release when the LED blinks red.
set -e
DIR="${0:A:h}/../firmware/xvf3800"
case "$1" in
  usb) IMG="$DIR/respeaker_xvf3800_usb_dfu_firmware_v2.1.1.bin" ;;
  i2s) IMG="$DIR/respeaker_xvf3800_i2s_slave_v1.0.9_16k.bin" ;;
  *) echo "usage: $0 usb|i2s"; exit 2 ;;
esac
[ -f "$IMG" ] || { echo "firmware image missing: $IMG"; echo "Download it from https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY (xmos_firmwares) into firmware/xvf3800/, see firmware/README.md"; exit 1; }
if ! dfu-util -l 2>/dev/null | grep -q "Found DFU: \[2886:001a\]"; then
  echo "The board is not visible over USB."
  echo "If it is on I2S firmware, enter SAFE MODE: unplug the board's USB-C (next to the 3.5 mm jack),"
  echo "hold the Mute button, plug it back in while holding, release when the LED blinks red. Then rerun."
  exit 1
fi
echo "Flashing $(basename "$IMG") ..."
dfu-util -R -e -a 1 -D "$IMG" 2>&1 | grep -E "Download done|Done!|Error|error|No DFU" || true
sleep 6
if [ "$1" = "usb" ]; then
  if system_profiler SPAudioDataType 2>/dev/null | grep -q "reSpeaker XVF3800"; then
    echo "OK: the reSpeaker is a USB audio device again. Start Smitty with: uv run smitty run"
  else
    echo "Flashed, but the reSpeaker has not shown up as an audio device yet. Unplug and replug the board, then check: uv run smitty devices"
  fi
else
  echo "OK: I2S firmware flashed. The board no longer appears on USB; use SMITTY_AUDIO=websocket with the XIAO."
fi
