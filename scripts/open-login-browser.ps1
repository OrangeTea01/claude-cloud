# Run on the Windows client, not on the Linux server. Requires OpenSSH and Chrome/Edge.
[CmdletBinding()]
param(
    [string]$Server,
    [ValidatePattern('^[A-Za-z_][A-Za-z0-9_.-]*$')][string]$SshUser = 'root',
    [ValidateRange(1, 65535)][int]$SshPort = 22,
    [string]$DashboardUrl = 'http://localhost:8000',
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'

function Test-LocalPort([int]$Port) {
    $probe = [System.Net.Sockets.TcpClient]::new()
    try {
        $pending = $probe.ConnectAsync('127.0.0.1', $Port)
        return ($pending.Wait(300) -and $probe.Connected)
    } catch { return $false } finally { $probe.Dispose() }
}

function Get-FreeLocalPort {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    try {
        $listener.Start()
        return $listener.LocalEndpoint.Port
    } finally { $listener.Stop() }
}

function Test-Socks5([int]$Port) {
    $probe = [System.Net.Sockets.TcpClient]::new()
    try {
        if (-not $probe.ConnectAsync('127.0.0.1', $Port).Wait(300)) { return $false }
        $stream = $probe.GetStream()
        $stream.ReadTimeout = 1000
        $stream.WriteTimeout = 1000
        $greeting = [byte[]]@(5, 1, 0)
        $stream.Write($greeting, 0, $greeting.Length)
        return ($stream.ReadByte() -eq 5 -and $stream.ReadByte() -eq 0)
    } catch { return $false } finally { $probe.Dispose() }
}

function Test-ExistingDashboard {
    # Identify an existing dashboard before reusing a busy port. No cookies or
    # credentials are sent; disable system proxies and HTTP redirects here.
    try {
        foreach ($path in @('/api/health', '/')) {
            $request = [System.Net.HttpWebRequest]::Create('http://127.0.0.1:8000' + $path)
            $request.Proxy = $null
            $request.AllowAutoRedirect = $false
            $request.Timeout = 2000
            $request.ReadWriteTimeout = 2000
            $response = $request.GetResponse()
            try {
                if ([int]$response.StatusCode -ne 200) { return $false }
                $reader = [System.IO.StreamReader]::new($response.GetResponseStream())
                try { $body = $reader.ReadToEnd() } finally { $reader.Dispose() }
            } finally { $response.Dispose() }
            if ($path -eq '/api/health') {
                $health = $body | ConvertFrom-Json
                if ($health.ok -isnot [bool] -or -not $health.ok) { return $false }
            } elseif ($body -notmatch '<title>Claude Cloud') { return $false }
        }
        return $true
    } catch { return $false }
}

if (-not $Server) { $Server = Read-Host 'Server IP or hostname (without root@)' }
$Server = $Server.Trim()
if ($Server -notmatch '^[A-Za-z0-9][A-Za-z0-9.:-]*$' -or $Server.Length -gt 253) {
    throw 'Enter only a server IP or hostname, without spaces, a URL, or root@.'
}

$dashboard = $null
if (-not [Uri]::TryCreate($DashboardUrl, [UriKind]::Absolute, [ref]$dashboard) -or
    $dashboard.Scheme -notin @('http', 'https') -or $dashboard.UserInfo -or
    $dashboard.AbsolutePath -ne '/' -or $dashboard.Query -or $dashboard.Fragment) {
    throw 'DashboardUrl must be a single http(s) origin without a path or credentials.'
}
$ssh = (Get-Command ssh.exe -ErrorAction Stop).Source
$candidates = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe"
)
$browser = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $browser) { throw 'Install Chrome or Microsoft Edge before running this launcher.' }

