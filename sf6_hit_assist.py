# -*- coding: utf-8 -*-
"""
街霸6 命中确认辅助练习器 (GUI 版)
--------------------------------
原理:监听手柄起手输入(如 下+A / RT+B),按出起手后的短窗口内盯着
"对手血条掉血端"小区域与基线对比 —— 掉血=命中 → 蜂鸣提示(可选:自动模拟绿冲),
随后冷却 5 秒不再触发,留给你接连段;不掉血=被防/挥空,无动静。

仅供训练模式离线练习使用!在线对战使用自动输入违反 Capcom 用户协议。

全局热键(游戏内生效):
  F9 : 采集基线    F8 : 启停检测    F7 : 3秒后试放绿冲    F12: 退出
手柄热键(按住 视图键/View 再按):
  View+A: 采基线   View+B: 启停检测

自动绿冲(可选,默认关):
  通过 ViGEm 虚拟 Xbox 360 手柄注入"前前"。物理手柄信号全程透传到虚拟手柄,
  游戏里把 P1 绑到虚拟手柄即可。需要 ViGEmBus 驱动(pip install vgamepad 附带)。

命令行:
  python sf6_hit_assist.py          打开界面
  python sf6_hit_assist.py --snap   保存全屏截图(找区域坐标用)
  python sf6_hit_assist.py --crop   保存两个检测区域的截图(验证用)
  python sf6_hit_assist.py --diag   手柄自检(测起手组合是否被识别),结果写 diag.txt
"""

import ctypes
import json
import queue
import sys
import threading
import time
import winsound
from ctypes import wintypes
from pathlib import Path

import numpy as np
import mss
import mss.tools

# 打包成 exe(PyInstaller onefile)后 __file__ 在临时解压目录,配置/截图要跟着 exe 走
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).parent
CONFIG_PATH = APP_DIR / "config.json"
FRAME = 1.0 / 60.0

# ---------------- Windows API ----------------
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # 防止显示缩放导致坐标错位
except Exception:
    pass

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
winmm = ctypes.WinDLL("winmm")
winmm.timeBeginPeriod(1)


def foreground_title():
    hwnd = user32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value


def foreground_process():
    """前台窗口的进程名,如 StreetFighter6.exe(取不到返回空串)"""
    hwnd = user32.GetForegroundWindow()
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
        return ""
    finally:
        kernel32.CloseHandle(h)


