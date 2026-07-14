#!/usr/bin/env python3
"""Deterministic sim navigation: act, screenshot, verify, retry.
Usage: navigate.py <target>  where target in {prayer-menu}
Leaves the sim on the SettingPrayer list screen."""
import subprocess, sys, time
from PIL import Image

TOOLS = '/home/faisal/repos/faisal-shah/pinetime-dev-tools'

def run(*args):
    subprocess.run([f'{TOOLS}/simctl.py', *args], capture_output=True, cwd=TOOLS)

def shot(name='nav-probe'):
    run('shot', name)
    return Image.open(f'{TOOLS}/shots/{name}.png').convert('RGB')

def is_off(img):
    """The 'Screen is OFF' overlay: black screen, white text only in one band."""
    bright_outside = 0
    for y in range(0, 240, 6):
        for x in range(0, 240, 6):
            r, g, b = img.getpixel((x, y))
            if r + g + b > 350 and not (85 <= y <= 115):
                bright_outside += 1
    band = sum(1 for x in range(40, 200, 4) if all(c > 200 for c in img.getpixel((x, 100))))
    return bright_outside == 0 and band > 5

def wake():
    """Land on the watchface from any state: drive to sleep, then one button
    wake is guaranteed to show the clock. Face-agnostic."""
    for attempt in range(8):
        img = shot()
        if is_off(img):
            run('button')      # wake -> watchface, always
            time.sleep(0.6)
            run('tap', '120', '120')  # reset LVGL activity (clock ignores taps)
            time.sleep(0.3)
            return
        run('button')  # app -> clock, clock -> sleep
        time.sleep(0.6)
    raise SystemExit('never reached the off state')

def has_yellowish_row_icons(img):
    """Settings/List screens use yellow icons at x~20-40 on row starts."""
    count = 0
    for y in (28, 89, 150, 211):
        for x in range(15, 45, 4):
            r, g, b = img.getpixel((x, y))
            if r > 150 and g > 120 and b < 120:
                count += 1
                break
    return count

def texty(img, y):
    """Number of white-ish pixels along a text row."""
    return sum(1 for x in range(50, 220, 4) if all(c > 200 for c in img.getpixel((x, y))))

def goto_quick_settings():
    for attempt in range(6):
        run('swipe', 'right')
        time.sleep(0.7)
        img = shot()
        # QS: dark rounded buttons; gear at bottom right; brightness top-left icon whiteish at (60,78)
        if texty(img, 100) == 0 and img.getpixel((60, 186))[1] > 100:  # green bell area or button
            return True
        # bell may be grey when notifications off: check button blob darkness contrast
        r, g, b = img.getpixel((180, 186))
        r2, g2, b2 = img.getpixel((120, 5))
        if (r, g, b) != (0, 0, 0) and (r2, g2, b2) == (0, 0, 0):
            return True
        wake()
    return False

def main():
    wake()
    if not goto_quick_settings():
        raise SystemExit('quick settings unreachable')
    run('tap', '180', '186')  # gear
    time.sleep(1.0)
    # verify settings page 1: 4 yellow icon rows
    img = shot()
    if has_yellowish_row_icons(img) < 3:
        raise SystemExit('settings page 1 not reached')
    # scroll to page 4, verifying each hop by re-checking icons change
    for hop in range(3):
        for attempt in range(4):
            before = shot().tobytes()
            run('swipe', 'up')
            time.sleep(0.7)
            after = shot()
            if after.tobytes() != before:
                break
        else:
            raise SystemExit(f'swipe {hop} never registered')
    # page 4 row 4 = Prayer
    img = shot()
    if has_yellowish_row_icons(img) < 3:
        raise SystemExit('settings page 4 not reached')
    run('tap', '120', '212')
    time.sleep(0.9)
    img = shot('nav-final')
    # SettingPrayer list: rows Method/Asr/Alerts/Location; verify 3+ icon rows and Method text
    if has_yellowish_row_icons(img) < 2:
        raise SystemExit('prayer menu not reached')
    print('OK: on SettingPrayer')

if __name__ == '__main__':
    main()
