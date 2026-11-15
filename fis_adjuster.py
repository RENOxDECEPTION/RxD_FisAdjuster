#!/usr/bin/env python3
"""RxD FIS Adjuster: contrast curve of the B5 (and C5 / A3 8L, untested) Audi VDO cluster centre display (FIS), plus odometer set for swapped clusters.

GUI:  python fis_adjuster.py
CLI:  python fis_adjuster.py read --port COM3
      python fis_adjuster.py shift --port COM3 0x10 [--write]      (positive = darker)
      python fis_adjuster.py set --port COM3 "28 28 28 28 28 28 25 25 18 10" [--write]
      python fis_adjuster.py restore --port COM3 backups/fis_backup_X.bin [--write]
      python fis_adjuster.py probe --port COM3                    (find where on the curve the cluster is now)
      python fis_adjuster.py odometer --port COM3 201370 --miles [--write]   (swapped cluster: set the true mileage)
      python fis_adjuster.py backlight --port COM3 fis 0x10 [--write]        (EXPERIMENTAL; fis or clock, positive = brighter)
      python fis_adjuster.py clock --port COM3 --write                       (set the cluster clock to this PC's time)

x is the display temperature sensor (hotter = lower x, measured). The C figures the app shows are guesses.

Needs: Python 3, pyserial (pip install pyserial), a dumb FTDI KKL K-line cable. Tkinter for the GUI.
EEPROM 0x26A: 0A, 10 x breakpoints (0x26B-0x274), 10 contrast bytes (0x275-0x27E). Lower byte = darker pixels.
"""
import argparse, datetime, math, os, re, struct, sys, threading, queue, time
import serial

FACTORY = [0x50, 0x50, 0x50, 0x50, 0x50, 0x50, 0x4D, 0x4D, 0x40, 0x1F]
XAXIS = [0x21, 0x3E, 0x79, 0xA5, 0xB7, 0xBF, 0xC6, 0xCC, 0xD1, 0xD6]
FLOOR = 0x10            # 0x10 already ghosts at room temperature
VERSION = '1.2'
# Measured shape, EXPERIMENTAL: freezer and heated-bed runs on ONE cluster (bench D11, 2026-11-14). Offsets from point 3
# (x 0x79, about 15 C): the hot points get 0x20 more drive, the cold points 0x1C less. Hotter needed more drive all the way
# from -8 to 36 C, and cold overdrive washed the display out, so the factory's extra cold-end drive is dropped.
SHAPE = [-0x20, -0x20, 0, 0x1C, 0x1C, 0x1C, 0x1C, 0x1C, 0x1C, 0x1C]


def shaped(c, measured=True):
    """The measured (or the factory) shape, at the level the curve already has at point 3."""
    d = SHAPE if measured else [v - FACTORY[2] for v in FACTORY]
    return [min(0xFF, max(FLOOR, c[2] + o)) for o in d]


def shape_of(c):
    return 'measured' if c == shaped(c) else 'factory' if c == shaped(c, False) else 'custom'


assert shaped([0x30] * 10) == [0x10, 0x10, 0x30] + [0x4C] * 7 and shaped([0x3C] * 10) == [0x1C, 0x1C, 0x3C] + [0x58] * 7
assert shaped(shaped([0x3C] * 10), False) == [0x3C] * 6 + [0x39, 0x39, 0x2C, 0x10] and shape_of(FACTORY) == 'factory'
assert shape_of(shaped(FACTORY)) == 'measured' and shape_of([0x20] * 10) == 'custom'
CLUSTER_MANAGED = (0x294, 0x6E9)   # write counter and a byte the cluster flips by itself
BACKUP_DIR = 'backups'

# x is the reading of the display's temperature sensor. Measured: hotter = lower x, colder = higher x.
# The C figures are GUESSES, not a calibration: an NTC divider with B = 3500 K is assumed, pinned to one in-car
# reading (x 0x79 with the cabin taken as 15 C). Expect +-5 C near room temperature and worse at the ends.
GUESS_B, GUESS_X, GUESS_C = 3500.0, 0x79, 15.0


def x_to_c(x):
    r = (x / (255.0 - x)) / (GUESS_X / (255.0 - GUESS_X))
    return 1 / (1 / (GUESS_C + 273.15) + math.log(r) / GUESS_B) - 273.15


def c_to_x(t):
    r = GUESS_X / (255.0 - GUESS_X) * math.exp(GUESS_B * (1 / (t + 273.15) - 1 / (GUESS_C + 273.15)))
    return 255 * r / (1 + r)


def active_for(t):
    """Indices of the curve points the cluster interpolates between at display temperature t (C). A guess, see above."""
    x = c_to_x(t)
    if x <= XAXIS[0]:
        return [0]
    if x >= XAXIS[-1]:
        return [9]
    i = max(k for k in range(10) if XAXIS[k] <= x)
    return [i, i + 1]


assert abs(x_to_c(GUESS_X) - GUESS_C) < 1e-6 and abs(c_to_x(x_to_c(0xA5)) - 0xA5) < 1e-6 and active_for(20) == [1, 2] and active_for(-40) == [9]
GUESS_ROW = ' '.join('%+3.0f' % x_to_c(x) for x in XAXIS)

# Odometer: a ring of 8 little-endian words at 0x042. Each word is the inverse of a counter in 16 km units, and the
# number of words one step ahead adds 2 km each. Stored value = shown km + ODO_OFS.
# ODO_OFS is measured, not exact: on a D11 the dash rolled over 2-3 km below the +50 that older tools use, so the dash
# can land a mile or two from what you typed. Check the dash after a write and set it again if needed.
ODO, ODO_OFS, KM_PER_MI = 0x42, 52, 1.609344


def ring_km(img):
    c = [~w & 0xFFFF for w in struct.unpack('<8H', bytes(img[ODO:ODO + 16]))]
    lo = min(c)
    n = sum(1 for v in c if v == lo + 1)
    if c != [lo + (i < n) for i in range(8)]:
        raise Fail('odometer ring is not a valid pattern: ' + hx(img[ODO:ODO + 16]))
    return 16 * lo + 2 * n - ODO_OFS


def ring_bytes(km):
    v = km + ODO_OFS
    return list(struct.pack('<8H', *[~((v >> 4) + (i < ((v & 15) >> 1))) & 0xFFFF for i in range(8)]))


