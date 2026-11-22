#!/usr/bin/env python3
"""
Ghost Hunter 2.1 -- CardputerZero (Linux) + M5Stack Cap CC1101

Launched from APPLaunch as user pi. No root, and no pip packages.

  * SCAN shows the spectrum and a smoothed threat level (fast attack / slow
    release, minimum time on each of the 6 levels). Tune the constants below.
  * TAB switches to GHOST MAP. Heading comes from the kernel IIO driver
    (BMI270 gyro + accel). If IIO is missing, turn with , and . or the arrows.
  * If the Cap CC1101 cannot be opened, the error is drawn on screen.
    S continues in simulation, Q quits.

Note: for fun only. The CC1101 antenna is not directional, so the bearing is
a sweep, and remotes, sensors and appliance noise show up as well.

Packages: python3-pygame python3-spidev alsa-utils fonts-dejavu-core
Run:      python3 ghost_hunter_v2.py            # real hardware
          python3 ghost_hunter_v2.py --sim      # simulated radio + IMU
          python3 ghost_hunter_v2.py --sim --size 320x170 --shot screenshots
Keys:     TAB screen   1-4 band   SPACE baseline   M mute   S level speed
          Z zero heading / clear map / re-calibrate gyro (hold still 1 s)
          F flip rotation direction   , and . (or arrows) turn manually if no IMU
          Q/ESC quit
"""
import argparse, errno, glob, math, os, random, struct, subprocess, tempfile, time, wave


class HardwareError(Exception):
    """Radio or GPIO setup failed. Drawn on screen instead of exiting silently."""

# ---------------- Config ----------------
SPI_BUS, SPI_DEV, SPI_HZ = 0, 1, 5_000_000   # EXT header exposes CS1 (G7) -> /dev/spidev0.1
BANDS = {                                    # name: (start MHz, end MHz)
    "1": ("315M", 314.0, 316.0),
    "2": ("433M", 432.0, 435.0),
    "3": ("868M", 867.0, 869.0),
    "4": ("915M", 902.0, 928.0),
}
# M5Stack Cap CC1101 on the CardputerZero EXT 14P header
PIN_POWER_EN, PIN_RF_SW0, PIN_NFC_CS = 26, 15, 22
USE_CAP = True
BAND_SW = {"1": (0, 1), "2": (1, 0), "3": (1, 1), "4": (1, 1)}   # (RF_SW0, RF_SW1)
STEPS = 24            # channels per band sweep
CH_PER_FRAME = 4      # channels scanned per rendered frame
XTAL = 26_000_000

# ---- Level timing (tune these to make levels stay longer / shorter) ----
TICK_HZ = 10          # how often the level logic updates
ATTACK_TAU = 0.35     # s  score rise time constant (small = reacts faster)
RELEASE_TAU = 3.0     # s  score fall time constant (big = falls slower)
HOLD_UP = 0.6         # s  minimum time a level is shown before stepping UP one level
HOLD_DOWN = 2.5       # s  minimum time a level is shown before stepping DOWN one level
SPEEDS = [("SLOW", 1.8), ("NORM", 1.0), ("FAST", 0.5)]   # multipliers on the hold times (key S)
BASE_ALPHA = 0.01     # background baseline adaptation per visit (small = slower)

# ---- Ghost map ----
NBINS = 36            # 10 degree bins
MAP_HALF_LIFE = 15.0  # s  how long a blip stays on the radar
GYRO_DEADBAND = 0.8   # deg/s ignored to reduce drift


# ---------------- GPIO helpers (Cap CC1101) ----------------
def gpio_out(pin, level):
    try:
        subprocess.run(["pinctrl", "set", str(pin), "op", "dh" if level else "dl"],
                       check=True, capture_output=True)
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        raise HardwareError(
            f"GPIO{pin} failed ({e}). The pi user needs the gpio group. "
            f"Or run: pinctrl set {pin} op {'dh' if level else 'dl'}"
        ) from e


