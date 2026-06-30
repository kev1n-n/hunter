import os
import signal
import subprocess
import sys
import time
import threading

from http.server import BaseHTTPRequestHandler, HTTPServer


def is_enabled(name: str, default: str = "true") -> bool:
    value = os.getenv(name, default).lower()
    return value in ["1", "true", "yes", "on"]


# 每個爬蟲啟動間隔，避免多個 Chromium 同時開造成 Zeabur 記憶體壓力
STARTUP_STAGGER_SECONDS = int(os.getenv("STARTUP_STAGGER_SECONDS", "15"))

# 子程序掛掉後，幾秒後重啟
RESTART_DELAY_SECONDS = int(os.getenv("RESTART_DELAY_SECONDS", "15"))


SERVICES = []

if is_enabled("ENABLE_ESLITE", "true"):
    SERVICES.append(("eslite", [sys.executable, "-u", "main.py"]))

if is_enabled("ENABLE_FUNBOX", "true"):
    SERVICES.append(("funbox", [sys.executable, "-u", "test_funbox.py"]))

if is_enabled("ENABLE_TCSB", "true"):
    SERVICES.append(("tcsb", [sys.executable, "-u", "test_tcsb.py"]))

if is_enabled("ENABLE_MOMO", "false"):
    SERVICES.append(("momo", [sys.executable, "-u", "test_momo.py"]))

if is_enabled("ENABLE_TAKARA", "false"):
    SERVICES.append(("takara", [sys.executable, "-u", "test_takara.py"]))


processes = {}


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ["/", "/health"]:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok")
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):
        # 不印 health check log，避免 Zeabur log 太吵
        return


def start_health_server():
    port = int(os.getenv("PORT", "8080"))

    server = HTTPServer(("0.0.0.0", port), HealthHandler)

    print(f"[runner] health server started on port {port}", flush=True)

    server.serve_forever()


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
        "started_at": time.time(),
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

    print("[runner] 所有監控已停止", flush=True)

    sys.exit(0)


def print_runner_config():
    enabled_names = [name for name, _ in SERVICES]

    print("=" * 50, flush=True)
    print("[runner] 陀螺獵人多網站監控啟動", flush=True)
    print(f"[runner] 已啟用服務：{', '.join(enabled_names) if enabled_names else '無'}", flush=True)
    print(f"[runner] ENABLE_ESLITE={os.getenv('ENABLE_ESLITE')}", flush=True)
    print(f"[runner] ENABLE_FUNBOX={os.getenv('ENABLE_FUNBOX')}", flush=True)
    print(f"[runner] ENABLE_TCSB={os.getenv('ENABLE_TCSB')}", flush=True)
    print(f"[runner] ENABLE_MOMO={os.getenv('ENABLE_MOMO')}", flush=True)
    print(f"[runner] ENABLE_TAKARA={os.getenv('ENABLE_TAKARA')}", flush=True)
    print(f"[runner] STARTUP_STAGGER_SECONDS={STARTUP_STAGGER_SECONDS}", flush=True)
    print(f"[runner] RESTART_DELAY_SECONDS={RESTART_DELAY_SECONDS}", flush=True)
    print("=" * 50, flush=True)


def main():
    # 先啟動 health server，讓 Zeabur 知道 container 是活的
    health_thread = threading.Thread(target=start_health_server, daemon=True)
    health_thread.start()

    signal.signal(signal.SIGTERM, stop_all)
    signal.signal(signal.SIGINT, stop_all)

    print_runner_config()

    if not SERVICES:
        print("[runner] 沒有任何服務被啟用，請檢查 ENABLE_* 環境變數", flush=True)

        while True:
            time.sleep(60)

    for index, (name, command) in enumerate(SERVICES):
        start_service(name, command)

        if index < len(SERVICES) - 1:
            print(
                f"[runner] 等待 {STARTUP_STAGGER_SECONDS} 秒後啟動下一個服務...",
                flush=True,
            )
            time.sleep(STARTUP_STAGGER_SECONDS)

    while True:
        time.sleep(10)

        for name, item in list(processes.items()):
            process = item["process"]
            command = item["command"]

            if process.poll() is not None:
                print(
                    f"[runner] {name} 已停止，exit code={process.returncode}，"
                    f"{RESTART_DELAY_SECONDS} 秒後重啟",
                    flush=True,
                )

                time.sleep(RESTART_DELAY_SECONDS)
                start_service(name, command)


if __name__ == "__main__":
    main()