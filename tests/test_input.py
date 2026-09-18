from actl.core.input import BracketedPasteParser


def test_bracketed_multiline_is_one_event_and_preserves_bytes():
    prompt = 'FINAL RECEIPT\nplanId:\n한글\n$HOME\n`backtick`\n```python\nprint("hello")\n```'
    parser = BracketedPasteParser()
    assert parser.feed(b"\x1b[200~" + prompt.encode("utf-8")[:20]) == []
    events = parser.feed(prompt.encode("utf-8")[20:] + b"\x1b[201~\r")
    assert len(events) == 1
    assert events[0].pasted is True
    assert events[0].text == prompt


def test_manual_line_is_one_nonpaste_event():
    events = BracketedPasteParser().feed("hello 한글\r".encode())
    assert events[0].text == "hello 한글"
    assert events[0].pasted is False


def test_one_read_chunk_keeps_every_control_line_in_order():
    events = BracketedPasteParser().feed(b"TEAM_ROUTE_TEST\r/switch cp\rPRO_ROUTE_TEST\r")
    assert [event.text for event in events] == ["TEAM_ROUTE_TEST", "/switch cp", "PRO_ROUTE_TEST"]
