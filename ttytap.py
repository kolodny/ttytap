#!/usr/bin/env python3
"""Run a command unchanged and serve its session on a Unix socket. Protocol: README.md."""
import argparse, errno, fcntl, os, pty, select, signal, socket, struct, sys, termios, time, tty


def size(s):
    cols, _, rows = s.partition('x')
    return int(cols), int(rows)


def parse(argv):
    p = argparse.ArgumentParser(prog='ttytap', usage='ttytap --socket PATH [options] -- command [args...]', allow_abbrev=False)
    p.add_argument('--socket', required=True, metavar='PATH', help='path to create the Unix socket at; must not exist')
    p.add_argument('--viewer-backlog', type=int, default=4 * 1024 * 1024, metavar='BYTES',
                   help='drop a viewer further behind live output than this (default: 4 MiB)')
    p.add_argument('--terminal-backlog', type=int, default=64 * 1024 * 1024, metavar='BYTES',
                   help="output queued for a slow terminal before the command's writes wait on it (default: 64 MiB)")
    p.add_argument('--headless', type=size, metavar='COLSxROWS',
                   help='no terminal of our own: start at this size, and let viewers resize with z frames')
    i = argv.index('--') if '--' in argv else len(argv)
    opts = p.parse_args(argv[:i])
    if not argv[i + 1:]:
        p.error('missing -- command')
    return opts, argv[i + 1:]


def frame(kind, payload):
    return kind + struct.pack('>I', len(payload)) + payload


def winsize(fd):
    rows, cols, _, _ = struct.unpack('HHHH', fcntl.ioctl(fd, termios.TIOCGWINSZ, b'\0' * 8))
    return cols, rows


