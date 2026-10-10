#Requires -Version 5.1
# Quita la tarea programada y la regla del firewall de kbm-follow.
# Tu configuración (%APPDATA%\kbm-follow) y el registro se quedan, por si lo reinstalas.
$ErrorActionPreference = 'Stop'
$TaskName = 'kbm-follow'

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host 'Tarea programada eliminada.'
} else {
    Write-Host 'La tarea programada no estaba instalada.'
}

$rule = Get-NetFirewallRule -DisplayName $TaskName -ErrorAction SilentlyContinue
if ($rule) {
    try {
        $rule | Remove-NetFirewallRule
        Write-Host 'Regla del firewall eliminada.'
    } catch {
        Write-Warning 'Para quitar la regla del firewall ejecútalo como administrador.'
    }
}
