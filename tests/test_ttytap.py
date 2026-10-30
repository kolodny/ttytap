import fcntl, os, pty, select, shutil, signal, socket, stat, struct, subprocess, sys, tempfile, termios, threading, time, unittest

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'ttytap.py')


def frame(kind, payload):
    return kind + struct.pack('>I', len(payload)) + payload


def set_size(fd, cols, rows):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))


def mode(fd):
    """The terminal settings raw mode changes. Not all of c_cc: musl builds of Python 3.9 return
    random bytes in its unused slots."""
    iflag, oflag, cflag, lflag, ispeed, ospeed, cc = termios.tcgetattr(fd)
    return iflag, oflag, cflag, lflag, ispeed, ospeed, cc[termios.VMIN], cc[termios.VTIME]


def until(pred, timeout=10):
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError('timed out')
        time.sleep(0.01)


class Terminal:
    """ttytap on a PTY whose other end we hold, like a terminal emulator. As the session leader by
    default, so it gets the terminal's SIGHUP and SIGWINCH."""

    def __init__(self, test, script, *opts, leader=True):
        self.test = test
        self.dir = tempfile.mkdtemp()
        test.addCleanup(shutil.rmtree, self.dir, True)
        self.sock = os.path.join(self.dir, 's.sock')
        self.master, self.slave = pty.openpty()
        set_size(self.slave, 80, 24)  # before ttytap starts: a PTY starts out 0x0
        self.output = bytearray()
        self.reading = True
        self.hung_up = False
        self.proc = subprocess.Popen(
            [sys.executable, SCRIPT, '--socket', self.sock, *opts, '--', 'sh', '-c', script],
            stdin=self.slave, stdout=self.slave, stderr=self.slave, cwd=self.dir, start_new_session=leader,
            preexec_fn=(lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0)) if leader else None)
        test.addCleanup(self.close)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        # Polls rather than blocking in read(), which would keep the PTY open after hang_up().
        while not self.hung_up:
            if not (self.reading and select.select([self.master], [], [], 0.02)[0]):
                time.sleep(0.01)
                continue
            try:
                data = os.read(self.master, 65536)
            except OSError:
                return
            if not data:
                return
            self.output += data

    def hang_up(self):
        self.hung_up = True
        self.reader.join()  # before the fd is closed and its number reused by another test
        os.close(self.master)

    def type(self, data):
        os.write(self.master, data)

    def viewer(self, read=True):
        until(lambda: os.path.exists(self.sock))
        return Viewer(self.test, self.sock, read)

    def close(self):
        if self.proc.poll() is None:  # still reading: on macOS a session leader's exit waits for its output to be read
            self.proc.kill()
            self.proc.wait()
        self.hung_up = True
        self.reader.join()
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass


class Viewer:
    def __init__(self, test, path, read):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(path)
        test.addCleanup(self.close)
        self.frames = []
        self.closed = threading.Event()
        self.reader = threading.Thread(target=self._read, daemon=True)
        if read:
            self.reader.start()

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)  # wakes the reader, which must stop before the fd is reused
        except OSError:
            pass
        if self.reader.is_alive():
            self.reader.join()
        self.sock.close()

    def _read(self):
        buf = bytearray()
        while True:
            try:
                data = self.sock.recv(65536)
            except OSError:
                data = b''
            if not data:
                self.closed.set()
                return
            buf += data
            while len(buf) >= 5 and len(buf) >= 5 + struct.unpack('>I', buf[1:5])[0]:
                n = struct.unpack('>I', buf[1:5])[0]
                self.frames.append((bytes(buf[:1]), bytes(buf[5:5 + n])))
                del buf[:5 + n]

    def output(self):
        return b''.join(p for k, p in self.frames if k == b'o')

    def last(self, kind):
        return next((p for k, p in reversed(self.frames) if k == kind), None)


