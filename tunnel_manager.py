# tunnel_manager.py
import subprocess
import threading
import time
import os
import requests
import psutil
import re
import json
from datetime import datetime

# Disable requests warnings for self-signed certs on localhost
from requests.packages.urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)


class TunnelManager:
    def __init__(self, log_path=None):
        """
        TunnelManager manages cloudflared tunnel subprocesses and writes a compact
        log.json snapshot (no verbose logs) every 3 seconds.

        On initialization, any existing log.json at `log_path` is removed so
        each restart begins with a fresh file (as requested).
        """
        self.active_tunnels = {}
        self.cloudflared_path = self.find_cloudflared()
        print(f"Using cloudflared at: {self.cloudflared_path}")

        # log.json path (default same folder)
        base_dir = os.path.dirname(__file__)
        self.log_path = log_path if log_path else os.path.join(base_dir, 'log.json')

        # remove existing log.json to ensure restart clears old logs
        try:
            if os.path.exists(self.log_path):
                os.remove(self.log_path)
                print(f"Removed existing log file: {self.log_path}")
        except Exception as e:
            print(f"Warning: Failed to remove existing log file: {e}")

        # synchronization lock for active_tunnels and writing log
        self.lock = threading.Lock()

        # background writer control
        self._stop_log_writer = threading.Event()
        self._log_writer_thread = threading.Thread(target=self._log_writer_loop, daemon=True)
        self._log_writer_thread.start()

    def find_cloudflared(self):
        """Find cloudflared executable"""
        # Look in a 'cloudflared' subdirectory first, then system path
        local_path_win = os.path.join(os.path.dirname(__file__), 'cloudflared', 'cloudflared.exe')
        local_path_nix = os.path.join(os.path.dirname(__file__), 'cloudflared', 'cloudflared')

        paths = [
            local_path_win,
            local_path_nix,
            'cloudflared.exe',  # Check PATH on Windows
            'cloudflared'       # Check PATH on *nix
        ]

        for path in paths:
            try:
                if os.path.exists(path) and os.path.isfile(path):
                    return os.path.abspath(path)
            except Exception:
                continue

        # Fallback to just 'cloudflared' and hope it's in the PATH
        print("Warning: cloudflared not found in local directory. Assuming 'cloudflared' is in system PATH.")
        return "cloudflared"

    def create_tunnel(self, port):
        """Create a Cloudflare tunnel on specified port. Returns (success, public_url, logs_list)"""
        logs = []
        port_str = str(port)

        def log_appender(line):
            log_entry = f"[{datetime.now().strftime('%H:%M:%S')}] {line.strip()}"
            logs.append(log_entry)
            # also keep in-memory logs for debugging (not saved to log.json)
            with self.lock:
                if port_str in self.active_tunnels:
                    self.active_tunnels[port_str]['logs'].append(log_entry)
            print(f"Port {port_str}: {log_entry}")

        try:
            # Start cloudflared tunnel using --url (no login required for trycloudflare)
            cmd = [self.cloudflared_path, 'tunnel', '--url', f'http://127.0.0.1:{port_str}']
            log_appender(f"Executing command: {' '.join(cmd)}")

            # Spawn process
            creationflags = 0
            if os.name == 'nt':
                # Hide console window on Windows
                creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                universal_newlines=True,
                creationflags=creationflags
            )

            public_url = None
            start_time = time.time()

            # Read lines until we find the trycloudflare URL or timeout
            for line in iter(process.stdout.readline, ''):
                if not line:
                    break
                log_appender(line)

                # regex to capture trycloudflare URL
                url_match = re.search(r'(https://[a-zA-Z0-9-]+\.trycloudflare\.com)', line)
                if url_match:
                    public_url = url_match.group(0)
                    log_appender(f"Public URL found: {public_url}")
                    break

                # safety timeout (20s)
                if time.time() - start_time > 20:
                    log_appender("Error: Timeout waiting for public URL.")
                    try:
                        process.terminate()
                    except Exception:
                        pass
                    return False, None, logs

            if public_url:
                with self.lock:
                    # initialize in-memory record (keep logs in memory only)
                    self.active_tunnels[port_str] = {
                        'process': process,
                        'public_url': public_url,
                        'start_time': time.time(),
                        'logs': logs.copy(),  # in-memory, not saved to log.json
                        'pid': process.pid,
                        'status': 'active',
                        'health': 100,
                        'ping': 0,
                        'status_text': 'Active',
                        'port': port_str
                    }

                # start a log reader thread to continue consuming stdout
                log_thread = threading.Thread(target=self.log_reader, args=(process, port_str), daemon=True)
                log_thread.start()

                return True, public_url, logs
            else:
                log_appender("Error: Process terminated before URL was found.")
                try:
                    process.terminate()
                except Exception:
                    pass
                return False, None, logs

        except Exception as e:
            log_appender(f"Fatal Error: {e}")
            return False, None, [f"Error: {e}"]

    def log_reader(self, process, port):
        """Read remaining stdout lines from process and append to in-memory logs."""
        port = str(port)
        try:
            for line in iter(process.stdout.readline, ''):
                if not line:
                    break
                log_entry = f"[{datetime.now().strftime('%H:%M:%S')}] {line.strip()}"
                with self.lock:
                    if port in self.active_tunnels:
                        self.active_tunnels[port]['logs'].append(log_entry)
                print(f"Port {port} Log: {log_entry}")
        except Exception as e:
            print(f"Log reader exception for port {port}: {e}")

    def terminate_tunnel(self, port):
        """Terminate tunnel on specified port. Returns True if terminated/cleaned."""
        port = str(port)
        try:
            with self.lock:
                if port in self.active_tunnels:
                    process_data = self.active_tunnels.pop(port)
                else:
                    process_data = None

            if not process_data:
                return False

            process = process_data.get('process')
            pid = process_data.get('pid')
            print(f"Terminating tunnel on port {port} (PID: {pid})")

            # Attempt graceful termination with psutil for process tree
            try:
                parent = psutil.Process(pid)
                children = parent.children(recursive=True)
                for child in children:
                    try:
                        child.terminate()
                    except Exception:
                        pass
                try:
                    parent.terminate()
                except Exception:
                    pass

                gone, alive = psutil.wait_procs([parent] + children, timeout=3)
                for p in alive:
                    try:
                        p.kill()
                    except Exception:
                        pass
            except psutil.NoSuchProcess:
                # process already gone
                print(f"Process {pid} already gone.")
            except Exception as e:
                print(f"psutil termination error: {e}. Falling back to process.terminate()")
                try:
                    process.terminate()
                except Exception:
                    pass

            try:
                process.wait(timeout=5)
            except Exception:
                pass

            print(f"Tunnel on port {port} terminated.")
            return True

        except Exception as e:
            print(f"Error terminating tunnel on port {port}: {e}")
            # ensure removal if present
            with self.lock:
                if port in self.active_tunnels:
                    del self.active_tunnels[port]
            return False

    def get_tunnel_stats(self, port):
        """Return (health, ping_ms, status_text) for the given port."""
        port = str(port)
        try:
            with self.lock:
                if port not in self.active_tunnels:
                    return 0, 0, "Tunnel not found"
                tunnel_data = self.active_tunnels[port]
                public_url = tunnel_data.get('public_url')
                process = tunnel_data.get('process')

            # check process still running
            if process.poll() is not None:
                print(f"Process for port {port} died unexpectedly.")
                # clean up
                self.terminate_tunnel(port)
                return 0, 0, "Process terminated"

            health = 0
            ping = 0
            status = "Checking..."

            try:
                start_time = time.time()
                response = requests.get(public_url, timeout=5, verify=False)
                ping = int((time.time() - start_time) * 1000)

                if 200 <= response.status_code < 500:
                    health = 100
                    status = "Healthy"
                else:
                    health = 25
                    status = f"HTTP {response.status_code}"

            except requests.exceptions.Timeout:
                health = 10
                ping = 5000
                status = "Connection timeout"
            except requests.exceptions.ConnectionError:
                health = 0
                ping = 5000
                status = "Connection failed"
            except Exception as e:
                health = 5
                ping = 5000
                status = "Request Error"
                print(f"Stats error port {port}: {e}")

            # update in-memory cached stats
            with self.lock:
                if port in self.active_tunnels:
                    self.active_tunnels[port].update({
                        'health': health,
                        'ping': ping,
                        'status_text': status
                    })

            return health, ping, status

        except Exception as e:
            print(f"get_tunnel_stats exception for port {port}: {e}")
            return 0, 0, f"Error: {e}"

    def _snapshot_for_log(self):
        """
        Build a compact snapshot suitable for JSON logging.
        This explicitly EXCLUDES the verbose per-process 'logs' to keep file small.
        """
        snapshot = {}
        with self.lock:
            for port, data in self.active_tunnels.items():
                snapshot[port] = {
                    'port': port,
                    'public_url': data.get('public_url'),
                    'status': data.get('status', 'unknown'),
                    'status_text': data.get('status_text', ''),
                    'health': data.get('health', 0),
                    'ping': data.get('ping', 0),
                    'pid': data.get('pid'),
                    'start_time': datetime.fromtimestamp(data.get('start_time')).isoformat() if data.get('start_time') else None
                    # intentionally do NOT include 'logs'
                }
        return snapshot

    def _write_json_atomic(self, data):
        """Atomically write JSON payload to self.log_path."""
        try:
            tmp_path = f"{self.log_path}.tmp"
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self.log_path)
        except Exception as e:
            print(f"Failed to write log.json: {e}")

    def _log_writer_loop(self):
        """Background loop: write compact snapshot to log.json every 3 seconds."""
        while not self._stop_log_writer.is_set():
            try:
                snapshot = self._snapshot_for_log()
                payload = {
                    'generated_at': datetime.now().isoformat(),
                    'tunnels': snapshot
                }
                # write atomically
                self._write_json_atomic(payload)
            except Exception as e:
                print(f"Log writer error: {e}")

            # wait up to 3 seconds, but can be interrupted by stop event
            self._stop_log_writer.wait(3)

    def stop(self):
        """Stop background writer thread (call on program exit)."""
        self._stop_log_writer.set()
        try:
            if self._log_writer_thread.is_alive():
                self._log_writer_thread.join(timeout=1)
        except Exception:
            pass
