import os
import signal
import subprocess
import sys
import time


def is_enabled(name: str, default: str = "true") -> bool:
    value = os.getenv(name, default).lower()
    return value in ["1", "true", "yes", "on"]


SERVICES = []

if is_enabled("ENABLE_ESLITE", "true"):
    SERVICES.append(("eslite", ["python", "main.py"]))

if is_enabled("ENABLE_FUNBOX", "true"):
    SERVICES.append(("funbox", ["python", "test_funbox.py"]))

if is_enabled("ENABLE_TCSB", "true"):
    SERVICES.append(("tcsb", ["python", "test_tcsb.py"]))

if is_enabled("ENABLE_TAKARA", "true"):
    SERVICES.append(("takara", ["python", "test_takara.py"]))


processes = {}


def start_service(name, command):
    print(f"[runner] 啟動 {name}: {' '.join(command)}", flush=True)

    process = subprocess.Popen(
        command,
        stdout=sys.stdout,
        stderr=sys.stderr,
        env=os.environ.copy(),
    )

    processes[name] = {
        "command": command,
        "process": process,
    }


def stop_all(signum=None, frame=None):
    print("[runner] 收到停止訊號，準備關閉所有監控...", flush=True)

    for name, item in processes.items():
        process = item["process"]

        if process.poll() is None:
            print(f"[runner] 停止 {name}", flush=True)
            process.terminate()

    time.sleep(5)

    for name, item in processes.items():
        process = item["process"]

        if process.poll() is None:
            print(f"[runner] 強制停止 {name}", flush=True)
            process.kill()

    sys.exit(0)


def main():
    if not SERVICES:
        print("[runner] 沒有任何服務被啟用，請檢查 ENABLE_* 環境變數", flush=True)
        return

    signal.signal(signal.SIGTERM, stop_all)
    signal.signal(signal.SIGINT, stop_all)

    print("[runner] 陀螺獵人多網站監控啟動", flush=True)

    for name, command in SERVICES:
        start_service(name, command)

    while True:
        time.sleep(10)

        for name, item in list(processes.items()):
            process = item["process"]
            command = item["command"]

            if process.poll() is not None:
                print(
                    f"[runner] {name} 已停止，exit code={process.returncode}，10 秒後重啟",
                    flush=True,
                )

                time.sleep(10)
                start_service(name, command)


if __name__ == "__main__":
    main()