class Viewers(unittest.TestCase):
    def test_watch_type_and_exit_code(self):
        t = Terminal(self, 'printf ready; read x; echo "got $x"; exit 3')
        v = t.viewer()
        until(lambda: b'ready' in v.output())
        self.assertEqual(v.frames[0], (b'r', struct.pack('>HH', 80, 24)))
        v.sock.sendall(frame(b'i', b'hi\r'))
        until(lambda: v.last(b'x') is not None)
        self.assertEqual(v.last(b'x'), b'3')
        self.assertIn(b'got hi', v.output())
        self.assertEqual(t.proc.wait(5), 3)
        self.assertIn(b'got hi', t.output)
        self.assertFalse(os.path.exists(t.sock))

    def test_input_frame_split_across_reads(self):
        t = Terminal(self, 'printf ready; read x; echo "got $x"')
        v = t.viewer()
        until(lambda: b'ready' in v.output())
        whole = frame(b'i', b'hi\r')
        v.sock.sendall(whole[:3])
        time.sleep(0.2)
        v.sock.sendall(whole[3:])
        until(lambda: b'got hi' in v.output())

    def test_late_viewer_gets_history_larger_than_its_backlog(self):
        t = Terminal(self, 'head -c 2000000 /dev/zero | tr "\\0" z; echo; echo end; read x; echo more; read y', '--viewer-backlog', '1000')
        until(lambda: b'end' in t.output)
        v = t.viewer(read=False)  # most of the history stays queued in ttytap
        time.sleep(0.2)
        t.type(b'\r')  # the cap is checked as output arrives
        until(lambda: b'more' in t.output)
        v.reader.start()
        until(lambda: b'more' in v.output())
        self.assertEqual(v.output().count(b'z'), 2000000)
        self.assertFalse(v.closed.is_set())

    def test_slow_viewer_is_dropped_and_the_command_keeps_going(self):
        t = Terminal(self, 'printf ready; read x; head -c 3000000 /dev/zero | tr "\\0" z; echo DONE; read y', '--viewer-backlog', '100000')
        until(lambda: b'ready' in t.output)
        v = t.viewer(read=False)
        time.sleep(0.1)  # accepted before the output starts
        t.type(b'\r')
        until(lambda: b'DONE' in t.output)
        v.sock.settimeout(5)
        got = 0
        while chunk := v.sock.recv(65536):
            got += len(chunk)
        self.assertLess(got, 3000000)

    def test_stuck_viewer_does_not_hold_up_exit(self):
        t = Terminal(self, 'printf ready; read x; head -c 1000000 /dev/zero | tr "\\0" z; exit 4')
        until(lambda: b'ready' in t.output)
        t.viewer(read=False)
        time.sleep(0.1)
        t.type(b'\r')
        self.assertEqual(t.proc.wait(5), 4)


class Terminals(unittest.TestCase):
    def test_typing_reaches_the_command(self):
        t = Terminal(self, 'printf ready; read x; echo "got $x"')
        until(lambda: b'ready' in t.output)
        t.type(b'yo\r')
        until(lambda: b'got yo' in t.output)
        self.assertEqual(t.proc.wait(5), 0)

    def test_resize_reaches_the_command_and_viewers(self):
        t = Terminal(self, 'printf ready; read x; stty size')
        v = t.viewer()
        until(lambda: b'ready' in v.output())
        set_size(t.slave, 100, 30)  # the kernel sends ttytap SIGWINCH
        until(lambda: v.last(b'r') == struct.pack('>HH', 100, 30))
        t.type(b'\r')
        until(lambda: b'30 100' in t.output)

    def test_slow_terminal_does_not_block_the_command(self):
        t = Terminal(self, 'printf ready; read x; head -c 2000000 /dev/zero | tr "\\0" z; echo DONE; read y')
        v = t.viewer()
        until(lambda: b'ready' in v.output())
        t.reading = False
        v.sock.sendall(frame(b'i', b'\r'))
        until(lambda: b'DONE' in v.output())
        t.reading = True
        until(lambda: b'DONE' in t.output)
        self.assertEqual(t.output.count(b'z'), 2000000)

    def test_output_written_just_before_exit_reaches_the_terminal(self):
        t = Terminal(self, 'printf ready; sleep 0.3; printf END')
        until(lambda: b'ready' in t.output)
        t.proc.send_signal(signal.SIGSTOP)  # so it sees the exit and the unread END at the same time
        time.sleep(1)
        t.proc.send_signal(signal.SIGCONT)
        self.assertEqual(t.proc.wait(5), 0)
        until(lambda: t.output.endswith(b'END'))

    def test_terminal_mode_and_flags_restored(self):
        # Not the leader, as under a shell: on macOS the terminal is revoked when its session leader exits.
        t = Terminal(self, 'printf ready; read x', leader=False)
        attrs = mode(t.slave)
        until(lambda: b'ready' in t.output)
        self.assertNotEqual(mode(t.slave), attrs)  # raw while running
        t.type(b'\r')
        self.assertEqual(t.proc.wait(5), 0)
        self.assertEqual(mode(t.slave), attrs)
        self.assertFalse(fcntl.fcntl(t.slave, fcntl.F_GETFL) & os.O_NONBLOCK)  # it would leak into the shell

    def test_terminal_closed(self):
        t = Terminal(self, 'trap "exit 9" HUP; printf ready; while :; do sleep 0.1; done')
        until(lambda: b'ready' in t.output)
        t.hang_up()
        self.assertEqual(t.proc.wait(5), 9)
        self.assertFalse(os.path.exists(t.sock))


