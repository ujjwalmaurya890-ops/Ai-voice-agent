from campusvoice.transcript import TranscriptAssembler


def test_new_line_only_when_speaker_changes():
    t = TranscriptAssembler()
    assert t.add_fragment("You", " hello") == (True, "hello")
    assert t.add_fragment("You", " there") == (False, " there")
    assert t.add_fragment("CampusVoice", "Hi") == (True, "Hi")


def test_end_turn_forces_new_line():
    t = TranscriptAssembler()
    t.add_fragment("You", "a")
    t.end_turn()
    assert t.add_fragment("You", "b")[0] is True
