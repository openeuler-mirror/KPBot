#Requires -Version 5.1
# Run with powershell or pwsh -NoProfile -NonInteractive -File tests/test_install_platform.ps1.
$ErrorActionPreference = 'Stop'

function Assert-True($Condition, $Message) {
    if (-not $Condition) { throw $Message }
}

function Write-Fixture($Path, $Content) {
    [void][IO.Directory]::CreateDirectory((Split-Path -Parent $Path))
    [IO.File]::WriteAllText($Path, $Content)
}

function Invoke-Install($Installer, $Level, $Tool, $Path) {
    $installArgs = @($Level, $Tool)
    if ($Path) { $installArgs += $Path }
    & $Installer @installArgs
    Assert-True ($LASTEXITCODE -eq 0) "Installation failed for $Level $Tool"
}

$repo = Split-Path -Parent $PSScriptRoot
$installer = Join-Path $repo 'install.ps1'
$temp = Join-Path ([IO.Path]::GetTempPath()) ('kpbot test [local] ' + [IO.Path]::GetRandomFileName())
$originalConfigHome = $env:XDG_CONFIG_HOME
$windows = [Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT
try {
    $project = Join-Path $temp 'actual project'
    Invoke-Install $installer 'project' 'claude' $project
    $claude = Join-Path $project '.claude'
    Assert-True (Test-Path -LiteralPath (Join-Path $claude 'skills/drive-claude-optimize-pipeline/SKILL.md')) 'Missing Claude-only skill'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $claude 'skills/kunpeng-knowledge'))) 'Installed OpenCode-only plugin in Claude'

    Invoke-Install $installer 'project' 'opencode' $project
    $open = Join-Path $project '.opencode'
    Assert-True (Test-Path -LiteralPath (Join-Path $open 'skills/kunpeng-knowledge/SKILL.md')) 'Missing OpenCode-only skill'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $open 'skills/drive-claude-optimize-pipeline'))) 'OpenCode exclusion failed'
    Assert-True (Test-Path -LiteralPath (Join-Path $open 'skills/agentenv-kernel-hz/references/validation.md')) 'Nested skill fallback missing'
    Assert-True (Test-Path -LiteralPath (Join-Path $open 'agents/faiss-reviewer.md')) 'Agent registration missing'
    Assert-True (Test-Path -LiteralPath (Join-Path $open 'skills/kpbot-code-optimizer/references/experiment-contract.md')) 'Overlay dropped base resources'
    $overlayFile = 'kpbot-code-optimizer/prompts/stage.parse-intent.md'
    $actualOverlay = [IO.File]::ReadAllText((Join-Path $open "skills/$overlayFile"))
    $expectedOverlay = [IO.File]::ReadAllText((Join-Path $repo "Plugins/code-optimizer/opencode/$overlayFile"))
    Assert-True ($actualOverlay -ceq $expectedOverlay) 'Overlay failed to replace base file'

    $fixture = Join-Path $temp ('source fixture ' + ('s' * 77))
    [void][IO.Directory]::CreateDirectory($fixture)
    $fixtureInstaller = Join-Path $fixture 'install.ps1'
    [IO.File]::Copy($installer, $fixtureInstaller)
    $windowsAdapter = 'adapters/windows/InstallTree.cs'
    $fixtureAdapter = Join-Path $fixture $windowsAdapter
    [void][IO.Directory]::CreateDirectory((Split-Path -Parent $fixtureAdapter))
    [IO.File]::Copy((Join-Path $repo $windowsAdapter), $fixtureAdapter)
    Write-Fixture (Join-Path $fixture 'package.json') '{"version":"test"}'
    Write-Fixture (Join-Path $fixture 'CLAUDE.md') '[Reference](Plugins/demo/guide.md)'
    Write-Fixture (Join-Path $fixture 'Plugins/demo/skills/group/DOMAIN.md') 'Group'
    Write-Fixture (Join-Path $fixture 'Plugins/demo/skills/group/old-skill/SKILL.md') 'Old skill'
    Write-Fixture (Join-Path $fixture 'Plugins/demo/skills/group/old-skill/agents/old-agent.md') 'Old agent'
    Write-Fixture (Join-Path $fixture 'Plugins/demo/skills/ungrouped/hidden/SKILL.md') 'Ungrouped skill'

    # Build the data under a short path, then rename only its shallow directory.
    # Windows PowerShell 5.1 can construct this fixture even with MAX_PATH enabled.
    $resource = (('r' * 48), ('r' * 48), ('r' * 48), 'payload.txt') -join [IO.Path]::DirectorySeparatorChar
    $sourceSkill = Join-Path $fixture 'Plugins/demo/skills/group/old-skill'
    $resourceRoot = Join-Path $temp 'data'
    Write-Fixture (Join-Path $resourceRoot $resource) 'Long-path resource'
    [IO.Directory]::Move($resourceRoot, (Join-Path $sourceSkill 'references'))
    Assert-True ((Join-Path $sourceSkill "references/$resource").Length -gt 260) 'Source fixture did not exceed MAX_PATH'

    $env:XDG_CONFIG_HOME = Join-Path $temp ('global config ' + ('g' * 83))
    $globalConfig = Join-Path $env:XDG_CONFIG_HOME 'opencode'
    $instructions = Join-Path $globalConfig 'AGENTS.md'
    Write-Fixture $instructions 'User configuration'
    Write-Fixture (Join-Path $globalConfig 'skills/custom/SKILL.md') 'User skill'
    Write-Fixture (Join-Path $globalConfig 'agents/custom.md') 'User agent'
    Invoke-Install $fixtureInstaller 'global' 'opencode'

    $installedConfig = [IO.File]::ReadAllText($instructions)
    $reference = $fixture.Replace('\', '/') + '/Plugins/demo/guide.md'
    Assert-True ($installedConfig.Contains("](<$reference>)")) 'Config contains an invalid path'
    $backup = Get-ChildItem -LiteralPath $globalConfig -Filter 'AGENTS.md.bak.*' -File
    Assert-True ([IO.File]::ReadAllText($backup.FullName) -eq 'User configuration') 'Config backup is missing or wrong'
    Assert-True (Test-Path -LiteralPath (Join-Path $globalConfig 'skills/old-skill/SKILL.md')) 'Domain skill not discovered'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $globalConfig 'skills/hidden'))) 'Ungrouped skill was installed'

    $installedSkill = Join-Path $globalConfig 'skills/old-skill'
    Assert-True ((Join-Path $installedSkill "references/$resource").Length -gt 260) 'Installed fixture did not exceed MAX_PATH'
    $verified = Join-Path $temp 'verify'
    [IO.Directory]::Move((Join-Path $installedSkill 'references'), $verified)
    Assert-True ([IO.File]::ReadAllText((Join-Path $verified $resource)) -ceq 'Long-path resource') 'Long-path resource was not copied'
    [IO.Directory]::Move($verified, (Join-Path $installedSkill 'references'))

    Invoke-Install $fixtureInstaller 'global' 'opencode'
    [IO.Directory]::Move((Join-Path $installedSkill 'references'), $verified)
    Assert-True ([IO.File]::ReadAllText((Join-Path $verified $resource)) -ceq 'Long-path resource') 'Long-path resource was lost on reinstall'
    [IO.Directory]::Move($verified, (Join-Path $installedSkill 'references'))

    [IO.Directory]::Move((Join-Path $sourceSkill 'references'), $resourceRoot)
    Remove-Item -LiteralPath $sourceSkill -Recurse -Force
    Write-Fixture (Join-Path $fixture 'Plugins/demo/skills/new-skill/SKILL.md') 'New skill'
    Invoke-Install $fixtureInstaller 'global' 'opencode'

    Assert-True (-not (Test-Path -LiteralPath (Join-Path $globalConfig 'skills/old-skill'))) 'Stale skill was not removed'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $globalConfig 'agents/old-agent.md'))) 'Stale agent was not removed'
    Assert-True (Test-Path -LiteralPath (Join-Path $globalConfig 'skills/custom/SKILL.md')) 'User skill was removed'
    Assert-True (Test-Path -LiteralPath (Join-Path $globalConfig 'agents/custom.md')) 'User agent was removed'
    $manifest = Get-Content -LiteralPath (Join-Path $globalConfig 'kpbot-manifest.json') -Raw | ConvertFrom-Json
    Assert-True (@($manifest.installed_skills).Count -eq 1 -and $manifest.installed_skills[0] -eq 'new-skill') 'Wrong managed skills in manifest'
    Assert-True (@(Get-ChildItem -LiteralPath $globalConfig -Filter 'AGENTS.md.bak.*' -File).Count -eq 1) 'Unchanged config was backed up again'

    Write-Host 'PowerShell installation regression checks passed.'
} finally {
    $env:XDG_CONFIG_HOME = $originalConfigHome
    if ($windows -and ('KPBot.InstallTree' -as [type])) {
        [KPBot.InstallTree]::Remove($temp)
    } elseif (Test-Path -LiteralPath $temp) {
        Remove-Item -LiteralPath $temp -Recurse -Force
    }
}