def pinctrl_dump(pins="7,9,10,11,15,17,22,23,26"):
    try:
        return subprocess.run(["pinctrl", "get", pins], capture_output=True, text=True).stdout.strip()
    except FileNotFoundError:
        return "(pinctrl command not found)"


def cap_power_on():
    gpio_out(PIN_NFC_CS, 1)       # deselect the NFC chip (shares MISO)
    gpio_out(PIN_POWER_EN, 1)     # enable the Cap DC-DC
    time.sleep(0.2)


# ---------------- CC1101 driver ----------------
SRES, SCAL, SRX, SIDLE = 0x30, 0x33, 0x34, 0x36
REG_RSSI = 0x34


class CC1101:
    def __init__(self, bus=SPI_BUS, dev=SPI_DEV):
        try:
            import spidev
        except ImportError as e:
            raise HardwareError("python3-spidev is not installed") from e
        path = f"/dev/spidev{bus}.{dev}"
        if not os.path.exists(path):
            raise HardwareError(
                f"{path} not found. SPI devices present: "
                f"{glob.glob('/dev/spidev*') or 'none'}. "
                "Enable SPI / the CS1 spidev node, or use --spi-dev."
            )
        if USE_CAP:
            cap_power_on()
        self.spi = spidev.SpiDev()
        self.spi.open(bus, dev)
        self.spi.mode = 0
        part, ver = 0, 0
        for hz, extra in ((SPI_HZ, None), (500_000, None), (500_000, 17)):
            if extra is not None and USE_CAP:
                print(f"  Retry: also driving G{extra} high")
                gpio_out(extra, 1)
                time.sleep(0.3)
            self.spi.max_speed_hz = hz
            self.reset()
            part, ver = self.read_status(0x30), self.read_status(0x31)
            if ver not in (0x00, 0xFF):
                break
        if ver in (0x00, 0xFF):
            raise HardwareError(
                f"CC1101 not responding (PARTNUM=0x{part:02X}, VERSION=0x{ver:02X}). "
                f"Pin states: {pinctrl_dump()}. "
                "Check: Cap fully seated, inserted with power off, G26 reads high."
            )
        self.spi.max_speed_hz = SPI_HZ
        print(f"CC1101 detected: PARTNUM=0x{part:02X} VERSION=0x{ver:02X}")
        self.init_radio()

    def select_band(self, key):
        if not USE_CAP:
            return
        sw0, sw1 = BAND_SW[key]
        gpio_out(PIN_RF_SW0, sw0)
        self.write(0x00, 0x6F if sw1 else 0x2F)   # IOCFG2 drives RF_SW1: 0x2F low, 0x6F high

    def strobe(self, cmd):
        self.spi.xfer2([cmd])

    def write(self, reg, val):
        self.spi.xfer2([reg, val])

    def read_status(self, reg):
        return self.spi.xfer2([reg | 0xC0, 0])[1]

    def reset(self):
        self.strobe(SRES)
        time.sleep(0.01)

    def init_radio(self):
        for reg, val in ((0x0B, 0x06), (0x10, 0x86), (0x11, 0x93), (0x12, 0x30),
                         (0x08, 0x32), (0x1B, 0x07), (0x1C, 0x00), (0x1D, 0x91)):
            self.write(reg, val)
        self.set_freq(433.92)

    def set_freq(self, mhz):
        f = int(mhz * 1e6 * 65536 / XTAL)
        self.strobe(SIDLE)
        self.write(0x0D, (f >> 16) & 0xFF)
        self.write(0x0E, (f >> 8) & 0xFF)
        self.write(0x0F, f & 0xFF)

    def rssi_at(self, mhz):
        self.set_freq(mhz)
        self.strobe(SCAL)
        time.sleep(0.001)
        self.strobe(SRX)
        time.sleep(0.002)
        raw = self.read_status(REG_RSSI)
        self.strobe(SIDLE)
        return (raw - 256) / 2 - 74 if raw >= 128 else raw / 2 - 74


