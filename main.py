# main.py
import webview
import threading
import os
import sys
import time
from tunnel_manager import TunnelManager

# --- API Class for pywebview ---
class Api:
    def __init__(self):
        # Initialize tunnel manager (it will start writing log.json)
        self.tunnel_manager = TunnelManager()
        # This will store the state of our tunnels for the UI
        self.port_data = {}
        self.lock = threading.Lock()

    def create_tunnel(self, port):
        """Create a new tunnel on specified port (non-blocking)"""
        port_str = str(port)

        with self.lock:
            if port_str in self.port_data:
                return {'success': False, 'message': f'Tunnel on port {port_str} already exists or is processing.'}

            # Set initial "creating" state
            self.port_data[port_str] = {
                'status': 'creating',
                'public_url': '',
                'health': 0,
                'ping': 0,
                'logs': [f"[{time.strftime('%H:%M:%S')}] Initiating tunnel creation for port {port_str}..."],
                'creation_time': time.time(),
            }

        def create_tunnel_thread():
            """Runs tunnel creation in a background thread"""
            success, public_url, logs = self.tunnel_manager.create_tunnel(port_str)

            with self.lock:
                if port_str not in self.port_data:
                    # Task was cancelled or terminated while creating
                    return

                if success:
                    self.port_data[port_str].update({
                        'status': 'active',
                        'public_url': public_url,
                        'logs': self.port_data[port_str]['logs'] + logs, # Append new logs
                        'health': 100 # Assume 100% on start
                    })
                    # Start monitoring this specific tunnel (UI side)
                    self.start_monitoring(port_str)
                else:
                    self.port_data[port_str].update({
                        'status': 'error',
                        'logs': self.port_data[port_str]['logs'] + logs,
                    })

        threading.Thread(target=create_tunnel_thread, daemon=True).start()
        return {'success': True, 'message': f'Creating tunnel on port {port_str}'}

    def terminate_tunnel(self, port):
        """Terminate tunnel on specified port (non-blocking)"""
        port_str = str(port)

        with self.lock:
            if port_str not in self.port_data:
                return {'success': False, 'message': f'No tunnel found on port {port_str}'}

            # Set terminating state
            self.port_data[port_str]['status'] = 'terminating'

        def terminate_thread():
            """Runs termination in background"""
            success = self.tunnel_manager.terminate_tunnel(port_str)

            with self.lock:
                # Remove from port_data regardless of success, to clean up UI
                if port_str in self.port_data:
                    del self.port_data[port_str]

            if not success:
                print(f"Failed to cleanly terminate tunnel on port {port_str}")
                # UI will reflect removal, log printed to console

        threading.Thread(target=terminate_thread, daemon=True).start()
        return {'success': True, 'message': f'Terminating tunnel on port {port_str}'}

    def terminate_all_tunnels(self):
        """Helper to terminate all running tunnels"""
        with self.lock:
            ports = list(self.port_data.keys())

        if not ports:
            return "No active tunnels to terminate."

        for port in ports:
            self.terminate_tunnel(port)

        return f"Termination signal sent to {len(ports)} tunnels."

    def start_monitoring(self, port_str):
        """Start monitoring tunnel health and ping for a specific port"""
        def monitor():
            """The monitoring loop"""
            while True:
                with self.lock:
                    # Stop monitoring if port is no longer in port_data or status isn't active
                    if port_str not in self.port_data or self.port_data[port_str]['status'] != 'active':
                        break

                try:
                    health, ping, status_text = self.tunnel_manager.get_tunnel_stats(port_str)

                    with self.lock:
                        if port_str in self.port_data:
                            self.port_data[port_str].update({
                                'health': health,
                                'ping': ping,
                                'status_text': status_text,
                            })
                            # If stats show process died, mark as error
                            if status_text == "Process terminated":
                                self.port_data[port_str]['status'] = 'error'
                                self.port_data[port_str]['logs'].append(f"[{time.strftime('%H:%M:%S')}] Connection lost. Process terminated.")

                except Exception as e:
                    print(f"Monitoring error for port {port_str}: {e}")

                # Check every 1 second
                time.sleep(1)

            print(f"Stopping monitoring for port {port_str}")

        threading.Thread(target=monitor, daemon=True).start()
        print(f"Started monitoring for port {port_str}")

    def get_port_data(self):
        """Get current status of all ports"""
        with self.lock:
            # Return a copy to avoid mutation issues
            return self.port_data.copy()

    def get_logs(self, port):
        """Get logs for specific port"""
        port_str = str(port)
        with self.lock:
            return self.port_data.get(port_str, {}).get('logs', [])

    def open_localhost(self, port):
        """Open localhost in browser"""
        import webbrowser
        webbrowser.open(f'http://127.0.0.1:{port}')
        return {'success': True}

    def open_public_url(self, port):
        """Open public URL in browser"""
        port_str = str(port)
        import webbrowser

        with self.lock:
            public_url = self.port_data.get(port_str, {}).get('public_url', '')

        if public_url:
            webbrowser.open(public_url)
            return {'success': True}
        return {'success': False, 'message': 'No public URL found'}

    def run_console_command(self, command_line):
        """Parses and executes commands from the UI console"""
        try:
            cmd, _, arg = command_line.partition('//')
            cmd = cmd.strip().lower()
            arg = arg.strip()

            if cmd == 'run':
                if not arg.isdigit():
                    return "Error: 'run' requires a numeric port. (e.g., run//5000)"
                result = self.create_tunnel(arg)
                return result['message']

            elif cmd == 'stop':
                if not arg.isdigit():
                    return "Error: 'stop' requires a numeric port. (e.g., stop//5000)"
                result = self.terminate_tunnel(arg)
                return result['message']

            elif cmd == 'stopall':
                return self.terminate_all_tunnels()

            elif cmd == 'status':
                with self.lock:
                    if not arg or arg.lower() == 'all':
                        if not self.port_data:
                            return "No active tunnels."
                        response = "--- ALL TUNNEL STATUS ---\n"
                        for port, data in self.port_data.items():
                            response += f"Port {port}: {data['status'].upper()}\n"
                        return response

                    elif arg in self.port_data:
                        data = self.port_data[arg]
                        response = f"--- STATUS FOR PORT {arg} ---\n"
                        response += f"Status: {data['status'].upper()}\n"
                        response += f"Public URL: {data.get('public_url', 'N/A')}\n"
                        response += f"Health: {data.get('health', 0)}%\n"
                        response += f"Ping: {data.get('ping', 0)}ms\n"
                        response += f"Monitored Status: {data.get('status_text', 'N/A')}\n"
                        return response
                    else:
                        return f"Error: No tunnel found on port {arg}."

            elif cmd == 'help':
                return (
                    "--- AVAILABLE COMMANDS ---\n"
                    "run//<port>       - Creates a new tunnel on the specified port.\n"
                    "stop//<port>      - Terminates the tunnel on the specified port.\n"
                    "stopall//         - Terminates all active tunnels.\n"
                    "status//<port>   - Shows detailed status for a specific port.\n"
                    "status//all       - (or status//) Shows a summary of all tunnels.\n"
                    "clear//           - Clears the console screen.\n"
                    "help//            - Shows this help message."
                )

            elif cmd == 'clear':
                return "CLEAR_CONSOLE" # Special string for JS to handle

            else:
                return f"Unknown command: '{cmd}'. Type 'help//' for commands."

        except Exception as e:
            print(f"Console Error: {e}")
            return f"An internal error occurred: {e}"

