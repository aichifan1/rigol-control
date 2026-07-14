#!/usr/bin/env python3
"""
USB TMC 设备诊断脚本
排查为什么设备管理器能看到设备但 pyvisa-py 扫不到
"""
import sys
print(f"Python: {sys.executable}")
print(f"版本: {sys.version}")
print()

# ── 1. 检查 pyvisa-py 看到什么 ──
print("=" * 60)
print("1. pyvisa-py 后端扫描")
print("=" * 60)
try:
    import pyvisa
    print(f"pyvisa 版本: {pyvisa.__version__}")
except ImportError:
    print("pyvisa 未安装!")
    sys.exit(1)

# 用 @py 后端（当前代码使用的方式）
rm_py = pyvisa.ResourceManager("@py")
all_py = rm_py.list_resources()
print(f"\n@py 后端 — 所有资源 ({len(all_py)}):")
for r in all_py:
    print(f"  {r}")

usb_py = rm_py.list_resources("?*::USB?*::INSTR")
print(f"\n@py 后端 — USB TMC 资源 ({len(usb_py)}):")
for r in usb_py:
    print(f"  {r}")
rm_py.close()

# ── 2. 检查系统是否有 NI-VISA 后端可用 ──
print("\n" + "=" * 60)
print("2. 默认后端（可能使用 NI-VISA）")
print("=" * 60)
try:
    rm_default = pyvisa.ResourceManager()  # 不带 @py，使用默认后端
    all_default = rm_default.list_resources()
    print(f"默认后端 — 所有资源 ({len(all_default)}):")
    for r in all_default:
        print(f"  {r}")
    rm_default.close()
except Exception as e:
    print(f"默认后端不可用: {e}")
    print("→ NI-VISA 未安装或未配置（这很正常）")

# ── 3. PyUSB 直接扫描 ──
print("\n" + "=" * 60)
print("3. PyUSB 直接扫描 (libusb)")
print("=" * 60)
try:
    import usb.core
    import usb.backend.libusb1

    # 查找所有 USB 设备
    devices = usb.core.find(find_all=True)
    found = []
    for dev in devices:
        found.append(dev)
        try:
            manufacturer = usb.util.get_string(dev, dev.iManufacturer) if dev.iManufacturer else "?"
        except Exception:
            manufacturer = "?"
        try:
            product = usb.util.get_string(dev, dev.iProduct) if dev.iProduct else "?"
        except Exception:
            product = "?"

        is_rigol = "rigol" in manufacturer.lower() or "rigol" in product.lower()
        is_tmc = dev.bDeviceClass == 0xFE or any(
            cfg.bInterfaceClass == 0xFE for cfg in dev
        )

        marker = " ← RIGOL/TMC" if (is_rigol or is_tmc) else ""
        print(f"  VID:0x{dev.idVendor:04X} PID:0x{dev.idProduct:04X} "
              f" mfr=\"{manufacturer}\" prod=\"{product}\"{marker}")

        # 对 TMC 类设备，尝试构造 VISA 资源字符串
        if is_rigol:
            sn = "?"
            try:
                sn = usb.util.get_string(dev, dev.iSerialNumber) if dev.iSerialNumber else "?"
            except Exception:
                pass
            visa_str = f"USB0::0x{dev.idVendor:04X}::0x{dev.idProduct:04X}::{sn}::INSTR"
            print(f"        → VISA: {visa_str}")

    if not found:
        print("  (未发现任何 USB 设备 — libusb 后端可能有问题)")
except ImportError:
    print("PyUSB 未安装。运行: pip install pyusb")
except Exception as e:
    print(f"PyUSB 扫描出错: {e}")
    print("→ 这通常说明 IVI 驱动占用了设备，libusb 无法访问")
    print("→ 需要用 Zadig 将驱动替换为 WinUSB")

# ── 4. 检查 libusb 后端 ──
print("\n" + "=" * 60)
print("4. libusb 后端信息")
print("=" * 60)
try:
    import usb.backend.libusb1
    backend = usb.backend.libusb1.get_backend()
    if backend:
        print(f"libusb1 后端: {backend}")
        print(f"后端库路径: {backend.lib}")
    else:
        print("libusb1 后端未找到!")
        print("→ 需要 libusb DLL，通常 pyvisa-py 会自动带")
except Exception as e:
    print(f"获取后端信息失败: {e}")

# ── 5. 小结 ──
print("\n" + "=" * 60)
print("5. 诊断总结")
print("=" * 60)
if usb_py:
    print("✅ pyvisa-py 能扫描到 USB TMC 设备 — 应该可以正常使用")
    print("   如果程序仍然连不上，可能是设备被其他程序占用")
elif all_py:
    print("⚠️  pyvisa-py 能看到非 USB 资源但看不到 USB TMC 设备")
    print("   → 确认设备已开机、USB 线已连接")
elif all_default:
    print("✅ 默认后端(NI-VISA)能扫描到设备")
    print("   → 修改代码去掉 @py，改用默认后端即可")
else:
    print("❌ 两个后端都扫不到设备")
    print("   → 用 Zadig (https://zadig.akeo.ie/) 将 IVI 驱动替换为 WinUSB")
    print("   → 操作: Options → List All Devices → 选 RIGOL → 选 WinUSB → Replace Driver")
    print("   → 对两个 Port 位置的设备都要操作")
    print()
    print("   或者: 安装 NI-VISA 运行时，然后去掉代码中的 @py")
