#!/usr/bin/env python3
"""
UMD2 GUI (enhanced + manual capture)
- Capture controls moved to Source tab with explicit Start/Stop Capture.
- GUI writes CSV directly while streaming (no need to restart backend).
"""

import sys, os, json, subprocess, signal, time, threading, csv
from pathlib import Path
from typing import List, Optional
from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

APP_NAME = "UMD2 Viewer+"
ROLLING_SECONDS = 30.0

DEFAULTS = dict(
    baud=921600,
    stepnm=None,       # if None -> lambda/scale-div
    lambda_nm=632.991,
    scale_div=8,
    startnm=0.0,
    straight_mult=1.0,
    mode="displacement",
    angle_norm_nm=1.0,
    angle_corr=1.0,
    ema_alpha=0.0,
    ma_window=0,
    env_temp=None, env_temp0=None, env_ktemp=0.0,
    env_press=None, env_press0=None, env_kpress=0.0,
    env_hum=None, env_hum0=None, env_khum=0.0,
    fs=0.0,
    emit="every",
    decimate=1,
    fft_len=0,
    fft_every=0,
    fft_signal="x",
    enable_xy=False,
)

CSV_HEADER = ["seq","fs_hz","D","deltaD","step_nm","x_nm","v_nm_s",
              "x_nm_ema","x_nm_ma","x_nm_env","angle_deg","x2","y2"]

