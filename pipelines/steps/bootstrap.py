"""컨테이너 준비. AWS 제공 PyTorch 컨테이너에는 mediapipe(common/face_regions가 import)가 시작할 때 여는
시스템 라이브러리(libGL·libglib·libEGL·libGLES)가 없을 수 있다 → 없으면 Dockerfile과 같은 apt 패키지를 설치한다.

로컬(macOS, 개발 PC)에서는 아무것도 하지 않는다. 각 단계 스크립트가 common을 import하기 전에 system_libs()를 부른다.
"""
import ctypes.util
import os
import shutil
import subprocess
import sys

# 라이브러리 이름(ctypes.util.find_library) → apt 패키지. 목록은 Dockerfile의 apt-get install과 같다.
LIBS = {"GL": "libgl1", "glib-2.0": "libglib2.0-0", "EGL": "libegl1", "GLESv2": "libgles2"}


def missing_packages():
    return [pkg for lib, pkg in LIBS.items() if ctypes.util.find_library(lib) is None]


def system_libs():
    """리눅스에서 빠진 라이브러리를 apt로 설치한다 (root일 때만). 설치 못 하면 경고만 — import 실패는 뒤에서 드러난다."""
    if not sys.platform.startswith("linux"):
        return []
    missing = missing_packages()
    if not missing:
        return []
    if os.geteuid() != 0 or not shutil.which("apt-get"):
        print(f"경고: 시스템 라이브러리 {missing} 없음 — root가 아니거나 apt-get이 없어 설치하지 못한다", flush=True)
        return missing
    print(f"시스템 라이브러리 설치: {missing}", flush=True)
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    subprocess.run(["apt-get", "update", "-qq"], check=True, env=env)
    subprocess.run(["apt-get", "install", "-y", "-qq", "--no-install-recommends", *missing], check=True, env=env)
    return missing
