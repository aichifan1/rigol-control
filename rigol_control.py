#!/usr/bin/env python3
"""
RIGOL 仪器控制面板
==================
通过 USB (USBTMC / IVI) 连接 RIGOL DM3068（电流表）和 DG1062（信号发生器），
实时显示电流读数，并在电流低于设定阈值时自动关闭信号发生器输出。

技术栈: Python 3.13 + tkinter + pyvisa + pyvisa-py
"""

import os
import queue
import sys
import threading
import time
import warnings
from dataclasses import dataclass
from datetime import datetime
from tkinter import messagebox

# 抑制 pyvisa-py 的 TCPIP/hislip 发现警告（我们只用 USB TMC）
warnings.filterwarnings("ignore", message=".*TCPIP.*resource discovery.*")
warnings.filterwarnings("ignore", message=".*hislip.*zeroconf.*")

# ── GUI 库 ──────────────────────────────────────────────────
import tkinter as tk
from tkinter import ttk

# ── VISA 仪器通信 ───────────────────────────────────────────
import pyvisa

# ── 绘图 ────────────────────────────────────────────────────
import matplotlib
matplotlib.use("TkAgg")  # 必须在 import pyplot 之前设置后端
import matplotlib.pyplot as plt
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
# 配置中文字体（Windows 下用 Microsoft YaHei）
plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


# ══════════════════════════════════════════════════════════════
# 数据类定义
# ══════════════════════════════════════════════════════════════

@dataclass
class DeviceInfo:
    """解析后的 *IDN? 响应"""
    manufacturer: str = ""
    model: str = ""
    serial_number: str = ""
    firmware: str = ""
    raw: str = ""

    @classmethod
    def from_idn(cls, raw: str) -> "DeviceInfo":
        parts = raw.split(",")
        return cls(
            manufacturer=parts[0].strip() if len(parts) > 0 else "",
            model=parts[1].strip() if len(parts) > 1 else "",
            serial_number=parts[2].strip() if len(parts) > 2 else "",
            firmware=parts[3].strip() if len(parts) > 3 else "",
            raw=raw.strip(),
        )


@dataclass
class AppSettings:
    """应用运行时设置"""
    dm3068_resource: str = ""
    dg1062_resource: str = ""
    poll_interval_ms: int = 250  # 默认 250ms（4 Hz），AC 电流测量受限于 RMS 计算时间
    threshold_ma: float = 1.000  # 默认阈值 1.000 mA
    protection_enabled: bool = False


# ══════════════════════════════════════════════════════════════
# 设备类
# ══════════════════════════════════════════════════════════════

class VisaDevice:
    """VISA (USBTMC) 仪器基类，封装 PyVISA 的 SCPI 通信"""

    def __init__(self, name: str, timeout: float = 3000):
        """
        Parameters
        ----------
        name : str
            设备显示名称（如 "DM3068"）
        timeout : float
            通信超时时间（毫秒）
        """
        self.name = name
        self.timeout = timeout
        self.instr: pyvisa.resources.MessageBasedResource | None = None
        self._lock = threading.Lock()
        self.info: DeviceInfo | None = None

    def connect(self, resource_name: str) -> str:
        """打开 VISA 资源并返回 *IDN? 响应。失败时抛出异常。

        Parameters
        ----------
        resource_name : str
            VISA 资源字符串，如 "USB0::0x1AB1::0x0588::DM3RXXXXXXXXX::INSTR"
        """
        self.disconnect()

        # 优先使用系统默认后端（含 IVI/NI-VISA），失败则回退到纯 Python 后端
        try:
            rm = pyvisa.ResourceManager()
        except Exception:
            rm = pyvisa.ResourceManager("@py")
        self.instr = rm.open_resource(resource_name)
        self.instr.timeout = self.timeout

        # 等待设备稳定
        time.sleep(0.15)
        idn = self.instr.query("*IDN?").strip()
        self.info = DeviceInfo.from_idn(idn)
        return idn

    def disconnect(self):
        """断开 VISA 连接（幂等操作）"""
        with self._lock:
            if self.instr is not None:
                try:
                    self.instr.close()
                except Exception:
                    pass
                self.instr = None
                self.info = None

    def is_connected(self) -> bool:
        """检查设备是否已连接"""
        if self.instr is None:
            return False
        try:
            # 尝试获取 session 以验证连接仍有效
            _ = self.instr.session
            return True
        except Exception:
            return False

    def query(self, cmd: str) -> str:
        """发送 SCPI 查询命令，返回响应字符串。线程安全。"""
        with self._lock:
            if not self.is_connected():
                raise ConnectionError(f"{self.name} 未连接")
            response = self.instr.query(cmd).strip()
            return response

    def write(self, cmd: str):
        """发送 SCPI 命令（不等待响应）。线程安全。"""
        with self._lock:
            if not self.is_connected():
                raise ConnectionError(f"{self.name} 未连接")
            self.instr.write(cmd)


class DM3068(VisaDevice):
    """RIGOL DM3068 数字万用表 — 用作 AC 电流表

    AC 电流测量说明:
    - AC 测量必须采集多个电源周期做 RMS 计算，单次至少 100-400ms
    - 理论最快约 2-5 次/秒，无法像 DC 测量那样跑到 100Hz
    - 可通过 :SENSe:CURRent:AC:NPLC 调速度，但会影响精度
    """

    def __init__(self):
        super().__init__(name="DM3068", timeout=10000)  # AC 测量需要较长超时
        self._below_count = 0  # 连续低于阈值计数

    def configure_ac_current_fast(self):
        """连接后尝试加速 AC 电流测量（安全模式：失败不影响正常使用）"""
        try:
            # 清除可能残留的错误状态
            self.write("*CLS")
            # 尝试设置 NPLC = 0.02（最快 AC 测量速度）
            # DM3068 NPLC: 0.02 / 0.2 / 1 / 10 / 100
            self.write(":SENSe:CURRent:AC:NPLC 0.02")
        except Exception:
            pass  # 静默失败，不影响后续 :MEASure? 的正常使用

    def measure_ac_current(self) -> float:
        """读取 AC 电流值，返回安培 (A)。使用 :MEASure:...? 确保每次都完整配置。"""
        resp = self.query(":MEASure:CURRent:AC?")
        return float(resp)