def main():
    opts, argv = parse(sys.argv[1:])
    sock_path, headless = opts.socket, opts.headless
    if not headless and not (os.isatty(0) and os.isatty(1)):
        sys.exit('ttytap: stdin and stdout must be a terminal (or use --headless)')
    cols, rows = headless or winsize(1)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(sock_path)
    except OSError as e:
        sys.exit(f'ttytap: {sock_path}: ' + ('exists' if e.errno == errno.EADDRINUSE else e.strerror))
    os.chmod(sock_path, 0o600)
    server.listen(8)
    server.setblocking(False)

    pid, master = pty.fork()
    if pid == 0:
        try:
            os.execvp(argv[0], argv)
        except OSError as e:
            os.write(2, f'ttytap: {argv[0]}: {e.strerror}\r\n'.encode())
            os._exit(127)

    def set_size(c, r):
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', r, c, 0, 0))
    set_size(cols, rows)

    history = bytearray(frame(b'r', struct.pack('>HH', cols, rows)))
    clients = {}  # socket -> {'out': bytearray, 'in': bytearray, 'cap': int}

    def broadcast(data):
        nonlocal history
        history += data  # all of it: a viewer that connects late rebuilds the whole scrollback from it
        for c, st in list(clients.items()):
            st['out'] += data
            if len(st['out']) > st['cap']:
                drop(c)

    def drop(c):
        clients.pop(c, None)
        try:
            c.close()
        except OSError:
            pass

    # Resizes arrive as SIGWINCH; a self-pipe wakes the select loop.
    wake_r, wake_w = os.pipe()
    os.set_blocking(wake_w, False)
    signal.set_wakeup_fd(wake_w)
    signal.signal(signal.SIGWINCH, lambda *_: None)
    # The session ends when the command exits, even if something it started in the background
    # still holds the terminal open, like closing a terminal window would.
    signal.signal(signal.SIGCHLD, lambda *_: None)
    # Ctrl-C etc. reach the child through the PTY in raw mode; signals sent to us go to it too.
    for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT, signal.SIGQUIT):
        signal.signal(s, lambda n, _f: os.kill(pid, n))

    def drain(fd):
        # Output the command wrote just before exiting, without waiting on anyone still attached.
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline and select.select([fd], [], [], 0.02)[0]:
            try:
                data = os.read(fd, 65536)
            except OSError:
                return
            if not data:
                return
            to_terminal(data)
            broadcast(frame(b'o', data))

    term_out = bytearray()  # output not yet accepted by our terminal
    term_gone = bool(headless)

    def to_terminal(data):
        if not term_gone:
            term_out.extend(data)

    exit_status = None
    if not headless:
        saved = termios.tcgetattr(0)
        saved_fl = fcntl.fcntl(1, fcntl.F_GETFL)
        fcntl.fcntl(1, fcntl.F_SETFL, saved_fl | os.O_NONBLOCK)  # restored on the way out
        tty.setraw(0)
    status = 0
    try:
        stdin_open = not headless
        while True:
            rl = [server, wake_r] + list(clients) + ([0] if stdin_open else [])
            if len(term_out) < opts.terminal_backlog:
                rl.append(master)
            wl = [c for c, st in clients.items() if st['out']] + ([1] if term_out else [])
            try:
                r, w, _ = select.select(rl, wl, [])
            except InterruptedError:
                continue
            if wake_r in r:
                os.read(wake_r, 512)
                done, st = os.waitpid(pid, os.WNOHANG)
                if done:
                    exit_status = os.waitstatus_to_exitcode(st)
                    drain(master)
                    break
                if headless:
                    continue
                try:
                    c2, r2 = winsize(1)
                except OSError:  # the terminal was closed; the SIGHUP that woke us is on its way to the command
                    c2, r2 = cols, rows
                if (c2, r2) != (cols, rows):
                    cols, rows = c2, r2
                    set_size(cols, rows)
                    broadcast(frame(b'r', struct.pack('>HH', cols, rows)))
            if master in r:
                try:
                    data = os.read(master, 65536)
                except OSError as e:
                    if e.errno != errno.EIO:
                        raise
                    data = b''
                if not data:
                    break
                to_terminal(data)
                broadcast(frame(b'o', data))
            if 1 in w:
                try:
                    del term_out[:os.write(1, term_out)]
                except BlockingIOError:
                    pass
                except OSError:  # our terminal went away; the child gets SIGHUP and exits
                    term_gone = True
                    term_out.clear()
            if 0 in r:
                try:
                    data = os.read(0, 65536)
                except BlockingIOError:  # stdin shares the terminal's non-blocking flag
                    continue
                except OSError:
                    data = b''
                if data:
                    os.write(master, data)
                else:
                    stdin_open = False
            if server in r:
                try:
                    c, _ = server.accept()
                    c.setblocking(False)
                    out = bytearray(history)
                    clients[c] = {'out': out, 'in': bytearray(), 'cap': len(out) + opts.viewer_backlog}
                except OSError:
                    pass
            for c in [c for c in r if c in clients]:
                try:
                    data = c.recv(65536)
                except OSError:
                    data = b''
                if not data:
                    drop(c)
                    continue
                buf = clients[c]['in']
                buf += data
                while len(buf) >= 5:
                    n = struct.unpack('>I', buf[1:5])[0]
                    if len(buf) < 5 + n:
                        break
                    kind, payload = bytes(buf[:1]), bytes(buf[5:5 + n])
                    del buf[:5 + n]
                    if kind == b'i':
                        os.write(master, payload)
                    elif kind == b'z' and headless and len(payload) == 4:
                        cols, rows = struct.unpack('>HH', payload)
                        set_size(cols, rows)
                        broadcast(frame(b'r', payload))
            for c in w:
                if c == 1:
                    continue
                st = clients.get(c)
                if not st:
                    continue
                try:
                    sent = c.send(st['out'])
                    del st['out'][:sent]
                except BlockingIOError:
                    pass
                except OSError:
                    drop(c)
        if exit_status is None:
            _, st = os.waitpid(pid, 0)
            exit_status = os.waitstatus_to_exitcode(st)
        status = exit_status
    finally:
        if not headless:
            try:  # what's still queued for the terminal, then its original (blocking) mode
                fcntl.fcntl(1, fcntl.F_SETFL, saved_fl)
                while term_out and not term_gone:
                    del term_out[:os.write(1, term_out)]
            except OSError:
                pass
            try:  # the terminal may already be gone (window closed)
                termios.tcsetattr(0, termios.TCSAFLUSH, saved)
            except termios.error:
                pass
        for c in list(clients):
            try:
                c.settimeout(1)
                c.sendall(bytes(clients[c]['out']) + frame(b'x', str(status).encode()))
            except OSError:
                pass
            drop(c)
        os.unlink(sock_path)
    if status < 0:  # killed by a signal: die the same way
        signal.signal(-status, signal.SIG_DFL)
        os.kill(os.getpid(), -status)
    sys.exit(status)


if __name__ == '__main__':
    main()