def key_pressed(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def sleep_until(t):
    while True:
        rem = t - time.perf_counter()
        if rem <= 0:
            return
        if rem > 0.002:
            time.sleep(rem - 0.002)


def beep_async(freq, ms):
    threading.Thread(target=winsound.Beep, args=(freq, ms), daemon=True).start()


# ---------------- 手柄: XInput 监听 + 可选 ViGEm 虚拟手柄 ----------------
class XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [("wButtons", wintypes.WORD), ("bLeftTrigger", ctypes.c_ubyte),
                ("bRightTrigger", ctypes.c_ubyte), ("sThumbLX", ctypes.c_short),
                ("sThumbLY", ctypes.c_short), ("sThumbRX", ctypes.c_short),
                ("sThumbRY", ctypes.c_short)]


class XINPUT_STATE(ctypes.Structure):
    _fields_ = [("dwPacketNumber", wintypes.DWORD), ("Gamepad", XINPUT_GAMEPAD)]


def _load_xinput():
    for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
        try:
            return ctypes.WinDLL(name)
        except OSError:
            pass
    return None


PAD_BUTTONS = {
    "dpad_up": 0x0001, "dpad_down": 0x0002, "dpad_left": 0x0004, "dpad_right": 0x0008,
    "start": 0x0010, "select": 0x0020, "l3": 0x0040, "r3": 0x0080,
    "lb": 0x0100, "rb": 0x0200,
    "a": 0x1000, "b": 0x2000, "x": 0x4000, "y": 0x8000,
}
TRIGGER_THR = 30        # 扳机算"按下"的阈值(0~255)
STICK_THR = 16384       # 摇杆算方向输入的阈值(半程)
# 起手组合里可写的键名(方向同时认十字键和左摇杆)
TOKEN_NAMES = set(PAD_BUTTONS) | {"up", "down", "left", "right", "lt", "rt"}


def token_down(token, gp):
    """判断一个键名当前是否按下(方向 = 十字键或左摇杆)"""
    b = gp.wButtons
    if token == "up":
        return bool(b & PAD_BUTTONS["dpad_up"]) or gp.sThumbLY > STICK_THR
    if token == "down":
        return bool(b & PAD_BUTTONS["dpad_down"]) or gp.sThumbLY < -STICK_THR
    if token == "left":
        return bool(b & PAD_BUTTONS["dpad_left"]) or gp.sThumbLX < -STICK_THR
    if token == "right":
        return bool(b & PAD_BUTTONS["dpad_right"]) or gp.sThumbLX > STICK_THR
    if token == "lt":
        return gp.bLeftTrigger > TRIGGER_THR
    if token == "rt":
        return gp.bRightTrigger > TRIGGER_THR
    return bool(b & PAD_BUTTONS[token])


class PadBridge:
    """监听物理手柄:起手组合 → ("attack", 组合名);View+A/B → ("hotkey", ...)。
    若 ViGEm 可用,同时创建虚拟手柄做 500Hz 透传,命中时可叠加注入绿冲。"""

    def __init__(self, eventq, log, cfg):
        self._xi = _load_xinput()
        if self._xi is None:
            raise RuntimeError("系统里找不到 XInput DLL")
        self._eventq = eventq
        self._log = log
        self._hotkeys = bool(cfg.get("pad_hotkeys", True))
        self.triggers = [[str(t).lower() for t in c]
                         for c in cfg.get("attack_triggers", [["down", "a"], ["rt", "b"]])]
        for c in self.triggers:
            for t in c:
                if t not in TOKEN_NAMES:
                    raise ValueError(f"attack_triggers 里的键 '{t}' 不认识。"
                                     f"可用: {' '.join(sorted(TOKEN_NAMES))}")
        self.trigger_text = " / ".join("+".join(c) for c in self.triggers)
        self._prev_act = [False] * len(self.triggers)
        self._prev_buttons = 0
        self._lock = threading.Lock()
        self._over = set()      # 注入中的按键名
        self._stop = False

        # 可选虚拟手柄:先记下已连接的物理槽位,新出现的槽位就是虚拟的(重扫排除,防自激)
        self._pad = None
        self._virtual = set()
        existing = {i for i in range(4) if self._read(i)}
        try:
            import vgamepad as vg
            self._pad = vg.VX360Gamepad()
            time.sleep(0.4)
            self._virtual = {i for i in range(4) if self._read(i)} - existing
            log("虚拟手柄已创建。要用自动绿冲:游戏里把 P1 绑到虚拟手柄,F7 试放验证。")
        except Exception as e:
            log(f"虚拟手柄不可用({e}),只监听、不模拟。")
        self._phys = min(existing) if existing else None
        if self._phys is not None:
            log(f"已连接物理手柄(XInput 槽位 {self._phys})。起手监听: {self.trigger_text}")
        else:
            log("暂未检测到物理手柄,插上后会自动识别。")

        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    @property
    def can_simulate(self):
        return self._pad is not None

    def _read(self, slot):
        state = XINPUT_STATE()
        if self._xi.XInputGetState(slot, ctypes.byref(state)) == 0:
            return state.Gamepad
        return None

    def _loop(self):
        period = 1.0 / 500.0 if self._pad else 1.0 / 120.0
        next_tick = time.perf_counter()
        next_rescan = 0.0
        while not self._stop:
            now = time.perf_counter()
            gp = self._read(self._phys) if self._phys is not None else None
            if gp is None and now >= next_rescan:
                next_rescan = now + 1.0
                for i in range(4):
                    if i not in self._virtual and self._read(i):
                        if self._phys != i:
                            self._phys = i
                            self._log(f"物理手柄已连接(槽位 {i})。")
                        gp = self._read(i)
                        break
            if gp is None:
                gp = XINPUT_GAMEPAD()

            buttons = gp.wButtons
            view_held = bool(buttons & PAD_BUTTONS["select"])

            # View+A/B = 热键(边沿)
            if self._hotkeys and view_held:
                for name, ev in (("a", "baseline"), ("b", "toggle")):
                    m = PAD_BUTTONS[name]
                    if (buttons & m) and not (self._prev_buttons & m):
                        self._eventq.put(("hotkey", ev))
            # 起手组合(整组同时按下的瞬间触发;View 按住时不算,避免和热键撞)
            for i, combo in enumerate(self.triggers):
                act = (not view_held) and all(token_down(t, gp) for t in combo)
                if act and not self._prev_act[i]:
                    self._eventq.put(("attack", "+".join(combo)))
                self._prev_act[i] = act
            self._prev_buttons = buttons

            # 透传 + 注入
            if self._pad:
                lt, rt = gp.bLeftTrigger, gp.bRightTrigger
                with self._lock:
                    over = tuple(self._over)
                for name in over:
                    if name == "lt":
                        lt = 255
                    elif name == "rt":
                        rt = 255
                    else:
                        buttons |= PAD_BUTTONS[name]
                r = self._pad.report
                r.wButtons = buttons
                r.bLeftTrigger, r.bRightTrigger = lt, rt
                r.sThumbLX, r.sThumbLY = gp.sThumbLX, gp.sThumbLY
                r.sThumbRX, r.sThumbRY = gp.sThumbRX, gp.sThumbRY
                self._pad.update()

            next_tick += period
            if next_tick < now:
                next_tick = now + period
            sleep_until(next_tick)

    # ---- 绿冲/连段注入(只在虚拟手柄可用时有效) ----
    @staticmethod
    def _resolve(name, side):
        """forward/back 按选边换算十字键: 1P前=右, 2P前=左"""
        if name == "forward":
            return "dpad_right" if side == "1P" else "dpad_left"
        if name == "back":
            return "dpad_left" if side == "1P" else "dpad_right"
        return name

    def _down(self, name):
        with self._lock:
            self._over.add(name)

    def _up(self, name):
        with self._lock:
            self._over.discard(name)

    def run_combo(self, combo, side):
        """阻塞执行注入序列。hold/wait 单位都是帧(1/60s)。"""
        if not self._pad:
            return False
        pressed = []
        t = time.perf_counter()
        try:
            for step in combo:
                keys = [self._resolve(k, side) for k in step.get("press", [])]
                for k in keys:
                    self._down(k)
                    pressed.append(k)
                t += step.get("hold", 2) * FRAME
                sleep_until(t)
                for k in keys:
                    self._up(k)
                    pressed.remove(k)
                wait = step.get("wait", 0)
                if wait:
                    t += wait * FRAME
                    sleep_until(t)
        finally:
            for k in pressed:
                self._up(k)
        return True

    def run_combo_async(self, combo, side):
        threading.Thread(target=self.run_combo, args=(combo, side), daemon=True).start()

    def close(self):
        self._stop = True
        self._thread.join(timeout=1)
        if self._pad:
            try:
                self._pad.reset()
                self._pad.update()
            except Exception:
                pass


# ---------------- 配置 ----------------
DEFAULT_CONFIG = {
    "_说明_side": "我是哪边: 1P(左) 或 2P(右)。界面里可以直接切,这里只是默认值。",
    "side": "1P",
    "_说明_attack_triggers": "起手输入组合:整组同时按下的瞬间开始等命中。方向(up/down/left/right)同时认十字键和左摇杆;扳机 lt/rt;按键 a b x y lb rb。",
    "attack_triggers": [["down", "a"], ["rt", "b"]],
    "_说明_confirm_window_frames": "按出起手后等掉血的窗口(帧)。窗口内掉血=命中,过了没掉=被防/挥空。起手招出招慢就加大。",
    "confirm_window_frames": 45,
    "_说明_cooldown_frames": "命中触发后的冷却(帧),期间不再触发。默认 300 帧 = 5 秒,留给接连段。",
    "cooldown_frames": 300,
    "_说明_trigger_mode": "pad=按出起手组合才检测(推荐); always=常开检测(手柄不可用时自动回退)。",
    "trigger_mode": "pad",
    "_说明_pad_hotkeys": "手柄热键: 按住 View(视图键) 再按 A=采基线, B=启停检测。不想要就改 false。",
    "pad_hotkeys": True,
    "_说明_region": "两个检测区域(屏幕像素坐标)。region_p2_bar=2P血条掉血端(我是1P时监控它), region_p1_bar=1P血条掉血端(我是2P时监控它)。用 --snap 截全屏找坐标, --crop 验证。下面是 1920x1080 参考值,务必自己校准!",
    "region_p2_bar": {"left": 1060, "top": 52, "width": 180, "height": 22},
    "region_p1_bar": {"left": 680, "top": 52, "width": 180, "height": 22},
    "_说明_threshold": "触发阈值。界面上看实时像素差:打中一下记峰值,被防一下记峰值,取中间。界面里调好后点「保存设置」。",
    "threshold": 10.0,
    "_说明_fps": "检测频率(每秒采样次数),120 足够,延迟约 1 帧。",
    "fps": 120,
    "_说明_window_process": "只有前台窗口的进程名等于它(或标题包含 window_title)才会触发,防止切出游戏误响。两项都留空 \"\" 则不检查。",
    "window_process": "StreetFighter6.exe",
    "window_title": "Street Fighter 6",
    "sound": {"freq": 1400, "ms": 90},
    "_说明_auto_dash": "true=命中后自动模拟绿冲(需 ViGEmBus 驱动 + 游戏里 P1 绑虚拟手柄)。界面里可随时切。",
    "auto_dash": False,
    "_说明_combo": "auto_dash 注入的序列,默认 前前=绿冲。可扩展成整套连段:press 可用 a b x y lb rb lt rt dpad_* forward back;hold=按住几帧,wait=松开后等几帧。",
    "combo": [
        {"press": ["forward"], "hold": 2, "wait": 2},
        {"press": ["forward"], "hold": 3, "wait": 0},
    ],
}


def notify(msg):
    print(msg)
    if getattr(sys, "frozen", False):  # 打包后无控制台,弹窗代替 print
        ctypes.windll.user32.MessageBoxW(0, msg, "SF6 命中确认辅助", 0x40)


def load_config():
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ---------------- 共享状态 ----------------
class State:
    def __init__(self, cfg):
        self.cfg = cfg
        self.side = cfg.get("side", "1P")
        self.threshold = float(cfg.get("threshold", 10.0))
        self.sound_on = True
        self.auto_dash = bool(cfg.get("auto_dash", False))
        self.armed = False
        self.quit = False
        self.req_baseline = False
        self.req_test = False
        self.diff = 0.0
        self.status = "未采基线"
        self.has_baseline = False
        self.preview = None        # (w, h, rgb bytes)
        self.logq = queue.Queue()
        self.padq = queue.Queue()  # 手柄事件 ("hotkey"/"attack", arg)

    def log(self, msg):
        self.logq.put(time.strftime("[%H:%M:%S] ") + msg)

    def region(self):
        key = "region_p2_bar" if self.side == "1P" else "region_p1_bar"
        return self.cfg[key]


# ---------------- 检测线程 ----------------
def worker(st: State, bridge):
    cfg = st.cfg
    cooldown_frames = int(cfg.get("cooldown_frames", 300))
    confirm_frames = int(cfg.get("confirm_window_frames", 45))
    window_title = cfg.get("window_title", "").lower()
    window_process = cfg.get("window_process", "StreetFighter6.exe").lower()

    def fg_ok():
        if not window_process and not window_title:
            return True
        if window_process and foreground_process().lower() == window_process:
            return True
        return bool(window_title) and window_title in foreground_title().lower()
    snd = cfg.get("sound", {"freq": 1400, "ms": 90})
    combo = cfg.get("combo", [])
    period = 1.0 / float(cfg.get("fps", 120))
    pad_mode = cfg.get("trigger_mode", "pad") == "pad" and bridge is not None
    idle_status = f"等待起手 {bridge.trigger_text}" if pad_mode else "监听中"

    baseline = None
    need_rearm = False
    rearm_ok = 0
    cooldown_until = 0.0
    confirm_until = 0.0    # >0 = 确认窗口截止时间
    last_side = st.side
    last_preview = 0.0
    prev_hk = {0x76: False, 0x77: False, 0x78: False, 0x7B: False}  # F7 F8 F9 F12

    with mss.mss() as sct:
        next_tick = time.perf_counter()
        while not st.quit:
            now = time.perf_counter()

            # 选边变了 → 基线作废
            if st.side != last_side:
                last_side = st.side
                baseline = None
                st.has_baseline = False
                st.armed = False
                st.log(f"已切换为 {st.side},监控{'2P' if st.side == '1P' else '1P'}血条。请重新采基线(F9)。")

            # ---- 键盘热键 ----
            for vk in prev_hk:
                cur = key_pressed(vk)
                if cur and not prev_hk[vk]:
                    if vk == 0x78:
                        st.req_baseline = True
                    elif vk == 0x77:
                        st.armed = not st.armed
                        st.log("检测已启用" if st.armed else "检测已停用")
                    elif vk == 0x76:
                        st.req_test = True
                    elif vk == 0x7B:
                        st.quit = True
                prev_hk[vk] = cur

            # ---- 手柄事件 ----
            try:
                while True:
                    ev, arg = st.padq.get_nowait()
                    if ev == "hotkey":
                        if arg == "baseline":
                            st.req_baseline = True
                        elif arg == "toggle":
                            st.armed = not st.armed
                            st.log("检测已启用" if st.armed else "检测已停用")
                    elif ev == "attack" and pad_mode:
                        if (st.armed and baseline is not None
                                and not need_rearm and now >= cooldown_until):
                            confirm_until = now + confirm_frames * FRAME
            except queue.Empty:
                pass

            # ---- 截屏 ----
            r = st.region()
            raw = sct.grab({"left": r["left"], "top": r["top"],
                            "width": r["width"], "height": r["height"]})
            arr = np.frombuffer(raw.rgb, dtype=np.uint8).reshape(raw.height, raw.width, 3)
            gray = arr.astype(np.float32).mean(axis=2)

            if now - last_preview > 0.2:
                st.preview = (raw.width, raw.height, raw.rgb)
                last_preview = now

            if st.req_baseline:
                st.req_baseline = False
                baseline = gray
                st.has_baseline = True
                need_rearm = False
                confirm_until = 0.0
                st.log("基线已采集(当前画面 = 无事发生)")

            if st.req_test:
                st.req_test = False
                if bridge and bridge.can_simulate:
                    st.log("3 秒后试放绿冲,请切回游戏窗口…")
                    time.sleep(3)
                    bridge.run_combo(combo, st.side)
                    st.log("试放完毕。角色没动 = 游戏里 P1 没绑到虚拟手柄。")
                    next_tick = time.perf_counter()
                else:
                    st.log("模拟不可用(没有虚拟手柄),无法试放。")

            # ---- 检测 ----
            if baseline is not None:
                diff = float(np.abs(gray - baseline).mean())
                st.diff = diff

                if need_rearm:
                    remain = cooldown_until - now
                    st.status = (f"冷却中 {remain:.0f}s" if remain > 0 else "等待画面恢复…")
                    if diff < st.threshold * 0.5:
                        rearm_ok += 1
                        if rearm_ok >= 10 and now >= cooldown_until:
                            need_rearm = False
                            st.log("冷却结束,画面已恢复,继续监听。")
                    else:
                        rearm_ok = 0
                elif st.armed:
                    in_window = (not pad_mode) or now < confirm_until
                    st.status = ("确认中…" if pad_mode and in_window else idle_status)
                    if in_window and diff > st.threshold and now >= cooldown_until:
                        if fg_ok():
                            dash = (st.auto_dash and bridge is not None
                                    and bridge.can_simulate and combo)
                            st.log(f"命中! diff={diff:.1f} → 提示音"
                                   + (" + 自动绿冲" if dash else ""))
                            if st.sound_on:
                                beep_async(int(snd.get("freq", 1400)), int(snd.get("ms", 90)))
                            if dash:
                                bridge.run_combo_async(combo, st.side)
                            cooldown_until = time.perf_counter() + cooldown_frames * FRAME
                            need_rearm = True
                            rearm_ok = 0
                            confirm_until = 0.0
                    elif pad_mode and confirm_until and now >= confirm_until:
                        confirm_until = 0.0
                        st.log("起手未命中(被防/挥空),不提示")
                else:
                    st.status = "已停用(F8 启用)"
            else:
                st.status = "未采基线(F9 采集)"

            next_tick += period
            if next_tick < now:
                next_tick = now + period
            sleep_until(next_tick)


# ---------------- 界面 ----------------
def gui():
    import tkinter as tk
    from tkinter import ttk
    from tkinter.scrolledtext import ScrolledText

    cfg = load_config()
    st = State(cfg)

    bridge = None
    try:
        bridge = PadBridge(st.padq, st.log, cfg)
    except Exception as e:
        st.log(f"手柄监听不可用: {e}")
        st.log("将回退为常开检测(F8 开关),热键用键盘 F 键。")

    root = tk.Tk()
    root.title("SF6 命中确认辅助 · 训练模式专用")
    root.geometry("460x640")
    root.attributes("-topmost", True)
    root.configure(bg="#14121a")

    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background="#14121a", foreground="#e8e4f0", font=("Microsoft YaHei", 10))
    style.configure("TLabelframe", background="#14121a", bordercolor="#36304a")
    style.configure("TLabelframe.Label", background="#14121a", foreground="#ff5c8a")
    style.configure("TButton", background="#262236", foreground="#e8e4f0", padding=6)
    style.map("TButton", background=[("active", "#36304a")])
    style.configure("TCheckbutton", background="#14121a", foreground="#e8e4f0")
    style.map("TCheckbutton", background=[("active", "#14121a")])
    style.configure("TRadiobutton", background="#14121a", foreground="#e8e4f0")
    style.map("TRadiobutton", background=[("active", "#14121a")])

    pad = {"padx": 10, "pady": 4}

    # --- 选边 ---
    frm_side = ttk.Labelframe(root, text=" 我是哪边 ")
    frm_side.pack(fill="x", **pad)
    side_var = tk.StringVar(value=st.side)

    side_hint = ttk.Label(frm_side, text="")

    def on_side(*_):
        st.side = side_var.get()
        opp = "2P(右)" if st.side == "1P" else "1P(左)"
        side_hint.config(text=f"监控 {opp} 血条")

    ttk.Radiobutton(frm_side, text="1P(我在左边)", value="1P",
                    variable=side_var, command=on_side).pack(side="left", padx=12, pady=4)
    ttk.Radiobutton(frm_side, text="2P(我在右边)", value="2P",
                    variable=side_var, command=on_side).pack(side="left", padx=12, pady=4)
    side_hint.pack(side="left", padx=8)
    on_side()

    # --- 状态 ---
    frm_st = ttk.Labelframe(root, text=" 状态 ")
    frm_st.pack(fill="x", **pad)
    status_lbl = tk.Label(frm_st, text="未采基线", font=("Microsoft YaHei", 13, "bold"),
                          bg="#14121a", fg="#8a849c")
    status_lbl.pack(anchor="w", padx=10, pady=(6, 2))

    diff_row = tk.Frame(frm_st, bg="#14121a")
    diff_row.pack(fill="x", padx=10, pady=2)
    tk.Label(diff_row, text="像素差", bg="#14121a", fg="#8a849c").pack(side="left")
    diff_canvas = tk.Canvas(diff_row, height=16, bg="#262236", highlightthickness=0)
    diff_canvas.pack(side="left", fill="x", expand=True, padx=8)
    diff_val = tk.Label(diff_row, text="0.0", width=6, bg="#14121a", fg="#e8e4f0",
                        font=("Consolas", 11))
    diff_val.pack(side="left")

    thr_row = tk.Frame(frm_st, bg="#14121a")
    thr_row.pack(fill="x", padx=10, pady=(2, 8))
    tk.Label(thr_row, text="阈值", bg="#14121a", fg="#8a849c").pack(side="left")
    thr_var = tk.DoubleVar(value=st.threshold)
    thr_lbl = tk.Label(thr_row, text=f"{st.threshold:.0f}", width=4,
                       bg="#14121a", fg="#ffb020", font=("Consolas", 11))

    def on_thr(v):
        st.threshold = float(v)
        thr_lbl.config(text=f"{st.threshold:.0f}")

    ttk.Scale(thr_row, from_=1, to=60, variable=thr_var, command=on_thr)\
        .pack(side="left", fill="x", expand=True, padx=8)
    thr_lbl.pack(side="left")

    # --- 开关 ---
    frm_sw = ttk.Labelframe(root, text=" 命中后做什么 ")
    frm_sw.pack(fill="x", **pad)
    snd_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(frm_sw, text="提示音", variable=snd_var,
                    command=lambda: setattr(st, "sound_on", snd_var.get())).pack(side="left", padx=12, pady=4)
    dash_var = tk.BooleanVar(value=st.auto_dash)
    dash_chk = ttk.Checkbutton(frm_sw, text="自动绿冲(需绑虚拟手柄)", variable=dash_var,
                               command=lambda: setattr(st, "auto_dash", dash_var.get()))
    dash_chk.pack(side="left", padx=12, pady=4)
    if not (bridge and bridge.can_simulate):
        dash_var.set(False)
        st.auto_dash = False
        dash_chk.state(["disabled"])

    # --- 按钮 ---
    frm_btn = tk.Frame(root, bg="#14121a")
    frm_btn.pack(fill="x", **pad)
    arm_btn = tk.Button(frm_btn, text="▶ 启动检测 (F8)", font=("Microsoft YaHei", 11, "bold"),
                        bg="#ff5c8a", fg="#fff", relief="flat", padx=10, pady=6,
                        command=lambda: setattr(st, "armed", not st.armed))
    arm_btn.pack(side="left", expand=True, fill="x", padx=(0, 6))
    ttk.Button(frm_btn, text="采基线 (F9)",
               command=lambda: setattr(st, "req_baseline", True)).pack(side="left", expand=True, fill="x", padx=3)
    ttk.Button(frm_btn, text="试放绿冲 (F7)",
               command=lambda: setattr(st, "req_test", True)).pack(side="left", expand=True, fill="x", padx=(6, 0))

    frm_btn2 = tk.Frame(root, bg="#14121a")
    frm_btn2.pack(fill="x", padx=10)
    top_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(frm_btn2, text="窗口置顶", variable=top_var,
                    command=lambda: root.attributes("-topmost", top_var.get())).pack(side="left")

    def do_save():
        cfg["side"] = st.side
        cfg["threshold"] = round(st.threshold, 1)
        cfg["auto_dash"] = st.auto_dash
        save_config(cfg)
        st.log("设置已保存到 config.json")

    ttk.Button(frm_btn2, text="保存设置", command=do_save).pack(side="right")

    # --- 区域预览 ---
    frm_pv = ttk.Labelframe(root, text=" 检测区域实时画面(应是对手血条掉血端) ")
    frm_pv.pack(fill="x", **pad)
    pv_label = tk.Label(frm_pv, bg="#262236")
    pv_label.pack(padx=10, pady=6)
    pv_img_ref = [None]

    # --- 日志 ---
    frm_log = ttk.Labelframe(root, text=" 记录 ")
    frm_log.pack(fill="both", expand=True, **pad)
    log_box = ScrolledText(frm_log, height=8, bg="#1e1b28", fg="#e8e4f0",
                           font=("Microsoft YaHei", 9), relief="flat", state="disabled")
    log_box.pack(fill="both", expand=True, padx=6, pady=6)

    st.log("仅限训练模式离线使用。流程:选边 → F9 采基线 → F8 启动 → 开打。")
    if bridge:
        st.log(f"按出起手({bridge.trigger_text})后掉血才响;命中后冷却 "
               f"{int(cfg.get('cooldown_frames', 300)) // 60} 秒接连段。")

    # --- 周期刷新 ---
    def tick():
        if st.quit:
            if bridge:
                bridge.close()
            root.destroy()
            return
        # 日志
        try:
            while True:
                line = st.logq.get_nowait()
                log_box.config(state="normal")
                log_box.insert("end", line + "\n")
                log_box.see("end")
                log_box.config(state="disabled")
        except queue.Empty:
            pass
        # 状态
        s = st.status
        if s.startswith("确认中"):
            color = "#ffb020"
        elif s.startswith(("等待起手", "监听中")):
            color = "#3ddc84"
        elif s.startswith("冷却中"):
            color = "#4d8dff"
        else:
            color = "#8a849c"
        status_lbl.config(text=s, fg=color)
        arm_btn.config(text="■ 停止检测 (F8)" if st.armed else "▶ 启动检测 (F8)",
                       bg="#4d8dff" if st.armed else "#ff5c8a")
        # diff 条
        w = diff_canvas.winfo_width() or 200
        diff_canvas.delete("all")
        frac = min(1.0, st.diff / 60.0)
        color = "#ff4d4d" if st.diff > st.threshold else "#3ddc84"
        diff_canvas.create_rectangle(0, 0, int(w * frac), 16, fill=color, width=0)
        tx = int(w * min(1.0, st.threshold / 60.0))
        diff_canvas.create_line(tx, 0, tx, 16, fill="#ffb020", width=2)
        diff_val.config(text=f"{st.diff:.1f}")
        # 预览
        if st.preview:
            pw, ph, rgb = st.preview
            try:
                img = tk.PhotoImage(data=f"P6 {pw} {ph} 255 ".encode() + rgb)
                z = max(1, min(3, 420 // pw))
                if z > 1:
                    img = img.zoom(z, z)
                pv_img_ref[0] = img
                pv_label.config(image=img)
            except Exception:
                pass
        root.after(50, tick)

    def on_close():
        st.quit = True
        if bridge:
            bridge.close()
        root.after(100, root.destroy)

    root.protocol("WM_DELETE_WINDOW", on_close)
    threading.Thread(target=worker, args=(st, bridge), daemon=True).start()
    root.after(50, tick)
    root.mainloop()


# ---------------- 截图辅助 ----------------
def snap_full():
    with mss.mss() as sct:
        mon = sct.monitors[1]
        img = sct.grab(mon)
        out = APP_DIR / "screenshot_full.png"
        mss.tools.to_png(img.rgb, img.size, output=str(out))
        notify(f"已保存全屏截图: {out}\n"
               "用画图打开,找到两条血条掉血端的坐标,填进 config.json 的 region_p1_bar / region_p2_bar。")


def snap_crop():
    cfg = load_config()
    lines = []
    with mss.mss() as sct:
        for name in ("region_p1_bar", "region_p2_bar"):
            r = cfg[name]
            img = sct.grab({"left": r["left"], "top": r["top"],
                            "width": r["width"], "height": r["height"]})
            out = APP_DIR / f"{name}.png"
            mss.tools.to_png(img.rgb, img.size, output=str(out))
            lines.append(f"已保存 {name}: {out} ({r['width']}x{r['height']} @ {r['left']},{r['top']})")
    notify("\n".join(lines))


# ---------------- 手柄自检 ----------------
def diag():
    """10 秒内按一遍起手组合,看脚本认不认。结果写 diag.txt。"""
    lines = [f"frozen={getattr(sys, 'frozen', False)}  python={sys.version.split()[0]}"]
    cfg = load_config()
    xi = _load_xinput()
    if xi is None:
        lines.append("XInput: 不可用!")
    else:
        def read(slot):
            s = XINPUT_STATE()
            return s.Gamepad if xi.XInputGetState(slot, ctypes.byref(s)) == 0 else None
        slots = [i for i in range(4) if read(i)]
        lines.append(f"已连接手柄槽位: {slots if slots else '无 — 请先插手柄!'}")
        try:
            import vgamepad  # noqa: F401
            lines.append("vgamepad(自动绿冲): 可用")
        except Exception as e:
            lines.append(f"vgamepad(自动绿冲): 不可用 — {e}")
        triggers = [[str(t).lower() for t in c]
                    for c in cfg.get("attack_triggers", [["down", "a"], ["rt", "b"]])]
        names = ["+".join(c) for c in triggers]
        counts = {n: 0 for n in names}
        if slots:
            print(f"10 秒内在手柄上按起手组合: {' / '.join(names)} …")
            prev = [False] * len(triggers)
            end = time.perf_counter() + 10
            while time.perf_counter() < end:
                gp = read(slots[0]) or XINPUT_GAMEPAD()
                for i, c in enumerate(triggers):
                    act = all(token_down(t, gp) for t in c)
                    if act and not prev[i]:
                        counts[names[i]] += 1
                        print(f"  识别到 {names[i]} (第 {counts[names[i]]} 次)")
                    prev[i] = act
                time.sleep(0.005)
        for n in names:
            lines.append(f"起手组合 {n}: 识别 {counts[n]} 次")
    text = "\n".join(lines)
    (APP_DIR / "diag.txt").write_text(text, encoding="utf-8")
    notify(text)


if __name__ == "__main__":
    if "--snap" in sys.argv:
        snap_full()
    elif "--crop" in sys.argv:
        snap_crop()
    elif "--diag" in sys.argv:
        diag()
    else:
        gui()
