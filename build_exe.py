# -*- coding: utf-8 -*-
"""打包 sf6_hit_assist.py 为单文件 exe(PyInstaller)。

用法: python build_exe.py    或直接双击 build_exe.bat
产物: dist/SF6HitAssist.exe — 拷到任意目录双击即用,首次运行自动生成默认 config.json。
"""
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
NAME = "SF6HitAssist"


def vgamepad_args():
    """vgamepad 的 ViGEmClient.dll 是 ctypes 加载的,PyInstaller 不会自动收集。
    没装 vgamepad 也能打包,只是 exe 里自动绿冲不可用。"""
    if importlib.util.find_spec("vgamepad") is None:
        print("提示: 未安装 vgamepad,exe 将不支持自动绿冲(pip install vgamepad 后重新打包)。")
        return []
    return ["--collect-all", "vgamepad"]


def main():
    print("=== SF6 命中确认辅助 - 打包 exe ===\n")

    if importlib.util.find_spec("PyInstaller") is None:
        print("未找到 PyInstaller,正在安装...")
        r = subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller"])
        if r.returncode != 0:
            print("安装失败,请手动执行: pip install pyinstaller")
            return 1

    r = subprocess.run([
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", NAME,
        *vgamepad_args(),
        str(HERE / "sf6_hit_assist.py"),
    ], cwd=HERE)
    if r.returncode != 0:
        print("\n打包失败,请查看上方报错。")
        return 1

    exe = HERE / "dist" / f"{NAME}.exe"
    # 把当前调好的配置一起放进 dist;exe 没带配置时首次运行也会自动生成默认值
    cfg = HERE / "config.json"
    if cfg.exists():
        shutil.copy2(cfg, exe.parent / "config.json")

    size_mb = exe.stat().st_size / 1024 / 1024
    print("\n============================================")
    print(f"打包完成: {exe} ({size_mb:.0f} MB)")
    print("把 exe(连同 config.json)拷到任意目录双击即用。")
    print("自动绿冲需要目标机器装 ViGEmBus 驱动(装一次即可)。")
    print(f"辅助命令: {NAME}.exe --snap / --crop / --diag")
    print("============================================")
    return 0


if __name__ == "__main__":
    sys.exit(main())