assert all(ring_km([0] * ODO + ring_bytes(k)) in (k, k - 1) for k in (0, 1, 17, 160950, 324075, 999999))


class Fail(Exception):
    pass


def hx(b):
    return ' '.join('%02X' % v for v in b)


CANCEL = threading.Event()      # set by the GUI's Cancel button; only honoured while still connecting, never after


class Cancelled(Exception):     # not a Fail on purpose: the retry loops must not swallow it
    pass


class Cluster:
    connected = False

    """KW1281 0x17 -> mode login 'loginas9' -> 19200 8E1 'D1' packets: D1 <len> <cmd> <data> <xor>."""

    def __init__(self, port, log=print):
        self.port, self.log = port, log
        self.ser = serial.Serial(port, 10400, timeout=0.005)   # set once: on Windows every timeout change re-sends the port config and glitches bytes still going out
        self.ser.dtr, self.ser.rts = True, False

    def close(self):
        try:
            self.ser.break_condition = False
            self.ser.close()
        except Exception:
            pass

    # -- low level --
    @staticmethod
    def wait(ms):   # busy wait: sleep() jitter breaks the 5-baud init on Windows
        t = time.perf_counter() + ms / 1000
        while time.perf_counter() < t:
            pass

    def rd(self, t=0.5):
        end = time.perf_counter() + t
        while True:
            b = self.ser.read(1)
            if b:
                return b[0]
            if time.perf_counter() >= end:
                return -1

    def put(self, b):   # write one byte, swallow the K-line echo
        self.ser.write(bytes([b]))
        self.rd(0.1)

    # -- KW1281 at 10400 8N1 --
    def init5baud(self):
        if CANCEL.is_set() and not self.connected:
            raise Cancelled('cancelled')
        self.ser.reset_input_buffer()
        self.ser.break_condition = False
        self.wait(300)
        bits = [0] + [(0x17 >> i) & 1 for i in range(7)]
        p = 1
        for b in bits[1:]:
            p ^= b
        for b in bits + [p, 1]:
            self.ser.break_condition = (b == 0)
            self.wait(200)
        self.ser.break_condition = False
        self.ser.reset_input_buffer()
        if self.rd(1.5) != 0x55:
            raise Fail('no answer from the cluster (check cable, ignition, port)')
        k1, k2 = self.rd(0.2), self.rd(0.2)
        self.wait(30)
        self.put(~k2 & 0xFF)
        if (k1, k2) != (0x01, 0x8A):
            raise Fail('unexpected keywords %02X %02X' % (k1 & 0xFF, k2 & 0xFF))

    def recv_block(self):
        n = self.rd(1.5)
        if n < 0:
            raise Fail('timeout waiting for block')
        blk = [n]
        for _ in range(1, n):
            self.wait(2)
            self.put(~blk[-1] & 0xFF)
            b = self.rd()
            if b < 0:
                raise Fail('timeout inside block')
            blk.append(b)
        self.wait(2)
        self.put(~blk[-1] & 0xFF)
        if self.rd() != 0x03:
            raise Fail('bad block end')
        self.cnt = blk[1]
        return blk

    def send_block(self, title, data=()):
        self.cnt = (self.cnt + 1) & 0xFF
        self.wait(25)
        for b in [3 + len(data), self.cnt, title] + list(data):
            self.put(b)
            if self.rd() != (~b & 0xFF):
                raise Fail('no complement from cluster')
        self.put(0x03)

    def login(self):
        for t in range(3):
            try:
                return self._login()
            except Fail as e:
                if t == 2:
                    raise
                self.log('login retry (%s)' % e)
                self.ser.baudrate, self.ser.parity = 10400, serial.PARITY_NONE
                time.sleep(2.5)            # the cluster drops back to KW1281 after about a second of silence

    def _login(self):
        self.ser.baudrate, self.ser.parity = 10400, serial.PARITY_NONE
        self.cnt = 0
        for t in range(3):
            try:
                self.init5baud()
                break
            except Fail:
                if t == 2:
                    raise
        ident = []
        while True:
            b = self.recv_block()
            if b[2] == 0x09:
                break
            ident.append(bytes(b[3:]).decode('latin1'))
            self.send_block(0x09)
        name = ' '.join(s.strip() for s in ident[:3])
        # B5 (8D0), C5 A6/allroad (4B0, 4Z7) and A3 8L (8L0) VDO clusters: same EEPROM layout. Later "KOMBI+WEGFAHRS"
        # units keep the tables elsewhere: they get past this, but check_layout refuses them before anything is written.
        if not ident or not ident[0].startswith(('8D0920', '4B0920', '4Z7920', '8L0920')) or 'VDO' not in ''.join(ident[1:3]):
            raise Fail('not a B5/C5/A3 VDO cluster: %r' % name)
        self.ident = name
        self.outside = None                # outside temperature from measuring block 2, field 4 (cars only; None if not plausible)
        if not getattr(self, 'no_blocks', False):
            try:
                self.send_block(0x29, [2])
                b = self.recv_block()
            except Fail:
                self.no_blocks = True      # this cluster does not like it: the login retry skips it
                raise
            if b[2] == 0xE7 and len(b) >= 15 and b[12] == 0x05:
                t = b[13] * (b[14] - 100) * 0.1
                if -40 <= t <= 60:
                    self.outside = t
        self.send_block(0x00)              # ident again, then answer the 4th block with the mode login
        for _ in range(3):
            self.recv_block()
            self.send_block(0x09)
        self.recv_block()
        self.send_block(0x1B, [0x80] + list(b'loginas9'))
        if self.recv_block()[2] != 0x09:
            raise Fail('mode login refused')
        self.wait(1600)
        self.ser.baudrate, self.ser.parity = 19200, serial.PARITY_EVEN
        self.ser.reset_input_buffer()
        ver = self.d1([0x49])[4]
        pw = 'w10serie' if ver == 0x56 else 'w10kombi' if 0x57 <= ver <= 0x59 else '19xDR8xS' if ver >= 0x70 else 'vdokombi'
        if self.d1([0x53, 0x9D] + list(pw.encode()))[2] != 0x06:
            raise Fail('password refused')
        self.d1([0x53, 0x96, 0x04])
        self.connected = True              # from here on Cancel is ignored: a job is never dropped half way
        CANCEL.clear()                     # a late press must not cancel a later reconnect or a safety restore
        self.log('connected: ' + name)

    # -- D1 at 19200 8E1 --
    def d1(self, body, tries=8):
        pkt = [0xD1, len(body) + 3] + list(body)
        x = 0
        for b in pkt:
            x ^= b
        pkt.append(x)
        seen = []
        for t in range(1, tries + 1):
            self.wait(5)
            self.ser.reset_input_buffer()
            self.ser.write(bytes(pkt))
            echo = [self.rd(0.2) for _ in pkt]
            h, n = (self.rd(0.3), self.rd(0.1)) if echo == pkt else (-1, -1)
            if h == 0xD1 and n >= 3:
                r = [h, n] + [self.rd(0.1) for _ in range(n - 2)]
                x = 0
                for b in r[:-1]:
                    x ^= b
                if -1 not in r and x == r[-1]:
                    return r
            seen.append('echo %s reply %02X %02X' % (hx([e & 0xFF for e in echo]), h & 0xFF, n & 0xFF))
            while self.rd(0.15) >= 0:      # let the line go quiet before retrying (the cluster may still be talking)
                pass
        raise Fail('no reply to D1 %s (%s)' % (hx(body), '; '.join(seen[:3])))

    def read_all(self):
        img = bytearray()
        for a in range(0, 2048, 16):
            try:
                r = self.d1([0x72, 0x10, a >> 8, a & 0xFF])
            except Fail:                   # session lost: log in again and carry on from here
                self.log('connection hiccup at %03X, reconnecting' % a)
                time.sleep(2)
                self.login()
                r = self.d1([0x72, 0x10, a >> 8, a & 0xFF])
            if r[2] != 0x72 or len(r) != 20:
                raise Fail('bad read reply at %03X' % a)
            img += bytes(r[3:19])
        return img

    def read_twice(self):
        a, b = self.read_all(), self.read_all()
        bad = [i for i in range(2048) if a[i] != b[i] and i not in CLUSTER_MANAGED]
        if bad:
            raise Fail('two reads differ at %03X: bad connection' % bad[0])
        return a

    def write16(self, adr, old, new):
        """Write 16 EEPROM bytes at adr. Never resends blind: if the ACK is lost, look at what is there now."""
        cmd = [adr >> 8, adr & 0xFF]
        for attempt in (1, 2):
            try:
                if self.d1([0x77, 0x10] + cmd + list(new), tries=1)[2] == 0x06:
                    return
            except Fail:
                pass
            now = list(self.d1([0x72, 0x10] + cmd)[3:19])
            if now == list(new):
                return                                  # it was written, only the ACK got lost
            if now != list(old) or attempt == 2:
                raise Fail('write not acknowledged and %03X is now %s. Read again / restore.' % (adr, hx(now)))
            self.log('write not acknowledged, bytes unchanged, trying once more')

    CLOCK = 0x0213      # CPU RAM: year, month, day, hour, minute, second (binary; bit 7 of each is a flag). From the D11 firmware.

    def read_clock(self):
        """(datetime, flag bits) of the cluster's running clock, or None if those bytes are not a clock on this firmware.
        Never raises: a cluster that does not answer is remembered as 'no clock' and not asked again."""
        def rd():
            r = self.d1([0x52, 0x06, 0x00, self.CLOCK >> 8, self.CLOCK & 0xFF], tries=2)
            if len(r) != 10 or r[2] != 0x52:
                return None
            v = [x & 0x7F for x in r[3:9]]
            try:
                return (datetime.datetime((1900 if v[0] >= 97 else 2000) + v[0], *v[1:]), [x & 0x80 for x in r[3:9]]) if v[0] <= 99 else None
            except ValueError:
                return None
        if getattr(self, 'clock_ok', None) is False:
            return None
        try:
            a = rd()
            if a and not getattr(self, 'clock_ok', None):       # prove once that it ticks before ever writing there
                time.sleep(1.5)
                b = rd()
                self.clock_ok = bool(b) and 1 <= (b[0] - a[0]).total_seconds() <= 3
                a = b
            if not a:
                self.clock_ok = False
            return a if self.clock_ok else None
        except Exception:
            self.clock_ok = False
            return None

    def set_clock(self, t):
        c = self.read_clock()
        if not c:
            raise Fail('this cluster version keeps its clock somewhere else: clock not set')
        v = [t.year % 100, t.month, t.day, t.hour, t.minute, t.second]
        if self.d1([0x57, 0x06, 0x00, self.CLOCK >> 8, self.CLOCK & 0xFF] + [x | f for x, f in zip(v, c[1])], tries=1)[2] != 0x06:
            raise Fail('the cluster refused the clock write')

    def restart(self):
        before, t0 = self.read_clock(), time.time()
        self.d1([0x53, 0x82])          # the cluster takes up the new values and drops the session
        self.ser.close()
        time.sleep(4)
        self.ser.open()
        for t in range(3):
            try:
                self.login()
                break
            except Fail:
                if t == 2:
                    raise Fail('written, but could not log in again to verify. Read it back.')
                time.sleep(3)
        try:                           # the restart can reset the clock to 12:00: put the cluster's own time back
            now = self.read_clock() if before else None
            if before and now:
                want = before[0] + datetime.timedelta(seconds=time.time() - t0)
                if abs((now[0] - want).total_seconds()) > 90:
                    self.set_clock(want)
                    self.log('the restart reset the clock: put back to %s' % want.strftime('%H:%M'))
        except Exception:              # the clock is a convenience: it must never get in the way of the verify
            self.log('could not put the clock back: set it by hand')

    def write_row(self, img, curve):
        row = list(img[0x270:0x280])
        row[5:15] = curve
        self.write16(0x270, list(img[0x270:0x280]), row)
        self.restart()


