"""Measure the four numbers that decide whether a scheduled double-tap is on time.

    python probe.py            # everything
    python probe.py --tap      # just the injection-latency block

Outputs a calibration block you can paste straight into config.json.
"""
import argparse
import json
import statistics as st
import subprocess
import sys
import time

import adbutil

S = None


def pct(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(len(v) * p / 100))]


def stats(name, vals, unit="ms"):
    if not vals:
        return {}
    print(f"  {name:<26} n={len(vals):<3} min={min(vals):7.1f} "
          f"med={st.median(vals):7.1f} p95={pct(vals,95):7.1f} max={max(vals):7.1f} {unit}")
    return {"min": round(min(vals), 1), "med": round(st.median(vals), 1),
            "p95": round(pct(vals, 95), 1), "max": round(max(vals), 1)}


def rtt(n=40):
    print(f"[1] persistent-shell roundtrip (write -> device answers) x{n}")
    v = []
    for _ in range(n):
        t0, out, t1 = S.run("echo 1")
        v.append(t1 - t0)
    return stats("roundtrip", v)


def clock(n=40):
    """Best estimate = the sample with the LOWEST roundtrip (NTP heuristic)."""
    print(f"[2] PC <-> phone clock offset x{n}")
    best = None
    for _ in range(n):
        t_send, out, t_done = S.run("date +%s%3N")
        rtt_ms = t_done - t_send
        dev = float(out[0])
        approx_pc_at_dev = t_send + rtt_ms / 2.0
        off = dev - approx_pc_at_dev
        if best is None or rtt_ms < best[0]:
            best = (rtt_ms, off)
    print(f"  best-rtt={best[0]:.1f}ms  ->  phone is {best[1]:+.1f}ms vs PC")
    return round(best[1], 1)


def tap_variants(reps=8, a=(300, 2000), b=(780, 2000), safe=True):
    """`input keyevent 0` (KEYCODE_UNKNOWN) pays exactly the same app_process +
    class-load cost as `input tap` but cannot touch the UI, so the inter-tap
    spacing -- the number that actually answers your question -- is measurable
    on a live screen without tapping anything. Pass --real-taps to confirm."""
    verb = "keyevent 0" if safe else "tap"
    ax, ay = a
    bx, by = b
    solo = "input keyevent 0" if safe else f"input tap {ax} {ay}"
    print(f"[3] injection latency, {reps} reps each -- using `input {verb}`"
          + ("  (SAFE: no UI impact)" if safe else "  (!! REAL TAPS)"))
    for _ in range(3):
        S.run(solo, timeout=30)

    def fire():
        if safe:
            fa = "(input keyevent 0; echo A=$(date +%s%3N))"
            fb = "(input keyevent 0; echo B=$(date +%s%3N))"
        else:
            fa = f"(input tap {ax} {ay}; echo A=$(date +%s%3N))"
            fb = f"(input tap {bx} {by}; echo B=$(date +%s%3N))"
        return fa, fb

    one, two, gap, seq = [], [], [], []
    for _ in range(reps):
        _, out, _ = S.run(f"date +%s%3N; {solo}; date +%s%3N", timeout=30)
        one.append(float(out[1]) - float(out[0]))

    for _ in range(reps):
        fa, fb = fire()
        _, out, _ = S.run(f"date +%s%3N; {fa} & {fb} & wait; date +%s%3N", timeout=30)
        d = [float(l.split("=")[1]) for l in out if l.startswith(("A=", "B="))]
        two.append(max(d) - float(out[0]))
        if len(d) == 2:
            gap.append(abs(d[1] - d[0]))

    for want in (40, 80, 150):
        for _ in range(reps):
            fa, fb = fire()
            fb = fb.replace("(input", f"(sleep {want/1000:.3f}; input", 1)
            _, out, _ = S.run(f"date +%s%3N; {fa} & {fb} & wait; date +%s%3N", timeout=30)
            d = [float(l.split("=")[1]) for l in out if l.startswith(("A=", "B="))]
            if len(d) == 2:
                seq.append((want, abs(d[1] - d[0])))

    print("    a single `input`, device-side duration (this is the cost you must lead by):")
    r1 = stats("      solo", one)
    print("    both fired with `&`: window from first start to last return")
    r2 = stats("      concurrent total", two)
    print("    |A-B| = spacing between the two -- THE number you asked for")
    r3 = stats("      concurrent spacing", gap)
    for want in (40, 80, 150):
        got = [g for w, g in seq if w == want]
        if got:
            stats(f"      forced gap ~{want}", got)
    print("    note: A/B are stamped *after* each input returns, so |A-B| overstates\n"
          "    the true injection spacing by the sub-shell's own `date` cost (~10ms).")
    return {"solo_ms": r1, "concurrent_total_ms": r2, "inter_tap_spacing_ms": r3}


