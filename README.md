# ttytap

Run a terminal program unchanged and serve its session on a Unix socket, so other programs can
watch it and type into it.

```sh
ttytap --socket /tmp/edit.sock -- vim notes.txt
```

Your terminal renders everything itself: nothing is redrawn, so native scrollback, keys, colors and
links behave as if you ran the command directly. Bytes, resizes and signals pass through, and
ttytap exits with the command's exit code.

Everything after `--` is the command. stdin and stdout must be a terminal, and the socket path must
not exist yet. The socket is created with mode 0600 and removed when the command exits; if ttytap
is killed with SIGKILL, removing it is up to you. `ttytap --help` lists the options.

`--headless COLSxROWS` runs the command with no terminal of ttytap's own (stdin and stdout can be
anything): it starts at that size, and viewers resize it with `z` frames.

The command is looked up on PATH. A wrapper script with the same name as the command it wraps
(`~/bin/vim` running `ttytap … -- vim`) must take its own directory off PATH or pass the real
command's absolute path, or it runs itself again.

## Install

Needs Python 3.9+ on Linux or macOS. No dependencies.

```sh
pipx install ttytap      # or: uv tool install ttytap, pip install ttytap
npm install -g ttytap
```

Or copy `ttytap.py` anywhere on your PATH.

## Socket protocol

Frames both ways: 1-byte type, 4-byte big-endian payload length, payload.

| Type | Direction | Payload |
|---|---|---|
| `o` | to viewer | output bytes |
| `r` | to viewer | terminal size: cols, rows as two big-endian u16 |
| `x` | to viewer | exit code, decimal ASCII (negative: killed by that signal) |
| `i` | from viewer | input bytes, written to the command as if typed |
| `z` | from viewer | new size: cols, rows as two big-endian u16 (`--headless` only) |

A viewer that connects gets every `o` and `r` frame so far, then live frames. A viewer further
behind live output than `--viewer-backlog` is disconnected; the command never waits on viewers.

## Testing

Run the test suite with:

```sh
python3 -m unittest
```

## License

MIT
