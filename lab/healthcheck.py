"""Docker HEALTHCHECK for the trader image: exit 0 when the bot API or the site answers.

A bot (lab/league.py) serves freqtrade's API on 8080, the site (web/app.py) serves /healthz on 8000.
Every workload runs the same image, so the check accepts either.
"""

import sys
import urllib.request

URLS = ("http://127.0.0.1:8080/api/v1/ping", "http://127.0.0.1:8000/healthz")


def healthy(urls=URLS, timeout=3.0) -> bool:
    for url in urls:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                if resp.status == 200:
                    return True
        except OSError:
            continue
    return False


if __name__ == "__main__":
    sys.exit(0 if healthy() else 1)
