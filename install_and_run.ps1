# Installs Python and QEMU (qemu-img) if missing, sets up a local venv, then opens the GUI.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}

function Find-Python {
    foreach ($c in @("py -3", "python")) {
        try {
            $parts = $c.Split(" ")
            $out = & $parts[0] $parts[1..($parts.Length)] --version 2>&1   # the Store stub prints no version
            if ($LASTEXITCODE -eq 0 -and "$out" -match "Python 3\.(\d+)" -and [int]$Matches[1] -ge 10) { return $c }
        } catch {}
    }
    $p = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue | Select-Object -Last 1
    if ($p) { return "`"$($p.FullName)`"" }
    return $null
}

function Install-Python {
    Write-Host "Installing Python 3.12 ..."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
    } else {
        $exe = Join-Path $env:TEMP "python-installer.exe"
        Invoke-WebRequest "https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe" -OutFile $exe
        Start-Process $exe -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1" -Wait
    }
    Refresh-Path
}

function Find-Qemu {
    if (Get-Command qemu-img -ErrorAction SilentlyContinue) { return $true }
    foreach ($d in @("$env:ProgramFiles\qemu", "${env:ProgramFiles(x86)}\qemu", "$env:LOCALAPPDATA\Programs\qemu")) {
        if (Test-Path "$d\qemu-img.exe") { $env:Path = "$d;$env:Path"; return $true }
    }
    return $false
}

function Install-Qemu {
    Write-Host "Installing QEMU (qemu-img) ..."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install -e --id SoftwareFreedomConservancy.QEMU --silent --accept-package-agreements --accept-source-agreements
        Refresh-Path
    }
    if (-not (Find-Qemu)) {
        Write-Warning "Could not install QEMU automatically. Install it from https://qemu.weilnetz.de/w64/ and run this script again."
        exit 1
    }
}

$py = Find-Python
if (-not $py) { Install-Python; $py = Find-Python }
if (-not $py) { Write-Warning "Python could not be installed automatically. Get it from https://www.python.org/downloads/"; exit 1 }
if (-not (Find-Qemu)) { Install-Qemu }

if (-not (Test-Path ".venv\Scripts\vm2morpheus.exe")) {
    Write-Host "Creating virtual environment and installing dependencies ..."
    Invoke-Expression "$py -m venv .venv"
    & .venv\Scripts\python.exe -m pip install --upgrade pip
    & .venv\Scripts\python.exe -m pip install -e ".[all]"
}
& .venv\Scripts\vm2morpheus.exe gui
