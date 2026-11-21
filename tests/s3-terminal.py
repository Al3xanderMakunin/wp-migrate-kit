"""Respond to the S3 prompts through a real pseudo-terminal."""
import errno
import os
import pty
import select
import signal
import sys
import time

prompts = (
    (b"S3 access key ID: ", b"fixture-access"),
    (b"S3 secret access key: ", b"fixture-secret"),
    (b"S3 session token (Enter to skip): ", b"fixture-token"),
)

pid, terminal = pty.fork()
if pid == 0:
    os.execvp(sys.argv[1], sys.argv[1:])

output = bytearray()
index = 0
deadline = time.monotonic() + 30
try:
    while select.select([terminal], [], [], max(0, deadline - time.monotonic()))[0]:
        try:
            chunk = os.read(terminal, 4096)
        except OSError as error:
            if error.errno != errno.EIO:
                raise
            break
        if not chunk:
            break
        output.extend(chunk)
        if index < len(prompts) and prompts[index][0] in output:
            os.write(terminal, prompts[index][1] + b"\n")
            index += 1
    else:
        os.kill(pid, signal.SIGKILL)
        raise TimeoutError("Timed out waiting for S3 prompts")
finally:
    os.close(terminal)
    _, status = os.waitpid(pid, 0)

if any(value in output for _, value in prompts):
    sys.exit("A credential appeared in terminal output")
sys.stdout.buffer.write(output)
if status or index != len(prompts):
    sys.exit("Interactive download failed")
