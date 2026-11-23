"""Live Linux/Docker smoke checks. Creates and removes only its own workspaces.

Requires an already deployed service, two free workspace slots, no running
workspaces and Docker CLI access. No Claude model calls or translation service
are needed; real direct-chat model responses remain a separate manual check.
"""
import argparse
import getpass
import http.cookiejar
import json
from pathlib import Path
import secrets
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request


class Client:
    def __init__(self, origin):
        self.origin = origin.rstrip("/")
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        self.csrf = ""

    def request(self, method, path, body=None, expected=200):
        headers = {"Origin": self.origin, "X-CSRF-Token": self.csrf}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.origin + path, data=data, headers=headers, method=method)
        try:
            response = self.opener.open(req, timeout=90)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            payload = json.load(response)
            if response.status != expected:
                raise RuntimeError("%s %s returned %s, expected %s: %s" %
                                   (method, path.split("?")[0], response.status, expected, payload.get("detail", "request failed")))
            return payload

def docker(*args, expected=0):
    result = subprocess.run(["docker", *args], text=True, capture_output=True, timeout=40)
    if expected is not None and result.returncode != expected:
        # Don't expose Docker inspect/config output or any environment values.
        raise RuntimeError("Docker check failed: " + args[0])
    return result


def container_for(workspace_id):
    result = docker("ps", "-aq", "--filter", "label=io.claude-cloud.workspace=" + workspace_id)
    ids = result.stdout.split()
    if len(ids) != 1:
        raise RuntimeError("Expected one managed smoke-test container")
    info = json.loads(docker("inspect", ids[0]).stdout)[0]
    if info["Config"]["Labels"].get("io.claude-cloud.workspace") != workspace_id:
        raise RuntimeError("Smoke-test container ownership mismatch")
    return ids[0], info


def check_container(workspace_id):
    container, info = container_for(workspace_id)
    host = info["HostConfig"]
    assert info["Config"]["User"] == "1000:1000", "worker must be non-root"
    assert host["ReadonlyRootfs"] and not host["Privileged"], "worker filesystem/capability boundary"
    assert "ALL" in [value.upper() for value in host["CapDrop"]], "all capabilities must be dropped"
    assert any(value.startswith("no-new-privileges") for value in host["SecurityOpt"])
    assert host["Memory"] > 0 and host["PidsLimit"] > 0
    assert not host.get("PortBindings"), "workers cannot expose host ports"
    allowed_mounts = {("volume", "/workspace"), ("volume", "/home/claude"),
                      ("volume", "/etc/claude-code"), ("volume", "/run/claude-cloud-auth"), ("tmpfs", "/tmp")}
    actual_mounts = {(m["Type"], m["Destination"]) for m in info["Mounts"]}
    assert actual_mounts <= allowed_mounts, "unexpected host bind or other mount detected"
    assert {("volume", "/workspace"), ("volume", "/home/claude"), ("volume", "/etc/claude-code"),
            ("volume", "/run/claude-cloud-auth")} <= actual_mounts
    policy_mount = next(mount for mount in info["Mounts"] if mount["Destination"] == "/etc/claude-code")
    assert policy_mount["RW"] is False, "model policy must be read-only"
    owner = info["Config"]["Labels"]["io.claude-cloud.owner"]
    assert policy_mount["Name"] == "cc-" + owner + "-model-policy", "foreign model policy volume"
    policy_volume = json.loads(docker("volume", "inspect", policy_mount["Name"]).stdout)[0]
    assert policy_volume["Labels"].get("io.claude-cloud.owner") == owner
    assert policy_volume["Labels"].get("io.claude-cloud.resource") == "model-policy"
    auth_mount = next(mount for mount in info["Mounts"] if mount["Destination"] == "/run/claude-cloud-auth")
    assert auth_mount["RW"] is False, "shared authorization must be read-only"
    assert auth_mount["Name"] == "cc-" + owner + "-shared-auth", "foreign shared authorization volume"
    auth_volume = json.loads(docker("volume", "inspect", auth_mount["Name"]).stdout)[0]
    assert auth_volume["Labels"].get("io.claude-cloud.owner") == owner
    assert auth_volume["Labels"].get("io.claude-cloud.resource") == "shared-auth"
    policy_check = '''import errno, json, os
path = '/etc/claude-code/managed-settings.json'
with open(path) as source:
    policy = json.load(source)
assert policy['enforceAvailableModels'] is True
assert {'sonnet', 'opus', 'haiku'} <= set(policy['availableModels'])
assert policy['deniedModels'] == ([] if 'fable' in policy['availableModels'] else ['fable'])
try:
    fd = os.open(path, os.O_WRONLY)
except OSError as error:
    assert error.errno in (errno.EROFS, errno.EACCES)
else:
    os.close(fd)
    raise AssertionError('worker can modify managed model policy')
'''
    docker("exec", container, "python3", "-I", "-c", policy_check)
    networks = list(info["NetworkSettings"]["Networks"])
    assert len(networks) == 1, "worker must have one isolated network"
    network = json.loads(docker("network", "inspect", networks[0]).stdout)[0]
    assert network["Internal"] is True
    assert network["Options"].get("com.docker.network.bridge.gateway_mode_ipv4") == "isolated"
    docker("exec", container, "python3", "-c", "import os; assert not os.path.exists('/var/run/docker.sock')")
    docker("exec", container, "ln", "-s", "/etc/passwd", "/workspace/blocked-link")
    print("PASS container flags, volumes, isolated network and no Docker socket")
    return container


