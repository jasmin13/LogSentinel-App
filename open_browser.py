import time
import urllib.request
import webbrowser

for _ in range(60):
    try:
        with urllib.request.urlopen("http://127.0.0.1:8002/") as res:
            if res.status == 200:
                webbrowser.open("http://localhost:8002")
                break
    except Exception:
        time.sleep(0.4)