class BackendThread(QtCore.QObject):
    line_received = QtCore.Signal(dict)
    started = QtCore.Signal()
    stopped = QtCore.Signal(str)
    error = QtCore.Signal(str)

    def __init__(self, umd2_path: str, args: List[str], parent=None):
        super().__init__(parent)
        self.umd2_path = umd2_path
        self.args = args
        self._proc = None
        self._stop = False
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True
        if self._proc and self._proc.poll() is None:
            try:
                if os.name == "nt":
                    self._proc.terminate()
                else:
                    self._proc.send_signal(signal.SIGINT)
                    time.sleep(0.2)
                    self._proc.terminate()
            except Exception:
                pass

    def _run(self):
        try:
            cmd = [sys.executable, self.umd2_path] + self.args
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, universal_newlines=True
            )
        except Exception as e:
            self.error.emit(f"Failed to start backend: {e}")
            self.stopped.emit("spawn-failed")
            return

        self.started.emit()
        try:
            for line in self._proc.stdout:
                if self._stop:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self.line_received.emit(rec)
        except Exception as e:
            self.error.emit(f"Streaming error: {e}")
        finally:
            try:
                self._proc.terminate()
            except:
                pass
            self.stopped.emit("exited")

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        pg.setConfigOptions(antialias=True)
        self._apply_dark_palette()

        central = QtWidgets.QWidget(self)
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)

        # Tabs
        ctrl = QtWidgets.QTabWidget()
        layout.addWidget(ctrl)

        # ===== Source tab =====
        self.tab_source = QtWidgets.QWidget(); ctrl.addTab(self.tab_source, "Source")
        sgrid = QtWidgets.QGridLayout(self.tab_source)

        self.mode_combo = QtWidgets.QComboBox(); self.mode_combo.addItems(["Serial","File"])
        self.port_combo = QtWidgets.QComboBox()
        self.refresh_btn = QtWidgets.QPushButton("Refresh Ports")
        self.refresh_btn.clicked.connect(self._populate_ports)

        self.file_edit = QtWidgets.QLineEdit()
        self.browse_btn = QtWidgets.QPushButton("Browse…")
        self.browse_btn.clicked.connect(self._browse_file)

        self.baud_spin = QtWidgets.QSpinBox(); self.baud_spin.setRange(9600,3000000); self.baud_spin.setValue(DEFAULTS["baud"])
        self.fs_spin = QtWidgets.QDoubleSpinBox(); self.fs_spin.setRange(0,1e9); self.fs_spin.setDecimals(2); self.fs_spin.setValue(DEFAULTS["fs"])

        # Capture controls (moved here)
        self.capture_path = QtWidgets.QLineEdit()
        self.capture_path.setPlaceholderText("path/to/capture.csv")
        self.capture_browse = QtWidgets.QPushButton("…")
        self.capture_browse.clicked.connect(self._browse_capture)
        self.capture_btn = QtWidgets.QPushButton("Start Capture")
        self.capture_btn.setCheckable(True)
        self.capture_btn.clicked.connect(self._toggle_capture)

        r=0
        sgrid.addWidget(QtWidgets.QLabel("Mode"), r,0); sgrid.addWidget(self.mode_combo, r,1)
        r+=1; sgrid.addWidget(QtWidgets.QLabel("Port"), r,0); sgrid.addWidget(self.port_combo, r,1); sgrid.addWidget(self.refresh_btn, r,2)
        r+=1; sgrid.addWidget(QtWidgets.QLabel("File"), r,0); sgrid.addWidget(self.file_edit, r,1); sgrid.addWidget(self.browse_btn, r,2)
        r+=1; sgrid.addWidget(QtWidgets.QLabel("Baud"), r,0); sgrid.addWidget(self.baud_spin, r,1)
        r+=1; sgrid.addWidget(QtWidgets.QLabel("fs (Hz) (0=auto)"), r,0); sgrid.addWidget(self.fs_spin, r,1)
        r+=1; sgrid.addWidget(QtWidgets.QLabel("Capture CSV"), r,0); sgrid.addWidget(self.capture_path, r,1); sgrid.addWidget(self.capture_browse, r,2)
        r+=1; sgrid.addWidget(self.capture_btn, r,1)

        # ===== Scaling tab =====
        self.tab_scale = QtWidgets.QWidget(); ctrl.addTab(self.tab_scale, "Scaling")
        cgrid = QtWidgets.QGridLayout(self.tab_scale)
        self.stepnm_edit = QtWidgets.QLineEdit(); self.stepnm_edit.setPlaceholderText("(optional override)")
        self.lambda_spin = QtWidgets.QDoubleSpinBox(); self.lambda_spin.setRange(1,1e6); self.lambda_spin.setValue(DEFAULTS["lambda_nm"]); self.lambda_spin.setDecimals(3)
        self.scale_div_combo = QtWidgets.QComboBox(); self.scale_div_combo.addItems(["1","2","4","8"]); self.scale_div_combo.setCurrentText(str(DEFAULTS["scale_div"]))
        self.startnm_spin = QtWidgets.QDoubleSpinBox(); self.startnm_spin.setRange(-1e15,1e15); self.startnm_spin.setDecimals(6); self.startnm_spin.setValue(DEFAULTS["startnm"])
        self.straight_spin = QtWidgets.QDoubleSpinBox(); self.straight_spin.setRange(0,1e6); self.straight_spin.setDecimals(6); self.straight_spin.setValue(DEFAULTS["straight_mult"])

        r=0
        cgrid.addWidget(QtWidgets.QLabel("stepnm override (nm/count)"), r,0); cgrid.addWidget(self.stepnm_edit, r,1)
        r+=1; cgrid.addWidget(QtWidgets.QLabel("lambda (nm)"), r,0); cgrid.addWidget(self.lambda_spin, r,1)
        r+=1; cgrid.addWidget(QtWidgets.QLabel("scale divisor"), r,0); cgrid.addWidget(self.scale_div_combo, r,1)
        r+=1; cgrid.addWidget(QtWidgets.QLabel("startnm (baseline)"), r,0); cgrid.addWidget(self.startnm_spin, r,1)
        r+=1; cgrid.addWidget(QtWidgets.QLabel("straightness multiplier"), r,0); cgrid.addWidget(self.straight_spin, r,1)

        # ===== Mode tab =====
        self.tab_mode = QtWidgets.QWidget(); ctrl.addTab(self.tab_mode, "Mode")
        mgrid = QtWidgets.QGridLayout(self.tab_mode)
        self.mode_calc_combo = QtWidgets.QComboBox(); self.mode_calc_combo.addItems(["displacement","angle"])
        self.angle_norm = QtWidgets.QDoubleSpinBox(); self.angle_norm.setRange(1e-9,1e12); self.angle_norm.setDecimals(6); self.angle_norm.setValue(DEFAULTS["angle_norm_nm"])
        self.angle_corr = QtWidgets.QDoubleSpinBox(); self.angle_corr.setRange(0,1e6); self.angle_corr.setDecimals(6); self.angle_corr.setValue(DEFAULTS["angle_corr"])

        r=0
        mgrid.addWidget(QtWidgets.QLabel("Compute"), r,0); mgrid.addWidget(self.mode_calc_combo, r,1)
        r+=1; mgrid.addWidget(QtWidgets.QLabel("angle_norm_nm"), r,0); mgrid.addWidget(self.angle_norm, r,1)
        r+=1; mgrid.addWidget(QtWidgets.QLabel("angle_corr"), r,0); mgrid.addWidget(self.angle_corr, r,1)

        # ===== Smooth & Env tab =====
        self.tab_filt = QtWidgets.QWidget(); ctrl.addTab(self.tab_filt, "Smooth & Env")
        fgrid = QtWidgets.QGridLayout(self.tab_filt)
        self.ema_alpha = QtWidgets.QDoubleSpinBox(); self.ema_alpha.setRange(0,1); self.ema_alpha.setSingleStep(0.05); self.ema_alpha.setValue(DEFAULTS["ema_alpha"])
        self.ma_window = QtWidgets.QSpinBox(); self.ma_window.setRange(0,100000); self.ma_window.setValue(DEFAULTS["ma_window"])

        self.env_temp = QtWidgets.QLineEdit(); self.env_temp.setPlaceholderText("temp C (optional)")
        self.env_temp0 = QtWidgets.QLineEdit(); self.env_temp0.setPlaceholderText("ref temp C")
        self.env_ktemp = QtWidgets.QLineEdit(); self.env_ktemp.setPlaceholderText("ktemp per C")

        self.env_press = QtWidgets.QLineEdit(); self.env_press.setPlaceholderText("press (opt)")
        self.env_press0 = QtWidgets.QLineEdit(); self.env_press0.setPlaceholderText("ref press")
        self.env_kpress = QtWidgets.QLineEdit(); self.env_kpress.setPlaceholderText("kpress")

        self.env_hum = QtWidgets.QLineEdit(); self.env_hum.setPlaceholderText("RH% (opt)")
        self.env_hum0 = QtWidgets.QLineEdit(); self.env_hum0.setPlaceholderText("ref RH%")
        self.env_khum = QtWidgets.QLineEdit(); self.env_khum.setPlaceholderText("khum")

        r=0
        fgrid.addWidget(QtWidgets.QLabel("EMA alpha"), r,0); fgrid.addWidget(self.ema_alpha, r,1)
        r+=1; fgrid.addWidget(QtWidgets.QLabel("MA window"), r,0); fgrid.addWidget(self.ma_window, r,1)
        r+=1; fgrid.addWidget(QtWidgets.QLabel("Temp/Press/Hum (opt)"), r,0)
        r+=1; fgrid.addWidget(self.env_temp, r,0); fgrid.addWidget(self.env_temp0, r,1); fgrid.addWidget(self.env_ktemp, r,2)
        r+=1; fgrid.addWidget(self.env_press, r,0); fgrid.addWidget(self.env_press0, r,1); fgrid.addWidget(self.env_kpress, r,2)
        r+=1; fgrid.addWidget(self.env_hum, r,0); fgrid.addWidget(self.env_hum0, r,1); fgrid.addWidget(self.env_khum, r,2)

        # ===== FFT & Extras tab =====
        self.tab_extra = QtWidgets.QWidget(); ctrl.addTab(self.tab_extra, "FFT & Extras")
        egrid = QtWidgets.QGridLayout(self.tab_extra)
        self.emit_combo = QtWidgets.QComboBox(); self.emit_combo.addItems(["every","onstep"])
        self.decimate_spin = QtWidgets.QSpinBox(); self.decimate_spin.setRange(1,1000); self.decimate_spin.setValue(DEFAULTS["decimate"])
        self.fft_len = QtWidgets.QSpinBox(); self.fft_len.setRange(0, 1_048_576); self.fft_len.setValue(DEFAULTS["fft_len"])
        self.fft_every = QtWidgets.QSpinBox(); self.fft_every.setRange(0, 1_000_000); self.fft_every.setValue(DEFAULTS["fft_every"])
        self.fft_signal = QtWidgets.QComboBox(); self.fft_signal.addItems(["x","v"])
        self.enable_xy = QtWidgets.QCheckBox("Parse X/Y second channel")

        r=0
        egrid.addWidget(QtWidgets.QLabel("emit"), r,0); egrid.addWidget(self.emit_combo, r,1)
        r+=1; egrid.addWidget(QtWidgets.QLabel("decimate"), r,0); egrid.addWidget(self.decimate_spin, r,1)
        r+=1; egrid.addWidget(QtWidgets.QLabel("fft_len"), r,0); egrid.addWidget(self.fft_len, r,1)
        r+=1; egrid.addWidget(QtWidgets.QLabel("fft_every"), r,0); egrid.addWidget(self.fft_every, r,1)
        r+=1; egrid.addWidget(QtWidgets.QLabel("fft_signal"), r,0); egrid.addWidget(self.fft_signal, r,1)
        r+=1; egrid.addWidget(self.enable_xy, r,0,1,2)

        # Start/Stop stream
        btns = QtWidgets.QHBoxLayout()
        layout.addLayout(btns)
        self.start_btn = QtWidgets.QPushButton("Start Stream")
        self.stop_btn = QtWidgets.QPushButton("Stop Stream"); self.stop_btn.setEnabled(False)
        btns.addWidget(self.start_btn); btns.addWidget(self.stop_btn)

        # Plots
        plots = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        layout.addWidget(plots, 1)
        self.plot_x = pg.PlotWidget(title="Displacement x_nm (rolling)")
        self.plot_v = pg.PlotWidget(title="Velocity v_nm_s (rolling)")
        for pw in (self.plot_x, self.plot_v):
            pw.showGrid(x=True, y=True, alpha=0.3)
        self.curve_x = self.plot_x.plot([], [], pen=pg.mkPen(width=2))
        self.curve_v = self.plot_v.plot([], [], pen=pg.mkPen(width=2))
        plots.addWidget(self.plot_x); plots.addWidget(self.plot_v)

        # Optional FFT plot (auto-shown when first FFT arrives)
        self.plot_fft = pg.PlotWidget(title="FFT (magnitude)")
        self.curve_fft = self.plot_fft.plot([], [], pen=pg.mkPen(width=2))
        layout.addWidget(self.plot_fft); self.plot_fft.setVisible(False)

        # Buffers
        self.t0 = None; self.ts = []; self.xs = []; self.vs = []
        self.worker: Optional[BackendThread] = None

        # Capture state
        self.capture_active = False
        self.capture_file = None
        self.capture_writer: Optional[csv.writer] = None
        self.capture_wrote_header = False

        # Wire
        self.start_btn.clicked.connect(self._start)
        self.stop_btn.clicked.connect(self._stop)
        self.mode_combo.currentIndexChanged.connect(self._sync_source_enable)
        self._populate_ports(); self._sync_source_enable()

        self.status = QtWidgets.QStatusBar(); self.setStatusBar(self.status)
        self.trim_timer = QtCore.QTimer(self); self.trim_timer.timeout.connect(self._trim); self.trim_timer.start(500)

    # ---------- UI helpers ----------
    def _apply_dark_palette(self):
        app = QtWidgets.QApplication.instance()
        app.setStyle("Fusion")
        pal = QtGui.QPalette()
        pal.setColor(QtGui.QPalette.Window, QtGui.QColor(30,30,30))
        pal.setColor(QtGui.QPalette.WindowText, QtCore.Qt.white)
        pal.setColor(QtGui.QPalette.Base, QtGui.QColor(24,24,24))
        pal.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor(36,36,36))
        pal.setColor(QtGui.QPalette.Text, QtCore.Qt.white)
        pal.setColor(QtGui.QPalette.Button, QtGui.QColor(45,45,45))
        pal.setColor(QtGui.QPalette.ButtonText, QtCore.Qt.white)
        pal.setColor(QtGui.QPalette.Highlight, QtGui.QColor(64,128,255))
        pal.setColor(QtGui.QPalette.HighlightedText, QtCore.Qt.black)
        app.setPalette(pal)

    def _populate_ports(self):
        try:
            import serial.tools.list_ports as lp
            ports = [p.device for p in lp.comports()]
        except Exception:
            ports = []
        self.port_combo.clear()
        if ports:
            self.port_combo.addItems(ports)
        else:
            self.port_combo.addItem("<no ports>")

    def _sync_source_enable(self):
        serial = (self.mode_combo.currentText() == "Serial")
        self.port_combo.setEnabled(serial); self.refresh_btn.setEnabled(serial)
        self.baud_spin.setEnabled(serial)
        self.file_edit.setEnabled(not serial); self.browse_btn.setEnabled(not serial)

    def _browse_file(self):
        p, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Choose input file", "", "All files (*)")
        if p:
            self.file_edit.setText(p)

    def _browse_capture(self):
        p, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Choose CSV to capture", "", "CSV files (*.csv);;All files (*)")
        if p:
            self.capture_path.setText(p)

    # ---------- Backend control ----------
    def _build_args(self):
        args: List[str] = []
        # source
        if self.mode_combo.currentText() == "Serial":
            port = self.port_combo.currentText()
            if port == "<no ports>":
                raise RuntimeError("No serial ports found.")
            args += ["--serial", port, "--baud", str(self.baud_spin.value())]
        else:
            p = self.file_edit.text().strip()
            if not p:
                raise RuntimeError("Select an input file or switch to Serial.")
            args += ["--file", p]

        # core (fs/emit/decimate)
        if self.fs_spin.value() > 0:
            args += ["--fs", str(self.fs_spin.value())]
        args += ["--emit", self.emit_combo.currentText(), "--decimate", str(self.decimate_spin.value())]

        # scaling
        stepov = self.stepnm_edit.text().strip()
        if stepov:
            args += ["--stepnm", stepov]
        args += ["--lambda-nm", str(self.lambda_spin.value()), "--scale-div", self.scale_div_combo.currentText()]
        args += ["--startnm", str(self.startnm_spin.value()), "--straight-mult", str(self.straight_spin.value())]

        # mode
        args += ["--mode", self.mode_calc_combo.currentText(),
                 "--angle-norm-nm", str(self.angle_norm.value()),
                 "--angle-corr", str(self.angle_corr.value())]

        # smoothing/env
        args += ["--ema-alpha", str(self.ema_alpha.value()), "--ma-window", str(self.ma_window.value())]
        def add_env(flag, widget):
            txt = widget.text().strip()
            if txt:
                args += [flag, txt]
        add_env("--env-temp", self.env_temp); add_env("--env-temp0", self.env_temp0); add_env("--env-ktemp", self.env_ktemp)
        add_env("--env-press", self.env_press); add_env("--env-press0", self.env_press0); add_env("--env-kpress", self.env_kpress)
        add_env("--env-hum", self.env_hum); add_env("--env-hum0", self.env_hum0); add_env("--env-khum", self.env_khum)

        # fft/extras
        args += ["--fft-len", str(self.fft_len.value()), "--fft-every", str(self.fft_every.value()), "--fft-signal", self.fft_signal.currentText()]
        if self.enable_xy.isChecked():
            args += ["--enable-xy"]

        # Always output JSONL to the GUI
        args += ["--out", "jsonl"]
        return args

    def _start(self):
        try:
            umd2_path = str(Path(__file__).with_name("umd2.py"))
            if not os.path.exists(umd2_path):
                raise RuntimeError("umd2.py not found next to GUI script.")
            args = self._build_args()
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Config error", str(e)); return

        self.ts.clear(); self.xs.clear(); self.vs.clear(); self.t0 = None
        self.curve_x.setData([], []); self.curve_v.setData([], [])

        self.worker = BackendThread(umd2_path, args)
        self.worker.line_received.connect(self._on_line)
        self.worker.started.connect(lambda: self._set_running(True))
        self.worker.stopped.connect(lambda reason: self._on_stopped(reason))
        self.worker.error.connect(lambda msg: self.status.showMessage(msg, 5000))
        self.worker.start()

    def _stop(self):
        if self.worker:
            self.worker.stop()
        self._set_running(False)
        # auto-stop capture if active
        if self.capture_active:
            self._toggle_capture(force_off=True)
        self.status.showMessage("Stopped.", 2000)

    def _set_running(self, running: bool):
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        for w in (self.mode_combo, self.port_combo, self.refresh_btn, self.file_edit, self.browse_btn,
                  self.stepnm_edit, self.lambda_spin, self.scale_div_combo, self.startnm_spin, self.straight_spin,
                  self.mode_calc_combo, self.angle_norm, self.angle_corr,
                  self.ema_alpha, self.ma_window,
                  self.env_temp, self.env_temp0, self.env_ktemp, self.env_press, self.env_press0, self.env_kpress,
                  self.env_hum, self.env_hum0, self.env_khum,
                  self.fs_spin, self.emit_combo, self.decimate_spin, self.fft_len, self.fft_every, self.fft_signal,
                  self.enable_xy):
            w.setEnabled(not running)

    # ---------- Capture control ----------
    def _toggle_capture(self, force_off: bool=False):
        if force_off or (self.capture_active and self.capture_btn.isChecked() is False):
            # stop capture
            self.capture_active = False
            self.capture_btn.setChecked(False)
            self.capture_btn.setText("Start Capture")
            try:
                if self.capture_file:
                    self.capture_file.flush(); self.capture_file.close()
            finally:
                self.capture_file = None
                self.capture_writer = None
            self.status.showMessage("Capture stopped.", 2000)
            return

        # start capture
        path = self.capture_path.text().strip()
        if not path:
            QtWidgets.QMessageBox.information(self, "Capture", "Choose a CSV file path first.")
            self.capture_btn.setChecked(False)
            return
        try:
            new_file = not os.path.exists(path)
            self.capture_file = open(path, "a", newline="")
            self.capture_writer = csv.writer(self.capture_file)
            if new_file:
                self.capture_writer.writerow(CSV_HEADER)
            self.capture_wrote_header = True
            self.capture_active = True
            self.capture_btn.setText("Stop Capture")
            self.status.showMessage(f"Capturing to {path}", 2000)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Capture", f"Failed to open file:\n{e}")
            self.capture_btn.setChecked(False)
            self.capture_file = None
            self.capture_writer = None
            self.capture_active = False

    # ---------- Stream handling ----------
    @QtCore.Slot(dict)
    def _on_line(self, rec: dict):
        if rec.get("type") == "fft":
            # show FFT snapshot
            self.plot_fft.setVisible(True)
            freq = rec.get("freq") or []
            mag = rec.get("mag") or []
            self.curve_fft.setData(freq, mag)
            return

        # live plots
        t = time.time()
        if self.t0 is None:
            self.t0 = t
        relt = t - self.t0

        x = float(rec.get("x_nm", 0.0))
        v = float(rec.get("v_nm_s", 0.0))
        self.ts.append(relt); self.xs.append(x); self.vs.append(v)
        self._trim()
        self.curve_x.setData(self.ts, self.xs)
        self.curve_v.setData(self.ts, self.vs)

        # capture to CSV (only data rows)
        if self.capture_active and self.capture_writer:
            row = [ rec.get("seq"), rec.get("fs_hz"), rec.get("D"), rec.get("deltaD"),
                    rec.get("step_nm"), rec.get("x_nm"), rec.get("v_nm_s"),
                    rec.get("x_nm_ema"), rec.get("x_nm_ma"), rec.get("x_nm_env"),
                    rec.get("angle_deg"), rec.get("x2"), rec.get("y2") ]
            try:
                self.capture_writer.writerow(row)
            except Exception as e:
                self.status.showMessage(f"Capture write error: {e}", 4000)

        # status line
        details = f"seq={rec.get('seq')} D={rec.get('D')} dD={rec.get('deltaD')} x={x:.3f}nm v={v:.3f}nm/s"
        ang = rec.get("angle_deg")
        if ang is not None:
            details += f" angle={ang:.4f}deg"
        self.status.showMessage(details, 800)

    def _trim(self):
        if not self.ts:
            return
        cutoff = self.ts[-1] - ROLLING_SECONDS
        i = 0
        while i < len(self.ts) and self.ts[i] < cutoff:
            i += 1
        if i > 0:
            self.ts = self.ts[i:]; self.xs = self.xs[i:]; self.vs = self.vs[i:]

    def _on_stopped(self, reason: str):
        self._set_running(False)
        # also stop capture if it was active
        if self.capture_active:
            self._toggle_capture(force_off=True)
        self.status.showMessage(f"Backend stopped: {reason}", 3000)

    def closeEvent(self, event):
        if self.capture_active:
            self._toggle_capture(force_off=True)
        super().closeEvent(event)

def main():
    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow(); w.resize(1200, 800); w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