def check_layout(img):
    if list(img[0x26A:0x275]) != [0x0A] + XAXIS:
        raise Fail('EEPROM 0x26A is %s, not the known layout. Unknown cluster, nothing written.' % hx(img[0x26A:0x275]))
    return list(img[0x275:0x27F])


# Backlight tables, same count / x / y format as the contrast table. EXPERIMENTAL: bench-tested on one D11 only.
# kind, address of the count byte, x axis (must match or nothing is written), factory y
BL = (('clock', 0x1FE, [0x00, 0x80, 0xFF], [0x0F, 0x29, 0xFF]),                              # clock + odometer displays
      ('fis', 0x205, [0x00, 0x80, 0xBF, 0xFF, 0xFF], [0x0F, 0x29, 0x93, 0xFF, 0xFF]),        # FIS top section
      ('fis', 0x210, [0x00, 0x80, 0xBF, 0xFF, 0xFF], [0x0F, 0x29, 0x93, 0xFF, 0xFF]))        # FIS lower section
BL_ADR = [a + 1 + len(x) + i for _k, a, x, _y in BL for i in range(len(x))]
BL_KIND = [k for k, _a, x, _y in BL for _ in x]
BL_FACTORY = [v for _k, _a, _x, y in BL for v in y]


def bl_read(img):
    """The 13 backlight bytes, or None if this cluster's tables are not the known layout."""
    for _k, a, x, _y in BL:
        if list(img[a:a + 1 + len(x)]) != [len(x)] + x:
            return None
    return [img[a] for a in BL_ADR]


