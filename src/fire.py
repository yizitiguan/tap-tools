"""Fire two taps at fixed coordinates on one precise instant.

  python fire.py --at "+20" --verify          # rehearse 20s from now, report ms error
  python fire.py --at "+20" --verify --repeat 8 --gap-sec 5
  python fire.py --at "2026-09-25 10:00:00" --dry-run

Timing model, all in PC epoch ms:   T_send = T_event - lead_ms
The command body is staged into the device shell well before T_send; at T_send
we write a single newline. Nothing but a 1-byte write() is on the critical path.
Get lead_ms from probe.py (blocks [3] and [4]).
"""
import argparse
import json
import random
import statistics as st
import sys
import time
from datetime import datetime

import adbutil


def load_cfg(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise SystemExit(f"no {path} - open DoubleTap.exe and use the 坐标 tab")
    except json.JSONDecodeError as e:
        raise SystemExit(f"{path} is corrupt: {e}")


def resolve_target(spec):
    """'+20' = 20s from now | '2026-09-25 10:00:00' = local wall clock."""
    if spec.startswith("+"):
        return adbutil.now_ms() + float(spec[1:]) * 1000
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%H:%M:%S.%f", "%H:%M:%S"):
        try:
            return datetime.strptime(spec, fmt).timestamp() * 1000.0
        except ValueError:
            pass
    raise SystemExit(f"cannot parse target time {spec!r}")


def build_cmd(a, b, verify, rehearse=False, bracket=True):
    """One line that fires BOTH taps with `&`, so their ~70ms cold starts
    overlap and the two injections land ~2ms apart.

    Only used for mode=concurrent. A deliberate gap cannot be expressed here:
    toybox `sleep` costs ~50-80ms of process spawn and is non-monotonic below
    ~100ms, so sequence mode staggers two separate shells from the PC instead.

    rehearse swaps `input tap` for `input keyevent 0` (KEYCODE_UNKNOWN): the
    same app_process + class-load + inject path with zero effect on whatever is
    on screen, so lead_ms can be dialled in against a live app without mis-taps.
    """
    A = B = "input keyevent 0"
    if not rehearse:
        A = f"input tap {a[0]} {a[1]}"
        B = f"input tap {b[0]} {b[1]}"
    if verify:
        if bracket:
            fa = f"(echo A0=$(date +%s%3N); {A}; echo A=$(date +%s%3N))"
            fb = f"(echo B0=$(date +%s%3N); {B}; echo B=$(date +%s%3N))"
        else:
            fa = f"({A}; echo A=$(date +%s%3N))"
            fb = f"({B}; echo B=$(date +%s%3N))"
    else:
        fa, fb = f"({A})", f"({B})"
    return f"{fa} & {fb} & wait"


def _tap_cmd(p, tag, verify, rehearse, bracket=True):
    body = "input keyevent 0" if rehearse else f"input tap {p[0]} {p[1]}"
    if not verify:
        return body
    if not bracket:
        # Trailing stamp only. The leading `$(date)` costs a fork+exec that sits
        # between the write() and the injection, so bracket mode pushes every
        # real tap ~10-15ms later than the unmeasured path. Measuring a live
        # shot must not change the shot.
        return f"{body}; echo {tag}=$(date +%s%3N)"
    # Bracket the tap with two stamps from the SAME clock. Their difference is
    # the device-side duration and needs no phone<->PC offset, which is the
    # only stable way to report landing error: the phone auto-syncs its clock
    # and this PC does not, so any stored offset goes stale within hours.
    return f"echo {tag}0=$(date +%s%3N); {body}; echo {tag}=$(date +%s%3N)"


def _num(s):
    try:
        return float(s)
    except ValueError:
        return None


def _read_pair(S, tag, timeout=5, need_start=True):
    """Device epochs bracketing one tap, as (before, after). Their difference is
    a same-clock duration, so no phone<->PC offset is involved.

    need_start=False matches the trailing-stamp form, where no `tag0=` line is
    ever printed -- waiting for one would stall here for the whole timeout.
    """
    p0, p1 = f"{tag}0=", f"{tag}="
    a = b = None
    deadline = time.time() + timeout
    while time.time() < deadline:
        if b is not None and (a is not None or not need_start):
            break
        try:
            line = S._readline().strip()
        except Exception:
            break
        if line.startswith(p0):
            a = _num(line[len(p0):])
        elif line.startswith(p1):
            b = _num(line[len(p1):])
    return a, b


def _wait_for_focus(poll, base, latest, cancel):
    """Poll mCurrentFocus until it leaves `base`. Returns (pc_ms_seen, new_focus)
    or (None, last) at `latest`.

    A tap that lands on a dialog still being drawn is a tap on nothing, so the
    only thing worth waiting for is the screen actually changing. Nothing sleeps
    here: each dumpsys read costs ~50-150ms, which is the pacing.
    """
    last = ""
    while adbutil.now_ms() < latest:
        if cancel and cancel():
            return None, last
        last = adbutil.focus_from(poll)
        now = adbutil.now_ms()
        if last and last != base:
            return now, last
    return None, last


def one_shot(cfg, target_ms, verify, say=print, tick=None, cancel=None, precheck=None,
             rehearse=False, end_stamp=False):
    """Arm and fire once. Returns None when the shot was aborted before anything
    was written, else a dict of measured timings in ms. `tick`/`cancel` let a GUI
    drive this without owning a stdout. `precheck` runs just after warm-up --
    still ~2.5s from the instant, but late enough that the screen state is the
    one the taps will actually land on -- and returning a message aborts the shot.

    `rehearse` is an explicit argument, not a cfg key: persisting it once made
    every later real shot fire no-op keyevents forever.

    `end_stamp` switches the measurement to a trailing-only stamp so a measured
    shot costs the same as an unmeasured one; cfg["second_tap"]="focus" makes the
    second tap wait for the screen to change instead of a fixed gap.
    """
    if tick is None:
        tick = cli_tick
    a = (cfg["tap_a"]["x"], cfg["tap_a"]["y"])
    b = (cfg["tap_b"]["x"], cfg["tap_b"]["y"])
    mode = cfg.get("mode", "concurrent")
    lead = auto_lead(cfg)
    # server_bias_ms shifts the LANDING instant, positive = land later.
    # It must not be folded into `lead`, which is a pure cost compensation.
    bias = cfg.get("server_bias_ms", 0)
    t_send = target_ms - lead + bias

    seq = mode == "sequence"
    # gap only controls when the PC writes. What the screen has done by then is
    # a separate question, so focus mode replaces that moment with an observed
    # change and keeps gap as the floor.
    second_mode = cfg.get("second_tap", "gap") if seq else "gap"
    fmax = float(cfg.get("focus_max_ms", 900))
    poll = None
    if second_mode == "focus":
        # A third shell, because the one holding tap B's staged command is
        # blocked mid-line: anything written there is glued onto tap B and
        # parsed as one command.
        sessions = [adbutil.Session() for _ in range(3)]
        poll = sessions[2]
    else:
        sessions = [adbutil.Session() for _ in range(2 if seq else 1)]
    try:
        for S in sessions:
            S.run("echo 1", timeout=10)

        gap = 0.0
        if seq:
            gap = cfg.get("gap_ms", 40) + random.uniform(
                0, max(0.0, cfg.get("gap_jitter_ms", 20)))
            plan = [(sessions[0],
                     _tap_cmd(a, "A", verify, rehearse, not end_stamp), t_send),
                    (sessions[1],
                     _tap_cmd(b, "B", verify, rehearse, not end_stamp),
                     t_send + gap)]
        else:
            plan = [(sessions[0],
                     build_cmd(a, b, verify, rehearse, not end_stamp), t_send)]

        for S, cmd, at in plan:
            say(f"  send@{at:.1f}  {cmd}")
        say(f"  lead={lead:.1f}ms bias={bias:+.1f}ms gap={gap:.1f}ms")

        # Warm-up must happen BEFORE staging: `Session.run` appends its own
        # newline-delimited command, and a staged body still missing its
        # terminator would be glued onto it and parsed as one line.
        #
        # Deliberately fire-and-forget, not run(): waiting for the device to
        # echo back put an unbounded (30s timeout) blocking read on the critical
        # path, and a stalled read made every later shot land seconds late
        # while still reporting success.
        warm = cfg.get("warm", {})
        if warm.get("enabled", True):
            sleep_until(t_send - warm.get("before_ms", 5000), "warm", tick, cancel)
            wc = warm.get("cmd", "input keyevent 0 & input keyevent 0 & wait")
            for S in sessions:
                S.fire(wc)
            say("  warmed")
            time.sleep(0.35)
        if precheck:
            msg = precheck()
            if msg:
                say(f"  !! {msg}")
                if str(msg).startswith("ABORT"):
                    return None

        # Baseline read ~3.5s out, next to the pre-check's own dumpsys and well
        # clear of the staging margin. If it comes back empty there is nothing to
        # compare against, so fall through to the plain gap rather than let the
        # first non-empty reading count as a change.
        base_focus = ""
        if poll is not None:
            base_focus = adbutil.focus_from(poll)
            if base_focus:
                say(f"  等界面变化：基准 {base_focus}，最早 {gap:.0f}ms 后，上限 {fmax:.0f}ms")
            else:
                say("  !! 焦点基准没读到，这一发改用固定间隔")
                poll = None
                second_mode = "gap"

        staged = cfg.get("stage", True)
        if staged:
            for S, cmd, at in plan:
                S.p.stdin.write(cmd.encode())
                S.p.stdin.flush()
                # the shell is now blocked mid-line with the whole command in
                # its buffer; only the newline is left to send
                margin = at - adbutil.now_ms()
                if margin < 50:
                    say(f"  !! staged too late ({margin:.0f}ms to fire) -- raise warm.before_ms")

        sent = {}
        followed = False
        for i, (S, cmd, at) in enumerate(plan):
            if i == 1 and poll is not None:
                seen, new = _wait_for_focus(poll, base_focus, t_send + fmax, cancel)
                if cancel and cancel():
                    say("  cancelled before firing")
                    return None
                if seen is not None:
                    at = max(seen, at)
                    followed = True
                    say(f"  界面在 T{seen - t_send:+.0f}ms 变为 {new}，第二下跟随")
                else:
                    at = t_send + fmax
                    say(f"  !! 界面 {fmax:.0f}ms 内没变化（仍是 {new or '未知'}），"
                        "按上限补出第二下")
            sleep_until(at, "fire", tick, cancel)
            if cancel and cancel():
                say("  cancelled before firing")
                return None
            late = adbutil.now_ms() - at
            # A late tap is not a degraded success, it is a miss. Report it as
            # one instead of firing seconds past the instant and printing 完成.
            # Skipped once the screen itself set the moment -- that delay is the
            # feature, not a slipped schedule.
            if late > cfg.get("max_late_ms", 150) and not followed:
                say(f"  ABORT 已错过时刻 {late:.0f}ms（上限 {cfg.get('max_late_ms',150)}ms），"
                    f"放弃这一发")
                return None
            sent[i] = S.fire("\n") if staged else S.fire(cmd)
            say(f"  fired #{i+1} at pc-epoch {sent[i]:.1f}  ({late:+.0f}ms vs plan)")

        if not verify:
            return {"send_pc": sent[0], "gap_req_ms": gap}

        a0, a1 = _read_pair(sessions[0], "A", need_start=not end_stamp)
        b_src = sessions[1] if seq else sessions[0]
        b0, b1 = _read_pair(b_src, "B", need_start=not end_stamp)

        if end_stamp:
            # Both stamps come off the device's own clock, so their difference IS
            # the real inter-tap spacing with no phone<->PC offset involved --
            # and its sign says which tap actually went first.
            res = {"mode": mode, "gap_req_ms": gap, "send_pc": sent[0],
                   "second_mode": second_mode,
                   "followed_focus": followed}
            if a1 is None or b1 is None:
                # The taps are already on the screen. Never let a lost stamp read
                # back to the caller as "this shot did not happen".
                say("  !! 设备端时间戳没读全：两下都已打出，只是这次没量到间隔")
                res["measured"] = False
                return res
            sp = b1 - a1
            res.update({"measured": True, "spacing_ms": abs(sp), "signed_spacing_ms": sp})
            say(f"  设备端两下真实间隔 {sp:+.0f}ms（请求 {gap:.0f}ms"
                + ("，第二下反而先落）" if sp < 0 else "）"))
            return res

        if not (a0 and a1 and b0 and b1):
            say("  !! device timestamps incomplete -- try --no-stage")
            return None
        # landing in PC clock = when we wrote + how long the device took,
        # where that duration came from one clock and so cancels any skew
        dur_a, dur_b = a1 - a0, b1 - b0
        land_a = sent[0] + dur_a
        land_b = sent[1 if seq else 0] + dur_b
        first, second = sorted([land_a, land_b])
        out = {"first_late_ms": first - target_ms,
               "second_late_ms": second - target_ms,
               "spacing_ms": second - first,
               "gap_req_ms": gap,
               "device_dur_ms": [dur_a, dur_b],
               "send_pc": sent[0]}
        say(f"  device duration A={dur_a:.0f}ms B={dur_b:.0f}ms")
        say(f"  first {out['first_late_ms']:+7.1f}ms vs T   "
            f"second {out['second_late_ms']:+7.1f}ms   "
            f"spacing {out['spacing_ms']:6.1f}ms (asked {gap:.0f})")
        want = out["first_late_ms"] - bias
        out["mode"] = mode
        out["suggest_lead_ms"] = auto_lead(cfg) - want
        say(f"  => lead_ms[{mode}] {auto_lead(cfg):.1f} -> {out['suggest_lead_ms']:.1f}")
        return out
    finally:
        for S in sessions:
            S.close()


def sleep_until(epoch_ms, label=None, tick=None, cancel=None):
    """Coarse sleep until 300ms out, then spin.

    Two reasons for the split: time.sleep() alone overshoots 10-30ms on Windows
    (timer coalescing), and we deliberately stop reporting progress once inside
    300ms -- touching the console or repainting a window in the critical window
    is itself a jitter source.
    """
    while True:
        remain = epoch_ms - adbutil.now_ms()
        if remain <= 0 or (cancel and cancel()):
            return remain
        if remain > 300:
            if tick:
                tick(remain, label)
            time.sleep(min((remain - 250) / 1000.0, 0.05))
        # <=300ms: spin silently


def auto_lead(cfg, mode=None):
    """Compensation for PC write -> injection, which is MODE dependent.

    concurrent runs both taps inside one line with `( ... ) &`, so it pays two
    subshell forks plus a cold app_process (~72ms measured). sequence sends a
    bare foreground `input tap` down a second warm shell (~18ms). One number
    cannot serve both, so each mode keeps its own and 标定 tunes just the one
    you are actually using.
    """
    mode = mode or cfg.get("mode", "concurrent")
    by = cfg.get("lead_ms_by_mode")
    if isinstance(by, dict) and by.get(mode) is not None:
        lead = float(by[mode])
    else:
        lead = cfg.get("lead_ms")
        if isinstance(lead, dict):
            lead = lead.get(mode)
    if lead is None:
        cal = cfg.get("calibration", {})
        e2e = cal.get("e2e_start_ms", {}).get("med")
        conc = cal.get("injection", {}).get("concurrent_total_ms", {}).get("med")
        lead = e2e + conc / 2.0 if (e2e is not None and conc is not None) else 0.0
    # lead is a duration, so a negative value is always a bad calibration
    # (one over-corrected rehearsal is enough to produce it). Flooring it keeps
    # the tool merely imprecise instead of silently scheduling the past.
    if lead < 0:
        return 0.0
    return lead


def cli_tick(remain, label):
    sys.stdout.write(f"\r  {label}  T-{remain/1000:6.2f}s" + " " * 12)
    sys.stdout.flush()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", required=True)
    ap.add_argument("--cfg", default="config.json")
    ap.add_argument("--mode", choices=["concurrent", "sequence"])
    ap.add_argument("--lead-ms", type=float)
    ap.add_argument("--gap-ms", type=float)
    ap.add_argument("--gap-jitter-ms", type=float)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--gap-sec", type=float, default=6.0)
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--no-stage", action="store_true")
    ap.add_argument("--no-ntp", action="store_true", help="trust the PC clock as-is")
    ap.add_argument("--rehearse", action="store_true",
                    help="fire two no-op keyevents instead of taps (same timing, safe)")
    ap.add_argument("--show-cmd", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    cfg = load_cfg(a.cfg)
    if "tap_a" not in cfg or "tap_b" not in cfg:
        raise SystemExit("config has no tap_a/tap_b - open DoubleTap.exe and use the 坐标 tab")
    for k, v in (("mode", a.mode), ("gap_ms", a.gap_ms), ("lead_ms", a.lead_ms),
                 ("gap_jitter_ms", a.gap_jitter_ms)):
        if v is not None:
            cfg[k] = v
    if a.no_stage:
        cfg["stage"] = False
    if a.verify and not a.rehearse:
        print("  (verify with REAL taps -- add --rehearse to calibrate without touching the UI)")
    if a.show_cmd:
        pa = (cfg["tap_a"]["x"], cfg["tap_a"]["y"])
        pb = (cfg["tap_b"]["x"], cfg["tap_b"]["y"])
        if cfg.get("mode") == "sequence":
            print("  cmd A:", _tap_cmd(pa, "A", a.verify, a.rehearse))
            print("  cmd B:", _tap_cmd(pb, "B", a.verify, a.rehearse), "(separate shell)")
        else:
            print("  cmd:", build_cmd(pa, pb, a.verify, a.rehearse))
    adbutil.ensure_device()

    # -- clock discipline --------------------------------------------------
    # `--at 10:00:00` means 10:00:00 in the real world, not whenever this PC
    # thinks it is. A machine whose Windows Time is stopped can be ~0.7s off,
    # which blows the event long before any tap latency matters.
    ntp_off = 0.0
    if not a.no_ntp and not cfg.get("no_ntp", False):
        print("querying NTP for this PC's own error ...")
        ntp_off, detail = adbutil.ntp_offset_ms()
        if ntp_off is None:
            print("  !! all NTP servers unreachable - falling back to the PC clock."
                  " Fix this or the schedule is meaningless (UDP 123 may be blocked)")
            ntp_off = 0.0
        else:
            for srv, (med, lo, n) in detail.items():
                print(f"  {srv:<18} median={med:+8.1f}ms  best={lo:+8.1f}ms  ({n} samples)")
            print(f"  => true_time = pc_time {ntp_off:+.1f}ms  "
                  f"(pc clock is {'FAST' if ntp_off < 0 else 'SLOW'} by {abs(ntp_off):.0f}ms)")

    print(f"tap_a={cfg.get('tap_a')}  tap_b={cfg.get('tap_b')}  mode={cfg.get('mode','concurrent')}  "
          f"staged={cfg.get('stage', True)}")
    print(f"lead={auto_lead(cfg):.1f}ms[{cfg.get('mode','concurrent')}]  "
          f"bias={cfg.get('server_bias_ms',0)}ms")
    if auto_lead(cfg) == 0:
        print("  WARNING lead=0 -> run probe.py and save calibration, or pass --lead-ms")

    results = []
    for i in range(a.repeat):
        raw = resolve_target(a.at) if i == 0 else adbutil.now_ms() + a.gap_sec * 1000
        tgt = raw - ntp_off
        wall = datetime.fromtimestamp(tgt / 1000).strftime("%H:%M:%S.%f")[:-3]
        print(f"--- shot {i+1}/{a.repeat}  fire at pc-epoch {wall} ---")
        if a.dry_run:
            sleep_until(tgt - 500, "dry", cli_tick)
            print(f"\nwould fire at {wall} (not writing to device)")
            continue
        r = one_shot(cfg, tgt, a.verify, rehearse=a.rehearse)
        if r:
            results.append(r)
        if i + 1 < a.repeat:
            time.sleep(0.6)

    if len(results) > 1:
        e = [x["first_late_ms"] for x in results]
        sp = [x["spacing_ms"] for x in results]
        lead = auto_lead(cfg)
        bias = cfg.get("server_bias_ms", 0)
        print(f"\n=== {len(results)} shots ===")
        print(f"  landing error vs T: med={st.median(e):+.1f}ms  min={min(e):+.1f}  "
              f"max={max(e):+.1f}  spread={max(e)-min(e):.1f}")
        print(f"  inter-tap gap:      med={st.median(sp):.1f}ms  max={max(sp):.1f}ms")
        print(f"  to centre the window: lead_ms {lead:.0f} -> "
              f"{lead - (st.median(e) - bias):.0f}")
        print(f"  to GUARANTEE never early (worst case lands at T): "
              f"server_bias_ms = {-min(e):.0f}")
        print(f"  to GUARANTEE never late:                        "
              f"server_bias_ms = {-max(e):.0f}")
