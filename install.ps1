#Requires -Version 5.1
# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# Install KPBot skills from Windows PowerShell or PowerShell 7.

$ErrorActionPreference = 'Stop'
$useWin32Tree = [Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT

function Show-Usage {
    Write-Host @'
Usage: install.ps1 [project|global] [claude|opencode|dsh] [install_path]
       install.ps1 -h | --help

Defaults: project, claude, current directory.
A global DSH path overrides DSH_HOME. Other global installs use the user config directory.
Examples:
  .\install.ps1 project claude 'C:\My Project'
  .\install.ps1 global opencode
  .\install.ps1 global dsh 'C:\My DSH Home'
'@
}

function Read-JsonFile($Path) {
    if ([IO.File]::Exists($Path)) {
        Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    }
}

function Get-SkillDirectories($Root, $Depth = 1) {
    if (-not [IO.Directory]::Exists($Root)) { return }
    foreach ($child in Get-ChildItem -LiteralPath $Root -Directory | Sort-Object Name) {
        if ([IO.File]::Exists((Join-Path $child.FullName 'SKILL.md'))) {
            $child
        } elseif ($Depth -lt 4 -and [IO.File]::Exists((Join-Path $child.FullName 'DOMAIN.md'))) {
            Get-SkillDirectories $child.FullName ($Depth + 1)
        } elseif (Get-ChildItem -LiteralPath $child.FullName -Directory |
            Where-Object { [IO.File]::Exists((Join-Path $_.FullName 'SKILL.md')) } |
            Select-Object -First 1) {
            Write-Warning "Skill group without DOMAIN.md, skipped: $($child.FullName)"
        }
    }
}

function Copy-Tree($Source, $Destination) {
    if ($useWin32Tree) {
        [KPBot.InstallTree]::Copy($Source, $Destination)
        return
    }
    [void][IO.Directory]::CreateDirectory($Destination)
    foreach ($child in Get-ChildItem -LiteralPath $Source -Force) {
        $target = Join-Path $Destination $child.Name
        if ($child.PSIsContainer) {
            Copy-Tree $child.FullName $target
        } else {
            [IO.File]::Copy($child.FullName, $target, $true)
        }
    }
}

function Remove-InstallItem($Path) {
    if ($useWin32Tree) {
        [KPBot.InstallTree]::Remove($Path)
        return
    }
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if ($null -eq $item) { return }
    if ($item.PSIsContainer -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        # Remove a Windows junction without following it into another directory.
        [IO.Directory]::Delete($item.FullName)
    } else {
        Remove-Item -LiteralPath $item.FullName -Recurse -Force
    }
}

function Assert-LeafName($Name) {
    if ($Name -isnot [string] -or $Name -in @('', '.', '..') -or
        $Name -match '[<>:"/\\|?*\x00-\x1f]|[. ]$') {
        throw "Invalid installation manifest entry: '$Name'"
    }
}

function Install-Config($Source, $Target, $Level, $ProjectRoot) {
    if (-not [IO.File]::Exists($Source) -or $Source -eq $Target) { return }

    $content = [IO.File]::ReadAllText($Source)
    if ($Level -eq 'global' -or $ProjectRoot -ne $PSScriptRoot) {
        $prefix = $PSScriptRoot.Replace('\', '/') + '/Plugins/'
        $content = [regex]::Replace($content, '\]\(Plugins/([^)\r\n]+)\)', {
            param($link)
            '](<' + $prefix + $link.Groups[1].Value + '>)'
        }).Replace('`Plugins/', '`' + $prefix)
    }
    if ([IO.File]::Exists($Target)) {
        $existing = [IO.File]::ReadAllText($Target)
        if ($content -ceq $existing) { return }

        $backup = "$Target.bak.$(Get-Date -Format 'yyyyMMdd_HHmmss_fffffff')"
        [IO.File]::Copy($Target, $backup)
        Write-Host "Backed up configuration to $backup"
        $nonInteractive = [Environment]::GetCommandLineArgs() -match '^[-/]noni(nteractive)?$'
        if ($Level -eq 'global' -and -not $nonInteractive -and $Host.Name -eq 'ConsoleHost' -and
            -not [Console]::IsInputRedirected -and -not [Console]::IsOutputRedirected) {
            $choice = Read-Host "Replace, merge, or skip $(Split-Path -Leaf $Target)? [O/M/S]"
            switch -Wildcard ($choice) {
                's*' { return }
                'm*' { $content += "`n`n<!-- === User custom content below === -->`n`n" + $existing }
            }
        }
    }

    $temp = "$Target.$([IO.Path]::GetRandomFileName())"
    try {
        [IO.File]::WriteAllText($temp, $content)
        Move-Item -LiteralPath $temp -Destination $Target -Force
    } finally {
        [IO.File]::Delete($temp)
    }
}

if ('--help' -in $args -or '-h' -in $args) {
    Show-Usage
    exit 0
}
if ($args.Count -eq 0) {
    Show-Usage
    exit 1
}

try {
    $level = 'project'
    $tool = 'claude'
    $position = 0
    if ($args[$position] -cin @('project', 'global')) {
        $level = $args[$position]
        $position++
    }
    if ($position -lt $args.Count -and $args[$position] -cin @('claude', 'opencode', 'dsh')) {
        $tool = $args[$position]
        $position++
    }
    if ($args.Count - $position -gt 1) {
        throw 'Expected [project|global] [claude|opencode|dsh] [install_path]. See --help.'
    }
    $installPath = $args[$position]

    if ($tool -eq 'dsh') {
        $node = Get-Command node -CommandType Application | Select-Object -First 1
        $adapterArgs = @('install', $level, 'dsh')
        if ($installPath) { $adapterArgs += $installPath }
        & $node.Source (Join-Path $PSScriptRoot 'adapters/dsh/install.mjs') @adapterArgs
        exit $LASTEXITCODE
    }

    if ($useWin32Tree -and -not ('KPBot.InstallTree' -as [type])) {
        Add-Type -Path (Join-Path $PSScriptRoot 'adapters/windows/InstallTree.cs')
    }

    $projectRoot = $PWD.ProviderPath
    if ($level -eq 'project' -and $installPath) {
        $projectRoot = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($installPath)
    }
    $configName = 'CLAUDE.md'
    $configRoot = Join-Path $projectRoot '.claude'
    if ($tool -eq 'opencode') {
        $configName = 'AGENTS.md'
        $configRoot = Join-Path $projectRoot '.opencode'
    }
    $configTarget = Join-Path $projectRoot $configName
    if ($level -eq 'global') {
        if ($tool -eq 'opencode') {
            $configHome = $env:XDG_CONFIG_HOME
            if (-not $configHome) { $configHome = Join-Path $HOME '.config' }
            $configHome = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($configHome)
            $configRoot = Join-Path $configHome 'opencode'
        } else {
            $configRoot = Join-Path $HOME '.claude'
        }
        $configTarget = Join-Path $configRoot $configName
    }

    $pluginsRoot = Join-Path $PSScriptRoot 'Plugins'
    $plugins = @(Get-ChildItem -LiteralPath $pluginsRoot -Directory | Sort-Object Name)
    $plugins += @($plugins | Get-ChildItem -Directory | Sort-Object Parent, Name)
    $skills = [ordered]@{}
    $overlays = @()
    $excludes = @()
    foreach ($plugin in $plugins) {
        $skillRoot = Join-Path $plugin.FullName 'skills'
        $overlayRoot = Join-Path $plugin.FullName 'opencode'
        $metadata = Read-JsonFile (Join-Path $plugin.FullName '.claude-plugin/plugin.json')
        if ($metadata.platforms -and $tool -cnotin $metadata.platforms) { continue }

        foreach ($skill in Get-SkillDirectories $skillRoot) {
            if ($skills.Contains($skill.Name)) {
                Write-Warning "Duplicate skill name: $($skill.Name) ($($skill.FullName))"
            }
            $skills[$skill.Name] = $skill.FullName
        }
        if ($tool -eq 'opencode') {
            $overlays += @(Get-SkillDirectories $overlayRoot)
            if ($metadata.opencode.exclude) { $excludes += $metadata.opencode.exclude }
        }
    }

    $skillsRoot = Join-Path $configRoot 'skills'
    $agentsRoot = Join-Path $configRoot 'agents'
    $manifestPath = Join-Path $configRoot 'kpbot-manifest.json'
    if ([IO.Directory]::Exists($manifestPath) -or [IO.Directory]::Exists($configTarget)) {
        throw 'Expected file paths for the manifest and tool configuration.'
    }
    $previous = Read-JsonFile $manifestPath
    $previousSkills = @()
    $previousAgents = @()
    # Bash manifests inventory all directories, including user skills.
    if ($previous.brand -eq 'KPBot' -and $previous.tool -eq $tool -and $previous.installer -eq 'powershell') {
        if ($previous.installed_skills) { $previousSkills = @($previous.installed_skills) }
        if ($previous.installed_agents) { $previousAgents = @($previous.installed_agents) }
    }
    foreach ($name in ($previousSkills + $previousAgents)) { Assert-LeafName $name }

    $package = Read-JsonFile (Join-Path $PSScriptRoot 'package.json')
    [void][IO.Directory]::CreateDirectory($configRoot)
    $stage = Join-Path $configRoot ([IO.Path]::GetRandomFileName())
    try {
        [void][IO.Directory]::CreateDirectory($stage)
        foreach ($name in $skills.Keys) {
            if ($name -notin $excludes) {
                Copy-Tree $skills[$name] (Join-Path $stage $name)
            }
        }
        foreach ($overlay in $overlays) {
            if ($overlay.Name -notin $excludes) {
                Copy-Tree $overlay.FullName (Join-Path $stage $overlay.Name)
            }
        }

        # Flatten packaged skills for discovery and supply package-level fallback resources.
        foreach ($packageDir in Get-ChildItem -LiteralPath $stage -Directory | Sort-Object Name) {
            $innerRoot = Join-Path $packageDir.FullName 'skills'
            if (-not [IO.Directory]::Exists($innerRoot)) { continue }
            foreach ($inner in Get-ChildItem -LiteralPath $innerRoot -Directory | Sort-Object Name) {
                if ($inner.Name -in $excludes -or -not [IO.File]::Exists((Join-Path $inner.FullName 'SKILL.md'))) {
                    continue
                }
                $target = Join-Path $stage $inner.Name
                if ([IO.Directory]::Exists($target)) { continue }
                Copy-Tree $inner.FullName $target
                foreach ($resource in 'subskills', 'references', 'scripts', 'agents') {
                    $fallback = Join-Path $packageDir.FullName $resource
                    $resourceTarget = Join-Path $target $resource
                    if ([IO.Directory]::Exists($fallback) -and -not [IO.Directory]::Exists($resourceTarget)) {
                        Copy-Tree $fallback $resourceTarget
                    }
                }
            }
        }

        $prepared = @(Get-ChildItem -LiteralPath $stage -Directory | Sort-Object Name)
        if ($prepared.Count -eq 0) { throw "No KPBot skills found for $tool." }
        $commonRoot = Join-Path $pluginsRoot 'common/scripts'
        $commonScripts = @(Get-ChildItem -LiteralPath $commonRoot -Filter '*.sh' -File -ErrorAction SilentlyContinue)
        $agents = [ordered]@{}
        foreach ($skill in $prepared) {
            $content = [IO.File]::ReadAllText((Join-Path $skill.FullName 'SKILL.md'))
            foreach ($script in $commonScripts) {
                if (-not $content.Contains($script.Name)) { continue }
                $scriptsDir = Join-Path $skill.FullName 'scripts'
                $target = Join-Path $scriptsDir $script.Name
                if (-not [IO.File]::Exists($target)) {
                    [void][IO.Directory]::CreateDirectory($scriptsDir)
                    [IO.File]::Copy($script.FullName, $target)
                }
            }
            $agentDir = Join-Path $skill.FullName 'agents'
            if (-not [IO.Directory]::Exists($agentDir)) { continue }
            foreach ($agent in Get-ChildItem -LiteralPath $agentDir -Filter '*.md' -File) {
                $target = Join-Path $agentsRoot $agent.Name
                if (-not $agents.Contains($agent.Name) -and
                    ($agent.Name -in $previousAgents -or -not (Test-Path -LiteralPath $target))) {
                    $agents[$agent.Name] = Join-Path (Join-Path $skill.Name 'agents') $agent.Name
                }
            }
        }

        Write-Host "Installing $($prepared.Count) KPBot skills for $tool ($level) in $configRoot"
        [void][IO.Directory]::CreateDirectory($skillsRoot)
        [void][IO.Directory]::CreateDirectory($agentsRoot)
        foreach ($skill in $prepared) {
            $target = Join-Path $skillsRoot $skill.Name
            Remove-InstallItem $target
            [IO.Directory]::Move($skill.FullName, $target)
        }
        foreach ($name in $previousSkills) {
            if ($name -notin $prepared.Name) { Remove-InstallItem (Join-Path $skillsRoot $name) }
        }
        foreach ($name in $agents.Keys) {
            $target = Join-Path $agentsRoot $name
            if ($name -in $previousAgents) { Remove-InstallItem $target }
            [IO.File]::Copy((Join-Path $skillsRoot $agents[$name]), $target)
        }
        foreach ($name in $previousAgents) {
            if (-not $agents.Contains($name)) { Remove-InstallItem (Join-Path $agentsRoot $name) }
        }
        Remove-InstallItem (Join-Path $configRoot 'kpbot')
        Install-Config (Join-Path $PSScriptRoot 'CLAUDE.md') $configTarget $level $projectRoot

        $manifest = [ordered]@{
            brand = 'KPBot'
            version = $package.version
            level = $level
            tool = $tool
            installed_skills = @($prepared.Name)
            installed_agents = @($agents.Keys)
            installer = 'powershell'
            brand_dir = $configRoot
            install_time = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
        }
        $tempManifest = Join-Path $stage 'kpbot-manifest.json'
        [IO.File]::WriteAllText($tempManifest, (($manifest | ConvertTo-Json) + "`n"))
        Move-Item -LiteralPath $tempManifest -Destination $manifestPath -Force
        Write-Host "KPBot installed successfully. Start $tool to use the skills."
    } finally {
        Remove-InstallItem $stage
    }
} catch {
    [Console]::Error.WriteLine("KPBot installation failed: $($_.Exception.Message)")
    exit 1
}
exit 0