class DG1062(VisaDevice):
    """RIGOL DG1062 函数/任意波形发生器"""

    def __init__(self):
        super().__init__(name="DG1062", timeout=3000)

    def set_output(self, on: bool):
        """开启或关闭输出"""
        cmd = ":OUTPut ON" if on else ":OUTPut OFF"
        self.write(cmd)

    def get_output_state(self) -> bool | None:
        """查询输出状态"""
        try:
            resp = self.query(":OUTPut?")
            return resp.strip() in ("1", "ON", "on")
        except Exception:
            return None


# ══════════════════════════════════════════════════════════════
# VISA 资源扫描工具
# ══════════════════════════════════════════════════════════════

def scan_usb_instruments() -> list[str]:
    """扫描所有 USB TMC 仪器，返回 VISA 资源字符串列表。

    使用 pyvisa-py 纯 Python 后端，无需安装 NI-VISA。
    返回的资源字符串格式如：
        "USB0::0x1AB1::0x0588::DM3RXXXXXXXXX::INSTR"
    """
    try:
        # 优先使用系统默认后端（含 IVI/NI-VISA），失败则回退到纯 Python 后端
        try:
            rm = pyvisa.ResourceManager()
        except Exception:
            rm = pyvisa.ResourceManager("@py")
        # 只列出 USB TMC 仪器（资源字符串以 USB 开头，如 USB0::VID::PID::SN::INSTR）
        resources = rm.list_resources("USB?*::INSTR")
        rm.close()
        return sorted(resources)
    except Exception:
        return []


def parse_resource_display(resource_str: str) -> str:
    """将 VISA 资源字符串解析为友好的显示格式。

    "USB0::0x1AB1::0x0588::DM3RXXXXXXXXX::INSTR"
    → "DM3RXXXXXXXXX [RIGOL, VID:0x1AB1, PID:0x0588]"
    """
    try:
        parts = resource_str.split("::")
        if len(parts) >= 5:
            vid = parts[1]  # 0x1AB1
            pid = parts[2]  # 0x0588
            sn = parts[3]   # serial number
            # 根据 VID 猜测厂商
            vendor_map = {
                "0x1AB1": "RIGOL",
                "0x0699": "Tektronix",
                "0x0957": "Keysight/Agilent",
                "0x2A8D": "Siglent",
                "0x1313": "Thorlabs",
                "0x0AAD": "Rohde&Schwarz",
            }
            vendor = vendor_map.get(vid, vid)  # vid 已是 "0x1AB1" 格式，直接匹配
            return f"{sn} [{vendor}]"
    except Exception:
        pass
    return resource_str


# ══════════════════════════════════════════════════════════════
# 主应用 GUI
# ══════════════════════════════════════════════════════════════

