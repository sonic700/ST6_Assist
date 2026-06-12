# -*- coding: utf-8 -*-
"""
街霸6 命中确认辅助练习器 (GUI 版)
--------------------------------
原理:监听手柄起手输入(如 下+A / RT+B),按出起手后的短窗口内盯着
"对手血条掉血端"小区域与基线对比 —— 掉血=命中 → 蜂鸣提示(可选:自动模拟绿冲),
随后冷却 5 秒不再触发,留给你接连段;不掉血=被防/挥空,无动静。
同时盯对方斗气槽:掉血的同时斗气也突降 = 对面在迸发(DI 霸体吸了你的招)
→ 低音双响提示,该反迸而不是接连段(此时也不会注入自动绿冲)。

仅供训练模式离线练习使用!在线对战使用自动输入违反 Capcom 用户协议。

全局热键(游戏内生效):
  F9 : 采集基线    F8 : 启停检测    F7 : 3秒后试放绿冲    F12: 退出
手柄热键(按住 视图键/View 再按):
  View+A: 采基线   View+B: 启停检测

自动绿冲(可选,默认关):
  通过 ViGEm 虚拟 Xbox 360 手柄注入"前前"。物理手柄信号全程透传到虚拟手柄,
  游戏里把 P1 绑到虚拟手柄即可。需要 ViGEmBus 驱动(pip install vgamepad 附带)。

区域定位:界面里点「框选对手血条 / 框选对手斗气」,在冻结的全屏截图上拖个框即可,
自动写回 config.json(也可手填坐标,--snap/--crop 仍可用于核对)。

命令行:
  python sf6_hit_assist.py          打开界面
  python sf6_hit_assist.py --snap   保存全屏截图(找区域坐标用)
  python sf6_hit_assist.py --crop   保存检测区域的截图(验证用)
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


def process_image_name(pid):
    """进程名,如 StreetFighter6.exe(取不到返回空串)"""
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
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


def foreground_process():
    """前台窗口的进程名"""
    hwnd = user32.GetForegroundWindow()
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return process_image_name(pid.value)


def find_window_by_process(proc_name):
    """按进程名(小写)找可见的主窗口,返回 hwnd 或 None"""
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if process_image_name(pid.value).lower() == proc_name:
                found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else None


def client_rect(hwnd):
    """窗口客户区的屏幕位置 (left, top, width, height);窗口无效/最小化返回 None"""
    if not user32.IsWindow(hwnd):
        return None
    rc = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rc)) or rc.right <= 0 or rc.bottom <= 0:
        return None
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return (pt.x, pt.y, rc.right, rc.bottom)


class GameWindow:
    """启动即按进程名绑定游戏窗口,持续跟踪客户区(移动/改分辨率自动跟随,丢了自动重找)。"""

    def __init__(self, proc_name, log=print):
        self._proc = (proc_name or "").lower()
        self._log = log
        self.hwnd = None
        self._next_scan = 0.0

    def rect(self):
        if not self._proc:
            return None
        if self.hwnd:
            r = client_rect(self.hwnd)
            if r:
                return r
            self.hwnd = None
            self._log("游戏窗口丢失(关闭/最小化),等待重新出现…")
        now = time.perf_counter()
        if now < self._next_scan:
            return None
        self._next_scan = now + 2.0
        self.hwnd = find_window_by_process(self._proc)
        if self.hwnd:
            r = client_rect(self.hwnd)
            if r:
                self._log(f"已绑定游戏窗口: {self._proc}(画面 {r[2]}x{r[3]} @ {r[0]},{r[1]})")
                return r
            self.hwnd = None
        return None


def key_pressed(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def sleep_until(t):
    while True:
        rem = t - time.perf_counter()
        if rem <= 0:
            return
        if rem > 0.002:
            time.sleep(rem - 0.002)


def beep_async(freq, ms, times=1, gap_ms=40):
    def run():
        for i in range(times):
            if i:
                time.sleep(gap_ms / 1000)
            winsound.Beep(freq, ms)
    threading.Thread(target=run, daemon=True).start()


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


CAPTURE_DIRS = ("up", "down", "left", "right")
CAPTURE_KEYS = ("lt", "rt", "a", "b", "x", "y", "lb", "rb", "l3", "r3")  # 录制时认的"主键"


class PadBridge:
    """监听物理手柄:起手组合 → ("attack", 组合名);View+A/B → ("hotkey", ...)。
    若 ViGEm 可用,同时创建虚拟手柄做 500Hz 透传,命中时可叠加注入绿冲。
    capq: 录制模式的结果队列(组合 list 或超时 None),由界面消费。"""

    def __init__(self, eventq, capq, log, cfg):
        self._xi = _load_xinput()
        if self._xi is None:
            raise RuntimeError("系统里找不到 XInput DLL")
        self._eventq = eventq
        self._capq = capq
        self._log = log
        self._hotkeys = bool(cfg.get("pad_hotkeys", True))
        self._lock = threading.Lock()
        self.set_triggers(cfg.get("attack_triggers", [["down", "a"], ["rt", "b"]]))
        self._prev_buttons = 0
        self._cap_until = 0.0   # >0 = 录制模式截止时间
        self._cap_prev = {}
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

    def set_triggers(self, triggers):
        """更新起手组合(界面录制/删除后实时生效)"""
        clean = [[str(t).lower() for t in c] for c in triggers]
        for c in clean:
            for t in c:
                if t not in TOKEN_NAMES:
                    raise ValueError(f"attack_triggers 里的键 '{t}' 不认识。"
                                     f"可用: {' '.join(sorted(TOKEN_NAMES))}")
        with self._lock:
            self.triggers = clean
            self.trigger_text = " / ".join("+".join(c) for c in clean) or "(未设置)"
            self._prev_act = [False] * len(clean)

    def start_capture(self, seconds=4.0):
        """进入录制模式:窗口内首个主键按下的瞬间,记下当时按住的整组键 → capq"""
        self._cap_prev = {k: True for k in CAPTURE_KEYS}  # 防止已按住的键立刻触发
        self._cap_until = time.perf_counter() + seconds

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

            if self._cap_until:
                # 录制模式:首个主键按下的瞬间,记录当时按住的整组键
                if now > self._cap_until:
                    self._cap_until = 0.0
                    self._capq.put(None)
                else:
                    for key in CAPTURE_KEYS:
                        down = token_down(key, gp)
                        if down and not self._cap_prev[key]:
                            combo = ([d for d in CAPTURE_DIRS if token_down(d, gp)]
                                     + [k for k in CAPTURE_KEYS if token_down(k, gp)])
                            self._cap_until = 0.0
                            self._capq.put(combo)
                            break
                        self._cap_prev[key] = down
            else:
                # 起手组合(整组同时按下的瞬间触发;View 按住时不算,避免和热键撞)
                with self._lock:
                    trigs, prev_act = self.triggers, self._prev_act
                for i, combo in enumerate(trigs):
                    act = (not view_held) and all(token_down(t, gp) for t in combo)
                    if act and not prev_act[i]:
                        self._eventq.put(("attack", "+".join(combo)))
                    prev_act[i] = act
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
    "_说明_calib_client": "框选时游戏画面(客户区)的尺寸。region_* 都是相对游戏窗口的坐标,运行时按当前窗口尺寸自动缩放;删掉此项则按屏幕绝对坐标解释(旧模式)。",
    "calib_client": [1920, 1080],
    "_说明_region": "血条检测区域(游戏窗口内坐标,用界面的框选按钮生成)。region_p2_bar=2P血条掉血端(我是1P时监控它), region_p1_bar=1P血条掉血端(我是2P时监控它)。下面是 1920x1080 画面的参考值,务必自己框一遍!",
    "region_p2_bar": {"left": 1060, "top": 52, "width": 180, "height": 22},
    "region_p1_bar": {"left": 680, "top": 52, "width": 180, "height": 22},
    "_说明_region_drive": "对方斗气槽检测区域(血条下方那排绿色格子的掉气端)。掉血同时掉斗气 = 对面迸发 → 双响低音提示(该反迸,不是接连段)。坐标同样用 --snap 找、--crop 验证。",
    "region_p2_drive": {"left": 1480, "top": 84, "width": 120, "height": 10},
    "region_p1_drive": {"left": 320, "top": 84, "width": 120, "height": 10},
    "_说明_threshold": "血条触发阈值。界面上看实时像素差:打中一下记峰值,被防一下记峰值,取中间。界面里调好后点「保存设置」。",
    "threshold": 10.0,
    "_说明_drive_threshold": "斗气突降判定阈值(迸发瞬间掉 1 格,像素差很大)。普通命中如果也会小掉斗气,把阈值调到两者之间。",
    "drive_threshold": 10.0,
    "_说明_fps": "检测频率(每秒采样次数),120 足够,延迟约 1 帧。",
    "fps": 120,
    "_说明_window_process": "只有前台窗口的进程名等于它(或标题包含 window_title)才会触发,防止切出游戏误响。两项都留空 \"\" 则不检查。",
    "window_process": "StreetFighter6.exe",
    "window_title": "Street Fighter 6",
    "sound": {"freq": 1400, "ms": 90},
    "_说明_sound_di": "检测到对面迸发时的提示音(低频双响,和命中音区分开)。",
    "sound_di": {"freq": 600, "ms": 120, "times": 2},
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
        self.drive_threshold = float(cfg.get("drive_threshold", 10.0))
        self.diff_drive = 0.0
        self.sound_on = True
        self.auto_dash = bool(cfg.get("auto_dash", False))
        self.armed = False
        self.quit = False
        self.req_baseline = False
        self.req_test = False
        self.req_invalidate = False  # 区域改了 → 基线作废
        self.diff = 0.0
        self.status = "未采基线"
        self.has_baseline = False
        self.preview = None        # 血条区域 (w, h, rgb bytes)
        self.preview_dr = None     # 斗气区域 (w, h, rgb bytes)
        self.logq = queue.Queue()
        self.padq = queue.Queue()  # 手柄事件 ("hotkey"/"attack", arg)
        self.capq = queue.Queue()  # 起手组合录制结果(list 或超时 None)

    def log(self, msg):
        self.logq.put(time.strftime("[%H:%M:%S] ") + msg)

    def region(self):
        key = "region_p2_bar" if self.side == "1P" else "region_p1_bar"
        return self.cfg[key]

    def region_drive(self):
        key = "region_p2_drive" if self.side == "1P" else "region_p1_drive"
        return self.cfg.get(key)  # 没配就不做迸发判定


# ---------------- 检测线程 ----------------
def worker(st: State, bridge, gw):
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
    snd_di = cfg.get("sound_di", {"freq": 600, "ms": 120, "times": 2})
    combo = cfg.get("combo", [])
    period = 1.0 / float(cfg.get("fps", 120))
    pad_mode = cfg.get("trigger_mode", "pad") == "pad" and bridge is not None

    baseline = None
    baseline_dr = None
    need_rearm = False
    rearm_ok = 0
    cooldown_until = 0.0
    confirm_until = 0.0    # >0 = 确认窗口截止时间
    last_side = st.side
    last_preview = 0.0
    last_client = None     # 游戏画面尺寸,变了基线作废
    prev_hk = {0x76: False, 0x77: False, 0x78: False, 0x7B: False}  # F7 F8 F9 F12

    with mss.mss() as sct:
        next_tick = time.perf_counter()
        while not st.quit:
            now = time.perf_counter()

            # 区域被重新框选 → 基线作废
            if st.req_invalidate:
                st.req_invalidate = False
                baseline = None
                baseline_dr = None
                st.has_baseline = False

            # 选边变了 → 基线作废
            if st.side != last_side:
                last_side = st.side
                baseline = None
                baseline_dr = None
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

            # ---- 游戏窗口跟踪 ----
            calib = cfg.get("calib_client")
            rect = gw.rect() if gw else None
            if calib and rect is None:
                st.status = "未找到游戏窗口(StreetFighter6.exe)"
                st.diff = 0.0
                next_tick = time.perf_counter() + 0.25  # 没窗口,降频等待
                sleep_until(next_tick)
                continue
            if rect:
                if last_client != (rect[2], rect[3]):
                    if last_client is not None:
                        baseline = None
                        baseline_dr = None
                        st.has_baseline = False
                        st.log(f"游戏画面尺寸变为 {rect[2]}x{rect[3]},基线作废,请重新采集(F9)。")
                    last_client = (rect[2], rect[3])

            def to_abs(rel):
                """窗口内坐标 → 屏幕坐标(按当前画面尺寸缩放)"""
                if not calib or rect is None:
                    return rel  # 旧模式:屏幕绝对坐标
                sx, sy = rect[2] / calib[0], rect[3] / calib[1]
                return {"left": rect[0] + int(rel["left"] * sx),
                        "top": rect[1] + int(rel["top"] * sy),
                        "width": max(1, int(rel["width"] * sx)),
                        "height": max(1, int(rel["height"] * sy))}

            # ---- 截屏(血条 + 对方斗气槽) ----
            r = to_abs(st.region())
            raw = sct.grab({"left": r["left"], "top": r["top"],
                            "width": r["width"], "height": r["height"]})
            arr = np.frombuffer(raw.rgb, dtype=np.uint8).reshape(raw.height, raw.width, 3)
            gray = arr.astype(np.float32).mean(axis=2)

            rd = st.region_drive()
            rd = to_abs(rd) if rd else None
            gray_dr = None
            if rd:
                raw_dr = sct.grab({"left": rd["left"], "top": rd["top"],
                                   "width": rd["width"], "height": rd["height"]})
                arr_dr = np.frombuffer(raw_dr.rgb, dtype=np.uint8).reshape(
                    raw_dr.height, raw_dr.width, 3)
                gray_dr = arr_dr.astype(np.float32).mean(axis=2)

            if now - last_preview > 0.2:
                st.preview = (raw.width, raw.height, raw.rgb)
                if rd:
                    st.preview_dr = (raw_dr.width, raw_dr.height, raw_dr.rgb)
                last_preview = now

            if st.req_baseline:
                st.req_baseline = False
                baseline = gray
                baseline_dr = gray_dr
                st.has_baseline = True
                need_rearm = False
                confirm_until = 0.0
                st.log("基线已采集(血条 + 斗气槽)" if rd else "基线已采集(未配置斗气区域,不做迸发判定)")

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
                if baseline_dr is not None and gray_dr is not None:
                    st.diff_drive = float(np.abs(gray_dr - baseline_dr).mean())

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
                    st.status = ("确认中…" if pad_mode and in_window
                                 else f"等待起手 {bridge.trigger_text}" if pad_mode
                                 else "监听中")
                    if in_window and diff > st.threshold and now >= cooldown_until:
                        if fg_ok():
                            # 掉血同时掉斗气 = 对面在迸发(迸发瞬间扣1格,先于霸体吃招)
                            di = (baseline_dr is not None
                                  and st.diff_drive > st.drive_threshold)
                            if di:
                                st.log(f"⚠ 对面迸发! diff={diff:.1f} 斗气差={st.diff_drive:.1f}"
                                       " → 低音双响,反迸!(不接连段)")
                                if st.sound_on:
                                    beep_async(int(snd_di.get("freq", 600)),
                                               int(snd_di.get("ms", 120)),
                                               int(snd_di.get("times", 2)))
                            else:
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

    gw = GameWindow(cfg.get("window_process", "StreetFighter6.exe"), st.log)
    if gw.rect() is None:
        st.log(f"暂未找到 {cfg.get('window_process', 'StreetFighter6.exe')},"
               "启动游戏后会自动绑定。")

    bridge = None
    try:
        bridge = PadBridge(st.padq, st.capq, st.log, cfg)
    except Exception as e:
        st.log(f"手柄监听不可用: {e}")
        st.log("将回退为常开检测(F8 开关),热键用键盘 F 键。")
    # 界面编辑的就是 cfg 里这份列表,「保存设置」时一起写回
    cfg["attack_triggers"] = [list(c) for c in (
        bridge.triggers if bridge
        else cfg.get("attack_triggers", [["down", "a"], ["rt", "b"]]))]

    root = tk.Tk()
    root.title("SF6 命中确认辅助 · 训练模式专用")
    root.geometry("460x870")
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

    dr_row = tk.Frame(frm_st, bg="#14121a")
    dr_row.pack(fill="x", padx=10, pady=2)
    tk.Label(dr_row, text="斗气差", bg="#14121a", fg="#8a849c").pack(side="left")
    dr_canvas = tk.Canvas(dr_row, height=16, bg="#262236", highlightthickness=0)
    dr_canvas.pack(side="left", fill="x", expand=True, padx=8)
    dr_val = tk.Label(dr_row, text="0.0", width=6, bg="#14121a", fg="#e8e4f0",
                      font=("Consolas", 11))
    dr_val.pack(side="left")

    dthr_row = tk.Frame(frm_st, bg="#14121a")
    dthr_row.pack(fill="x", padx=10, pady=(2, 8))
    tk.Label(dthr_row, text="斗气阈值", bg="#14121a", fg="#8a849c").pack(side="left")
    dthr_var = tk.DoubleVar(value=st.drive_threshold)
    dthr_lbl = tk.Label(dthr_row, text=f"{st.drive_threshold:.0f}", width=4,
                        bg="#14121a", fg="#ffb020", font=("Consolas", 11))

    def on_dthr(v):
        st.drive_threshold = float(v)
        dthr_lbl.config(text=f"{st.drive_threshold:.0f}")

    ttk.Scale(dthr_row, from_=1, to=60, variable=dthr_var, command=on_dthr)\
        .pack(side="left", fill="x", expand=True, padx=8)
    dthr_lbl.pack(side="left")

    # --- 起手输入 ---
    frm_trig = ttk.Labelframe(root, text=" 起手输入(整组按下才开确认窗口) ")
    frm_trig.pack(fill="x", **pad)
    trig_row = tk.Frame(frm_trig, bg="#14121a")
    trig_row.pack(fill="x", padx=10, pady=(4, 6))
    trig_list = tk.Listbox(trig_row, height=3, bg="#1e1b28", fg="#e8e4f0",
                           font=("Consolas", 11), relief="flat",
                           selectbackground="#36304a", highlightthickness=0,
                           exportselection=False)
    trig_list.pack(side="left", fill="x", expand=True)
    trig_btns = tk.Frame(trig_row, bg="#14121a")
    trig_btns.pack(side="left", padx=(8, 0))

    def refresh_triggers():
        trig_list.delete(0, "end")
        for c in cfg.get("attack_triggers", []):
            trig_list.insert("end", " + ".join(c))

    def record_trigger():
        if not bridge:
            st.log("手柄监听不可用,请直接改 config.json 的 attack_triggers。")
            return
        bridge.start_capture(4.0)
        st.log("录制中:4 秒内在手柄上按住你的起手组合(先按住方向/扳机,再按攻击键)…")

    def delete_trigger():
        sel = trig_list.curselection()
        if not sel:
            st.log("先在列表里选中要删除的组合。")
            return
        removed = cfg["attack_triggers"].pop(sel[0])
        if bridge:
            bridge.set_triggers(cfg["attack_triggers"])
        refresh_triggers()
        st.log(f"已删除起手组合: {'+'.join(removed)}(记得点「保存设置」)")

    ttk.Button(trig_btns, text="手柄录制", command=record_trigger).pack(fill="x", pady=(0, 4))
    ttk.Button(trig_btns, text="删除选中", command=delete_trigger).pack(fill="x")
    refresh_triggers()

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
        cfg["drive_threshold"] = round(st.drive_threshold, 1)
        cfg["auto_dash"] = st.auto_dash
        save_config(cfg)
        st.log("设置已保存到 config.json")

    ttk.Button(frm_btn2, text="保存设置", command=do_save).pack(side="right")

    # --- 区域预览 + 框选校准 ---
    frm_pv = ttk.Labelframe(root, text=" 检测区域实时画面(上:对手血条掉血端 下:对手斗气槽) ")
    frm_pv.pack(fill="x", **pad)
    pv_label = tk.Label(frm_pv, bg="#262236")
    pv_label.pack(padx=10, pady=(6, 2))
    pv_dr_label = tk.Label(frm_pv, bg="#262236")
    pv_dr_label.pack(padx=10, pady=(0, 4))
    pv_img_ref = [None, None]

    def select_region(key, title):
        """冻结游戏画面截图,拖动框选一个区域,以窗口内坐标写回 cfg[key] 并存盘。"""
        rect = gw.rect()
        if rect is None:
            st.log("未找到游戏窗口,无法框选——先启动街霸6(无边框窗口/窗口模式)。")
            return
        gx, gy, gwidth, gheight = rect
        with mss.mss() as sct:
            shot = sct.grab({"left": gx, "top": gy, "width": gwidth, "height": gheight})
        ov = tk.Toplevel(root)
        ov.overrideredirect(True)
        ov.geometry(f"{shot.width}x{shot.height}+{gx}+{gy}")
        ov.attributes("-topmost", True)
        img = tk.PhotoImage(data=f"P6 {shot.width} {shot.height} 255 ".encode() + shot.rgb)
        ov._img = img  # 防回收
        cv = tk.Canvas(ov, width=shot.width, height=shot.height,
                       highlightthickness=0, cursor="crosshair")
        cv.pack()
        cv.create_image(0, 0, anchor="nw", image=img)
        cv.create_text(shot.width // 2, 40, text=f"拖动框选: {title}  (Esc 取消)",
                       fill="#ff5c8a", font=("Microsoft YaHei", 16, "bold"))
        sel = {"x": 0, "y": 0, "rect": None}

        def press(e):
            sel["x"], sel["y"] = e.x, e.y
            sel["rect"] = cv.create_rectangle(e.x, e.y, e.x, e.y,
                                              outline="#ff5c8a", width=2)

        def move(e):
            if sel["rect"]:
                cv.coords(sel["rect"], sel["x"], sel["y"], e.x, e.y)

        def release(e):
            x1, y1 = min(sel["x"], e.x), min(sel["y"], e.y)
            w, h = abs(e.x - sel["x"]), abs(e.y - sel["y"])
            ov.destroy()
            if w < 5 or h < 3:
                st.log("框太小,已取消。")
                return
            # 存窗口内坐标 + 当前画面尺寸,窗口移动/改分辨率自动跟随
            cfg[key] = {"left": x1, "top": y1, "width": w, "height": h}
            cfg["calib_client"] = [gwidth, gheight]
            save_config(cfg)
            st.req_invalidate = True
            st.log(f"{title} 已更新并存盘: {w}x{h} @ 窗口内({x1},{y1})。请重新采基线(F9)。")

        cv.bind("<ButtonPress-1>", press)
        cv.bind("<B1-Motion>", move)
        cv.bind("<ButtonRelease-1>", release)
        for wgt in (ov, cv):
            wgt.bind("<Escape>", lambda e: ov.destroy())
        ov.focus_force()

    pv_btns = tk.Frame(frm_pv, bg="#14121a")
    pv_btns.pack(fill="x", padx=10, pady=(0, 6))
    for i, (text, key, title) in enumerate((
            ("框选 1P血条", "region_p1_bar", "1P 血条掉血端"),
            ("框选 2P血条", "region_p2_bar", "2P 血条掉血端"),
            ("框选 1P斗气", "region_p1_drive", "1P 斗气槽掉气端"),
            ("框选 2P斗气", "region_p2_drive", "2P 斗气槽掉气端"))):
        ttk.Button(pv_btns, text=text,
                   command=lambda k=key, t=title: select_region(k, t))\
            .pack(side="left", expand=True, fill="x",
                  padx=(0 if i == 0 else 3, 0))

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
        # 录制结果
        try:
            while True:
                combo = st.capq.get_nowait()
                if combo is None:
                    st.log("录制超时,没按出组合。再点一次「手柄录制」重试。")
                elif combo in cfg["attack_triggers"]:
                    st.log(f"组合 {'+'.join(combo)} 已存在,跳过。")
                else:
                    cfg["attack_triggers"].append(combo)
                    if bridge:
                        bridge.set_triggers(cfg["attack_triggers"])
                    refresh_triggers()
                    st.log(f"已录制起手组合: {'+'.join(combo)}(记得点「保存设置」)")
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
        # 斗气差条
        w = dr_canvas.winfo_width() or 200
        dr_canvas.delete("all")
        frac = min(1.0, st.diff_drive / 60.0)
        color = "#ff4d4d" if st.diff_drive > st.drive_threshold else "#3ddc84"
        dr_canvas.create_rectangle(0, 0, int(w * frac), 16, fill=color, width=0)
        tx = int(w * min(1.0, st.drive_threshold / 60.0))
        dr_canvas.create_line(tx, 0, tx, 16, fill="#ffb020", width=2)
        dr_val.config(text=f"{st.diff_drive:.1f}")
        # 预览
        for i, (pv, lbl) in enumerate(((st.preview, pv_label),
                                       (st.preview_dr, pv_dr_label))):
            if not pv:
                continue
            pw, ph, rgb = pv
            try:
                img = tk.PhotoImage(data=f"P6 {pw} {ph} 255 ".encode() + rgb)
                z = max(1, min(3, 420 // pw))
                if z > 1:
                    img = img.zoom(z, z)
                pv_img_ref[i] = img
                lbl.config(image=img)
            except Exception:
                pass
        root.after(50, tick)

    def on_close():
        st.quit = True
        if bridge:
            bridge.close()
        root.after(100, root.destroy)

    root.protocol("WM_DELETE_WINDOW", on_close)
    threading.Thread(target=worker, args=(st, bridge, gw), daemon=True).start()
    root.after(50, tick)
    root.mainloop()


# ---------------- 截图辅助 ----------------
def _game_rect(cfg):
    gw = GameWindow(cfg.get("window_process", "StreetFighter6.exe"))
    return gw.rect()


def snap_full():
    cfg = load_config()
    rect = _game_rect(cfg)
    with mss.mss() as sct:
        if rect:
            img = sct.grab({"left": rect[0], "top": rect[1],
                            "width": rect[2], "height": rect[3]})
            src = f"游戏窗口画面({rect[2]}x{rect[3]})"
        else:
            img = sct.grab(sct.monitors[1])
            src = "全屏(未找到游戏窗口)"
        out = APP_DIR / "screenshot_full.png"
        mss.tools.to_png(img.rgb, img.size, output=str(out))
        notify(f"已保存{src}截图: {out}\n推荐直接用界面里的框选按钮定位区域。")


def snap_crop():
    cfg = load_config()
    calib = cfg.get("calib_client")
    rect = _game_rect(cfg)
    if calib and rect is None:
        notify("未找到游戏窗口,无法截取检测区域(区域坐标是相对游戏窗口的)。")
        return
    lines = []
    with mss.mss() as sct:
        for name in ("region_p1_bar", "region_p2_bar",
                     "region_p1_drive", "region_p2_drive"):
            r = cfg.get(name)
            if not r:
                continue
            if calib and rect:
                sx, sy = rect[2] / calib[0], rect[3] / calib[1]
                r = {"left": rect[0] + int(r["left"] * sx),
                     "top": rect[1] + int(r["top"] * sy),
                     "width": max(1, int(r["width"] * sx)),
                     "height": max(1, int(r["height"] * sy))}
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