def bl_shift(bl, kind, n):
    """Shift one kind ('fis' or 'clock') by n, positive = brighter. n None = that kind back to factory."""
    return [(f if n is None else min(0xFF, max(0, v + n))) if k == kind else v for v, k, f in zip(bl, BL_KIND, BL_FACTORY)]


def bl_text(bl, kind):
    parts, i = [], 0
    for k, _a, x, _y in BL:
        if k == kind:
            parts.append(hx(bl[i:i + len(x)]))
        i += len(x)
    return parts[0] if len(set(parts)) == 1 else 'top %s / lower %s' % tuple(parts)


assert all(0x200 <= a < 0x220 for a in BL_ADR) and len(BL_ADR) == 13
assert bl_shift(BL_FACTORY, 'fis', 0x10) == [0x0F, 0x29, 0xFF] + [0x1F, 0x39, 0xA3, 0xFF, 0xFF] * 2
assert bl_shift(bl_shift(BL_FACTORY, 'clock', -0x40), 'clock', None) == BL_FACTORY and bl_text(BL_FACTORY, 'fis') == '0F 29 93 FF FF'


def backup(img):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    p = os.path.join(BACKUP_DIR, 'fis_backup_%s.bin' % datetime.datetime.now().strftime('%Y%m%d_%H%M%S'))
    with open(p, 'wb') as f:
        f.write(img)
    return p


def clamp(c):
    return [min(0xFF, max(FLOOR, v)) for v in c]


def apply_curve(port, curve=None, log=print, bl=None):
    """Full guarded write: read twice, back up, check layout, write the changed rows, re-login, verify. Returns backup path.
    curve: 10 contrast bytes or None (keep). bl: 13 backlight bytes or None (keep)."""
    cl = Cluster(port, log)
    try:
        cl.login()
        img = cl.read_twice()
        bak = backup(img)
        log('backup: ' + bak)
        check_layout(img)
        want = bytearray(img)
        if curve is not None:
            want[0x275:0x27F] = bytes(clamp(curve))
            log('contrast ' + hx(want[0x275:0x27F]))
        if bl is not None:
            if bl_read(img) is None:
                raise Fail('the backlight tables in this cluster are not the known layout. Nothing written.')
            for adr, v in zip(BL_ADR, bl):
                want[adr] = v
            log('backlight FIS %s, clock %s' % (bl_text(bl, 'fis'), bl_text(bl, 'clock')))
        rows = [r for r in (0x200, 0x210, 0x270) if want[r:r + 16] != img[r:r + 16]]
        if not rows:
            log('nothing to change')
            return bak
        for r in rows:
            cl.write16(r, list(img[r:r + 16]), list(want[r:r + 16]))
        cl.restart()
        after = cl.read_all()
        bad = [i for i in range(2048) if after[i] != want[i] and i not in CLUSTER_MANAGED]
        if bad:
            raise Fail('VERIFY FAILED at %s. Restore from %s' % (' '.join('%03X' % i for i in bad), bak))
        log('written and verified')
        return bak
    finally:
        cl.close()


def read_curve(port, log=print):
    cl = Cluster(port, log)
    try:
        cl.login()
        img = cl.read_twice()
        log('backup: ' + backup(img))
        try:
            km = ring_km(img)
        except Fail:
            km = None
        return check_layout(img), cl.ident, cl.outside, km, bl_read(img)
    finally:
        cl.close()


def set_odometer(port, km, log=print):
    """For swapped clusters: set the odometer to the car's true mileage. Read twice, back up, write the ring, verify."""
    if not 0 <= km <= 999999:
        raise Fail('odometer out of range')
    cl = Cluster(port, log)
    try:
        cl.login()
        img = cl.read_twice()
        bak = backup(img)
        log('backup: ' + bak)
        check_layout(img)                        # only clusters with the known EEPROM layout
        old = ring_km(img)                       # refuses a ring it does not understand
        new = ring_bytes(km)
        log('odometer %d km -> %d km' % (old, km))
        cl.write16(ODO, img[ODO:ODO + 16], new)
        cl.restart()
        after = cl.read_all()
        want = bytearray(img)
        want[ODO:ODO + 16] = bytes(new)
        bad = [i for i in range(2048) if after[i] != want[i] and i not in CLUSTER_MANAGED]
        if bad:
            raise Fail('VERIFY FAILED at %s. Backup: %s' % (' '.join('%03X' % i for i in bad), bak))
        log('odometer written and verified. Check the dash: it can land a mile or two off, set it again if so.')
        return bak
    finally:
        cl.close()


def set_clock_pc(port, log=print):
    """Set the cluster's clock and date to this PC's time. RAM only: no EEPROM write, no backup needed."""
    cl = Cluster(port, log)
    try:
        cl.login()
        t = datetime.datetime.now()
        cl.set_clock(t)
        log('clock set to %s' % t.strftime('%Y-%m-%d %H:%M:%S'))
    finally:
        cl.close()


