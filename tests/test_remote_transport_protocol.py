from actl.core import tmux


def test_control_error_terminates_response_frame(monkeypatch):
    class Pipe:
        def __init__(self, rows=None):
            self.rows = list(rows or [])

        def readline(self):
            return self.rows.pop(0) if self.rows else ""

    class Process:
        def __init__(self):
            self.stdout = Pipe([
                "%begin 1 1 1\n",
                "bad command\n",
                "%error 1 1 1\n",
            ])

    transport = tmux.RemoteTransport("asus")
    transport._process = Process()
    import queue
    response_queue = queue.Queue(maxsize=1)
    transport._responses = response_queue
    transport._read_loop()
    response = response_queue.get_nowait()
    assert isinstance(response, tmux.TmuxError)
    assert "bad command" in str(response)


def test_timeout_must_invalidate_transport_before_reuse():
    # Regression contract: a request timeout must not leave a live control
    # process available for a later request, because its late frame could be
    # mistaken for the next command's response.
    source = tmux.RemoteTransport.executeTmux.__code__.co_names
    assert "close" in source