def e2e(reps=15, phone_off=0.0):
    """PC write() -> device epoch at the moment its shell starts running the line.

    device_epoch = pc_epoch + phone_off, so subtract it or we just measure the skew.
    `lead_ms` in config = this number + the device-side injection duration.
    """
    print(f"[4] PC write -> device starts executing, {reps} reps (the other half of lead)")
    v = []
    for _ in range(reps):
        S.run("echo READY", timeout=10)
        time.sleep(0.05)
        t_send = S.fire("echo F=$(date +%s%3N)")
        while True:
            line = S._readline()
            if line.startswith("F="):
                v.append(float(line[2:]) - phone_off - t_send)
                break
    stats("device_start - pc_send", v)
    return v


def _ntp_epoch(raw):
    """First 32 bits = seconds since 1900, last 32 = fraction of a second."""
    secs = int.from_bytes(raw[:4], "big")
    frac = int.from_bytes(raw[4:8], "big") / 2**32
    return secs - 2208988800.0 + frac


def sntp(servers=("ntp.aliyun.com", "cn.pool.ntp.org", "pool.ntp.org")):
    print("[5] PC clock vs NTP (do you even trust your own machine?)")
    import socket
    for srv in servers:
        try:
            msg = b"\x1b" + 47 * b"\0"
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(4)
            t0 = time.time()
            sock.sendto(msg, (srv, 123))
            data, _ = sock.recvfrom(64)
            t1 = time.time()
            rx, tx = _ntp_epoch(data[32:40]), _ntp_epoch(data[40:48])
            off = ((rx - t0) + (tx - t1)) / 2.0 * 1000
            print(f"  {srv:<20} rtt={(t1-t0)*1000:8.1f}ms  "
                  f"pc_is_off={off:+9.1f}ms  {'<-- matters' if abs(off)>30 else ''}")
        except Exception as e:
            print(f"  {srv:<20} FAILED {type(e).__name__}: {e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["rtt", "clock", "tap", "e2e", "sntp"])
    ap.add_argument("--reps", type=int, default=8)
    ap.add_argument("--real-taps", action="store_true",
                    help="use real `input tap` in [3] instead of no-op keyevents")
    a = ap.parse_args()
    adbutil.ensure_device()
    W, H = adbutil.dev_resolution()
    print(f"device {W}x{H}   python time base = epoch ms\n")
    S = adbutil.Session()
    try:
        S.run("echo 1", timeout=10)
        res = {}
        todo = ["rtt", "clock", "tap", "e2e"] if not a.only else [a.only]
        if "rtt" in todo:
            res["roundtrip_ms"] = rtt()
        off = 0.0
        if "clock" in todo:
            off = clock()
            res["phone_minus_pc_ms"] = off
        if "tap" in todo:
            res["injection"] = tap_variants(a.reps, ((W // 4), H - 200),
                                            ((W * 3) // 4, H - 200), safe=not a.real_taps)
        if "e2e" in todo:
            e2e(12, off)
        if a.only == "sntp" or not a.only:
            sntp()
        print("\n--- paste into config.json as `calibration` ---")
        print(json.dumps({k: v for k, v in res.items()}, indent=2, ensure_ascii=False))
    finally:
        S.close()