def probe(port, ask, log=print):
    """Light one breakpoint at a time (others very light) and ask the user if text shows. Restores the curve after.
    ask() returns True (visible), False (not visible) or None (cancel)."""
    cl = Cluster(port, log)
    orig = None
    try:
        cl.login()
        img = cl.read_twice()
        log('backup: ' + backup(img))
        orig = check_layout(img)
        strong = max(FLOOR, min(min(orig[:6]) - 0x30, 0x20))
        seen = []
        for i in range(10):
            c = [0x7F] * 10
            c[i] = strong
            cl.write_row(img, c)
            time.sleep(2)
            a = ask('Point %d of 10 (x=%02X) is lit. Is any text visible on the display now?' % (i + 1, XAXIS[i]))
            if a is None:
                log('cancelled, putting your curve back')
                cl.write_row(img, orig)
                if cl.read_all()[0x275:0x27F] != bytes(orig):
                    raise Fail('could not restore the original curve. Use restore with the backup.')
                return 'cancelled, original curve restored', []
            if a:
                seen.append(i)
            elif seen:
                break                     # only the one or two points around the current x light up: done
        cl.write_row(img, orig)
        if cl.read_all()[0x275:0x27F] != bytes(orig):
            raise Fail('could not restore the original curve. Use restore with the backup.')
        log('original curve restored')
        if not seen:
            return 'no point lit the text: display may be off, or the cluster sits outside x %02X-%02X' % (XAXIS[0], XAXIS[-1]), seen
        lo, hi = XAXIS[seen[0]], XAXIS[seen[-1]]
        return ('cluster is at x %02X now' % lo if lo == hi else 'cluster is between x %02X and %02X now' % (lo, hi)), seen
    except Exception:
        cl.close()
        if orig is not None:              # something failed mid-probe: never leave a test pattern in the cluster
            log('error during probe, putting your curve back')
            apply_curve(port, orig, log)
        raise
    finally:
        cl.close()