class RigolControlApp:
    """RIGOL 仪器控制面板主应用"""

    def __init__(self):
        # ── 设备实例 ────────────────────────────────────
        self.dm3068 = DM3068()
        self.dg1062 = DG1062()

        # ── 运行时状态 ───────────────────────────────────
        self.settings = AppSettings()
        self._poll_thread: threading.Thread | None = None
        self._poll_stop_event = threading.Event()
        self._polling_active = False
        self._protection_tripped = False
        self._reading_count = 0

        # 后台线程 → GUI 线程的消息队列（tkinter 非线程安全，经队列转发）
        self._gui_queue: queue.Queue = queue.Queue()
        self._poll_generation = 0   # 监控代数，用于丢弃上一次监控的过期消息
        self._closing = False       # 窗口关闭标志，防止销毁后再调度

        # ── 数据记录状态 ─────────────────────────────
        self._logging_active = False
        self._log_file_path: str = ""
        self._log_interval_ms: int = 1000  # 默认 1 秒记录一次
        self._log_file_handle = None  # 文件句柄，用于实时写入
        self._log_start_time: float = 0.0  # 记录开始时刻（time.time()）
        self._log_entry_count: int = 0

        # ── 绘图数据 ─────────────────────────────────
        self._plot_times: list[float] = []    # 相对时间 (s)
        self._plot_currents: list[float] = []  # 电流 (mA)
        self._last_plot_update_time: float = 0.0  # 上次绘图刷新时刻

        # ── 输出状态缓存（避免每次读数都查询 DG1062）──
        self._cached_output_state: bool | None = None  # 缓存的输出状态
        self._last_output_query_time: float = 0.0       # 上次查询输出状态时刻

        # ── 数据记录节流 ─────────────────────────
        self._last_log_write_time: float = 0.0  # 上次写入记录文件的时刻

        # ── 主窗口 ──────────────────────────────────────
        self.root = tk.Tk()
        self.root.title("RIGOL 仪器控制面板")
        self.root.geometry("680x1050")
        self.root.minsize(620, 900)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # ── 构建界面 ─────────────────────────────────────
        self._setup_style()
        self._build_connection_panel()
        self._build_reading_panel()
        self._build_protection_panel()
        self._build_logging_panel()
        self._build_plot_panel()
        self._build_log_panel()
        self._build_status_bar()

        # ── 初始设备扫描 ───────────────────────────
        self.root.after(100, self._refresh_devices)

        # 启动 GUI 消息队列处理器（后台线程消息 → 主线程界面更新）
        self.root.after(50, self._process_gui_queue)

        # ── 启动日志 ─────────────────────────────────────
        self._log_message("应用启动 — 使用 USBTMC (PyVISA) 通信")

    # ══════════════════════════════════════════════════════
    # 样式
    # ══════════════════════════════════════════════════════

    def _setup_style(self):
        """配置 ttk 样式"""
        style = ttk.Style()
        style.theme_use("clam")

        # 大字体样式
        self.FONT_LARGE = ("Consolas", 40, "bold")
        self.FONT_NORMAL = ("Microsoft YaHei UI", 10)
        self.FONT_LOG = ("Consolas", 9)
        self.FONT_STATUS = ("Microsoft YaHei UI", 9)

        self.root.option_add("*Font", self.FONT_NORMAL)

        # 颜色
        self.COLOR_CONNECTED = "#228B22"
        self.COLOR_DISCONNECTED = "#B22222"
        self.COLOR_NORMAL_BG = "#f0fff0"
        self.COLOR_TRIPPED_BG = "#fff0f0"
        self.COLOR_OUTPUT_ON = "#228B22"
        self.COLOR_OUTPUT_OFF = "#888888"

    # ══════════════════════════════════════════════════════
    # 面板1: 设备连接
    # ══════════════════════════════════════════════════════

    def _build_connection_panel(self):
        """构建设备连接面板"""
        frame = ttk.LabelFrame(self.root, text="设备连接 (USBTMC)", padding=10)
        frame.pack(fill="x", padx=8, pady=(8, 4))

        # ── DM3068 行 ──
        dm_row = ttk.Frame(frame)
        dm_row.pack(fill="x", pady=(0, 8))

        ttk.Label(dm_row, text="DM3068 电流表:", width=14, anchor="e").pack(side="left", padx=(0, 4))

        self.dm_port_var = tk.StringVar()
        self.dm_port_combo = ttk.Combobox(
            dm_row, textvariable=self.dm_port_var, width=42, state="readonly"
        )
        self.dm_port_combo.pack(side="left", padx=2)

        self.dm_connect_btn = ttk.Button(
            dm_row, text="连接", width=6,
            command=lambda: self._connect_device(self.dm3068, self.dm_port_var,
                                                  self.dm_status_label,
                                                  self.dm_connect_btn)
        )
        self.dm_connect_btn.pack(side="left", padx=2)

        self.dm_disconnect_btn = ttk.Button(
            dm_row, text="断开", width=6,
            command=lambda: self._disconnect_device(self.dm3068, self.dm_status_label,
                                                     self.dm_connect_btn, self.dm_disconnect_btn)
        )
        self.dm_disconnect_btn.pack(side="left", padx=2)
        self.dm_disconnect_btn.configure(state="disabled")

        # DM3068 状态
        status_row = ttk.Frame(frame)
        status_row.pack(fill="x")
        ttk.Label(status_row, text="  ").pack(side="left")
        self.dm_status_label = ttk.Label(
            status_row,
            text="● 未连接",
            foreground=self.COLOR_DISCONNECTED,
            font=self.FONT_STATUS,
        )
        self.dm_status_label.pack(side="left")

        # ── 分隔线 ──
        ttk.Separator(frame, orient="horizontal").pack(fill="x", pady=8)

        # ── DG1062 行 ──
        dg_row = ttk.Frame(frame)
        dg_row.pack(fill="x", pady=(0, 8))

        ttk.Label(dg_row, text="DG1062 信号源:", width=14, anchor="e").pack(side="left", padx=(0, 4))

        self.dg_port_var = tk.StringVar()
        self.dg_port_combo = ttk.Combobox(
            dg_row, textvariable=self.dg_port_var, width=42, state="readonly"
        )
        self.dg_port_combo.pack(side="left", padx=2)

        self.dg_connect_btn = ttk.Button(
            dg_row, text="连接", width=6,
            command=lambda: self._connect_device(self.dg1062, self.dg_port_var,
                                                  self.dg_status_label,
                                                  self.dg_connect_btn)
        )
        self.dg_connect_btn.pack(side="left", padx=2)

        self.dg_disconnect_btn = ttk.Button(
            dg_row, text="断开", width=6,
            command=lambda: self._disconnect_device(self.dg1062, self.dg_status_label,
                                                     self.dg_connect_btn, self.dg_disconnect_btn)
        )
        self.dg_disconnect_btn.pack(side="left", padx=2)
        self.dg_disconnect_btn.configure(state="disabled")

        # DG1062 状态
        status_row2 = ttk.Frame(frame)
        status_row2.pack(fill="x")
        ttk.Label(status_row2, text="  ").pack(side="left")
        self.dg_status_label = ttk.Label(
            status_row2,
            text="● 未连接",
            foreground=self.COLOR_DISCONNECTED,
            font=self.FONT_STATUS,
        )
        self.dg_status_label.pack(side="left")

        # ── 刷新按钮 ──
        refresh_row = ttk.Frame(frame)
        refresh_row.pack(fill="x", pady=(4, 0))
        ttk.Button(
            refresh_row, text="↻ 刷新设备列表", command=self._refresh_devices
        ).pack(side="right")

    # ══════════════════════════════════════════════════════
    # 面板2: 电流读数
    # ══════════════════════════════════════════════════════

    def _build_reading_panel(self):
        """构建电流读数面板"""
        frame = ttk.LabelFrame(self.root, text="电流监测", padding=10)
        frame.pack(fill="both", expand=True, padx=8, pady=4)

        # 电流读数大字体显示
        self.reading_var = tk.StringVar(value="---.----  mA")
        self.reading_label = tk.Label(
            frame,
            textvariable=self.reading_var,
            font=self.FONT_LARGE,
            relief="sunken",
            bg=self.COLOR_NORMAL_BG,
            anchor="center",
            padx=20,
            pady=15,
        )
        self.reading_label.pack(fill="x", padx=20, pady=10)

        # 单位提示
        ttk.Label(frame, text="AC 电流", foreground="#666666").pack()

        # 控制行：阈值 + 监控按钮
        ctrl_row = ttk.Frame(frame)
        ctrl_row.pack(fill="x", pady=(15, 5))

        # 阈值输入
        ttk.Label(ctrl_row, text="电流阈值:").pack(side="left", padx=(20, 4))
        self.threshold_var = tk.StringVar(value="1.000")
        self.threshold_entry = ttk.Entry(
            ctrl_row, textvariable=self.threshold_var, width=10, justify="center"
        )
        self.threshold_entry.pack(side="left", padx=2)
        ttk.Label(ctrl_row, text="mA").pack(side="left", padx=(2, 20))

        # 保护使能
        self.protection_var = tk.BooleanVar(value=True)  # 默认启用保护
        self.protection_check = ttk.Checkbutton(
            ctrl_row, text="启用保护", variable=self.protection_var,
            command=self._on_protection_toggle
        )
        self.protection_check.pack(side="left", padx=10)

        # 监控按钮
        self.start_btn = ttk.Button(
            ctrl_row, text="▶ 开始监控", command=self._start_polling
        )
        self.start_btn.pack(side="right", padx=2)

        self.stop_btn = ttk.Button(
            ctrl_row, text="⏹ 停止监控", command=self._stop_polling, state="disabled"
        )
        self.stop_btn.pack(side="right", padx=2)

        # 保护状态
        protect_row = ttk.Frame(frame)
        protect_row.pack(fill="x", pady=(5, 0))
        self.protect_status_label = ttk.Label(
            protect_row,
            text="保护就绪",
            font=("Microsoft YaHei UI", 11, "bold"),
            foreground="#888888",
        )
        self.protect_status_label.pack(side="left", padx=20)

        self.output_state_label = ttk.Label(
            protect_row,
            text="DG1062 输出: ---",
            font=self.FONT_STATUS,
        )
        self.output_state_label.pack(side="right", padx=20)

    # ══════════════════════════════════════════════════════
    # 面板3: 保护设置 + DG1062 输出控制
    # ══════════════════════════════════════════════════════

    def _build_protection_panel(self):
        """构建保护/输出控制面板"""
        frame = ttk.LabelFrame(self.root, text="DG1062 输出控制", padding=10)
        frame.pack(fill="x", padx=8, pady=4)

        # ── 输出控制按钮 ──
        btn_row = ttk.Frame(frame)
        btn_row.pack(fill="x")

        self.output_on_btn = ttk.Button(
            btn_row, text="● 开启输出", command=lambda: self._set_dg1062_output(True)
        )
        self.output_on_btn.pack(side="left", padx=5)

        self.output_off_btn = ttk.Button(
            btn_row, text="○ 关闭输出", command=lambda: self._set_dg1062_output(False)
        )
        self.output_off_btn.pack(side="left", padx=5)

        self.reset_protection_btn = ttk.Button(
            btn_row, text="↺ 重置保护", command=self._reset_protection
        )
        self.reset_protection_btn.pack(side="right", padx=5)

    # ══════════════════════════════════════════════════════
    # 面板4: 数据记录
    # ══════════════════════════════════════════════════════

    def _build_logging_panel(self):
        """构建数据记录面板（记录随输出自动启停）"""
        frame = ttk.LabelFrame(self.root, text="数据记录（随输出自动启停）", padding=10)
        frame.pack(fill="x", padx=8, pady=4)

        # ── 第1行：文件名 ──
        file_row = ttk.Frame(frame)
        file_row.pack(fill="x", pady=(0, 5))

        ttk.Label(file_row, text="保存文件:").pack(side="left", padx=(5, 5))

        # 默认文件名：当天日期
        default_filename = datetime.now().strftime("%Y-%m-%d") + ".txt"
        self.log_filename_var = tk.StringVar(value=default_filename)
        self.log_filename_entry = ttk.Entry(
            file_row, textvariable=self.log_filename_var, width=30
        )
        self.log_filename_entry.pack(side="left", padx=2, fill="x", expand=True)

        ttk.Button(
            file_row, text="浏览…", width=6,
            command=self._browse_log_file
        ).pack(side="left", padx=2)

        # ── 第2行：记录间隔 + 状态 ──
        ctrl_row = ttk.Frame(frame)
        ctrl_row.pack(fill="x")

        ttk.Label(ctrl_row, text="记录间隔:").pack(side="left", padx=(5, 5))

        self.log_interval_var = tk.StringVar(value="1 s")
        log_interval_combo = ttk.Combobox(
            ctrl_row, textvariable=self.log_interval_var, width=10, state="readonly",
            values=["0.1 s", "0.25 s", "0.5 s", "1 s", "2 s", "5 s", "10 s", "30 s", "60 s"],
        )
        log_interval_combo.pack(side="left", padx=2)
        log_interval_combo.bind("<<ComboboxSelected>>", self._on_log_interval_change)

        # 记录状态
        self.log_status_label = ttk.Label(
            ctrl_row,
            text="○ 等待输出开启",
            foreground="#888888",
            font=self.FONT_STATUS,
        )
        self.log_status_label.pack(side="left", padx=(20, 5))

        # 记录计数
        self.log_count_var = tk.StringVar(value="")
        ttk.Label(ctrl_row, textvariable=self.log_count_var,
                  font=self.FONT_STATUS, foreground="#666666").pack(side="left", padx=5)

    # ══════════════════════════════════════════════════════
    # 面板5: 实时曲线图
    # ══════════════════════════════════════════════════════

    def _build_plot_panel(self):
        """构建实时电流-时间曲线图面板"""
        frame = ttk.LabelFrame(self.root, text="电流-时间曲线", padding=5)
        frame.pack(fill="both", expand=True, padx=8, pady=4)

        # 创建 matplotlib Figure
        self.plot_figure = Figure(figsize=(6.5, 3.0), dpi=100, facecolor="#fafafa")
        self.plot_axes = self.plot_figure.add_subplot(111)
        self.plot_axes.set_xlabel("时间 (s)")
        self.plot_axes.set_ylabel("电流 (mA)")
        self.plot_axes.set_title("实时电流监测")
        self.plot_axes.grid(True, linestyle="--", alpha=0.5)
        # 初始空图
        self._plot_line, = self.plot_axes.plot([], [], "b-", linewidth=1.2)
        self._plot_hline = self.plot_axes.axhline(
            y=self.settings.threshold_ma, color="red", linestyle="--",
            linewidth=1.0, alpha=0.7, label=f"阈值={self.settings.threshold_ma:.3f} mA"
        )
        self.plot_axes.legend(loc="upper right", fontsize=8)
        self.plot_figure.tight_layout()

        # 嵌入 tkinter
        self.plot_canvas = FigureCanvasTkAgg(self.plot_figure, master=frame)
        self.plot_canvas.draw()
        self.plot_canvas.get_tk_widget().pack(fill="both", expand=True, padx=2, pady=2)

    def _update_plot(self):
        """用当前缓冲区数据刷新曲线图（节流：最多每 100ms 刷新一次）"""
        if not hasattr(self, "plot_axes"):
            return

        now = time.time()
        if now - self._last_plot_update_time < 0.1:  # 距上次刷新不到 100ms，跳过
            return
        self._last_plot_update_time = now

        times = self._plot_times
        currents = self._plot_currents

        self._plot_line.set_data(times, currents)

        if times:
            # 自动调整 X/Y 范围
            x_min, x_max = 0, max(times) + 1
            if currents:
                y_min = min(currents)
                y_max = max(currents)
                y_margin = max((y_max - y_min) * 0.1, 0.05)
                self.plot_axes.set_xlim(x_min, x_max)
                self.plot_axes.set_ylim(y_min - y_margin, y_max + y_margin)
        else:
            self.plot_axes.set_xlim(0, 10)
            self.plot_axes.set_ylim(0, 2)

        # 更新阈值参考线
        self._plot_hline.set_ydata([self.settings.threshold_ma, self.settings.threshold_ma])
        self._plot_hline.set_label(f"阈值={self.settings.threshold_ma:.3f} mA")
        self.plot_axes.legend(loc="upper right", fontsize=8)

        self.plot_figure.tight_layout()
        self.plot_canvas.draw_idle()

    # ══════════════════════════════════════════════════════
    # 面板6: 消息日志
    # ══════════════════════════════════════════════════════

    def _build_log_panel(self):
        """构建消息日志面板"""
        frame = ttk.LabelFrame(self.root, text="消息日志", padding=5)
        frame.pack(fill="both", expand=True, padx=8, pady=(4, 2))

        # 使用 Text + Scrollbar
        log_container = ttk.Frame(frame)
        log_container.pack(fill="both", expand=True)

        self.log_text = tk.Text(
            log_container,
            height=10,
            font=self.FONT_LOG,
            wrap="word",
            state="disabled",
            borderwidth=1,
            relief="solid",
        )
        scrollbar = ttk.Scrollbar(log_container, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)

        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # 配置日志颜色标签
        self.log_text.tag_configure("error", foreground="#CC0000")
        self.log_text.tag_configure("warn", foreground="#CC6600")
        self.log_text.tag_configure("success", foreground="#228B22")
        self.log_text.tag_configure("info", foreground="#0066CC")

    # ══════════════════════════════════════════════════════
    # 状态栏
    # ══════════════════════════════════════════════════════

    def _build_status_bar(self):
        """构建底部状态栏"""
        status_frame = ttk.Frame(self.root, relief="sunken", borderwidth=1)
        status_frame.pack(fill="x", side="bottom", padx=8, pady=(2, 8))

        self.status_text = tk.StringVar(value="就绪 — 请连接设备")
        self.status_bar_label = ttk.Label(
            status_frame, textvariable=self.status_text, font=self.FONT_STATUS, padding=(8, 3)
        )
        self.status_bar_label.pack(side="left")

    # ══════════════════════════════════════════════════════
    # 设备列表管理
    # ══════════════════════════════════════════════════════

    def _refresh_devices(self):
        """扫描可用 USBTMC 设备并更新下拉框"""
        try:
            resources = scan_usb_instruments()
            display_list = []
            for r in resources:
                friendly = parse_resource_display(r)
                display_list.append(f"{r}  →  {friendly}")

            for combo in (self.dm_port_combo, self.dg_port_combo):
                combo["values"] = display_list
                if display_list and not combo.get():
                    combo.set(display_list[0])
                elif not display_list:
                    combo.set("")

            if not display_list:
                self._log_message(
                    "未检测到 USB TMC 设备。请确认：\n"
                    "  1. 设备已开机并通过 USB 连接\n"
                    "  2. 设备管理器中出现 'USB Test and Measurement Devices'",
                    tag="warn",
                )
            else:
                self._log_message(f"检测到 {len(resources)} 个 USB TMC 设备")
        except Exception as e:
            self._log_message(f"设备扫描失败: {e}", tag="error")

    def _get_selected_resource(self, port_var: tk.StringVar) -> str:
        """从下拉框提取纯 VISA 资源字符串（如 USB0::0x1AB1::...）"""
        text = port_var.get().strip()
        if "  →  " in text:
            return text.split("  →  ")[0].strip()
        if "::" in text:
            return text
        return text

    # ══════════════════════════════════════════════════════
    # 设备连接/断开
    # ══════════════════════════════════════════════════════

    def _connect_device(self, device: VisaDevice, port_var: tk.StringVar,
                        status_label: ttk.Label, connect_btn: ttk.Button):
        """连接设备"""
        resource_name = self._get_selected_resource(port_var)
        if not resource_name:
            messagebox.showwarning(
                "未选择设备",
                f"请先为 {device.name} 选择一个 USB TMC 设备。\n\n"
                "如果列表为空，请点击「刷新设备列表」并检查 USB 连接。",
            )
            return

        try:
            idn = device.connect(resource_name)
        except pyvisa.errors.VisaIOError as e:
            messagebox.showerror(
                "连接失败",
                f"无法打开设备:\n{e}\n\n"
                "请检查:\n"
                "• 设备是否已开机?\n"
                "• 是否被其他程序占用?\n"
                f"• 资源: {resource_name}",
            )
            self._log_message(f"[失败] {device.name} 连接失败: {e}", tag="error")
            return
        except Exception as e:
            messagebox.showerror("连接失败", f"{device.name} 连接失败:\n{e}")
            self._log_message(f"[失败] {device.name} 连接失败: {e}", tag="error")
            return

        # 连接成功
        info_str = f"{device.info.manufacturer},{device.info.model}" if device.info else idn
        status_label.configure(
            text=f"● 已连接 — {info_str}",
            foreground=self.COLOR_CONNECTED,
        )
        connect_btn.configure(state="disabled")

        # 启用断开按钮
        if device is self.dm3068:
            self.dm_disconnect_btn.configure(state="normal")
        else:
            self.dg_disconnect_btn.configure(state="normal")

        self._log_message(f"[成功] {device.name} 已连接", tag="success")
        self._log_message(f"       设备信息: {idn}")
        self._log_message(f"       资源: {resource_name}")

        # DM3068 连接后尝试加速（*CLS 清错 + NPLC=0.02）
        if device is self.dm3068:
            device.configure_ac_current_fast()
            self._log_message("       DM3068 AC 电流模式已就绪")

        self._update_status()

        # 自动开始监控：当两个设备都连接好且未在监控中
        if (self.dm3068.is_connected() and self.dg1062.is_connected()
                and not self._polling_active):
            self.protection_var.set(True)  # 自动启用保护
            self.root.after(500, self._start_polling)

    def _disconnect_device(self, device: VisaDevice, status_label: ttk.Label,
                           connect_btn: ttk.Button, disconnect_btn: ttk.Button):
        """断开设备连接"""
        # 断开 DM3068：停止监控 + 停止记录
        if device is self.dm3068 and self._polling_active:
            self._stop_polling()
        # 断开 DG1062：停止记录（输出已不可用）
        if device is self.dg1062 and self._logging_active:
            self._stop_logging()

        device.disconnect()
        status_label.configure(
            text="● 未连接",
            foreground=self.COLOR_DISCONNECTED,
        )
        connect_btn.configure(state="normal")
        disconnect_btn.configure(state="disabled")

        # 断开 DG1062 时清除输出状态缓存
        if device is self.dg1062:
            self._cached_output_state = None
            self._update_output_state_display()

        self._log_message(f"{device.name} 已断开连接", tag="warn")
        self._update_status()

    # ══════════════════════════════════════════════════════
    # 监控轮询
    # ══════════════════════════════════════════════════════

    def _start_polling(self):
        """开始监控电流"""
        if self._polling_active:
            return
        if not self.dm3068.is_connected():
            messagebox.showwarning("未连接", "请先连接 DM3068 电流表。")
            return
        if not self.dg1062.is_connected():
            messagebox.showwarning("未连接", "请先连接 DG1062 信号源。")
            return

        # 读取阈值
        try:
            threshold = float(self.threshold_var.get())
        except ValueError:
            messagebox.showwarning("无效阈值", "请输入有效的电流阈值（数字）。")
            return
        self.settings.threshold_ma = threshold

        # 读取保护开关
        self.settings.protection_enabled = self.protection_var.get()

        self._poll_stop_event.clear()
        self._polling_active = True
        self._reading_count = 0
        self._protection_tripped = False
        self.dm3068._below_count = 0  # 重置欠流计数，避免上一次监控残留

        # 递增监控代数，用于丢弃上一次监控遗留的过期消息
        self._poll_generation += 1
        gen = self._poll_generation

        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.threshold_entry.configure(state="disabled")
        self.protection_check.configure(state="disabled")

        self.reading_label.configure(bg=self.COLOR_NORMAL_BG, fg="black")
        self.protect_status_label.configure(text="监控中…", foreground="#0066CC")

        self._poll_thread = threading.Thread(
            target=self._poll_loop, args=(gen,), daemon=True
        )
        self._poll_thread.start()

        self._log_message(
            f"开始监控 — 间隔 {self.settings.poll_interval_ms}ms, "
            f"阈值 {self.settings.threshold_ma:.4f} mA, "
            f"保护 {'开启' if self.settings.protection_enabled else '关闭'}",
            tag="info",
        )

        # 若 DG1062 输出当前为开启状态，立即开始记录（数据将随监控流入）
        if self.dg1062.is_connected() and self.dg1062.get_output_state() is True:
            self._start_logging()

        self._update_status()

    def _stop_polling(self):
        """停止监控"""
        if not self._polling_active:
            return
        self._poll_stop_event.set()
        # 线程会在下次循环时退出
        self._polling_active = False
        self._poll_thread = None

        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.threshold_entry.configure(state="normal")
        self.protection_check.configure(state="normal")

        if not self._protection_tripped:
            self.reading_label.configure(bg=self.COLOR_NORMAL_BG)
            self.protect_status_label.configure(text="监控已停止", foreground="#888888")

        self._log_message("监控已停止", tag="warn")
        self._update_status()

    def _poll_loop(self, gen: int):
        """后台轮询循环（在 daemon 线程中运行）。

        线程安全说明：tkinter 的 root.after() 非线程安全，不能在后台线程直接调用。
        这里改为把结果放入线程安全队列 _gui_queue，由主线程定时取走并更新界面。
        gen 为本次监控的代数，用于丢弃上一次监控遗留的过期消息。
        """
        error_count = 0  # 连续错误计数
        while not self._poll_stop_event.is_set():
            try:
                current_a = self.dm3068.measure_ac_current()
                error_count = 0  # 成功后清零
            except Exception as exc:
                error_count += 1
                if error_count >= 3:
                    # 连续 3 次错误才断开
                    self._gui_queue.put(("poll_error", (gen, str(exc))))
                    return
                else:
                    # 偶发错误 → 记录但继续
                    self._gui_queue.put(
                        ("log", (f"[重试 {error_count}/3] 读取异常: {exc}", "warn"))
                    )
                    self._poll_stop_event.wait(0.1)
                    continue

            # 读数经队列交给 GUI 线程处理（线程安全）
            self._gui_queue.put(("reading", (gen, current_a)))

            # 可中断的等待（使用用户设定的轮询间隔）
            self._poll_stop_event.wait(self.settings.poll_interval_ms / 1000.0)

        self._gui_queue.put(("poll_stopped", gen))

    def _process_gui_queue(self):
        """在主线程中定时清空队列，处理后台线程发来的消息（线程安全的 GUI 更新）"""
        if self._closing:
            return
        try:
            while True:
                try:
                    kind, payload = self._gui_queue.get_nowait()
                except queue.Empty:
                    break

                try:
                    if kind == "reading":
                        gen, current_a = payload
                        if gen == self._poll_generation:
                            self._on_reading(current_a)
                    elif kind == "log":
                        message, tag = payload
                        self._log_message(message, tag)
                    elif kind == "poll_error":
                        gen, error_msg = payload
                        if gen == self._poll_generation:
                            self._on_poll_error(error_msg)
                    elif kind == "poll_stopped":
                        gen = payload
                        if gen == self._poll_generation:
                            self._on_poll_stopped()
                except Exception:
                    # 单条消息处理失败不应中断后续消息处理
                    try:
                        self._log_message("界面更新异常（已忽略）", tag="error")
                    except Exception:
                        pass
        finally:
            if not self._closing:
                self.root.after(50, self._process_gui_queue)

    # ══════════════════════════════════════════════════════
    # 读取回调（在 GUI 线程执行）
    # ══════════════════════════════════════════════════════

    def _on_reading(self, current_a: float):
        """处理一次电流读数（GUI 线程）"""
        current_ma = current_a * 1000.0  # 转换为毫安

        # 根据量级自动选择 mA 或 µA 显示
        if abs(current_ma) < 1.0:
            current_ua = current_a * 1_000_000.0
            self.reading_var.set(f"{current_ua:.3f}  µA")
        else:
            self.reading_var.set(f"{current_ma:.4f}  mA")

        self._reading_count += 1

        # 数据记录：如果记录已开启，写入文件（复用监控读数）
        if self._logging_active:
            self._write_log_entry(current_a)

        # 每 5 次记录一次日志（用合适的单位）
        if self._reading_count % 5 == 0:
            if abs(current_ma) < 1.0:
                self._log_message(f"电流: {current_a * 1_000_000.0:.3f} µA")
            else:
                self._log_message(f"电流: {current_ma:.4f} mA")

        # 输出状态更新
        self._update_output_state_display()

        # 保护判断（阈值已是 mA，直接与 current_ma 比较）
        if self.settings.protection_enabled and not self._protection_tripped:
            if current_ma < self.settings.threshold_ma:
                # 连续 2 次确认
                self.dm3068._below_count += 1
                if self.dm3068._below_count >= 2:
                    self._trigger_protection(current_ma)
            else:
                self.dm3068._below_count = 0

    def _on_poll_error(self, error_msg: str):
        """轮询出错回调（GUI 线程）— 连续错误≥3次后触发，停止监控但不强制断开设备"""
        self._log_message(f"[错误] 连续读取失败，监控已停止: {error_msg}", tag="error")
        self._polling_active = False
        self._poll_thread = None

        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.threshold_entry.configure(state="normal")
        self.protection_check.configure(state="normal")

        self.reading_var.set("---.----  mA")
        self.reading_label.configure(bg=self.COLOR_NORMAL_BG)
        self.protect_status_label.configure(
            text="错误 — 通信异常，请检查设备", foreground="#CC0000"
        )

    def _on_poll_stopped(self):
        """轮询正常停止回调（GUI 线程）"""
        self._polling_active = False

    # ══════════════════════════════════════════════════════
    # 保护逻辑
    # ══════════════════════════════════════════════════════

    def _trigger_protection(self, current_ma: float):
        """触发欠流保护（GUI 线程）"""
        self._protection_tripped = True

        # 根据量级显示
        if abs(current_ma) < 1.0:
            current_display = f"{current_ma * 1000:.3f} µA"
        else:
            current_display = f"{current_ma:.4f} mA"

        self._log_message(
            f"[保护触发] 电流 {current_display} "
            f"< 阈值 {self.settings.threshold_ma:.4f} mA",
            tag="error",
        )

        # 关闭 DG1062 输出
        try:
            self.dg1062.set_output(False)
            self._log_message("[保护] DG1062 输出已自动关闭", tag="success")
        except Exception as exc:
            self._log_message(f"[错误] 关闭 DG1062 输出失败: {exc}", tag="error")
            messagebox.showerror(
                "关闭输出失败",
                f"无法关闭 DG1062 输出:\n{exc}\n\n请手动关闭设备输出！",
            )

        # 输出关断后自动停止数据记录
        self._stop_logging()

        # 更新界面（监测继续运行，仅标记保护状态）
        self.reading_label.configure(bg=self.COLOR_TRIPPED_BG, fg="#CC0000")
        self.protect_status_label.configure(
            text="⚠ 保护已触发 — 输出已关闭（监测继续）", foreground="#CC0000"
        )
        self._update_output_state_display(force_query=True)

        # 弹窗警告
        self.root.after(300, lambda: messagebox.showwarning(
            "保护触发",
            f"电流 ({current_display}) 低于阈值 ({self.settings.threshold_ma:.4f} mA)\n\n"
            "DG1062 输出已自动关闭，电流监测仍在继续。\n请检查后点击「重置保护」恢复。",
        ))

    def _reset_protection(self):
        """重置保护状态"""
        if not self._protection_tripped:
            return
        self._protection_tripped = False
        self.dm3068._below_count = 0
        self.reading_label.configure(bg=self.COLOR_NORMAL_BG, fg="black")
        self.protect_status_label.configure(text="保护已重置 — 就绪", foreground="#228B22")
        self._log_message("保护已重置 — 就绪", tag="info")
        self._update_status()

    def _on_protection_toggle(self):
        """保护开关切换"""
        enabled = self.protection_var.get()
        self._log_message(
            f"保护已{'启用' if enabled else '关闭'}",
            tag="info" if enabled else "warn",
        )

    # ══════════════════════════════════════════════════════
    # 数据记录
    # ══════════════════════════════════════════════════════

    def _browse_log_file(self):
        """打开文件保存对话框选择记录文件"""
        from tkinter import filedialog
        filepath = filedialog.asksaveasfilename(
            title="选择数据记录文件",
            initialdir=os.path.dirname(os.path.abspath(__file__)),
            initialfile=self.log_filename_var.get(),
            defaultextension=".txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if filepath:
            # 只保留文件名（相对路径），完整路径存到内部变量
            self.log_filename_var.set(os.path.basename(filepath))
            self._log_file_path = filepath
        else:
            # 用户取消 — 用默认目录 + 文件名
            self._log_file_path = ""

    def _get_log_full_path(self) -> str:
        """获取日志文件的完整路径"""
        if self._log_file_path:
            return self._log_file_path
        script_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(script_dir, self.log_filename_var.get())

    def _on_log_interval_change(self, event=None):
        """记录间隔切换"""
        interval_str = self.log_interval_var.get()
        try:
            seconds = float(interval_str.replace("s", "").strip())
            self._log_interval_ms = int(seconds * 1000)
            if self._logging_active:
                self._log_message(
                    f"记录间隔已更改为 {seconds:.2f}s（下次记录生效）", tag="info"
                )
        except (ValueError, IndexError):
            pass

    def _start_logging(self):
        """开始数据记录（由输出开启触发）"""
        if self._logging_active:
            return
        if not self.dm3068.is_connected():
            self._log_message("[记录] DM3068 未连接，无法记录", tag="warn")
            return

        # 解析记录间隔
        self._on_log_interval_change()

        # 确定文件路径
        log_path = self._get_log_full_path()

        # 打开文件（追加模式）
        try:
            self._log_file_handle = open(log_path, "a", encoding="utf-8")
        except OSError as e:
            self._log_message(f"[错误] 无法创建/打开文件: {log_path}\n{e}", tag="error")
            return

        self._logging_active = True
        self._log_file_path = log_path
        self._log_entry_count = 0
        self._log_start_time = time.time()  # 记录起始时刻
        self._last_log_write_time = 0.0     # 重置节流，确保第一条立刻写入

        # 清空上一轮的绘图数据
        self._plot_times.clear()
        self._plot_currents.clear()
        self._update_plot()

        # 写入文件头
        self._log_file_handle.write(
            f"# RIGOL DM3068 电流数据记录\n"
            f"# 开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"# 格式: 相对时间(s)\\t电流(mA)\n"
        )
        self._log_file_handle.flush()

        # 更新界面
        self.log_filename_entry.configure(state="disabled")
        self.log_status_label.configure(text="● 记录中", foreground="#CC0000")
        self.log_count_var.set(f"已记录: 0 条")

        self._log_message(
            f"开始数据记录 → {os.path.basename(log_path)}"
            f"（间隔 {self._log_interval_ms}ms，时间从0开始）",
            tag="success",
        )

    def _stop_logging(self):
        """停止数据记录（由输出关断触发）"""
        if not self._logging_active:
            return

        self._logging_active = False

        # 关闭文件
        if self._log_file_handle:
            elapsed_total = time.time() - self._log_start_time
            self._log_file_handle.write(
                f"# 结束时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"# 总时长: {elapsed_total:.3f} s\n"
                f"# 共记录: {self._log_entry_count} 条\n"
            )
            self._log_file_handle.close()
            self._log_file_handle = None

        # 更新界面
        self.log_filename_entry.configure(state="normal")
        self.log_status_label.configure(text="○ 等待输出开启", foreground="#888888")
        self.log_count_var.set("")

        self._log_message(
            f"数据记录已停止 — 共 {self._log_entry_count} 条 → "
            f"{os.path.basename(self._log_file_path)}",
            tag="info",
        )

    def _write_log_entry(self, current_a: float):
        """将一次电流读数写入记录文件（按设定的记录间隔节流），并更新实时曲线图"""
        if not self._logging_active or not self._log_file_handle:
            return
        try:
            now = time.time()
            # 按记录间隔节流：距上次写入时间不足则跳过
            if (now - self._last_log_write_time) * 1000.0 < self._log_interval_ms:
                return

            current_ma = current_a * 1000.0
            elapsed = now - self._log_start_time  # 从0开始的相对秒数
            line = f"{elapsed:.3f}\t{current_ma:.6f}\n"
            self._log_file_handle.write(line)
            self._log_file_handle.flush()
            self._log_entry_count += 1
            self._last_log_write_time = now

            # 追加绘图数据
            self._plot_times.append(elapsed)
            self._plot_currents.append(current_ma)

            # 更新计数显示（每 10 条更新一次，减少 GUI 压力）
            if self._log_entry_count % 10 == 0:
                self.log_count_var.set(f"已记录: {self._log_entry_count} 条")

            # 刷新曲线图（每条记录都触发，内部有节流控制）
            self._update_plot()
        except OSError as e:
            self._log_message(f"[错误] 写入记录文件失败: {e}", tag="error")
            self.root.after(0, self._stop_logging)

    # ══════════════════════════════════════════════════════
    # DG1062 输出控制
    # ══════════════════════════════════════════════════════

    def _set_dg1062_output(self, on: bool):
        """手动控制 DG1062 输出，并自动启停数据记录"""
        if not self.dg1062.is_connected():
            messagebox.showwarning("未连接", "请先连接 DG1062 信号源。")
            return

        try:
            self.dg1062.set_output(on)
            state_str = "开启" if on else "关闭"
            self._log_message(f"DG1062 输出: {state_str}", tag="success" if on else "warn")
            self._update_output_state_display(force_query=True)

            # 开启输出 → 自动开始记录；关闭输出 → 自动停止记录
            # 数据只在监控轮询时产生，故仅在监控运行中才真正开始记录，
            # 否则会写出一个没有数据的空记录块。
            if on:
                if self._polling_active:
                    self._start_logging()
                else:
                    self._log_message(
                        "[提示] 监控未运行，暂不记录；开始监控后将自动开始记录",
                        tag="warn",
                    )
            else:
                self._stop_logging()
        except Exception as exc:
            messagebox.showerror("操作失败", f"无法控制 DG1062 输出:\n{exc}")
            self._log_message(f"[错误] DG1062 输出控制失败: {exc}", tag="error")

    def _update_output_state_display(self, force_query: bool = False):
        """更新 DG1062 输出状态显示。

        为避免每次读数都阻塞查询 DG1062，默认使用缓存状态，
        仅在以下情况才真正查询硬件：
        - force_query=True（手动开关输出、保护触发等状态变更后）
        - 距上次查询超过 2 秒（周期性确认）
        """
        if not self.dg1062.is_connected():
            self._cached_output_state = None
            self.output_state_label.configure(
                text="DG1062 输出: ---", foreground="#888888"
            )
            return

        now = time.time()
        # 判断是否需要查询硬件
        need_query = (
            force_query
            or self._cached_output_state is None
            or (now - self._last_output_query_time) > 2.0  # 每 2 秒确认一次
        )

        if need_query:
            state = self.dg1062.get_output_state()
            self._cached_output_state = state
            self._last_output_query_time = now
        else:
            state = self._cached_output_state

        if state is True:
            self.output_state_label.configure(
                text="DG1062 输出: ● ON", foreground=self.COLOR_OUTPUT_ON
            )
        elif state is False:
            self.output_state_label.configure(
                text="DG1062 输出: ○ OFF", foreground=self.COLOR_OUTPUT_OFF
            )
        else:
            self.output_state_label.configure(
                text="DG1062 输出: ?", foreground="#CC6600"
            )

    # ══════════════════════════════════════════════════════
    # 消息日志
    # ══════════════════════════════════════════════════════

    def _log_message(self, message: str, tag: str = ""):
        """向消息日志添加一行带时间戳的消息"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{timestamp}] {message}\n"

        self.log_text.configure(state="normal")
        self.log_text.insert("end", line, tag)
        self.log_text.see("end")  # 自动滚动到底部
        self.log_text.configure(state="disabled")

    # ══════════════════════════════════════════════════════
    # 状态更新
    # ══════════════════════════════════════════════════════

    def _update_status(self):
        """更新底部状态栏"""
        dm_ok = self.dm3068.is_connected()
        dg_ok = self.dg1062.is_connected()

        if dm_ok and dg_ok and self._polling_active:
            self.status_text.set("运行中 — DM3068 ✓ | DG1062 ✓")
        elif dm_ok and dg_ok:
            self.status_text.set("就绪 — DM3068 ✓ | DG1062 ✓")
        elif dm_ok:
            self.status_text.set("DM3068 ✓ | DG1062 未连接")
        elif dg_ok:
            self.status_text.set("DM3068 未连接 | DG1062 ✓")
        else:
            self.status_text.set("就绪 — 请连接设备")

    # ══════════════════════════════════════════════════════
    # 关闭处理
    # ══════════════════════════════════════════════════════

    def _on_close(self):
        """窗口关闭时的清理"""
        self._closing = True  # 停止队列处理器继续调度，避免销毁后再回调
        self._stop_logging()
        self._stop_polling()
        self.dm3068.disconnect()
        self.dg1062.disconnect()
        self.root.destroy()


# ══════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════

def main():
    """程序入口"""
    app = RigolControlApp()

    # 窗口居中
    app.root.update_idletasks()
    w = app.root.winfo_width()
    h = app.root.winfo_height()
    sw = app.root.winfo_screenwidth()
    sh = app.root.winfo_screenheight()
    x = (sw - w) // 2
    y = (sh - h) // 2
    app.root.geometry(f"+{x}+{y}")

    app.root.mainloop()


if __name__ == "__main__":
    main()