def check_network(container):
    # Check both literal and IPv4-mapped destinations, plus the domain ACL.
    for host in ("169.254.169.254", "127.0.0.1", "[::1]",
                 "[::ffff:127.0.0.1]", "[::ffff:a9fe:a9fe]",
                 "localhost", "nested.localhost"):
        blocked = docker("exec", container, "curl", "--globoff", "--max-time", "5", "--silent",
                         "--output", "/dev/null", "--write-out", "%{http_connect}",
                         "https://" + host + "/", expected=None)
        assert blocked.stdout.strip() == "403", "proxy failed to reject private destination: " + host
    direct = docker("exec", container, "curl", "--noproxy", "*", "--max-time", "4", "--silent", "--output", "/dev/null",
                    "https://1.1.1.1/", expected=None)
    assert direct.returncode != 0, "worker unexpectedly has direct internet access"
    proxied = docker("exec", container, "curl", "--max-time", "20", "--silent", "--output", "/dev/null",
                     "--write-out", "%{http_connect}", "https://api.anthropic.com/", expected=None)
    assert proxied.stdout.strip() == "200", "cannot establish public HTTPS tunnel to Anthropic"
    print("PASS proxy denies private/mapped destinations and direct internet, permits public HTTPS")


def check_terminal(client, workspace_id, solo=False):
    # Account controls now require the separate administrator session. This test
    # deliberately neither signs in to Claude nor requests a paid model response.
    admin = client.request("GET", "/api/admin/session")
    assert type(admin.get("enabled")) is bool, "administrator availability must be explicit"
    if solo:
        assert admin.get("authenticated") is True, "personal login must also authorize its settings"
        assert admin.get("csrfToken") == client.csrf, "personal settings must use the same session"
        client.request("GET", "/api/admin/claude/status")
    else:
        client.request("GET", "/api/admin/claude/status", expected=401 if admin["enabled"] else 503)
    base = "/api/workspaces/" + workspace_id
    client.request("GET", base + "/login-browser", expected=403)
    client.request("POST", base + "/terminal/stop", expected=403)
    print("PASS personal settings share the login session" if solo else "PASS ordinary dashboard session cannot access Claude account controls")


