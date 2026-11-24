"""A shell command's long output says it was cut and how to read on; screen input stays out of the shell."""

import pytest

from app.tools.device import INPUT_COMMAND, OUTPUT_LIMIT, clipped


def test_short_output_is_whole():
    assert clipped("total 8\nnotes.txt") == "total 8\nnotes.txt"


def test_long_output_says_how_much_was_cut_and_how_to_read_on():
    text = "Recipe: Vegetarian Chili\n" * 400
    shown = clipped(text)
    assert shown.startswith(text[:OUTPUT_LIMIT])
    assert f"first {OUTPUT_LIMIT} of {len(text)} characters" in shown and "sed -n" in shown


@pytest.mark.parametrize("command", [
    "adb shell input tap 10 20",
    "adb shell input touchscreen swipe 150 300 350 450 800",
    "adb shell input -d 2 keyevent 4",
    "adb shell input draganddrop 1 2 3 4",
    "adb shell input mouse motionevent DOWN 1 2",
])
def test_screen_input_in_any_form_is_refused(command):
    assert INPUT_COMMAND.search(command)


@pytest.mark.parametrize("command", ["adb shell ime list", "adb shell dumpsys input_method", "adb shell cat input.txt"])
def test_other_commands_are_not_taken_for_input(command):
    assert not INPUT_COMMAND.search(command)


def test_tunnels_are_refused_and_reading_the_screen_size_is_not():
    from app.tools.device import MOVES_PHONE, TUNNEL
    assert TUNNEL.search("adb forward tcp:9222 localabstract:chrome_devtools_remote")
    assert TUNNEL.search("adb -s emulator-5554 reverse tcp:8000 tcp:8000")
    assert not MOVES_PHONE.search("adb shell wm size")
    assert MOVES_PHONE.search("adb shell wm size 1080x2400") and MOVES_PHONE.search("adb shell wm density reset")


def test_root_in_quotes_is_refused():
    from app.tools.device import MOVES_PHONE
    assert MOVES_PHONE.search("adb shell \"su -c 'ls /data/data/org.videolan.vlc/'\"")
    assert MOVES_PHONE.search("adb shell 'su -c id'")
    assert not MOVES_PHONE.search("adb shell ls /sdcard/sushi.txt")
