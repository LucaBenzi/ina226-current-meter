#!/usr/bin/env python3
"""
ina226-current-meter
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Real-time current monitor based on the INA226 sensor read by an Arduino and
streamed over serial. Averages samples, plots live data, and logs to CSV with
daily rotation.

Serial format: micros,shunt,bus   (one line = one sample)
  micros  : unsigned long  — Arduino timestamp in µs
  shunt   : int16_t        — INA226 shunt register  (LSB = 2.5 µV)
  bus     : int16_t        — INA226 bus register    (LSB = 1.25 mV)

Current [A] = (shunt_raw × 2.5e-6) / R_shunt_ohm

Dependencies:
  pip install pyserial numpy matplotlib
  sudo apt install python3-tk    # if TkAgg backend is missing

Usage examples:
  python monitor.py                         # default: shunt=0.1 Ω, avg=50
  python monitor.py --shunt 1.0            # 1 Ω shunt resistor
  python monitor.py --shunt 0.1 --avg 100  # more aggressive averaging
  python monitor.py --window 300           # 5-minute plot window
  python monitor.py --capacity 2000        # 2000 mAh battery life estimate
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import argparse
import csv
import logging
import logging.handlers
import os
import queue
import signal
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime, date

import matplotlib
# Prova backend in ordine di preferenza (TkAgg richiede python3-tk)
for _backend in ('TkAgg', 'Qt5Agg', 'GTK3Agg', 'WXAgg'):
    try:
        matplotlib.use(_backend)
        break
    except Exception:
        pass

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from matplotlib.animation import FuncAnimation
import numpy as np
import serial


# ═══════════════════════════════════════════════════════════════════════════════
#  Costanti hardware INA226
# ═══════════════════════════════════════════════════════════════════════════════
SHUNT_LSB_V  = 2.5e-6    # 2.5 µV per LSB (registro shunt)
BUS_LSB_V    = 1.25e-3   # 1.25 mV per LSB (registro bus)
UINT32_WRAP  = 2 ** 32   # overflow micros() su AVR (71.58 min)


# ═══════════════════════════════════════════════════════════════════════════════
#  Argomenti CLI
# ═══════════════════════════════════════════════════════════════════════════════
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description='ina226-current-meter — Arduino serial → real-time plot + CSV')
    p.add_argument('--port',   default='/dev/ttyUSB0',
                   help='Serial port (default: /dev/ttyUSB0)')
    p.add_argument('--baud',   default=115200, type=int,
                   help='Baud rate (default: 115200)')
    p.add_argument('--shunt',  default=10.0, type=float,
                   help='Shunt resistor value in Ohm (default: 10.0)')
    p.add_argument('--avg',    default=50, type=int,
                   help='Raw samples to average per data point (default: 50)')
    p.add_argument('--window', default=60, type=int,
                   help='Plot time window in seconds (default: 60)')
    p.add_argument('--outdir', default='measurements',
                   help='CSV output directory (default: measurements/)')
    p.add_argument('--capacity', default=None, type=float,
                   help='Battery capacity in mAh for life estimate (e.g. --capacity 2000)')
    p.add_argument('--offset', default=0.0, type=float,
                   help='Input-referred voltage offset correction in µV (e.g. --offset -5)')
    return p.parse_args()


# ═══════════════════════════════════════════════════════════════════════════════
#  Unit formatting
# ═══════════════════════════════════════════════════════════════════════════════
def auto_unit(peak_abs_a: float) -> tuple[float, str]:
    """Returns (multiplier, label) for the most readable current unit."""
    if peak_abs_a <= 0:
        return 1e6, 'µA'
    if peak_abs_a < 1e-6:
        return 1e9, 'nA'
    if peak_abs_a < 1e-3:
        return 1e6, 'µA'
    if peak_abs_a < 1.0:
        return 1e3, 'mA'
    return 1.0, 'A'


def fmt_current(amps: float) -> str:
    mul, unit = auto_unit(abs(amps))
    return f'{amps * mul:.3f} {unit}'


def fmt_battery_life(capacity_mah: float, mean_current_a: float) -> str:
    if mean_current_a <= 0:
        return '---'
    hours = capacity_mah / (mean_current_a * 1e3)
    days  = hours / 24
    if days >= 1:
        return f'{days:.1f} days ({hours:.0f} h)'
    return f'{hours:.1f} h'


def fmt_charge(coulombs: float) -> str:
    uah = coulombs / 3600 * 1e6
    if uah < 1e3:
        return f'{uah:.3f} µAh'
    mah = uah / 1e3
    if mah < 1e3:
        return f'{mah:.4f} mAh'
    return f'{mah / 1e3:.6f} Ah'


def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    return f'{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}'


def time_axis_params(elapsed_s: float) -> tuple[float, str]:
    if elapsed_s < 120:
        return 1.0, 'Elapsed time [s]'
    if elapsed_s < 7200:
        return 60.0, 'Elapsed time [min]'
    if elapsed_s < 86400:
        return 3600.0, 'Elapsed time [h]'
    return 86400.0, 'Elapsed time [days]'


# ═══════════════════════════════════════════════════════════════════════════════
#  Thread lettura seriale — riconnessione automatica
# ═══════════════════════════════════════════════════════════════════════════════
class SerialReader(threading.Thread):
    def __init__(self, port: str, baud: int, out_q: queue.Queue,
                 log: logging.Logger):
        super().__init__(daemon=True, name='serial-reader')
        self.port  = port
        self.baud  = baud
        self.out_q = out_q
        self.log   = log
        self._stop = threading.Event()
        self.connected = False

    def run(self):
        while not self._stop.is_set():
            try:
                with serial.Serial(self.port, self.baud, timeout=1.0) as ser:
                    self.connected = True
                    msg = f'connected to {self.port} @ {self.baud} bps'
                    print(f'[serial] {msg}')
                    self.log.info(msg)
                    while not self._stop.is_set():
                        line = ser.readline()
                        if line:
                            try:
                                self.out_q.put_nowait(line)
                            except queue.Full:
                                self.log.warning('Serial buffer full — sample dropped')
            except serial.SerialException as exc:
                self.connected = False
                print(f'[serial] {exc!r} — retrying in 5 s…')
                self.log.error('SerialException: %s — retrying in 5 s', exc)
                time.sleep(5)

    def stop(self):
        self._stop.set()


# ═══════════════════════════════════════════════════════════════════════════════
#  Thread elaborazione dati — media + integrazione carica + CSV
# ═══════════════════════════════════════════════════════════════════════════════
class DataProcessor(threading.Thread):
    def __init__(
        self,
        in_q:       queue.Queue,
        plot_q:     queue.Queue,
        outdir:     str,
        shunt_ohm:  float,
        avg_n:      int,
        offset_uv:  float = 0.0,
        log:        logging.Logger = None,
    ):
        super().__init__(daemon=True, name='processor')
        self.in_q       = in_q
        self.log        = log or logging.getLogger('monitor')
        self.plot_q     = plot_q
        self.outdir     = outdir
        self.shunt_ohm  = shunt_ohm
        self.avg_n      = avg_n
        self.offset_a   = offset_uv * 1e-6 / shunt_ohm   # voltage offset → current correction
        self._stop      = threading.Event()

        # Buffer campioni (svuotato ogni avg_n campioni)
        self._b_us  : list[float] = []   # timestamp unwrappato [µs]
        self._b_i   : list[float] = []   # corrente [A]
        self._b_v   : list[float] = []   # tensione bus [V]

        # Gestione overflow micros() su AVR (wrappa ogni ~71.58 min)
        self._last_raw_us: int | None = None
        self._us_offset:   int        = 0

        # Integrazione carica
        self._charge_C:   float       = 0.0
        self._last_t_s:   float | None = None
        self._start_t_s:  float | None = None

        # Stima frequenza di campionamento
        self._rate_buf: deque[float] = deque(maxlen=20)
        self._last_flush_wall: float | None = None

        # Statistiche
        self.total_raw: int = 0
        self.total_avg: int = 0

        # CSV (rotazione giornaliera)
        self._current_date: date | None = None
        self._csv_f = None
        self._writer = None
        os.makedirs(outdir, exist_ok=True)
        self._open_csv()

    # ── CSV ────────────────────────────────────────────────────────────────────
    def _csv_path(self) -> str:
        d = date.today().strftime('%Y%m%d')
        return os.path.join(self.outdir, f'current_{d}.csv')

    def _open_csv(self):
        """Apre (o riapre) il file CSV per la data odierna."""
        today = date.today()
        if self._current_date == today:
            return
        if self._csv_f:
            self._csv_f.flush()
            self._csv_f.close()
        path   = self._csv_path()
        is_new = not os.path.exists(path)
        self._csv_f = open(path, 'a', newline='', buffering=1)   # buffering=1: line-buffered
        self._writer = csv.writer(self._csv_f)
        if is_new:
            self._writer.writerow([
                'wall_time', 'elapsed_s', 'current_A', 'peak_sample_A', 'bus_V',
                'charge_uAh', 'n_avg', 'rate_sps',
            ])
        self._current_date = today
        print(f'[csv] logging → {path}')
        self.log.info('CSV opened: %s', path)

    # ── Gestione overflow uint32 micros() ─────────────────────────────────────
    def _unwrap_micros(self, raw: int) -> int:
        """
        Se il firmware manda micros() a 32 bit (standard AVR), gestisce il rollover.
        If values > 2³² are received (extended firmware or ARM 64-bit), use them directly.
        """
        if raw > UINT32_WRAP:
            # Firmware already extends the counter — no unwrap needed
            self._last_raw_us = None
            return raw

        r = raw & 0xFFFFFFFF
        if self._last_raw_us is not None:
            delta = (r - self._last_raw_us) & 0xFFFFFFFF
            if delta > UINT32_WRAP // 2:   # salto in avanti > ~35 min → overflow
                self._us_offset += UINT32_WRAP
        self._last_raw_us = r
        return r + self._us_offset

    # ── Flush buffer → CSV + coda plot ────────────────────────────────────────
    def _flush(self):
        if not self._b_i:
            return

        n      = len(self._b_i)
        t_us   = float(np.mean(self._b_us))
        t_s    = t_us * 1e-6
        i_a    = float(np.mean(self._b_i))
        peak_i = float(np.max(self._b_i))
        v_v    = float(np.mean(self._b_v))

        if self._start_t_s is None:
            self._start_t_s = t_s

        elapsed = t_s - self._start_t_s

        # Integrazione carica (regola rettangolare — intervalli piccoli)
        if self._last_t_s is not None:
            dt = t_s - self._last_t_s
            if 0 < dt < 60:   # ignora gap > 60 s (es. riconnessione)
                self._charge_C += i_a * dt
        self._last_t_s = t_s

        charge_uah = self._charge_C / 3600 * 1e6

        # Stima frequenza campionamento (basata su wall clock, non su micros)
        now = time.monotonic()
        rate_sps = 0.0
        if self._last_flush_wall is not None:
            dt_wall = now - self._last_flush_wall
            if dt_wall > 0:
                rate_sps = n / dt_wall
                self._rate_buf.append(rate_sps)
        self._last_flush_wall = now
        mean_rate = float(np.mean(self._rate_buf)) if self._rate_buf else 0.0

        # Rotazione file CSV a mezzanotte
        self._open_csv()

        self._writer.writerow([
            datetime.now().isoformat(timespec='milliseconds'),
            f'{elapsed:.3f}',
            f'{i_a:.10e}',
            f'{peak_i:.10e}',
            f'{v_v:.5f}',
            f'{charge_uah:.6f}',
            n,
            f'{mean_rate:.1f}',
        ])

        try:
            self.plot_q.put_nowait({
                'elapsed':    elapsed,
                'current':    i_a,
                'peak':       peak_i,
                'bus_v':      v_v,
                'charge_C':   self._charge_C,
                'charge_uah': charge_uah,
                'rate':       mean_rate,
                'n':          n,
            })
        except queue.Full:
            pass

        self.total_avg += 1
        self._b_us.clear()
        self._b_i.clear()
        self._b_v.clear()

    # ── Loop principale thread ─────────────────────────────────────────────────
    def run(self):
        while not self._stop.is_set():
            try:
                line = self.in_q.get(timeout=0.5)
            except queue.Empty:
                continue

            try:
                txt = line.decode('ascii', errors='ignore').strip()
                if not txt or txt.startswith('#'):
                    continue
                parts = txt.split(',')
                if len(parts) != 3:
                    continue
                raw_us, raw_sh, raw_bus = int(parts[0]), int(parts[1]), int(parts[2])
            except (ValueError, IndexError) as exc:
                self.log.debug('Invalid serial line %r: %s', line, exc)
                continue

            us  = self._unwrap_micros(raw_us)
            i_a = float(np.int16(raw_sh)) * SHUNT_LSB_V / self.shunt_ohm - self.offset_a
            v_v = float(np.int16(raw_bus)) * BUS_LSB_V

            self._b_us.append(us)
            self._b_i.append(i_a)
            self._b_v.append(v_v)
            self.total_raw += 1

            if len(self._b_i) >= self.avg_n:
                self._flush()

    def stop(self):
        self._stop.set()
        self._flush()
        if self._csv_f:
            self._csv_f.flush()
            self._csv_f.close()


# ═══════════════════════════════════════════════════════════════════════════════
#  Finestra di monitoraggio in tempo reale
# ═══════════════════════════════════════════════════════════════════════════════
class Monitor:
    # Colori catppuccin-mocha
    BG      = '#1e1e2e'
    FG      = '#cdd6f4'
    ACCENT  = '#89dceb'
    GRID    = '#313244'
    BORDER  = '#45475a'
    TITLE   = '#cba6f7'
    STAT_OK = '#a6e3a1'
    WARN    = '#fab387'

    def __init__(
        self,
        plot_q:    queue.Queue,
        window_s:  int,
        processor: DataProcessor,
        reader:    SerialReader,
        args:      argparse.Namespace,
    ):
        self.plot_q    = plot_q
        self.window_s  = window_s
        self.proc      = processor
        self.reader    = reader
        self.args      = args
        self.capacity_mah = args.capacity   # None se non specificata

        # Buffer dati per il plot (solo finestra visibile + margine)
        _maxpts = max(window_s * 50, 5000)
        self._t: deque[float] = deque(maxlen=_maxpts)
        self._i: deque[float] = deque(maxlen=_maxpts)

        # Statistiche globali di sessione
        self._i_min      =  float('inf')
        self._i_max      = -float('inf')
        self._i_sum      = 0.0
        self._i_cnt      = 0
        self._peak_sample = -float('inf')   # picco singolo campione raw
        self._charge_C   = 0.0
        self._bus_v      = 0.0
        self._rate       = 0.0
        self._last_i     = 0.0

        # Traccia il divisore asse X attuale per aggiornare il formatter solo se cambia
        self._x_div    = 1.0
        self._x_div_prev = None

        self._build_figure()

    # ── Costruzione figura ────────────────────────────────────────────────────
    def _build_figure(self):
        self.fig = plt.figure(figsize=(13, 7), facecolor=self.BG)
        self.fig.canvas.manager.set_window_title('ina226-current-meter')

        gs = self.fig.add_gridspec(
            2, 1, height_ratios=[5, 1],
            top=0.93, bottom=0.10, hspace=0.08)

        self.ax = self.fig.add_subplot(gs[0])
        self.ax_info = self.fig.add_subplot(gs[1])

        # Stile main plot
        self.ax.set_facecolor(self.BG)
        self.ax.tick_params(colors=self.FG, labelsize=9)
        for spine in self.ax.spines.values():
            spine.set_color(self.BORDER)
        self.ax.grid(True, color=self.GRID, linewidth=0.5, alpha=0.8)
        self.ax.set_xlabel('Waiting for data…', color=self.FG, fontsize=9)
        self.ax.set_ylabel('Corrente [µA]', color=self.FG, fontsize=9)

        self.line, = self.ax.plot([], [], color=self.ACCENT, linewidth=0.9,
                                  antialiased=True)
        self.fill  = None   # fill_between aggiornato dinamicamente

        self.fig.suptitle('ina226-current-meter',
                          color=self.TITLE, fontsize=12, fontweight='bold')

        # Pannello statistiche (testo)
        self.ax_info.set_facecolor(self.BG)
        self.ax_info.axis('off')
        self.txt_stats = self.ax_info.text(
            0.5, 0.5, 'Waiting for serial data…',
            transform=self.ax_info.transAxes,
            ha='center', va='center', fontsize=9,
            color=self.FG, family='monospace',
            linespacing=1.6)

    # ── Drain della coda plot ─────────────────────────────────────────────────
    def _drain(self):
        new_points = 0
        while True:
            try:
                d = self.plot_q.get_nowait()
            except queue.Empty:
                break
            self._t.append(d['elapsed'])
            self._i.append(d['current'])
            self._i_min       = min(self._i_min,       d['current'])
            self._i_max       = max(self._i_max,       d['current'])
            self._i_sum      += d['current']
            self._i_cnt      += 1
            self._peak_sample = max(self._peak_sample, d['peak'])
            self._charge_C    = d['charge_C']
            self._bus_v       = d['bus_v']
            self._rate        = d['rate']
            self._last_i      = d['current']
            new_points    += 1
        return new_points

    # ── Aggiornamento frame animazione ────────────────────────────────────────
    def update(self, _frame):
        if self._drain() == 0 and not self._t:
            return

        t_arr = np.asarray(self._t, dtype=np.float64)
        i_arr = np.asarray(self._i, dtype=np.float64)

        if len(t_arr) == 0:
            return

        # Finestra visibile
        t_now = t_arr[-1]
        t_lo  = t_now - self.window_s
        mask  = t_arr >= t_lo
        t_vis = t_arr[mask]
        i_vis = i_arr[mask]

        # ── Auto-scale current unit ────────────────────────────────────────
        peak = float(np.max(np.abs(i_vis))) if len(i_vis) else 1e-12
        mul, unit = auto_unit(peak)

        i_plot = i_vis * mul

        # ── Aggiorna dati line ─────────────────────────────────────────────
        # Asse X: dividiamo per il divisore del tempo
        x_div, xlabel = time_axis_params(t_now)
        x_plot = t_vis / x_div

        self.line.set_data(x_plot, i_plot)

        # Formatter asse X (aggiorna solo se cambia scala)
        if x_div != self._x_div_prev:
            self.ax.xaxis.set_major_formatter(
                mticker.FuncFormatter(lambda v, _: f'{v:.1f}'))
            self.ax.set_xlabel(xlabel, color=self.FG, fontsize=9)
            self._x_div_prev = x_div

        # Limiti X
        self.ax.set_xlim(t_lo / x_div, t_now / x_div)

        # Limiti Y con margine 15%
        if len(i_plot) > 0:
            lo, hi = i_plot.min(), i_plot.max()
            pad = max((hi - lo) * 0.15, abs(hi) * 0.1, 0.01)
            self.ax.set_ylim(lo - pad, hi + pad)
        self.ax.set_ylabel(f'Corrente [{unit}]', color=self.FG, fontsize=9)
        self.ax.tick_params(colors=self.FG)

        # Fill sotto la curva
        if self.fill:
            self.fill.remove()
        self.fill = self.ax.fill_between(
            x_plot, i_plot, alpha=0.15, color=self.ACCENT)

        # ── Pannello statistiche ───────────────────────────────────────────
        conn_str = '● CONNECTED' if self.reader.connected else '○ disconnected'
        mean_i   = self._i_sum / self._i_cnt if self._i_cnt else 0.0
        uptime   = fmt_duration(t_now)
        rate_col = self.STAT_OK if self._rate > 10 else self.WARN

        peak_str = fmt_current(self._peak_sample) \
                   if self._peak_sample > -float('inf') else '---'
        if self.capacity_mah is not None:
            batt_str = fmt_battery_life(self.capacity_mah, mean_i)
            batt_field = f'   Batt. life: {batt_str}'
        else:
            batt_field = ''
        stats = (
            f'Now: {fmt_current(self._last_i):>14s}   '
            f'Mean: {fmt_current(mean_i):>14s}   '
            f'Min: {fmt_current(self._i_min):>14s}   '
            f'Max mean: {fmt_current(self._i_max):>14s}\n'
            f'Peak sample: {peak_str:>14s}   '
            f'Charge: {fmt_charge(self._charge_C):>14s}   '
            f'Bus: {self._bus_v:>7.4f} V   '
            f'Rate: {self._rate:>6.1f} sps   '
            f'Uptime: {uptime}   {conn_str}'
            f'{batt_field}'
        )
        self.txt_stats.set_text(stats)

    # ── Start animation (blocks until the window is closed) ───────────────────
    def run(self, interval_ms: int = 250):
        self._anim = FuncAnimation(
            self.fig, self.update,
            interval=interval_ms,
            blit=False,
            cache_frame_data=False,
        )
        plt.show()


# ═══════════════════════════════════════════════════════════════════════════════
#  Logging su file
# ═══════════════════════════════════════════════════════════════════════════════
def setup_logging(outdir: str) -> logging.Logger:
    os.makedirs(outdir, exist_ok=True)
    log_path = os.path.join(outdir, 'monitor.log')

    logger = logging.getLogger('monitor')
    logger.setLevel(logging.DEBUG)

    # Rotazione giornaliera, conserva 30 giorni
    fh = logging.handlers.TimedRotatingFileHandler(
        log_path, when='midnight', backupCount=30, encoding='utf-8')
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        '%(asctime)s  %(levelname)-8s  %(threadName)s  %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%S'))

    # Console: solo WARNING e superiori (non intralcia l'output normale)
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.WARNING)
    ch.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


# ═══════════════════════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    args = parse_args()

    log = setup_logging(args.outdir)

    # Cattura qualsiasi eccezione non gestita nel thread principale
    def _excepthook(exc_type, exc_value, exc_tb):
        log.critical('Unhandled exception — unexpected exit',
                     exc_info=(exc_type, exc_value, exc_tb))
        sys.__excepthook__(exc_type, exc_value, exc_tb)
    sys.excepthook = _excepthook

    # Cattura eccezioni non gestite negli altri thread
    def _thread_excepthook(args_):
        log.critical('Unhandled exception in thread %s',
                     args_.thread.name if args_.thread else '?',
                     exc_info=(args_.exc_type, args_.exc_value, args_.exc_traceback))
    threading.excepthook = _thread_excepthook

    print('━' * 60)
    print('ina226-current-meter')
    print('━' * 60)
    print(f'  Serial port   : {args.port} @ {args.baud} bps')
    print(f'  Shunt         : {args.shunt} Ω')
    print(f'  Resolution    : {fmt_current(SHUNT_LSB_V / args.shunt)} per LSB')
    print(f'  Offset        : {args.offset:+.2f} µV → {fmt_current(args.offset * 1e-6 / args.shunt)} correction')
    print(f'  Averaging     : {args.avg} raw samples per point')
    print(f'  Plot window   : {args.window} s')
    print(f'  CSV output    : {args.outdir}/')
    print(f'  Error log     : {args.outdir}/monitor.log')
    print('━' * 60)

    log.info('Start — port=%s shunt=%.3fΩ offset=%.2fµV avg=%d window=%ds',
             args.port, args.shunt, args.offset, args.avg, args.window)

    raw_q  = queue.Queue(maxsize=200_000)
    plot_q = queue.Queue(maxsize=20_000)

    reader = SerialReader(args.port, args.baud, raw_q, log)
    proc   = DataProcessor(raw_q, plot_q, args.outdir,
                            shunt_ohm=args.shunt, avg_n=args.avg,
                            offset_uv=args.offset, log=log)

    reader.start()
    proc.start()

    def _shutdown(sig=None, _frame=None):
        signame = signal.Signals(sig).name if sig else 'manual'
        log.info('Shutdown requested (signal: %s)', signame)
        print('\n[main] shutting down…')
        reader.stop()
        proc.stop()
        log.info('Clean shutdown complete')
        plt.close('all')
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        monitor = Monitor(plot_q, window_s=args.window,
                          processor=proc, reader=reader, args=args)
        monitor.run(interval_ms=250)
    except Exception:
        log.critical('Crash nella finestra di monitoraggio', exc_info=True)
        raise
    finally:
        log.info('Programma terminato')


if __name__ == '__main__':
    main()