def check_proxy_recovery(client, workspace_id):
    """Simulate egress replacement by detaching only this test workspace."""
    container, before = container_for(workspace_id)
    networks = list(before["NetworkSettings"]["Networks"])
    assert len(networks) == 1
    network_name = networks[0]
    network = json.loads(docker("network", "inspect", network_name).stdout)[0]
    labels = before["Config"]["Labels"]
    assert network["Labels"].get("io.claude-cloud.workspace") == workspace_id
    assert network["Labels"].get("io.claude-cloud.owner") == labels["io.claude-cloud.owner"]
    proxy_ids = docker("compose", "ps", "-q", "egress").stdout.split()
    assert len(proxy_ids) == 1
    proxy_id = proxy_ids[0]
    base = "/api/workspaces/" + workspace_id
    try:
        for stopped in (False, True):
            if stopped:
                client.request("POST", base + "/stop")
            docker("network", "disconnect", network_name, proxy_id)
            client.request("POST", base + "/start")
            current, after = container_for(workspace_id)
            assert current == container, "network repair recreated the worker"
            before_mounts = {mount["Destination"]: mount for mount in before["Mounts"]}
            after_mounts = {mount["Destination"]: mount for mount in after["Mounts"]}
            assert after_mounts == before_mounts, "network repair changed workspace volumes"
            proxy = json.loads(docker("container", "inspect", proxy_id).stdout)[0]
            endpoint = proxy["NetworkSettings"]["Networks"][network_name]
            assert "egress" in endpoint["Aliases"], "proxy DNS alias was not restored"
            docker("exec", container, "getent", "hosts", "egress")
        check_network(container)
    finally:
        # Restore only the test network if an assertion failed before API repair.
        proxy = json.loads(docker("container", "inspect", proxy_id).stdout)[0]
        if network_name not in proxy["NetworkSettings"]["Networks"]:
            docker("network", "connect", "--alias", "egress", network_name, proxy_id)
    print("PASS running/stopped workspace proxy recovery without replacing its container or volumes")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default="http://localhost:8000")
    parser.add_argument("--username", help="An existing user created by the administrator; no default account")
    parser.add_argument("--password-file", type=Path, help="CI only: private file containing an ephemeral dashboard password")
    args = parser.parse_args()
    origin = urllib.parse.urlsplit(args.origin)
    if origin.scheme not in ("http", "https") or not origin.netloc or origin.username or origin.path not in ("", "/") or origin.query or origin.fragment:
        parser.error("--origin must be an http(s) origin without a path or credentials")
    if origin.scheme == "http" and origin.hostname not in ("localhost", "127.0.0.1", "::1"):
        parser.error("Use HTTPS for a remote origin, or a localhost SSH tunnel")
    client = Client(args.origin)
    client.request("GET", "/api/health")
    solo = client.request("GET", "/api/session").get("mode") == "solo"
    username = None if solo else (args.username if args.username is not None else input("Dashboard username: ")).strip()
    if not solo and not username:
        parser.error("A dashboard username is required; create the user in /admin first")
    password = args.password_file.read_text().strip() if args.password_file else getpass.getpass("Website password: ")
    client.request("GET", "/api/workspaces", expected=401)
    credentials = {"password": password} if solo else {"username": username, "password": password}
    client.csrf = client.request("POST", "/api/login", credentials)["csrfToken"]
    password = None
    credentials.clear()
    status = client.request("GET", "/api/status")
    assert status["docker"] and status["workerImage"] and not status.get("error"), "Docker, worker image or proxy unavailable"
    existing = client.request("GET", "/api/workspaces")["workspaces"]
    if any(item["running"] for item in existing):
        raise RuntimeError("Stop existing workspaces in the dashboard before smoke testing; none were modified")
    if len(existing) + 2 > status["maxWorkspaces"]:
        raise RuntimeError("Two free workspace slots are required; no existing workspaces were modified")
    marker = "smoke-" + secrets.token_hex(8)
    test_names = {marker + "-A", marker + "-B"}
    created = []
    try:
        first = client.request("POST", "/api/workspaces", {"name": marker + "-A"}, expected=201)
        created.append(first["id"])
        base = "/api/workspaces/" + first["id"]
        client.request("PUT", base + "/file", {"path": "sentinel.txt", "content": marker})
        assert client.request("GET", base + "/file?path=sentinel.txt")["content"] == marker
        client.request("GET", base + "/file?path=..%2Fetc%2Fpasswd", expected=400)
        container = check_container(first["id"])
        client.request("GET", base + "/file?path=blocked-link", expected=400)
        check_network(container)
        check_proxy_recovery(client, first["id"])
        check_terminal(client, first["id"], solo=solo)
        client.request("POST", base + "/stop")
        second = client.request("POST", "/api/workspaces", {"name": marker + "-B"}, expected=201)
        created.append(second["id"])
        second_base = "/api/workspaces/" + second["id"]
        entries = client.request("GET", second_base + "/files")["entries"]
        assert not any(item["name"] == "sentinel.txt" for item in entries), "workspace file leakage"
        client.request("POST", second_base + "/stop")
        client.request("POST", base + "/start")
        assert client.request("GET", base + "/file?path=sentinel.txt")["content"] == marker
        print("PASS workspace file isolation, path/symlink refusal and stop/start persistence")
    finally:
        # Provisioning can fail after registering an ID. Discover only our exact
        # random test names as well, then remove only those resources via the API.
        try:
            listing = client.request("GET", "/api/workspaces")["workspaces"]
            cleanup_ids = {item["id"] for item in listing if item["id"] in created or item["name"] in test_names}
            for workspace_id in cleanup_ids:
                client.request("DELETE", "/api/workspaces/" + workspace_id)
            print("Removed temporary smoke-test workspaces")
        except Exception:
            print("Cleanup incomplete: remove workspaces beginning with %s in the dashboard." % marker, file=sys.stderr)
            raise
    client.request("POST", "/api/logout")
    print("Live smoke checks passed. Official account sign-in still requires your manual verification.")


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt) as exc:
        print("Smoke check failed: %s" % exc, file=sys.stderr)
        sys.exit(1)
