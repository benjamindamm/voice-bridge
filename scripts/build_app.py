"""Baut eine startbare macOS-App: ~/Applications/VoiceBridge.app.

Die App ist ein schlanker Launcher auf die vorhandene venv und das Repo;
sie bringt ein Icon und die Mikrofon-Nutzungsbeschreibung mit.
"""

from __future__ import annotations

import argparse
import plistlib
import shutil
import struct
import subprocess
import tempfile
import zlib
from pathlib import Path


def write_png(path: Path, rgba) -> None:
    height, width, _ = rgba.shape
    raw = b"".join(b"\x00" + rgba[row].tobytes() for row in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def make_master_icon(path: Path, size: int = 1024) -> None:
    import numpy as np

    yy, xx = np.mgrid[0:size, 0:size]
    canvas = np.zeros((size, size, 4), dtype=np.uint8)

    margin = 80
    radius = 200
    half = (size - 2 * margin) / 2.0
    dx = np.maximum(np.abs(xx - size / 2.0) - (half - radius), 0)
    dy = np.maximum(np.abs(yy - size / 2.0) - (half - radius), 0)
    mask = dx * dx + dy * dy <= radius * radius

    # dezenter Verlauf von oben nach unten
    top = np.array([44, 48, 72], dtype=np.float64)
    bottom = np.array([20, 21, 34], dtype=np.float64)
    gradient = top + (bottom - top) * (yy[..., None] / float(size))
    canvas[mask] = np.concatenate(
        [gradient[mask], np.full((int(mask.sum()), 1), 255.0)], axis=1
    ).astype(np.uint8)

    bars = (0.36, 0.56, 0.80, 1.0, 0.80, 0.56, 0.36)
    bar_width = 52
    gap = 24
    total = len(bars) * bar_width + (len(bars) - 1) * gap
    x = (size - total) // 2
    center = size // 2
    max_height = 380
    color = np.array([150, 182, 255, 255], dtype=np.uint8)

    for factor in bars:
        height = int(max_height * factor)
        top_y = center - height // 2
        bottom_y = center + height // 2
        canvas[top_y:bottom_y, x:x + bar_width] = color

        cap_radius = bar_width // 2
        for cap_y in (top_y, bottom_y):
            cx, cy = x + cap_radius, cap_y
            y0, y1 = max(0, cy - cap_radius), min(size, cy + cap_radius + 1)
            x0, x1 = max(0, cx - cap_radius), min(size, cx + cap_radius + 1)
            yy2, xx2 = np.mgrid[y0:y1, x0:x1]
            cap = (xx2 - cx) ** 2 + (yy2 - cy) ** 2 <= cap_radius * cap_radius
            region = canvas[y0:y1, x0:x1]
            region[cap] = color
            canvas[y0:y1, x0:x1] = region

        x += bar_width + gap

    write_png(path, canvas)


def make_icns(resources: Path) -> bool:
    sips = shutil.which("sips")
    iconutil = shutil.which("iconutil")
    if not sips or not iconutil:
        return False

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        master = tmp_path / "master.png"
        make_master_icon(master)

        iconset = tmp_path / "VoiceBridge.iconset"
        iconset.mkdir()
        sizes = (
            (16, "icon_16x16.png"),
            (32, "icon_16x16@2x.png"),
            (32, "icon_32x32.png"),
            (64, "icon_32x32@2x.png"),
            (128, "icon_128x128.png"),
            (256, "icon_128x128@2x.png"),
            (256, "icon_256x256.png"),
            (512, "icon_256x256@2x.png"),
            (512, "icon_512x512.png"),
            (1024, "icon_512x512@2x.png"),
        )
        for pixels, name in sizes:
            subprocess.run(
                [sips, "-z", str(pixels), str(pixels), str(master), "--out", str(iconset / name)],
                check=True,
                capture_output=True,
            )

        target = resources / "VoiceBridge.icns"
        try:
            subprocess.run(
                [iconutil, "-c", "icns", str(iconset), "-o", str(target)],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError:
            return False
    return target.is_file()


def write_shell_launcher(launcher: Path, repo: Path, venv: Path) -> None:
    launcher.write_text(
        "#!/bin/bash\n"
        'export PATH="$HOME/.opencode/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"\n'
        'mkdir -p "$HOME/Library/Logs"\n'
        'cd "{repo}" || exit 1\n'
        'exec /usr/bin/arch -arm64 "{python}" -m voicebridge --menubar >> "$HOME/Library/Logs/VoiceBridge.log" 2>&1\n'.format(
            repo=repo, python=venv / "bin" / "python"
        ),
        encoding="utf-8",
    )


def write_swift_launcher(launcher: Path, repo: Path, venv: Path) -> bool:
    swiftc = shutil.which("swiftc")
    if not swiftc:
        return False

    home = str(Path.home())
    source_text = """import Foundation
import AVFoundation

let logPath = "__LOG__"
let fileManager = FileManager.default
if !fileManager.fileExists(atPath: logPath) {
    fileManager.createFile(atPath: logPath, contents: nil)
}
let log = FileHandle(forWritingAtPath: logPath) ?? FileHandle.nullDevice
log.seekToEndOfFile()

let semaphore = DispatchSemaphore(value: 0)
var microphoneGranted = false
AVCaptureDevice.requestAccess(for: .audio) { granted in
    microphoneGranted = granted
    semaphore.signal()
}
if semaphore.wait(timeout: .now() + 20.0) == .timedOut {
    let message = "VoiceBridge: Keine Antwort auf die Mikrofon-Freigabe; starte trotzdem.\\n"
    if let data = message.data(using: .utf8) {
        log.write(data)
    }
} else if !microphoneGranted {
    let message = "VoiceBridge: Mikrofonzugriff wurde abgelehnt. Bitte in den Systemeinstellungen erlauben.\\n"
    if let data = message.data(using: .utf8) {
        log.write(data)
    }
}

let process = Process()
process.executableURL = URL(fileURLWithPath: "/usr/bin/arch")
process.arguments = ["-arm64", "__PYTHON__", "-m", "voicebridge", "--menubar"]
process.currentDirectoryURL = URL(fileURLWithPath: "__REPO__")
var environment = ProcessInfo.processInfo.environment
environment["PATH"] = "__PATH__"
process.environment = environment
process.standardOutput = log
process.standardError = log

do {
    try process.run()
} catch {
    let message = "VoiceBridge: Python konnte nicht gestartet werden: \\(error)\\n"
    if let data = message.data(using: .utf8) {
        log.write(data)
    }
    exit(1)
}
process.waitUntilExit()
exit(process.terminationStatus)
"""

    replacements = {
        "__LOG__": home + "/Library/Logs/VoiceBridge.log",
        "__PYTHON__": str(venv / "bin" / "python"),
        "__REPO__": str(repo),
        "__PATH__": home
        + "/.opencode/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    }
    for key, value in replacements.items():
        source_text = source_text.replace(key, value)

    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "main.swift"
        source.write_text(source_text, encoding="utf-8")
        try:
            subprocess.run(
                [
                    swiftc,
                    "-O",
                    "-o",
                    str(launcher),
                    str(source),
                    "-framework",
                    "Foundation",
                    "-framework",
                    "AVFoundation",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            print("  Swift-Launcher fehlgeschlagen, nutze Shell-Fallback.")
            if exc.stderr:
                print("  " + exc.stderr.strip().splitlines()[-1])
            return False

    return launcher.is_file()


def build(target: Path, repo: Path, venv: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    macos = target / "Contents" / "MacOS"
    resources = target / "Contents" / "Resources"
    macos.mkdir(parents=True, exist_ok=True)
    resources.mkdir(parents=True, exist_ok=True)

    launcher = macos / "VoiceBridge"
    if not write_swift_launcher(launcher, repo, venv):
        write_shell_launcher(launcher, repo, venv)
    launcher.chmod(0o755)

    icon = make_icns(resources)

    info = {
        "CFBundleName": "VoiceBridge",
        "CFBundleDisplayName": "VoiceBridge",
        "CFBundleIdentifier": "local.voicebridge",
        "CFBundleVersion": "1.0",
        "CFBundleShortVersionString": "1.0",
        "CFBundlePackageType": "APPL",
        "CFBundleExecutable": "VoiceBridge",
        "CFBundleDevelopmentRegion": "de",
        "CFBundleInfoDictionaryVersion": "6.0",
        "LSUIElement": True,
        "LSArchitecturePriority": ["arm64", "x86_64"],
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
        "LSApplicationCategoryType": "public.app-category.productivity",
        "NSMicrophoneUsageDescription": "VoiceBridge benötigt das Mikrofon für Spracheingaben.",
    }
    if icon:
        info["CFBundleIconFile"] = "VoiceBridge"

    with (target / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump(info, handle)

    codesign = shutil.which("codesign")
    if codesign:
        subprocess.run(
            [codesign, "--force", "--deep", "--sign", "-", str(target)],
            capture_output=True,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="VoiceBridge.app bauen")
    parser.add_argument(
        "--target",
        type=Path,
        default=Path("~/Applications/VoiceBridge.app").expanduser(),
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
    )
    parser.add_argument(
        "--venv",
        type=Path,
        default=Path("~/voice-venv").expanduser(),
    )
    args = parser.parse_args()

    target = args.target.expanduser().resolve()
    repo = args.repo.expanduser().resolve()
    venv = args.venv.expanduser().resolve()

    if not (venv / "bin" / "python").is_file():
        print("venv nicht gefunden: {}".format(venv))
        return 1
    if not (repo / "voicebridge").is_dir():
        print("Repo nicht gefunden: {}".format(repo))
        return 1

    target.parent.mkdir(parents=True, exist_ok=True)
    build(target, repo, venv)

    lsregister = Path(
        "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
        "LaunchServices.framework/Support/lsregister"
    )
    if lsregister.is_file():
        subprocess.run([str(lsregister), "-f", str(target)], capture_output=True)

    print("App erstellt: {}".format(target))
    print('Starten:      open "{}"'.format(target))
    print("Log:          ~/Library/Logs/VoiceBridge.log")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
