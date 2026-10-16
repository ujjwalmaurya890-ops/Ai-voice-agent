from campusvoice.audio import PlaybackBuffer


def test_read_pads_with_silence_when_empty():
    buf = PlaybackBuffer()
    assert buf.read(8) == b"\x00" * 8


def test_write_then_read_in_order():
    buf = PlaybackBuffer()
    buf.write(b"\x01\x02\x03\x04")
    buf.write(b"\x05\x06")
    assert buf.read(6) == b"\x01\x02\x03\x04\x05\x06"
    assert buf.pending_bytes() == 0


def test_clear_drops_everything_for_barge_in():
    buf = PlaybackBuffer()
    buf.write(b"\x11\x22" * 500)
    assert buf.clear() == 1000
    assert buf.pending_bytes() == 0
    assert buf.read(4) == b"\x00" * 4


def test_odd_length_chunks_stay_sample_aligned():
    buf = PlaybackBuffer()
    buf.write(b"\x01\x02\x03")  # trailing byte is carried
    assert buf.pending_bytes() == 2
    buf.write(b"\x04\x05\x06")
    assert buf.read(6) == b"\x01\x02\x03\x04\x05\x06"


def test_overflow_drops_oldest_and_stays_even():
    buf = PlaybackBuffer(max_seconds=1, rate=100)  # 200-byte cap
    buf.write(b"\x01\x01" * 100)
    buf.write(b"\x02\x02" * 10)
    assert buf.pending_bytes() == 200
    assert buf.pending_bytes() % 2 == 0
    assert buf.read(200)[-20:] == b"\x02\x02" * 10