class SimRadio:
    """Simulated radio: noise floor + a ghost that lives at a fixed bearing."""
    def __init__(self):
        self.heading = 0.0
        self._respawn()

    def _respawn(self):
        self.bearing = random.uniform(0, 360)
        self.amp = random.uniform(20, 32)
        self.ttl = time.time() + random.uniform(30, 50)

    def select_band(self, key):
        pass

    def rssi_at(self, mhz):
        if time.time() > self.ttl:
            self._respawn()
        gain = max(0.0, math.cos(math.radians(self.heading - self.bearing))) ** 2
        spike = self.amp * gain * random.uniform(0.5, 1.0) if random.random() < 0.6 else 0.0
        return -98 + random.gauss(0, 1.2) + spike


# ---------------- IMU / heading ----------------
class IIOImu:
    """Reads gyro + accel from the Linux IIO sysfs interface (e.g. bmi270 driver)."""
    def __init__(self):
        self.g, self.a = self._find("anglvel"), self._find("accel")
        if not self.g or not self.a:
            raise RuntimeError("no IIO gyro/accel under /sys/bus/iio/devices")
        self.name = self.g[2]

    @staticmethod
    def _rd(path):
        with open(path) as f:
            return f.read().strip()

    def _find(self, kind):
        for d in sorted(glob.glob("/sys/bus/iio/devices/iio:device*")):
            if os.path.exists(f"{d}/in_{kind}_x_raw"):
                scale = 1.0
                for cand in (f"{d}/in_{kind}_scale", f"{d}/in_{kind}_x_scale"):
                    if os.path.exists(cand):
                        scale = float(self._rd(cand))
                        break
                name = self._rd(f"{d}/name") if os.path.exists(f"{d}/name") else "iio"
                return d, scale, name
        return None

    def read(self):
        (gd, gs, _), (ad, a_s, _) = self.g, self.a
        g = [int(self._rd(f"{gd}/in_anglvel_{c}_raw")) * gs for c in "xyz"]   # rad/s
        a = [int(self._rd(f"{ad}/in_accel_{c}_raw")) * a_s for c in "xyz"]    # m/s^2
        return (*g, *a)


class SimImu:
    name = "SIM"
    no_cal = True          # simulated rotation is not sensor bias

    def __init__(self):
        self.t0 = time.time()

    def read(self):
        t = time.time() - self.t0
        rate = 200 * 0.3 * math.cos(0.3 * t)          # deg/s, sweeps +-200 deg
        return (0.0, 0.0, -math.radians(rate), 0.0, 0.0, 9.81)


def make_imu(mode, sim):
    if mode == "sim" or (sim and mode == "auto"):
        return SimImu()
    if mode in ("auto", "iio"):
        try:
            return IIOImu()
        except Exception as e:
            print(f"IMU via IIO not available ({e}).")
    print("Falling back to manual heading: use , and . (or arrow keys) to turn.")
    return None


class Heading:
    """Yaw angle (0-360, clockwise) from gyro rate projected on the gravity axis."""
    def __init__(self, imu, flip=False):
        self.imu, self.flip = imu, flip
        self.deg, self.t = 0.0, time.time()
        self.bias, self.cal_end, self.cal_sum, self.cal_n = (0.0, 0.0, 0.0), 0.0, [0.0] * 3, 0
        self.error = False

    @property
    def calibrating(self):
        return time.time() < self.cal_end

    def calibrate(self, secs=1.0):
        if getattr(self.imu, "no_cal", False):
            return
        self.cal_end, self.cal_sum, self.cal_n = time.time() + secs, [0.0] * 3, 0

    def update(self, manual_dir=0):
        now = time.time()
        dt, self.t = min(now - self.t, 0.2), now
        rate = 0.0
        if self.imu is None:
            rate = 90.0 * manual_dir
        else:
            try:
                gx, gy, gz, ax, ay, az = self.imu.read()
                self.error = False
            except Exception:
                self.error = True
                return
            if self.calibrating or self.cal_n and now < self.cal_end + 0.05:
                self.cal_sum = [s + v for s, v in zip(self.cal_sum, (gx, gy, gz))]
                self.cal_n += 1
                return
            if self.cal_n:
                self.bias = tuple(s / self.cal_n for s in self.cal_sum)
                self.cal_n = 0
            n = math.sqrt(ax * ax + ay * ay + az * az) or 1.0
            up = (ax / n, ay / n, az / n)                      # accel points "up" at rest
            g = (gx - self.bias[0], gy - self.bias[1], gz - self.bias[2])
            rate = -math.degrees(g[0] * up[0] + g[1] * up[1] + g[2] * up[2])   # clockwise +
            if abs(rate) < GYRO_DEADBAND:
                rate = 0.0
        if self.flip:
            rate = -rate
        self.deg = (self.deg + rate * dt) % 360


