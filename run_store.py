import os
import signal
import subprocess
import sys
import time
import threading

from http.server import BaseHTTPRequestHandler, HTTPServer


def get_env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


STORE = os.getenv("STORE", "").lower().strip()

STORE_COMMANDS = {
    "eslite": [sys.executable, "-u", "main.py", "--once"],
    "funbox": [sys.executable, "-u", "test_funbox.py", "--once"],
    "momo": [sys.executable, "-u", "test_momo.py", "--once"],
    "takara": [sys.executable, "-u", "test_takara.py", "--once"],
    "toysrus": [sys.executable, "-u", "test_toysrus.py", "--once"],
    "shopee": [sys.executable, "-u", "test_shopee.py", "--once"],
    "tcsb": [sys.executable, "-u", "test_tcsb.py", "--once"],
}

CHECK_INTERVAL_SECONDS = get_env_int("CHECK_INTERVAL_SECONDS", 90)
SERVICE_TIMEOUT_SECONDS = get_env_int("SERVICE_TIMEOUT_SECONDS", 150)
MAX_ROUNDS_BEFORE_EXIT = get_env_int("MAX_ROUNDS_BEFORE_EXIT", 80)

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

    print(f"[store-runner] health server started on port {port}", flush=True)

    server.serve_forever()


def stop_current_process():
    global current_process

    if current_process is None:
        return

    if current_process.poll() is not None:
        return

    print("[store-runner] 停止目前正在執行的爬蟲...", flush=True)

    current_process.terminate()

    try:
        current_process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        print("[store-runner] 爬蟲未正常停止，強制 kill", flush=True)
        current_process.kill()
        current_process.wait(timeout=10)


def stop_all(signum=None, frame=None):
    global should_stop

    should_stop = True

    print("[store-runner] 收到停止訊號，準備關閉...", flush=True)

    stop_current_process()

    print("[store-runner] runner 已停止", flush=True)

    sys.exit(0)


def print_config():
    print("=" * 50, flush=True)
    print("[store-runner] 單店模式啟動", flush=True)
    print(f"[store-runner] STORE={STORE}", flush=True)
    print(f"[store-runner] CHECK_INTERVAL_SECONDS={CHECK_INTERVAL_SECONDS}", flush=True)
    print(f"[store-runner] SERVICE_TIMEOUT_SECONDS={SERVICE_TIMEOUT_SECONDS}", flush=True)
    print(f"[store-runner] MAX_ROUNDS_BEFORE_EXIT={MAX_ROUNDS_BEFORE_EXIT}", flush=True)

    if STORE in STORE_COMMANDS:
        print(f"[store-runner] command: {' '.join(STORE_COMMANDS[STORE])}", flush=True)
    else:
        print("[store-runner] 找不到對應 STORE，請設定 eslite / funbox / momo / takara / toysrus / shopee / tcsb", flush=True)

    print("=" * 50, flush=True)


def run_once(command):
    global current_process

    print("=" * 50, flush=True)
    print(f"[store-runner] 開始掃描 {STORE}", flush=True)
    print(f"[store-runner] command: {' '.join(command)}", flush=True)

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
            f"[store-runner] {STORE} 掃描完成，exit code={exit_code}，耗時 {elapsed} 秒",
            flush=True,
        )

    except subprocess.TimeoutExpired:
        print(
            f"[store-runner] {STORE} 超過 {SERVICE_TIMEOUT_SECONDS} 秒未結束，強制停止",
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

    print_config()

    if STORE not in STORE_COMMANDS:
        while True:
            time.sleep(60)

    command = STORE_COMMANDS[STORE]
    round_count = 1

    while not should_stop:
        print("=" * 50, flush=True)
        print(f"[store-runner] 開始第 {round_count} 輪：{STORE}", flush=True)
        print("=" * 50, flush=True)

        run_once(command)

        print("=" * 50, flush=True)
        print(f"[store-runner] 第 {round_count} 輪完成：{STORE}", flush=True)

        if round_count >= MAX_ROUNDS_BEFORE_EXIT:
            print(
                f"[store-runner] 已完成 {MAX_ROUNDS_BEFORE_EXIT} 輪，主動結束讓 Zeabur 重啟",
                flush=True,
            )
            print("[store-runner] 這是正常保護機制，不是程式錯誤", flush=True)
            sys.exit(0)

        print(
            f"[store-runner] 等待 {CHECK_INTERVAL_SECONDS} 秒後開始下一輪",
            flush=True,
        )
        print("=" * 50, flush=True)

        round_count += 1
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()