# --- Main Application Setup ---

def get_html_content():
    """Load HTML content from index.html"""
    try:
        # Get path to index.html relative to this script
        html_file_path = os.path.join(os.path.dirname(__file__), 'index.html')

        with open(html_file_path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        print(f"Error loading index.html: {e}")
        # Return fallback HTML
        return f"""
        <html>
            <body style="background: #0f0c29; color: #ff4d6d; font-family: sans-serif; padding: 20px;">
                <h1>FATAL ERROR</h1>
                <p>Could not load <strong>index.html</strong>.</p>
                <p>Please ensure 'index.html' is in the same directory as 'main.py'.</p>
                <p>Error details: {e}</p>
            </body>
        </html>
        """

if __name__ == '__main__':
    api = Api()

    # Load the HTML content
    html_content = get_html_content()

    # Create the pywebview window
    window = webview.create_window(
        '◆ CLOUDCORE INTERFACE ◆',
        html=html_content,
        js_api=api,
        width=1200,
        height=800,
        resizable=True,
        background_color='#0f0c29',
        frameless=False,
        easy_drag=False
    )

    try:
        # Start the application
        webview.start(debug=False)
    finally:
        # On exit, attempt graceful stop
        try:
            api.tunnel_manager.stop()
        except Exception:
            pass