# A new profile prevents an already-open ordinary browser from taking this command.
# It also avoids modifying any existing browser settings or signed-in profile.
$profile = Join-Path $env:LOCALAPPDATA ('ClaudeCloud\LoginBrowser\' + [Guid]::NewGuid().ToString('N'))
$socksPort = if ($CheckOnly) { 1080 } else { Get-FreeLocalPort }
$sshArgs = @('-N', '-T', '-a', '-x', '-p', "$SshPort", '-l', $SshUser,
    '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=30',
    '-o', 'ServerAliveCountMax=3', '-D', "127.0.0.1:$socksPort")
if ($dashboard.Host -in @('localhost', '127.0.0.1') -and $dashboard.Scheme -eq 'http') {
    if ($dashboard.Port -ne 8000) { throw 'The local dashboard must use port 8000, matching PUBLIC_ORIGIN.' }
    if ($CheckOnly -or -not (Test-LocalPort 8000)) {
        $sshArgs += @('-L', '127.0.0.1:8000:127.0.0.1:8000')
    } else {
        if (-not (Test-ExistingDashboard)) {
            throw 'Port 8000 is occupied but is not a healthy Claude Cloud dashboard. Close that conflicting service or repair the existing tunnel first.'
        }
        Write-Host 'Found an existing Claude Cloud dashboard on port 8000. Keeping its connection.'
    }
} elseif ($dashboard.Scheme -ne 'https') {
    throw 'Use HTTPS for a public dashboard, or http://localhost:8000 for the SSH tunnel.'
}
$sshArgs += $Server

# Only one proxy, with no DIRECT fallback. Localhost is retained for the dashboard.
# Both WebRTC switches are supplied for Chromium versions before/after the rename;
# the profile preference below provides the same policy independently of the switch.
$browserArgs = @(
    ('--user-data-dir="' + $profile + '"'),
    "--proxy-server=socks5://127.0.0.1:$socksPort",
    # Chromium maps literal IPs too: the SOCKS endpoint itself must be excluded.
    '--host-resolver-rules="MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost"',
    '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
    '--webrtc-ip-handling-policy=disable_non_proxied_udp',
    '--disable-quic', '--disable-extensions', '--disable-background-networking',
    '--disable-sync', '--no-first-run', '--no-default-browser-check', '--new-window',
    'https://www.cloudflare.com/cdn-cgi/trace',
    ('"' + $dashboard.GetLeftPart([UriPartial]::Authority) + '"')
)
$preferences = @{
    webrtc = @{ ip_handling_policy = 'disable_non_proxied_udp' }
    credentials_enable_service = $false
    profile = @{
        name = 'Claude Cloud - SSH Login'
        password_manager_enabled = $false
        default_content_setting_values = @{ geolocation = 2; notifications = 2 }
    }
}
if ($CheckOnly) {
    # Offline validation: no profile writes, processes, SSH, or network probes.
    [pscustomobject]@{
        Browser = $browser; Ssh = $ssh; SshArguments = $sshArgs
        BrowserArguments = $browserArgs; ProfilePreferences = $preferences
    } | ConvertTo-Json -Depth 6
    return
}

Write-Host 'An SSH window will open. Complete its password/key and host verification prompts.'
Write-Host 'Keep that SSH window open for the entire browser login.'
# This is an interactive terminal, intentionally visible for SSH authentication.
$tunnel = Start-Process -FilePath $ssh -ArgumentList $sshArgs -WindowStyle Normal -PassThru
try {
    $deadline = [DateTime]::UtcNow.AddMinutes(3)
    $ready = $false
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($tunnel.HasExited) { throw "SSH exited (code $($tunnel.ExitCode)). Check the server address, login and SSH settings." }
        if (Test-Socks5 $socksPort) { $ready = $true; break }
        Start-Sleep -Milliseconds 250
    }
    if (-not $ready) { throw 'SSH did not become ready within 3 minutes. Check or close its window before retrying.' }

    $defaultProfile = Join-Path $profile 'Default'
    New-Item -ItemType Directory -Path $defaultProfile -Force | Out-Null
    $utf8 = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText((Join-Path $defaultProfile 'Preferences'), ($preferences | ConvertTo-Json -Depth 6), $utf8)
    Start-Process -FilePath $browser -ArgumentList $browserArgs | Out-Null
    Write-Host ''
    Write-Host 'Opened a dedicated browser with the SSH proxy.' -ForegroundColor Green
    Write-Host 'First tab: check the ip= line against your server outbound IP. No IP match was verified automatically.'
    Write-Host 'Second tab: open your workspace and perform the official Claude login IN THIS WINDOW.'
    Write-Host 'Paste any login code back into the original terminal session that generated its link.'
    Write-Host 'A new external HTTPS request should fail if the SSH window is closed.'
    Write-Host 'When finished, close the dedicated browser and its SSH window.'
    Write-Host "Dedicated browser data remains at: $profile"
} catch {
    # This exact process was started above; never stop a pre-existing SSH tunnel.
    if (-not $tunnel.HasExited) { $tunnel.Kill() }
    throw
}
