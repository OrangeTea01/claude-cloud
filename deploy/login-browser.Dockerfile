FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium xvfb x11vnc openbox python3 ca-certificates fonts-liberation fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --user-group --no-create-home --home-dir /tmp/browser-home browser
COPY deploy/login_browser/ /opt/login-browser/
RUN python3 -m py_compile /opt/login-browser/runner.py /opt/login-browser/control.py /opt/login-browser/relay.py
USER 1000:1000
ENV HOME=/tmp/browser-home DISPLAY=:99 LANG=C.UTF-8
CMD ["python3", "-I", "/opt/login-browser/runner.py"]
