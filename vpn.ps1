# ============================================================================
# vpn.ps1 - UNICT student VPN (L2TP/IPsec, built into Windows) for local tests.
#
# Usage (from the manager folder):
#     .\vpn.ps1 -Action connect      # create the VPN entry if missing, dial, add route to the cluster
#     .\vpn.ps1 -Action status
#     .\vpn.ps1 -Action test         # ssh 'echo ok' to the cluster, same options as the t2g driver
#     .\vpn.ps1 -Action disconnect
#
# Reads VPN_HOST, VPN_USER, VPN_PASSWORD (+ optional VPN_PSK, default VPNUNICT
# from the public UNICT guide) and T2G_SSH_* from .env. Split tunnel: only the
# cluster IP goes through the VPN. Windows only; does NOT run on Render.
# ============================================================================
param(
    [Parameter(Mandatory)][ValidateSet("connect", "disconnect", "status", "test")]
    [string]$Action
)

$ErrorActionPreference = "Stop"
$Name = "UNICT"

$envFile = Join-Path $PSScriptRoot ".env"
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
        $t = $line.Trim()
        if ($t -and -not $t.StartsWith("#")) {
            $k, $v = $t -split '=', 2
            if ($k -and $null -ne $v) { Set-Item -Path "Env:$($k.Trim())" -Value $v.Trim().Trim('"') }
        }
    }
}

function Get-ClusterIp {
    # ssh -G resolves ~/.ssh/config aliases; the DNS lookup must happen BEFORE dialing.
    $hostName = (ssh -G $Env:T2G_SSH_HOST | Where-Object { $_ -like "hostname *" }) -replace "^hostname ", ""
    (Resolve-DnsName $hostName -Type A | Select-Object -First 1).IPAddress
}

switch ($Action) {
    "connect" {
        foreach ($var in "VPN_HOST", "VPN_USER", "VPN_PASSWORD", "T2G_SSH_HOST") {
            if (-not (Get-Item "Env:$var" -ErrorAction SilentlyContinue)) { throw "$var missing in .env" }
        }
        $ip = Get-ClusterIp
        $psk = if ($Env:VPN_PSK) { $Env:VPN_PSK } else { "VPNUNICT" }
        if (-not (Get-VpnConnection -Name $Name -ErrorAction SilentlyContinue)) {
            Add-VpnConnection -Name $Name -ServerAddress $Env:VPN_HOST -TunnelType L2tp `
                -L2tpPsk $psk -AuthenticationMethod MSChapv2 -EncryptionLevel Required `
                -SplitTunneling -Force
        }
        # shortcut: rasdial takes the password as an argument (briefly visible in the process list), fine on a personal PC
        $out = rasdial $Name $Env:VPN_USER $Env:VPN_PASSWORD
        if ($LASTEXITCODE -ne 0) {
            # 691 = wrong credentials: do not retry in a loop (portal account lockout)
            throw "rasdial failed (exit $LASTEXITCODE): $($out | Select-Object -Last 2)"
        }
        Add-VpnConnectionRoute -ConnectionName $Name -DestinationPrefix "$ip/32" -ErrorAction SilentlyContinue
        Write-Host "connected; route to the cluster: $ip/32"
    }
    "disconnect" { rasdial $Name /disconnect }
    "status" {
        Get-VpnConnection -Name $Name | Format-List Name, ServerAddress, ConnectionStatus, SplitTunneling
        (Get-VpnConnection -Name $Name).Routes | Format-Table -AutoSize
    }
    "test" {
        $sshArgs = @("-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "StrictHostKeyChecking=accept-new")
        if ($Env:T2G_SSH_KEY_FILE) { $sshArgs += @("-i", $Env:T2G_SSH_KEY_FILE) }
        $target = if ($Env:T2G_SSH_USER) { "$($Env:T2G_SSH_USER)@$($Env:T2G_SSH_HOST)" } else { $Env:T2G_SSH_HOST }
        ssh @sshArgs $target "echo ok"
        if ($LASTEXITCODE -ne 0) { throw "ssh failed (exit $LASTEXITCODE): VPN up? check '.\vpn.ps1 -Action status'" }
    }
}
