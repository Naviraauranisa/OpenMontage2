"""python -m studio [--port 4760] [--no-browser]"""

from __future__ import annotations

import argparse
import threading
import webbrowser


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m studio")
    ap.add_argument("--port", type=int, default=4760)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    import uvicorn

    url = f"http://127.0.0.1:{args.port}"
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"Storyboard Studio jalan di {url}  (Ctrl+C untuk berhenti)")
    uvicorn.run("studio.server:app", host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
