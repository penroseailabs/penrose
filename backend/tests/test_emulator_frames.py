"""The emulator's screen stream: the two hand-encoded protobuf messages, and finding the emulator's gRPC port."""

from app.gateway import emulator_frames


def test_image_format_encodes_rgb_and_size():
    # format = 1 (varint RGB888 = 2), width = 3, height = 4; 1200 needs two varint bytes.
    assert emulator_frames.image_format(540, 1200) == bytes(
        [0x08, 2, 0x18, 0x9C, 0x04, 0x20, 0xB0, 0x09]
    )


def test_parse_image_reads_size_and_pixels_and_skips_other_fields():
    fmt = bytes([0x08, 2, 0x18, 3, 0x20, 2])  # RGB888, 3 x 2
    pixels = bytes(range(18))
    message = (
        bytes([0x0A, len(fmt)])
        + fmt  # format
        + bytes([0x22, len(pixels)])
        + pixels  # image
        + bytes([0x28, 7])  # seq
        + bytes([0x31])
        + bytes(8)  # a fixed64 field
    )
    assert emulator_frames.parse_image(message) == (3, 2, pixels)


def test_parse_image_of_an_inactive_display():
    assert emulator_frames.parse_image(b"") == (0, 0, b"")


def test_discover_finds_the_emulator_by_its_serial(tmp_path, monkeypatch):
    (tmp_path / "pid_11.ini").write_text("port.serial=5580\ngrpc.port=8580\ngrpc.token=other\n")
    (tmp_path / "pid_22.ini").write_text(
        "avd.name=relay\nport.serial=5570\ngrpc.port=8570\ngrpc.token=a=b\n"
    )
    monkeypatch.setattr(emulator_frames, "_discovery_dirs", lambda: [tmp_path])
    assert emulator_frames.discover("emulator-5570") == (8570, "a=b")
    assert emulator_frames.discover("emulator-5590") is None


def test_discover_ignores_an_emulator_without_grpc(tmp_path, monkeypatch):
    (tmp_path / "pid_33.ini").write_text("port.serial=5570\navd.name=relay\n")
    monkeypatch.setattr(emulator_frames, "_discovery_dirs", lambda: [tmp_path])
    assert emulator_frames.discover("emulator-5570") is None
