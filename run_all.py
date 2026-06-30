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


# 每間店掃完後，等幾秒再掃下一間
STORE_STAGGER_SECONDS = int(os.getenv("STORE_STAGGER_SECONDS", "20"))

# 全部店掃完一輪後，等幾秒再開始下一輪
CYCLE_SLEEP_SECONDS = int(os.getenv("CYCLE_SLEEP_SECONDS", "60"))

# 單一爬蟲最多跑幾秒，超過就強制結束，避免卡死
SERVICE_TIMEOUT_SECONDS = int(os.getenv("SERVICE_TIMEOUT_SECONDS", "180"))


SERVICES = []

if is_enabled("ENABLE_ESLITE", "true"):
    SERVICES.append(("eslite", [sys.executable, "-u", "main.py", "--once"]))

if is_enabled("ENABLE_FUNBOX", "true"):
    SERVICES.append(("funbox", [sys.executable, "-u", "test_funbox.py", "--once"]))

if is_enabled("ENABLE_TCSB", "true"):
    SERVICES.append(("tcsb", [sys.executable, "-u", "test_tcsb.py", "--once"]))

if is_enabled("ENABLE_MOMO", "false"):
    SERVICES.append(("momo", [sys.executable, "-u", "test_momo.py", "--once"]))

if is_enabled("ENABLE_TAKARA", "false"):
    SERVICES.append(("takara", [sys.executable, "-u", "test_takara.py", "--once"]))


current_process = None
should_stop = False


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
        return


def start_health_server():
    port = int(os.getenv("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)

    print(f"[runner] health server started on port {port}", flush=True)

    server.serve_forever()


def stop_current_process():
    global current_process

    if current_process is None:
        return

    if current_process.poll() is not None:
        return

    print("[runner] 停止目前正在執行的爬蟲...", flush=True)

    current_process.terminate()

    try:
        current_process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        print("[runner] 爬蟲未正常停止，強制 kill", flush=True)
        current_process.kill()
        current_process.wait(timeout=10)


def stop_all(signum=None, frame=None):
    global should_stop

    should_stop = True

    print("[runner] 收到停止訊號，準備關閉...", flush=True)

    stop_current_process()

    print("[runner] runner 已停止", flush=True)

    sys.exit(0)


def print_runner_config():
    enabled_names = [name for name, _ in SERVICES]

    print("=" * 50, flush=True)
    print("[runner] 陀螺獵人輪巡模式啟動", flush=True)
    print(f"[runner] 已啟用服務：{', '.join(enabled_names) if enabled_names else '無'}", flush=True)
    print(f"[runner] ENABLE_ESLITE={os.getenv('ENABLE_ESLITE')}", flush=True)
    print(f"[runner] ENABLE_FUNBOX={os.getenv('ENABLE_FUNBOX')}", flush=True)
    print(f"[runner] ENABLE_TCSB={os.getenv('ENABLE_TCSB')}", flush=True)
    print(f"[runner] ENABLE_MOMO={os.getenv('ENABLE_MOMO')}", flush=True)
    print(f"[runner] ENABLE_TAKARA={os.getenv('ENABLE_TAKARA')}", flush=True)
    print(f"[runner] STORE_STAGGER_SECONDS={STORE_STAGGER_SECONDS}", flush=True)
    print(f"[runner] CYCLE_SLEEP_SECONDS={CYCLE_SLEEP_SECONDS}", flush=True)
    print(f"[runner] SERVICE_TIMEOUT_SECONDS={SERVICE_TIMEOUT_SECONDS}", flush=True)
    print("=" * 50, flush=True)


def run_service_once(name, command):
    global current_process

    print("=" * 50, flush=True)
    print(f"[runner] 開始掃描 {name}", flush=True)
    print(f"[runner] command: {' '.join(command)}", flush=True)

    started_at = time.time()

    current_process = subprocess.Popen(
        command,
        stdout=sys.stdout,
        stderr=sys.stderr,
        env=os.environ.copy(),
    )

    try:
        exit_code = current_process.wait(timeout=SERVICE_TIMEOUT_SECONDS)

        elapsed = int(time.time() - started_at)

        print(
            f"[runner] {name} 掃描完成，exit code={exit_code}，耗時 {elapsed} 秒",
            flush=True,
        )

    except subprocess.TimeoutExpired:
        print(
            f"[runner] {name} 超過 {SERVICE_TIMEOUT_SECONDS} 秒未結束，強制停止",
            flush=True,
        )

        stop_current_process()

    finally:
        current_process = None


def main():
    health_thread = threading.Thread(target=start_health_server, daemon=True)
    health_thread.start()

    signal.signal(signal.SIGTERM, stop_all)
    signal.signal(signal.SIGINT, stop_all)

    print_runner_config()

    if not SERVICES:
        print("[runner] 沒有任何服務被啟用，請檢查 ENABLE_* 環境變數", flush=True)

        while True:
            time.sleep(60)

    round_count = 1

    while not should_stop:
        print("=" * 50, flush=True)
        print(f"[runner] 開始第 {round_count} 輪掃描", flush=True)
        print("=" * 50, flush=True)

        for index, (name, command) in enumerate(SERVICES):
            if should_stop:
                break

            run_service_once(name, command)

            if index < len(SERVICES) - 1:
                print(
                    f"[runner] 等待 {STORE_STAGGER_SECONDS} 秒後掃描下一間...",
                    flush=True,
                )
                time.sleep(STORE_STAGGER_SECONDS)

        print("=" * 50, flush=True)
        print(f"[runner] 第 {round_count} 輪掃描完成", flush=True)
        print(f"[runner] 等待 {CYCLE_SLEEP_SECONDS} 秒後開始下一輪", flush=True)
        print("=" * 50, flush=True)

        round_count += 1
        time.sleep(CYCLE_SLEEP_SECONDS)


if __name__ == "__main__":
    main()