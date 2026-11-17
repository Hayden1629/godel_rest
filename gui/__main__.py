"""Launch the Gödel relay control panel in its own window.

    python3 -m gui              # native window (pywebview), else Chrome app window
    python3 -m gui --browser    # just serve + open in the default browser
    python3 -m gui --port 8766

If a panel is already running on the port, this only opens another window on it.
"""
import argparse
import os
import subprocess
import threading
import webbrowser

from .server import HOST, PORT, make_server

WINDOW_TITLE = "Gödel Relay"
WINDOW_SIZE = (1320, 860)
CHROME_APP = "/Applications/Google Chrome.app"


def open_fallback(url: str) -> None:
    """Chrome --app gives a chromeless standalone window; else a normal tab."""
    if os.path.isdir(CHROME_APP):
        subprocess.Popen(["open", "-na", CHROME_APP, "--args", f"--app={url}"])
    else:
        webbrowser.open(url)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--port", type=int, default=PORT)
    p.add_argument("--browser", action="store_true",
                   help="open in the browser instead of a native window")
    args = p.parse_args()
    url = f"http://{HOST}:{args.port}/"

    try:
        server = make_server(args.port)
    except OSError:
        print(f"[gui] already running at {url}; opening a window", flush=True)
        open_fallback(url)
        return
    print(f"[gui] serving {url}", flush=True)

    webview = None
    if not args.browser:
        try:
            import webview  # pywebview
        except ImportError:
            print("[gui] pywebview not installed; using a browser window "
                  "(pip install pywebview for a native one)", flush=True)

    if webview is None:
        open_fallback(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return

    threading.Thread(target=server.serve_forever, daemon=True).start()
    webview.create_window(WINDOW_TITLE, url, width=WINDOW_SIZE[0],
                          height=WINDOW_SIZE[1], background_color="#000000",
                          min_size=(900, 600))
    webview.start()  # blocks until the window closes
    server.shutdown()


if __name__ == "__main__":
    main()