# ---------------- Ghost analysis ----------------
LEVELS = [
    (0, "CALM", (80, 200, 120)),
    (1, "FAINT SIGNS", (140, 220, 80)),
    (2, "PRESENCE", (240, 220, 60)),
    (3, "SPIRIT NEAR!", (255, 150, 40)),
    (4, "STRONG!!", (255, 70, 50)),
    (5, "BEHIND YOU", (255, 0, 120)),
]


class Analyzer:
    def __init__(self, n):
        self.n = n
        self.reset()

    def reset(self):
        self.base, self.cur = [None] * self.n, [-100.0] * self.n
        self.win = self.inst = self.score = self.peak = 0.0
        self.level, self.since = 0, time.time()

    def update(self, i, rssi):
        """Called for every channel visit (fast). Only collects the window maximum."""
        b = self.base[i] = self.base[i] if self.base[i] is not None else rssi
        delta = rssi - b
        a = BASE_ALPHA if delta < 6 else BASE_ALPHA * 0.2      # barely adapt while a signal is present
        self.base[i] = b + delta * a
        self.cur[i] = rssi
        self.win = max(self.win, min(max(delta, 0.0) / 25.0, 1.0))

    def tick(self, dt, speed):
        """Called at TICK_HZ (slow). Smooths the score and steps the displayed level."""
        self.inst, self.win = self.win, 0.0
        tau = ATTACK_TAU if self.inst > self.score else RELEASE_TAU
        self.score += (self.inst - self.score) * (1 - math.exp(-dt / tau))
        self.peak = max(self.peak * math.exp(-dt / 20.0), self.score)
        now, x = time.time(), self.score * 6
        held = now - self.since
        if x >= self.level + 1 and self.level < 5 and held >= HOLD_UP * speed:
            self.level, self.since = self.level + 1, now
        elif x < self.level - 0.3 and self.level > 0 and held >= HOLD_DOWN * speed:
            self.level, self.since = self.level - 1, now


class GhostMap:
    def __init__(self):
        self.clear()

    def clear(self):
        self.bins, self.last = [0.0] * NBINS, None

    def add(self, heading, val, dt):
        k = 0.5 ** (dt / MAP_HALF_LIFE)
        self.bins = [v * k for v in self.bins]
        if self.last is None:
            arc = [heading]
        else:                                        # fill bins skipped by fast turns
            d = ((heading - self.last + 180) % 360) - 180
            n = max(1, int(abs(d) / (360 / NBINS)) + 1)
            arc = [(self.last + d * j / n) % 360 for j in range(1, n + 1)]
        for h in arc:
            i = int(h / (360 / NBINS)) % NBINS
            for off, w in ((0, 1.0), (-1, 0.5), (1, 0.5)):
                j = (i + off) % NBINS
                self.bins[j] = max(self.bins[j], val * w)
        self.last = heading

    def peak(self):
        i = max(range(NBINS), key=lambda k: self.bins[k])
        return (i + 0.5) * 360 / NBINS, self.bins[i]