class Exit(unittest.TestCase):
    def test_exits_with_the_command_while_a_background_job_holds_the_pty(self):
        t = Terminal(self, 'trap "" HUP; sleep 30 & echo $! > pid; exit 3')
        self.assertEqual(t.proc.wait(3), 3)
        with open(os.path.join(t.dir, 'pid')) as f:
            job = int(f.read())
        os.kill(job, signal.SIGKILL)  # still alive, so it was still holding the PTY

    def test_dies_by_the_commands_signal(self):
        t = Terminal(self, 'printf ready; read x; kill -TERM $$')
        v = t.viewer()
        until(lambda: b'ready' in v.output())
        t.type(b'\r')
        self.assertEqual(t.proc.wait(5), -signal.SIGTERM)
        until(lambda: v.last(b'x') is not None)
        self.assertEqual(v.last(b'x'), b'-15')

    def test_forwards_signals_to_the_command(self):
        t = Terminal(self, 'trap "echo got-term; exit 5" TERM; printf ready; while :; do sleep 0.1; done')
        until(lambda: b'ready' in t.output)
        t.proc.send_signal(signal.SIGTERM)
        self.assertEqual(t.proc.wait(5), 5)
        self.assertIn(b'got-term', t.output)

    def test_command_not_found(self):
        t = Terminal(self, 'exec no-such-command-ttytap')
        self.assertEqual(t.proc.wait(5), 127)


class Socket(unittest.TestCase):
    def test_owner_only(self):
        t = Terminal(self, 'printf ready; read x')
        until(lambda: os.path.exists(t.sock))
        self.assertEqual(stat.S_IMODE(os.stat(t.sock).st_mode), 0o600)

    def test_refuses_a_path_that_exists(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        sock, ran = os.path.join(d, 's.sock'), os.path.join(d, 'ran')
        open(sock, 'w').close()
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        r = subprocess.run([sys.executable, SCRIPT, '--socket', sock, '--', 'touch', ran],
                           stdin=slave, stdout=slave, stderr=subprocess.PIPE, timeout=5)
        self.assertEqual((r.returncode, r.stderr), (1, f'ttytap: {sock}: exists\n'.encode()))
        self.assertFalse(os.path.exists(ran))
        self.assertTrue(os.path.exists(sock))

    def test_no_terminal_is_an_error_and_leaves_no_socket(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        sock = os.path.join(d, 's.sock')
        r = subprocess.run([sys.executable, SCRIPT, '--socket', sock, '--', 'true'],
                           stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
        self.assertEqual((r.returncode, r.stderr), (1, b'ttytap: stdin and stdout must be a terminal (or use --headless)\n'))
        self.assertFalse(os.path.exists(sock))

    def test_z_frames_ignored_with_a_terminal(self):
        t = Terminal(self, 'printf ready; read x; stty size')
        v = t.viewer()
        until(lambda: b'ready' in v.output())
        v.sock.sendall(frame(b'z', struct.pack('>HH', 100, 30)))
        v.sock.sendall(frame(b'i', b'\r'))
        until(lambda: b'24 80' in v.output())


class Headless(unittest.TestCase):
    def start(self, script, size='100x30', stdout=subprocess.DEVNULL):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        sock = os.path.join(d, 's.sock')
        proc = subprocess.Popen([sys.executable, SCRIPT, '--socket', sock, '--headless', size, '--', 'sh', '-c', script],
                                stdin=subprocess.DEVNULL, stdout=stdout, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait()))
        until(lambda: os.path.exists(sock))
        return proc, Viewer(self, sock, True)

    def test_starts_at_the_given_size_and_viewers_resize_it(self):
        proc, v = self.start('printf ready; read x; stty size; read y; stty size')
        until(lambda: b'ready' in v.output())
        self.assertEqual(v.frames[0], (b'r', struct.pack('>HH', 100, 30)))
        v.sock.sendall(frame(b'i', b'\r'))
        until(lambda: b'30 100' in v.output())
        v.sock.sendall(frame(b'z', struct.pack('>HH', 90, 20)) + frame(b'i', b'\r'))
        until(lambda: b'20 90' in v.output())
        self.assertEqual(v.last(b'r'), struct.pack('>HH', 90, 20))
        self.assertEqual(proc.wait(5), 0)
        self.assertEqual(v.last(b'x'), b'0')

    def test_sigwinch_keeps_the_size_even_on_a_terminal(self):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        set_size(slave, 80, 24)
        proc, v = self.start('printf ready; read x; stty size', stdout=slave)
        until(lambda: b'ready' in v.output())
        proc.send_signal(signal.SIGWINCH)
        time.sleep(0.2)
        v.sock.sendall(frame(b'i', b'\r'))
        until(lambda: b'30 100' in v.output())
        self.assertEqual([p for k, p in v.frames if k == b'r'], [struct.pack('>HH', 100, 30)])

    def test_bad_size(self):
        r = subprocess.run([sys.executable, SCRIPT, '--socket', 'x', '--headless', '100', '--', 'true'], capture_output=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn(b"invalid size value: '100'", r.stderr)


if __name__ == '__main__':
    unittest.main()
