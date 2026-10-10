#Requires -Version 5.1
<#
.SYNOPSIS
  Instala kbm-follow para que arranque solo cada vez que inicias sesión en Windows.

.DESCRIPTION
  1. Crea el entorno virtual (.venv) e instala el paquete.
  2. Revisa que ya exista la configuración (kbm-follow init ...).
  3. Registra la tarea programada "kbm-follow" (al iniciar sesión, sin ventana).
  4. Si lo ejecutas como administrador, abre el puerto UDP en el firewall (redes privadas).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\install\windows\install.ps1
#>
[CmdletBinding()]
param(
    [int]$Port = 47809
)

$ErrorActionPreference = 'Stop'
$TaskName = 'kbm-follow'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Venv = Join-Path $Root '.venv'
$Python = Join-Path $Venv 'Scripts\python.exe'
# pythonw.exe es Python sin consola: así no queda una ventana negra abierta todo el día.
$PythonW = Join-Path $Venv 'Scripts\pythonw.exe'

function Test-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$identity).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function New-Venv {
    if (Get-Command py -ErrorAction SilentlyContinue) { & py -3 -m venv $Venv }
    elseif (Get-Command python -ErrorAction SilentlyContinue) { & python -m venv $Venv }
    else { throw 'No encontré Python. Instálalo con: winget install Python.Python.3.12' }
    if ($LASTEXITCODE -ne 0) { throw 'No se pudo crear el entorno virtual' }
}

# OneDrive intenta sincronizar los miles de archivos de .venv y a veces los deja a medias.
if ($Root -match 'OneDrive') {
    Write-Warning ('Esta carpeta está sincronizada con OneDrive y eso puede romper el entorno ' +
        'virtual. Mejor clona el proyecto en una carpeta normal, por ejemplo $HOME\kbm-follow.')
}

Push-Location $Root
try {
    if (-not (Test-Path $Python)) {
        Write-Host '-> Creando el entorno virtual...'
        New-Venv
    }

    Write-Host '-> Instalando kbm-follow...'
    & $Python -m pip install --quiet --disable-pip-version-check -e $Root
    if ($LASTEXITCODE -ne 0) { throw 'Falló pip install' }

    & $Python -m kbm_follow status
    if ($LASTEXITCODE -ne 0) {
        throw ('Primero crea la configuración, por ejemplo: .venv\Scripts\kbm-follow init --channel 1 ' +
            '--leader "F75" --follower "logitech-hidpp:MX Master 3S"')
    }

    Write-Host '-> Registrando la tarea programada...'
    $user = "$env:USERDOMAIN\$env:USERNAME"
    $action = New-ScheduledTaskAction -Execute $PythonW -Argument '-m kbm_follow run' -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    # Sin límite de tiempo, aunque estés con batería, y si se cae se reinicia al minuto.
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    try {
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
            -Settings $settings -Principal $principal -Force | Out-Null
    } catch {
        throw "No se pudo registrar la tarea ($($_.Exception.Message)). Ejecuta PowerShell como administrador."
    }

    # Sin esta regla, Windows bloquea los avisos que llegan de la otra compu.
    if (Test-Admin) {
        Write-Host "-> Abriendo el puerto UDP $Port en el firewall (redes privadas)..."
        Get-NetFirewallRule -DisplayName $TaskName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
        New-NetFirewallRule -DisplayName $TaskName -Direction Inbound -Protocol UDP -LocalPort $Port `
            -Action Allow -Profile Private, Domain | Out-Null
    } else {
        Write-Warning ('No lo ejecutaste como administrador, así que no se configuró el firewall. Si ' +
            'la otra compu no recibe los avisos, vuelve a ejecutarlo como administrador.')
    }

    Start-ScheduledTask -TaskName $TaskName
    Write-Host ''
    Write-Host 'Listo. kbm-follow ya corre en segundo plano y arrancará solo al iniciar sesión.'
    Write-Host "Registro: $env:LOCALAPPDATA\kbm-follow\kbm-follow.log"
} finally {
    Pop-Location
}