# ---------------- GUI ----------------
def gui():
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
    from serial.tools import list_ports

    root = tk.Tk()
    root.title('RxD FIS Adjuster ' + VERSION)
    q = queue.Queue()
    state = {'read': None, 'active': [], 'exact': False, 'busy': False, 'closing': False, 'km': None, 'outside': None, 'us': True, 'bl': None, 'bl_new': None, 'bl_base': None, 'bl_ofs': {}}

    top = ttk.Frame(root, padding=6)
    top.pack(fill='x')
    ttk.Label(top, text='Port').pack(side='left')
    ports = [p.device for p in list_ports.comports()]
    port = ttk.Combobox(top, values=ports, width=14)
    port.set(ports[0] if ports else '')
    port.pack(side='left', padx=4)
    units = ttk.Combobox(top, values=['F, miles', 'C, km'], width=8, state='readonly')
    units.set('F, miles')
    units.pack(side='left', padx=4)
    nb = ttk.Notebook(root)                        # display settings and the odometer are kept apart
    nb.pack(fill='x', padx=6)
    disp, odo_tab = ttk.Frame(nb, padding=6), ttk.Frame(nb, padding=16)
    nb.add(disp, text='Display')
    nb.add(odo_tab, text='Odometer (swapped clusters)')
    clk_tab = ttk.Frame(nb, padding=16)
    nb.add(clk_tab, text='Clock')
    ttk.Label(clk_tab, text='Sets the time and date of the cluster from this PC. Nothing is written to the EEPROM.', foreground='gray').pack(pady=(0, 10))
    ttk.Button(clk_tab, text='Set clock to the time of this PC', command=lambda: run(lambda: set_clock_pc(port.get(), log))).pack()
    cfr = ttk.LabelFrame(disp, text='Contrast', padding=4)
    cfr.pack(fill='x')
    trow = ttk.Frame(cfr)
    trow.pack()
    temp_lbl = ttk.Label(trow)
    temp_lbl.pack(side='left')
    temp = ttk.Entry(trow, width=5, justify='center')
    temp.pack(side='left', padx=4)
    temp_note = ttk.Label(trow, text='type it to mark the active points', foreground='gray')
    temp_note.pack(side='left')

    grid = ttk.Frame(cfr, padding=6)
    grid.pack()
    ttk.Label(grid, text='x').grid(row=0, column=0, sticky='e')
    c_name = ttk.Label(grid, foreground='gray')
    c_name.grid(row=1, column=0, sticky='e')
    ttk.Label(grid, text='factory').grid(row=2, column=0, sticky='e')
    ttk.Label(grid, text='cluster').grid(row=3, column=0, sticky='e')
    ttk.Label(grid, text='new').grid(row=4, column=0, sticky='e')
    cur_lbl, ent, x_lbl, c_lbl = [], [], [], []
    for i in range(10):
        xl = tk.Label(grid, text='%02X' % XAXIS[i])
        xl.grid(row=0, column=i + 1)
        x_lbl.append(xl)
        cl_ = ttk.Label(grid, foreground='gray')
        cl_.grid(row=1, column=i + 1)
        c_lbl.append(cl_)
        ttk.Label(grid, text='%02X' % FACTORY[i], foreground='gray').grid(row=2, column=i + 1)
        l = ttk.Label(grid, text='--')
        l.grid(row=3, column=i + 1)
        cur_lbl.append(l)
        e = ttk.Entry(grid, width=4, justify='center')
        e.insert(0, '%02X' % FACTORY[i])
        e.grid(row=4, column=i + 1, padx=1)
        e.bind('<KeyRelease>', lambda _e: draw())
        ent.append(e)

    W, H, M = 560, 240, 30
    cv = tk.Canvas(cfr, width=W, height=H, bg='white')
    cv.pack(padx=6)

    def new_curve():
        try:
            return [int(e.get(), 16) for e in ent]
        except ValueError:
            return None

    shape_var = tk.StringVar(value='factory')

    def set_new(c):
        for e, v in zip(ent, clamp(c)):
            e.delete(0, 'end')
            e.insert(0, '%02X' % v)
        draw()

    def draw():
        c = new_curve()
        shape_var.set(shape_of(c) if c else 'custom')        # the switch follows the curve, typed or read
        cv.delete('all')
        px = lambda x: M + x * (W - 2 * M) / 255
        py = lambda y: M + y * (H - 2 * M) / 0x80          # 0 at top = darker, 0x80 at bottom = lighter
        cv.create_rectangle(px(0), py(0), px(255), py(FLOOR), fill='#fde', outline='')
        a = state['active']
        if a:                                                 # where the cluster is on the curve: exact (Find position) or a guess from the temperature
            x0, x1 = px(XAXIS[a[0]]) - 4, px(XAXIS[a[-1]]) + 4
            cv.create_rectangle(x0, py(0), x1, py(0x80), fill='#dfd', outline='green')
            cv.create_text((x0 + x1) / 2, py(0x80) + 8, text='active now' if state['exact'] else 'active (guess)', fill='green')
        for i, xl in enumerate(x_lbl):
            xl.config(fg='green' if i in a else 'black', font=('TkDefaultFont', 9, 'bold' if i in a else 'normal'))
        cv.create_text(px(255) - 4, py(0) + 8, text='darker', anchor='e', fill='gray')
        cv.create_text(px(255) - 4, py(0x80) - 8, text='lighter', anchor='e', fill='gray')
        cv.create_text(px(128), H - 8, text='x = display temperature sensor:   hot  <--   -->  cold', fill='gray')
        for y in range(0, 0x81, 0x20):
            cv.create_text(M - 4, py(y), text='%02X' % y, anchor='e', fill='gray')
        for c, col in ((FACTORY, 'gray'), (state['read'], 'blue'), (new_curve(), 'red')):
            if c:
                pts = [v for i in range(10) for v in (px(XAXIS[i]), py(c[i]))]
                cv.create_line(*pts, fill=col, width=2)
                for i in range(10):
                    cv.create_oval(px(XAXIS[i]) - 3, py(c[i]) - 3, px(XAXIS[i]) + 3, py(c[i]) + 3, outline=col)
        cv.create_text(M, 10, text='gray = factory   blue = in cluster   red = new', anchor='w')

    btns = ttk.Frame(cfr, padding=4)
    btns.pack()
    bfr = ttk.LabelFrame(disp, text='Backlight (experimental: tested on one bench cluster only)', padding=4)
    bfr.pack(fill='x', pady=(6, 0))
    bl_lbl, bl_sl = {}, {}
    for r_, (kind, name) in enumerate((('fis', 'FIS'), ('clock', 'Clock + odometer'))):
        ttk.Label(bfr, text=name).grid(row=r_, column=0, sticky='e', padx=4)
        bl_lbl[kind] = ttk.Label(bfr, text='read first', width=40, foreground='gray')
        bl_lbl[kind].grid(row=r_, column=1, sticky='w')
        ttk.Label(bfr, text='dimmer', foreground='gray').grid(row=r_, column=2)
        bl_sl[kind] = tk.Scale(bfr, from_=-128, to=128, resolution=8, orient='horizontal', showvalue=False, length=170,
                               command=lambda v, k=kind: bl_step(k, int(float(v))))      # middle = as in the cluster
        bl_sl[kind].grid(row=r_, column=3)
        ttk.Label(bfr, text='brighter', foreground='gray').grid(row=r_, column=4)
        ttk.Button(bfr, text='Factory', width=8, command=lambda k=kind: bl_step(k, None)).grid(row=r_, column=5, padx=4, pady=1)
    wrow = ttk.Frame(disp, padding=(0, 6, 0, 0))
    wrow.pack()
    ttk.Label(odo_tab, text='For a replacement cluster that shows the donor car\'s mileage: set it to your car\'s true mileage.',
              foreground='gray').pack(pady=(0, 10))
    odo = ttk.Frame(odo_tab)
    odo.pack()
    odo_lbl = ttk.Label(odo, text='Odometer: read first')
    odo_lbl.pack(side='left')
    ttk.Label(odo, text='    set to').pack(side='left')
    odo_ent = ttk.Entry(odo, width=9, justify='center')
    odo_ent.pack(side='left', padx=4)
    odo_unit = ttk.Label(odo)
    odo_unit.pack(side='left')

    def all_btns(w=None):
        out = []
        for c in (w or root).winfo_children():
            out += [c] if isinstance(c, ttk.Button) and c is not cancel_btn else all_btns(c)
        return out

    def bl_show():
        for kind, l in bl_lbl.items():
            if state['bl'] is None:
                l.config(text='read first' if state['read'] is None else 'unknown table layout in this cluster: not adjustable')
            else:
                l.config(text='cluster %s   new %s' % (bl_text(state['bl'], kind), bl_text(state['bl_new'], kind)), foreground='black')

    def bl_step(kind, n):
        if state['bl'] is None:
            return
        if n is None:                                 # Factory button: factory values become the base, slider back to the middle
            state['bl_base'], n = bl_shift(state['bl_base'], kind, None), 0
            bl_sl[kind].set(0)
        state['bl_ofs'][kind] = n                     # slider position = offset from the base, so sliding back returns exactly
        new = state['bl_base']
        for k, v in state['bl_ofs'].items():
            new = bl_shift(new, k, v)
        state['bl_new'] = new
        bl_show()

    def bl_base(bl):                                  # new base (from the cluster or a backup): sliders to the middle
        state['bl'] = state['bl'] if bl and state['bl'] else bl
        state['bl_base'], state['bl_new'], state['bl_ofs'] = bl, bl and list(bl), {}
        for sl in bl_sl.values():
            sl.set(0)
        bl_show()

    def show(c):                                  # temperature in the chosen unit
        return c * 1.8 + 32 if state['us'] else c

    def odo_text(km):
        return '%d mi' % round(km / KM_PER_MI) if state['us'] else '%d km' % km

    def refresh_units(_e=None):
        was = state['us']
        state['us'] = units.get().startswith('F')
        u = 'F' if state['us'] else 'C'
        temp_lbl.config(text='  Display temperature, %s (guess):' % u)
        c_name.config(text='about %s (guess)' % u)
        for i, l in enumerate(c_lbl):
            l.config(text='%+.0f' % show(x_to_c(XAXIS[i])))
        odo_unit.config(text='miles' if state['us'] else 'km')
        odo_lbl.config(text='Odometer: ' + (odo_text(state['km']) if state['km'] is not None else 'read first'))
        if was != state['us']:                    # convert what is typed in the boxes
            for e, f_ in ((temp, lambda v: v * 1.8 + 32 if state['us'] else (v - 32) / 1.8),
                          (odo_ent, lambda v: v / KM_PER_MI if state['us'] else v * KM_PER_MI)):
                try:
                    v = float(e.get().replace(',', ''))
                except ValueError:
                    continue
                e.delete(0, 'end')
                e.insert(0, '%.0f' % f_(v))
        if state['outside'] is not None:
            temp_note.config(text='outside sensor: %.0f %s' % (show(state['outside']), u))
    units.bind('<<ComboboxSelected>>', refresh_units)
    logbox = tk.Text(root, height=8, width=80)
    logbox.pack(padx=6, pady=6)

    def log(s):
        q.put(('log', s))

    def run(job, done=None):
        state['busy'] = True
        CANCEL.clear()
        cancel_btn.state(['!disabled'])
        for b in all_btns():
            b.state(['disabled'])

        def worker():
            try:
                r = job()
                q.put(('done', (done, r)))
            except Exception as ex:
                q.put(('err', str(ex)))
        threading.Thread(target=worker, daemon=True).start()

    def poll():
        while not q.empty():
            k, v = q.get()
            if k == 'log':
                logbox.insert('end', v + '\n')
                logbox.see('end')
            elif k == 'ask' and state['closing']:
                v[1].append(None)                     # window is closing: cancel the probe, curve goes back
                v[2].set()
            elif k == 'ask':
                v[1].append(messagebox.askyesnocancel('Find position', v[0] + '\n\nCancel stops and puts your curve back.'))
                v[2].set()
            else:
                state['busy'] = False
                if state['closing']:
                    root.destroy()
                    return
                for b in all_btns():
                    b.state(['!disabled'])
                cancel_btn.state(['disabled'])
                if k == 'err' and v == 'cancelled':
                    logbox.insert('end', 'cancelled' + chr(10))
                    logbox.see('end')
                elif k == 'err':
                    logbox.insert('end', 'ERROR: ' + v + '\n')
                    messagebox.showerror('FIS Adjuster', v)
                elif v[0]:
                    v[0](v[1])
        root.after(100, poll)

    def mark_temp(_e=None):
        try:
            t = float(temp.get())
        except ValueError:
            return
        state['active'], state['exact'] = active_for((t - 32) / 1.8 if state['us'] else t), False
        draw()
    temp.bind('<KeyRelease>', mark_temp)

    def do_read():
        def done(r):
            state['read'] = r[0]
            for l, v in zip(cur_lbl, r[0]):
                l.config(text='%02X' % v)
            set_new(r[0])
            log('cluster curve: ' + hx(r[0]))
            state['outside'], state['km'] = r[2], r[3]
            state['bl'] = r[4]
            bl_base(r[4])
            if r[4]:
                log('backlight: FIS %s, clock %s (higher = brighter)' % (bl_text(r[4], 'fis'), bl_text(r[4], 'clock')))
            refresh_units()
            if r[3] is not None:
                log('odometer: %d km = %d mi (stored value; the dash can read a mile or two different)' % (r[3], round(r[3] / KM_PER_MI)))
            if r[2] is not None:
                log('outside temperature from the car: %.0f C / %.0f F. The display is at that temperature only after the car has sat for hours.' % (r[2], r[2] * 1.8 + 32))
                if not temp.get().strip():
                    temp.insert(0, '%.0f' % show(r[2]))
                    mark_temp()
        run(lambda: read_curve(port.get(), log), done)

    def do_write():
        c = new_curve()
        if c is None:
            return messagebox.showerror('FIS Adjuster', 'new curve: 10 hex bytes please')
        bl = state['bl_new'] if state['bl_new'] != state['bl'] else None
        msg = 'Write to the cluster?\n\ncontrast: ' + hx(clamp(c))
        if bl:
            msg += '\nbacklight (experimental): FIS %s, clock %s' % (bl_text(bl, 'fis'), bl_text(bl, 'clock'))
        if not messagebox.askyesno('Write', msg + '\n\nA backup is made first.'):
            return
        run(lambda: apply_curve(port.get(), c, log, bl), lambda _r: do_read())

    def do_restore():
        f = filedialog.askopenfilename(initialdir=BACKUP_DIR, filetypes=[('backup', '*.bin')])
        if f:
            img = open(f, 'rb').read()
            if len(img) != 2048:
                return messagebox.showerror('FIS Adjuster', 'not a 2048-byte backup')
            set_new(list(img[0x275:0x27F]))
            if state['bl'] is not None and bl_read(img):
                bl_base(bl_read(img))
            log('loaded contrast and backlight from the backup into "new": press Write to apply')

    def do_probe(confirm=True):
        if confirm and not messagebox.askyesno('Find position', 'This lights one curve point at a time and asks you each '
                                               'time whether text shows on the display. The display needs some text on it. '
                                               'Your curve is restored at the end. Go?'):
            return

        def ask(msg):
            ans, ev = [], threading.Event()
            q.put(('ask', (msg, ans, ev)))
            ev.wait()
            return ans[0]
        def done(r):
            state['active'], state['exact'] = r[1], bool(r[1])
            draw()
            log(r[0])
            messagebox.showinfo('Find position', r[0])
        run(lambda: probe(port.get(), ask, log), done)

    def do_odo():
        try:
            v = float(odo_ent.get().replace(',', ''))
        except ValueError:
            return messagebox.showerror('Odometer', 'type the mileage to set first')
        km = round(v * KM_PER_MI) if state['us'] else round(v)
        if state['km'] is None:
            return messagebox.showerror('Odometer', 'Read first')
        if not messagebox.askyesno('Set odometer', 'Set the odometer to %d mi (%d km)?\n\nThe app reads %s in the cluster now. If that '
                                   'is not what the dash shows (within a mile or two), stop: this cluster stores it differently.\n\n'
                                   'This is for a swapped cluster: set it to the car\'s true mileage. A backup is made first.'
                                   % (round(km / KM_PER_MI), km, odo_text(state['km']))):
            return
        run(lambda: set_odometer(port.get(), km, log), lambda _r: do_read())

    def shift(n):
        c = new_curve()
        if c:
            set_new([v - n for v in c])

    def shape():
        c, m = new_curve(), shape_var.get() == 'measured'
        if c:
            set_new(shaped(c, m))
            log(('measured cluster results at your level %02X (point 3): hot end darker, cold end lighter. EXPERIMENTAL: '
                 'measured on one cluster.' if m else 'factory calibration shape at your level %02X (point 3).') % c[2])

    srow = ttk.Frame(cfr)
    srow.pack()
    ttk.Label(srow, text='Curve shape:').pack(side='left', padx=4)
    ttk.Radiobutton(srow, text='Factory calibration curve', variable=shape_var, value='factory', command=shape).pack(side='left')
    ttk.Radiobutton(srow, text='Measured cluster results (1 cluster, experimental)', variable=shape_var, value='measured',
                    command=shape).pack(side='left', padx=6)

    ttk.Button(top, text='Read', command=do_read).pack(side='left', padx=8)

    def do_cancel():
        CANCEL.set()
        log('cancel: stops if it is still trying to connect. Once connected, the job finishes so nothing is left half written.')
    cancel_btn = ttk.Button(top, text='Cancel', command=do_cancel)
    cancel_btn.pack(side='left')
    cancel_btn.state(['disabled'])
    for text, cmd in (('Darker', lambda: shift(8)), ('Lighter', lambda: shift(-8)),
                      ('Factory', lambda: set_new(FACTORY)), ('Find position', do_probe)):
        ttk.Button(btns, text=text, command=cmd).pack(side='left', padx=2)
    for text, cmd in (('Load backup', do_restore), ('Write contrast + backlight', do_write)):
        ttk.Button(wrow, text=text, command=cmd).pack(side='left', padx=2)
    ttk.Button(odo, text='Set odometer', command=do_odo).pack(side='left', padx=6)
    refresh_units()

    draw()
    log('Read first. Darker/Lighter move the whole contrast curve by 8. Write makes a backup, writes, and verifies.')
    log('Backlight is experimental. The sliders move a table up or down from what is in the cluster; one Write applies contrast and backlight together.')
    log('The C row and the green "active (guess)" band are estimates, not measurements. Find position gives the exact points (3-4 test writes).')
    def on_close():
        if not state['busy']:
            return root.destroy()
        state['closing'] = True                       # never kill a write or probe half way: let it finish / restore
        log('closing: finishing the current job first (a probe puts your curve back), then the window closes')
    root.protocol('WM_DELETE_WINDOW', on_close)
    poll()
    root.mainloop()


