import sys

# Ensure UTF-8 output on Windows console
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import uvicorn
import argparse
import os

def main():
    parser = argparse.ArgumentParser(description="Run Jio Number Bot Web Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="Port (default: 8000)")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload for development")
    args = parser.parse_args()

    print(f"[*] Starting Jio Number Bot API & WebSocket Server on http://{args.host}:{args.port}")
    uvicorn.run("server.api:app", host=args.host, port=args.port, reload=args.reload)

if __name__ == "__main__":
    main()
