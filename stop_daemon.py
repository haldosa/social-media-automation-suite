import os
import sys

pid_file = os.path.join(os.path.dirname(__file__), "daemon.pid")

try:
    pid = int(open(pid_file).read().strip())
    os.system(f"taskkill /PID {pid} /F")
    print(f"Sent kill to PID {pid}")
except FileNotFoundError:
    print("daemon.pid not found — daemon may not be running")
except ValueError:
    print("daemon.pid contains invalid PID")