# ---------------- Sound ----------------
class Beeper:
    def __init__(self):
        self.muted, self.last, self.files = False, 0, {}

    def _tone(self, freq):
        if freq not in self.files:
            path = os.path.join(tempfile.gettempdir(), f"gh_{freq}.wav")
            with wave.open(path, "w") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
                n = 1600
                w.writeframes(b"".join(struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / 16000)
                                                              * (1 - i / n))) for i in range(n)))
            self.files[freq] = path
        return self.files[freq]

    def beep(self, level):
        now = time.time()
        if self.muted or level == 0 or now - self.last < max(0.12, 0.9 - level * 0.16):
            return
        self.last = now
        try:
            subprocess.Popen(["aplay", "-q", self._tone(400 + level * 200)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:                        # no aplay installed -> stay silent
            self.muted = True
            print("aplay not found (sudo apt install alsa-utils); sound disabled.")


# ---------------- Main ----------------
def wrap_text(font, text, width):
    lines = []
    for para in text.splitlines() or [""]:
        words = para.split()
        if not words:
            lines.append("")
            continue
        cur = ""
        for word in words:
            trial = word if not cur else cur + " " + word
            if font.size(trial)[0] <= width or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
    return lines


class FbMirror:
    """Copy the pygame window onto the panel framebuffer APPLaunch hands over."""

    def __init__(self, path):
        base = "/sys/class/graphics/" + os.path.basename(path)
        self.w, self.h = self._size(base)
        self.bpp = int(self._rd(f"{base}/bits_per_pixel"))
        self.stride = self.w * (2 if self.bpp == 16 else 4)
        for name in ("line_length", "stride"):
            try:
                self.stride = int(self._rd(f"{base}/{name}"))
                break
            except OSError:
                pass
        if self.bpp not in (16, 32):
            raise HardwareError(f"{path} is {self.bpp}-bit; need 16 or 32")
        try:
            self.fd = os.open(path, os.O_RDWR)
        except OSError:
            self.fd = os.open(path, os.O_WRONLY)
        self._np = None
        try:
            import numpy
            self._np = numpy
        except ImportError:
            pass

    @staticmethod
    def _rd(path):
        with open(path) as f:
            return f.read().strip()

    def _size(self, base):
        try:
            for mode in self._rd(f"{base}/modes").split():
                spec = mode.split(":", 1)[-1]
                spec = spec.split("p", 1)[0].split("i", 1)[0]
                if "x" in spec:
                    w, h = spec.split("x", 1)
                    return int(w), int(h)
        except OSError:
            pass
        w, h = self._rd(f"{base}/virtual_size").split(",")
        return int(w), int(h)

    def present(self, screen):
        import pygame
        rgb = pygame.image.tobytes(screen, "RGB")
        if self._np is not None:
            frame = self._pack_numpy(rgb)
        else:
            frame = self._pack_python(rgb)
        row = self.w * (2 if self.bpp == 16 else 4)
        if self.stride == row:
            os.lseek(self.fd, 0, os.SEEK_SET)
            os.write(self.fd, frame)
            return
        pos = 0
        for y in range(self.h):
            start = y * row
            os.lseek(self.fd, pos, os.SEEK_SET)
            os.write(self.fd, frame[start:start + row])
            pos += self.stride

    def _pack_numpy(self, rgb):
        px = self._np.frombuffer(rgb, dtype=self._np.uint8).reshape(-1, 3)
        r = px[:, 0].astype(self._np.uint16)
        g = px[:, 1].astype(self._np.uint16)
        b = px[:, 2].astype(self._np.uint16)
        if self.bpp == 32:
            out = self._np.empty((px.shape[0], 4), dtype=self._np.uint8)
            out[:, 0], out[:, 1], out[:, 2], out[:, 3] = px[:, 2], px[:, 1], px[:, 0], 255
            return out.tobytes()
        pix = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        return pix.astype("<u2").tobytes()

    def _pack_python(self, rgb):
        mv = memoryview(rgb)
        n = self.w * self.h
        if self.bpp == 32:
            out = bytearray(n * 4)
            o = 0
            for i in range(0, n * 3, 3):
                out[o:o + 4] = bytes((mv[i + 2], mv[i + 1], mv[i], 255))
                o += 4
            return out
        out = bytearray(n * 2)
        o = 0
        for i in range(0, n * 3, 3):
            v = ((mv[i] & 0xF8) << 8) | ((mv[i + 1] & 0xFC) << 3) | (mv[i + 2] >> 3)
            out[o] = v & 0xFF
            out[o + 1] = v >> 8
            o += 2
        return out


def offer_sim(screen, font, font_xs, message, present):
    """Hardware is missing. S starts the simulator, Q quits. None means quit."""
    import pygame
    W, H = screen.get_size()
    clock = pygame.time.Clock()
    lines = wrap_text(font, "NO RADIO", W - 16) + [""] + wrap_text(font, message, W - 16)
    hint = "S: simulate    Q: quit"
    while True:
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                return None
            if e.type != pygame.KEYDOWN:
                continue
            if e.key in (pygame.K_q, pygame.K_ESCAPE):
                return None
            if e.key == pygame.K_s:
                return SimRadio()
        screen.fill((8, 8, 14))
        y = 8
        line_h = font.get_height() + 2
        room = H - font_xs.get_height() - 16
        for line in lines:
            if y + line_h > room:
                break
            screen.blit(font.render(line, True, (255, 180, 80)), (8, y))
            y += line_h
        screen.blit(font_xs.render(hint, True, (180, 180, 200)), (8, H - font_xs.get_height() - 6))
        present()
        clock.tick(30)


def polar(cx, cy, r, deg):
    a = math.radians(deg)
    return int(cx + r * math.sin(a)), int(cy - r * math.cos(a))


def main():
    global USE_CAP
    ap = argparse.ArgumentParser()
    ap.add_argument("--sim", action="store_true", help="simulated radio + IMU (no hardware)")
    ap.add_argument("--spi-dev", default=f"{SPI_BUS}.{SPI_DEV}", help="spidev id, default 0.1")
    ap.add_argument("--no-cap", action="store_true", help="not using the Cap CC1101")
    ap.add_argument("--imu", choices=["auto", "iio", "manual", "sim"], default="auto")
    ap.add_argument("--flip", action="store_true", help="invert rotation direction")
    ap.add_argument("--size", help="windowed size WxH instead of fullscreen (e.g. 320x170)")
    ap.add_argument("--shot", help="(testing) save screenshots into this folder and exit")
    args = ap.parse_args()
    USE_CAP = not args.no_cap
    bus, dev = (int(x) for x in args.spi_dev.split("."))

    import pygame
    fb_path = os.environ.get("GHOST_HUNTER_FB")
    if fb_path:
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
    pygame.init()
    fb_out = None
    if args.size:
        screen = pygame.display.set_mode(tuple(int(v) for v in args.size.lower().split("x")))
    elif fb_path:
        try:
            fb_out = FbMirror(fb_path)
        except OSError as e:
            hint = "Log out and back in so the video group applies." if e.errno == errno.EACCES else ""
            raise SystemExit(f"Cannot open {fb_path}: {e}" + (f"\n{hint}" if hint else "")) from e
        screen = pygame.display.set_mode((fb_out.w, fb_out.h))
    else:
        screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
    pygame.mouse.set_visible(False)

    def present():
        pygame.display.flip()
        if fb_out is not None:
            fb_out.present(screen)
    W, H = screen.get_size()
    fam = ["dejavusans", "liberationsans", "freesans"]
    font_xs = pygame.font.SysFont(fam, max(10, H // 14))
    font_s = pygame.font.SysFont(fam, max(12, H // 11))
    font_l = pygame.font.SysFont(fam, max(16, H // 7), bold=True)
    screen.fill((8, 8, 14))
    screen.blit(font_l.render("GHOST HUNTER", True, (80, 220, 140)), (8, H // 3))
    present()

    if args.sim:
        radio = SimRadio()
    else:
        try:
            radio = CC1101(bus, dev)
        except HardwareError as e:
            radio = offer_sim(screen, font_s, font_xs, str(e), present)
            if radio is None:
                pygame.quit()
                return
    imu = make_imu(args.imu, args.sim)
    imu_name = imu.name if imu else "MANUAL"
    hd = Heading(imu, flip=args.flip)
    hd.calibrate(1.0)
    gmap, beeper, az = GhostMap(), Beeper(), Analyzer(STEPS)
    band_key, view, speed_idx, idx = "3", 0, 1, 0
    radio.select_band(band_key)
    clock, flash = pygame.time.Clock(), 0
    last_tick = t_start = time.time()
    shots = 0

    def freq_of(i):
        _, lo, hi = BANDS[band_key]
        return lo + (hi - lo) * i / (STEPS - 1)

    def text(font, s, color, pos):
        screen.blit(font.render(s, True, color), pos)

    running = True
    while running:
        for e in pygame.event.get():
            if e.type != pygame.KEYDOWN:
                continue
            if e.key in (pygame.K_q, pygame.K_ESCAPE):
                running = False
            elif e.key == pygame.K_TAB:
                view = (view + 1) % 2
            elif e.unicode in BANDS:
                band_key = e.unicode
                radio.select_band(band_key)
                az.reset(); gmap.clear(); idx = 0
            elif e.key == pygame.K_SPACE:
                az.reset()
            elif e.key == pygame.K_m:
                beeper.muted = not beeper.muted
            elif e.key == pygame.K_s:
                speed_idx = (speed_idx + 1) % len(SPEEDS)
            elif e.key == pygame.K_z:
                hd.deg = 0.0; hd.calibrate(1.0); gmap.clear()
            elif e.key == pygame.K_f:
                hd.flip = not hd.flip

        keys = pygame.key.get_pressed()
        manual = (keys[pygame.K_PERIOD] or keys[pygame.K_RIGHT]) - (keys[pygame.K_COMMA] or keys[pygame.K_LEFT])

        for _ in range(CH_PER_FRAME):                    # fast: raw channel scan
            az.update(idx, radio.rssi_at(freq_of(idx)))
            idx = (idx + 1) % STEPS
        hd.update(manual)
        if isinstance(radio, SimRadio):
            radio.heading = hd.deg

        now = time.time()
        if now - last_tick >= 1.0 / TICK_HZ:             # slow: level logic + map
            dt, last_tick = now - last_tick, now
            az.tick(dt, SPEEDS[speed_idx][1])
            if not hd.calibrating:
                gmap.add(hd.deg, az.inst, dt)

        lvl = az.level
        beeper.beep(lvl)
        name, color = LEVELS[lvl][1], LEVELS[lvl][2]
        flash = (flash + 1) % 6 if lvl >= 4 else 0
        screen.fill((20, 0, 30) if (lvl >= 4 and flash < 3) else (8, 8, 14))
        bname = BANDS[band_key][0]
        hh = font_s.get_height() + 4

        if view == 0:                                    # ---------- SCAN screen ----------
            text(font_s, f"{bname}  SCAN  SPD:{SPEEDS[speed_idx][0]}" + ("  MUTED" if beeper.muted else ""),
                 (150, 150, 170), (6, 2))
            top, bottom = int(H * 0.20), int(H * 0.58)
            bw = (W - 12) / STEPS
            for i, r in enumerate(az.cur):
                h = max(1, int((min(max(r, -110), -50) + 110) / 60 * (bottom - top)))
                c = color if az.base[i] is not None and r - az.base[i] > 6 else (60, 110, 150)
                pygame.draw.rect(screen, c, (6 + i * bw, bottom - h, max(1, bw - 2), h))
            bar_y, bar_h = int(H * 0.63), int(H * 0.07)
            pygame.draw.rect(screen, (40, 40, 60), (6, bar_y, W - 12, bar_h), 1)
            pygame.draw.rect(screen, color, (8, bar_y + 2, int((W - 16) * az.score), bar_h - 4))
            pk = 6 + int((W - 12) * az.peak)
            pygame.draw.line(screen, (255, 255, 255), (pk, bar_y), (pk, bar_y + bar_h), 2)
            text(font_l, name, color, (6, int(H * 0.73)))
            text(font_s, f"{az.score * 100:3.0f}%  Lv{lvl}", color, (W - W // 4, int(H * 0.76)))
            text(font_xs, "TAB:map 1-4:band SPC:base M:mute S:speed", (110, 110, 130),
                 (6, H - font_xs.get_height() - 2))
        else:                                            # ---------- GHOST MAP screen ----------
            text(font_s, f"GHOST MAP  {bname}", (150, 150, 170), (6, 2))
            foot = font_xs.get_height() + 2
            R = (H - hh - foot - 4) // 2
            cx, cy = R + 8, hh + 2 + R
            hdg = hd.deg
            # pointing cone, rings, cross
            pygame.draw.polygon(screen, (10, 40, 22), [(cx, cy), polar(cx, cy, R, hdg - 14), polar(cx, cy, R, hdg + 14)])
            for k in (1, 2, 3):
                pygame.draw.circle(screen, (30, 70, 45), (cx, cy), R * k // 3, 1)
            pygame.draw.line(screen, (30, 70, 45), (cx - R, cy), (cx + R, cy), 1)
            pygame.draw.line(screen, (30, 70, 45), (cx, cy - R), (cx, cy + R), 1)
            text(font_xs, "0", (90, 160, 110), (cx - 3, cy - R + 1))
            # blips: stronger signal -> closer to the centre
            for i, v in enumerate(gmap.bins):
                if v < 0.12:
                    continue
                p = polar(cx, cy, R * (0.95 - 0.85 * v), (i + 0.5) * 360 / NBINS)
                pygame.draw.circle(screen, (int(110 + 145 * v), int(30 * (1 - v)), int(30 * (1 - v))), p, 2 + int(3 * v))
            bearing, bval = gmap.peak()
            if bval >= 0.12:                             # strongest spot: pulsing red dot
                p = polar(cx, cy, R * (0.95 - 0.85 * bval), bearing)
                pulse = 0.5 + 0.5 * math.sin(time.time() * 6)
                pygame.draw.circle(screen, (255, 40, 40), p, 5 + int(3 * pulse))
                pygame.draw.circle(screen, (255, 160, 160), p, 9 + int(4 * pulse), 1)
            pygame.draw.line(screen, (90, 255, 130), (cx, cy), polar(cx, cy, R, hdg), 2)   # sweep line
            pygame.draw.circle(screen, (255, 255, 255), (cx, cy), 3)
            if hd.calibrating:
                text(font_s, "HOLD STILL", (255, 220, 80), (cx - 40, cy + R // 3))
            # info panel
            px, py, step = cx + R + 14, hh + 4, font_s.get_height() + 2
            text(font_s, f"HDG {hdg:03.0f}", (200, 200, 220), (px, py))
            text(font_s, f"SIG {az.score * 100:3.0f}%", color, (px, py + step))
            text(font_s, name, color, (px, py + 2 * step))
            if bval >= 0.12:
                d = ((bearing - hdg + 180) % 360) - 180
                text(font_s, f"BRG {bearing:03.0f}", (255, 90, 90), (px, py + 3 * step))
                text(font_s, "ON TARGET" if abs(d) < 12 else f"TURN {'R' if d > 0 else 'L'} {abs(d):.0f}",
                     (255, 160, 160), (px, py + 4 * step))
            else:
                text(font_s, "SWEEP AROUND", (130, 130, 150), (px, py + 3 * step))
            text(font_xs, f"IMU:{imu_name}{' FLIP' if hd.flip else ''}{' ERR' if hd.error else ''}",
                 (110, 110, 130), (px, py + 5 * step))
            text(font_xs, "TAB:scan Z:zero F:flip ,/.:turn", (110, 110, 130), (6, H - foot))
        present()

        if args.shot:                                    # testing helper: screenshots, then exit
            el = time.time() - t_start
            if shots == 0 and el > 5:
                os.makedirs(args.shot, exist_ok=True)
                pygame.image.save(screen, os.path.join(args.shot, "scan.png")); shots, view = 1, 1
            elif shots == 1 and el > 20:
                pygame.image.save(screen, os.path.join(args.shot, "map.png")); running = False
        clock.tick(30)
    pygame.quit()


if __name__ == "__main__":
    main()