# ---------------- CLI ----------------
def main():
    if len(sys.argv) == 1:
        return gui()
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--port', required=True)
    common.add_argument('--write', action='store_true', help='actually write (otherwise dry run)')
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('read', parents=[common])
    sub.add_parser('probe', parents=[common])
    s = sub.add_parser('shape', parents=[common], help='curve shape at the current level: measured (EXPERIMENTAL) or factory')
    s.add_argument('which', choices=['measured', 'factory'])
    s = sub.add_parser('shift', parents=[common])
    s._negative_number_matcher = re.compile(r'^-(0[xX])?[0-9a-fA-F]+$')     # let -0x10 through as a number
    s.add_argument('n', type=lambda v: int(v, 0), help='positive = darker, e.g. 0x10')
    s = sub.add_parser('set', parents=[common])
    s.add_argument('curve', help='10 hex bytes')
    s = sub.add_parser('restore', parents=[common])
    s.add_argument('file')
    sub.add_parser('clock', parents=[common])
    s = sub.add_parser('backlight', parents=[common])
    s.add_argument('which', choices=['fis', 'clock'])
    s._negative_number_matcher = re.compile(r'^-(0[xX])?[0-9a-fA-F]+$')     # let -0x10 through as a number
    s.add_argument('n', type=lambda v: int(v, 0), help='positive = brighter, e.g. 0x10')
    s = sub.add_parser('odometer', parents=[common])
    s.add_argument('value', type=int, help='mileage to set, in km (or miles with --miles)')
    s.add_argument('--miles', action='store_true')
    a = ap.parse_args()
    try:
        cur, name, outside, km, bl = read_curve(a.port)
        print('cluster     :', name)
        print('x           :', hx(XAXIS), '  (display temperature sensor: left = hot, right = cold)')
        print('about C     :', GUESS_ROW, '  (GUESS, not calibrated)')
        if outside is not None:
            act = active_for(outside)
            print('outside temp: %.0f C from the car. If the car has sat for hours, the active points are about x %s (guess)'
                  % (outside, ' and '.join('%02X' % XAXIS[i] for i in act)))
        print('curve now   :', hx(cur), '  (factory', hx(FACTORY) + ', lower = darker)')
        if km is not None:
            print('odometer    : %d km = %d mi  (stored value; the dash can read a mile or two different)' % (km, round(km / KM_PER_MI)))
        if bl:
            print('backlight   : FIS %s, clock+odometer %s  (higher = brighter; EXPERIMENTAL)' % (bl_text(bl, 'fis'), bl_text(bl, 'clock')))
        if a.cmd == 'read':
            return
        if a.cmd == 'clock':
            if not a.write:
                print('dry run: add --write to set the cluster clock to the time of this PC')
                return
            set_clock_pc(a.port)
            return
        if a.cmd == 'backlight':
            if not bl:
                raise Fail('the backlight tables in this cluster are not the known layout')
            new_bl = bl_shift(bl, a.which, a.n)
            print('backlight new: FIS %s, clock+odometer %s' % (bl_text(new_bl, 'fis'), bl_text(new_bl, 'clock')))
            if not a.write:
                print('dry run: add --write to apply')
                return
            apply_curve(a.port, bl=new_bl)
            return
        if a.cmd == 'odometer':
            new_km = round(a.value * KM_PER_MI) if a.miles else a.value
            print('odometer new: %d km = %d mi' % (new_km, round(new_km / KM_PER_MI)))
            if not a.write:
                print('dry run: add --write to apply')
                return
            set_odometer(a.port, new_km)
            return
        if a.cmd == 'probe':
            print(probe(a.port, lambda m: {'y': True, 'n': False}.get(input(m + ' [y/n/c=cancel] ').strip().lower()[:1]))[0])
            return
        new = (clamp([v - a.n for v in cur]) if a.cmd == 'shift' else
               shaped(cur, a.which == 'measured') if a.cmd == 'shape' else
               clamp([int(v, 16) for v in a.curve.split()]) if a.cmd == 'set' else
               list(open(a.file, 'rb').read()[0x275:0x27F]))
        if len(new) != 10:
            raise Fail('need 10 values')
        print('curve new   :', hx(new))
        new_bl = bl_read(open(a.file, 'rb').read()) if a.cmd == 'restore' and bl else None      # restore brings the backlight back too
        if new_bl:
            print('backlight new: FIS %s, clock+odometer %s' % (bl_text(new_bl, 'fis'), bl_text(new_bl, 'clock')))
        if not a.write:
            print('dry run: add --write to apply')
            return
        apply_curve(a.port, new, bl=new_bl)
    except Fail as e:
        sys.exit('ERROR: %s' % e)


if __name__ == '__main__':
    main()
