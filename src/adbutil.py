"""Shared adb helpers. No third-party deps."""
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path


def appdir():
    """The folder holding the .exe (or this file when running from source).

    Config and screenshots must live here, NOT in sys._MEIPASS -- onefile
    builds delete that temp dir on exit, which would silently reset settings.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def pkgdir():
    """The shipped package root, i.e. the folder holding config.json.

    The exe gets this from sys.executable, but sources live in <root>/src, so
    walk up until a platform-tools sibling appears. Without this, editing
    settings via `python src/gui.py` would write src/config.json while
    DoubleTap.exe keeps reading the package one -- two files, one app.
    """
    here = appdir()
    for cand in (here, *here.parents):
        if (cand / "platform-tools").is_dir():
            return cand
    return here


def find_adb():
    """Bundled copy first so the package is self-contained and version-pinned,
    then a PATH adb, then the dev-machine install.

    Deliberately NOT auto-downloading: a tool that silently fetches a binary
    named 'adb' is how you end up running someone else's.
    """
    for cand in (pkgdir() / "platform-tools" / "adb.exe",
                 Path(getattr(sys, "_MEIPASS", ".")) / "platform-tools" / "adb.exe"):
        if cand.exists():
            return str(cand)
    found = shutil.which("adb")
    if found:
        return found
    fallback = Path("G:/tools/platform-tools/adb.exe")
    return str(fallback) if fallback.exists() else "adb"


ADB = find_adb()
_SERIAL = None


def set_adb(path):
    global ADB
    if path and Path(path).exists():
        ADB = path


def version():
    try:
        return subprocess.run([ADB, "version"], capture_output=True, text=True,
                              timeout=10).stdout.splitlines()[0]
    except Exception as e:
        return f"unavailable ({e.__class__.__name__})"


def set_serial(serial):
    global _SERIAL
    _SERIAL = serial


def base():
    return [ADB] + (["-s", _SERIAL] if _SERIAL else [])


def sh(cmd, timeout=20):
    """Run a device shell command, return stdout text."""
    r = subprocess.run(base() + ["shell", cmd], capture_output=True, text=True,
                       timeout=timeout, encoding="utf-8", errors="replace")
    return (r.stdout or "").strip()


def sh_ts(cmd, timeout=20):
    """Run a device shell command; return (pc_epoch_ms_at_completion, stdout)."""
    r = subprocess.run(base() + ["shell", cmd], capture_output=True, text=True,
                       timeout=timeout, encoding="utf-8", errors="replace")
    return now_ms(), (r.stdout or "").strip()


def spawn_shell():
    """A long-lived `adb shell` with stdin piped, for zero-process-launch firing."""
    return subprocess.Popen(base() + ["shell"], stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def now_ms():
    return time.time() * 1000.0


def perf():
    return time.perf_counter()


def dev_resolution():
    out = sh("wm size")
    part = out.rsplit(":", 1)[1].strip()
    w, h = part.lower().split("x")
    return int(w), int(h)


NTP_SERVERS = ("ntp.aliyun.com", "cn.pool.ntp.org", "pool.ntp.org", "time.windows.com")


def _ntp_epoch(raw):
    secs = int.from_bytes(raw[:4], "big")
    frac = int.from_bytes(raw[4:8], "big") / 2**32
    return secs - 2208988800.0 + frac


def ntp_offset_ms(servers=NTP_SERVERS, samples=3):
    """Return (offset_ms, detail) where true_time = pc_time + offset_ms/1000.

    Negative offset means your PC clock runs FAST. A machine with Windows Time
    stopped routinely sits hundreds of ms off, which is far more damaging to a
    scheduled tap than any injection latency.
    """
    import statistics
    best = {}
    for srv in servers:
        vals = []
        for _ in range(samples):
            try:
                import socket
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.settimeout(3)
                t0 = time.time()
                s.sendto(b"\x1b" + 47 * b"\0", (srv, 123))
                data, _ = s.recvfrom(64)
                t1 = time.time()
                s.close()
                vals.append((( _ntp_epoch(data[32:40]) - t0)
                             + (_ntp_epoch(data[40:48]) - t1)) / 2.0 * 1000)
            except Exception as e:
                vals.append(None)
        good = [v for v in vals if v is not None]
        if good:
            # lowest-RTT sample is the least congested -> closest to the truth,
            # but median is steadier when a server is flaky, so report both
            best[srv] = (statistics.median(good), min(good, key=abs), len(good))
    if not best:
        return None, {}
    med = statistics.median([v[0] for v in best.values()])
    return med, best


def focus():
    """Current foreground window as "package/Activity".

    Recorded next to the coordinates and re-checked before firing, because a
    tap set picked on one screen silently hits whatever happens to be there
    later -- e.g. a session that timed out and fell back to a login page.
    """
    try:
        line = sh("dumpsys window 2>/dev/null | grep -m1 mCurrentFocus")
    except Exception:
        return ""
    if "=" not in line:
        return ""
    w = line.split("=", 1)[1].strip().rstrip("}")
    parts = w.split()
    if len(parts) >= 2 and "/" in parts[-1]:
        return parts[-1]
    return w


def stay_awake(on):
    """Keep the screen alive while USB-powered -- the phone is always plugged
    in for adb, so this removes the whole class of "fired into a dark screen".
    """
    try:
        sh(f"svc power stayon {'usb' if on else 'false'}")
        return True
    except Exception:
        return False


def wake_screen():
    try:
        sh("input keyevent 224")  # KEYCODE_WAKEUP: turns on without toggling off
        return True
    except Exception:
        return False


def screen_on():
    """True/False, or None when this device gives no readable signal.

    HyperOS has no mWakeState line; mScreenState in `dumpsys display` is the
    one that exists. Returning None on failure matters: a wrong "screen is off"
    warning is worse than no warning, because you learn to ignore it.
    """
    try:
        out = sh("dumpsys display 2>/dev/null | grep -m1 'mScreenState='")
    except Exception:
        return None
    m = re.search(r"mScreenState=(\w+)", out or "")
    if not m:
        return None
    return m.group(1).upper() == "ON"


def ensure_device():
    out = subprocess.run(base() + ["devices"], capture_output=True, text=True,
                         timeout=15).stdout
    lines = [l for l in out.splitlines()[1:] if l.strip() and "device" in l]
    if not lines:
        raise SystemExit("no device visible to adb - check USB cable + USB debugging")
    return lines[0].split()[0]


class Session:
    """One long-lived `adb shell` with piped stdin/stdout.

    Why: spawning `adb shell ...` costs a Windows process launch plus adb
    transport setup, and that cost is what jitters at the critical moment.
    Here the pipe is already open and the device-side sh is already sitting
    blocked on read(), so firing is a single write().
    """

    def __init__(self):
        self.p = subprocess.Popen(base() + ["shell"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        # default bufsize -> BufferedReader, which is what gives us read1()
        self._n = 0
        self._buf = b""

    def _readline(self):
        while b"\n" not in self._buf:
            chunk = self.p.stdout.read1(4096)
            if not chunk:
                raise RuntimeError("adb shell died")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line.decode("utf-8", "replace").rstrip("\r")

    def run(self, cmd, timeout=30):
        """Send cmd, return (pc_send_ms, output_lines, pc_done_ms)."""
        self._n += 1
        end = f"__E{self._n}__"
        t_send = now_ms()
        self.p.stdin.write((cmd + f"\necho {end}\n").encode())
        self.p.stdin.flush()
        deadline = time.time() + timeout
        out = []
        while time.time() < deadline:
            line = self._readline()
            if line.strip() == end:
                return t_send, out, now_ms()
            out.append(line)
        raise RuntimeError("timeout waiting for shell")

    def fire(self, cmd):
        """Write a command and do NOT wait for output. This is the at-T primitive."""
        self.p.stdin.write((cmd + "\n").encode())
        self.p.stdin.flush()
        return now_ms()

    def close(self):
        try:
            self.p.stdin.close()
        except Exception:
            pass
        self.p.terminate()

