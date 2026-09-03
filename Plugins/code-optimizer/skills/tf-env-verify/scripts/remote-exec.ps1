<#
.SYNOPSIS
    通过堡垒机在远程目标服务器上执行命令（脱敏版）。

.DESCRIPTION
    登录链路: 本地 -> <BASTION_HOST>(密码+选1) -> ext主机(root) -> ssh免密登录目标服务器 -> 执行命令

    使用前请按实际环境填写：
      - $Target 默认值：目标服务器 IP（占位符 <TARGET_IP>）
      - remote-exec.exp 内的堡垒机地址/用户/密码（占位符 <BASTION_HOST>/<BASTION_USER>/<BASTION_PASS>）

.PARAMETER Target
    目标服务器 IP (默认: <TARGET_IP>，请填写实际值)

.PARAMETER Command
    要执行的命令 (默认: lscpu)

.EXAMPLE
    .\remote-exec.ps1
    在默认目标服务器上执行 lscpu

.EXAMPLE
    .\remote-exec.ps1 -Command "df -h"
    执行 df -h

.EXAMPLE
    .\remote-exec.ps1 -Target "10.0.0.1" -Command "hostname"
    在指定服务器上执行命令
#>

param(
    [string]$Target = "<TARGET_IP>",
    [string]$Command = "lscpu",
    [int]$Timeout = 10
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ExpectScript = Join-Path $ScriptDir "remote-exec.exp"

if (-not (Test-Path $ExpectScript)) {
    Write-Error "找不到 expect 脚本: $ExpectScript"
    exit 1
}

Write-Host "=== 堡垒机链路远程执行 ===" -ForegroundColor Cyan
Write-Host "目标: $Target" -ForegroundColor Yellow
Write-Host "命令: $Command" -ForegroundColor Yellow
Write-Host ""

$drive = $ExpectScript.Substring(0, 1).ToLower()
$wslScriptPath = "/mnt/$drive" + $ExpectScript.Substring(2) -replace '\\', '/'

& wsl expect "$wslScriptPath" $Target $Command $Timeout 2>&1
