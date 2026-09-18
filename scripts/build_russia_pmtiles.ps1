# Copyright (c) 2026 Игорь Заломский <igor@zalomskij.ru>
# SPDX-License-Identifier: GPL-3.0-or-later

<#
.SYNOPSIS
Скачивает карту России из OpenStreetMap и собирает векторный PMTiles без Docker.

.DESCRIPTION
Для сборки используется Java из PATH и автоматически скачиваемый Planetiler.
Planetiler сам докачивает требуемые ему вспомогательные геоданные. В плитки
добавляется только русский языковой атрибут `name:ru`; стиль карты должен
использовать его для подписей.
#>
[CmdletBinding()]
param(
    [string]$OutputDirectory = (Join-Path $PSScriptRoot "..\build\map-russia"),
    [ValidateRange(0, 14)]
    [int]$MinZoom = 0,
    [ValidateRange(0, 14)]
    [int]$MaxZoom = 14,
    [ValidateRange(0, 128)]
    [int]$Threads = 0,
    [string]$JavaMemory = "8g",
    [string]$TemporaryDirectory = "",
    [switch]$ForceDownload,
    [switch]$Force,
    [switch]$VerifyDownload,
    [switch]$RefreshDependencies
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($MinZoom -gt $MaxZoom) {
    throw "MinZoom не может быть больше MaxZoom."
}

$geofabrikPbfUrl = "https://download.geofabrik.de/russia-latest.osm.pbf"
$planetilerUrl = "https://github.com/onthegomap/planetiler/releases/latest/download/planetiler.jar"

function Download-Atomically {
    param(
        [Parameter(Mandatory)] [string]$Url,
        [Parameter(Mandatory)] [string]$Destination
    )

    $temporary = "$Destination.download"
    Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    try {
        Invoke-WebRequest -Uri $Url -OutFile $temporary
        Move-Item -LiteralPath $temporary -Destination $Destination -Force
    }
    finally {
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
}

function Test-GeofabrikChecksum {
    param(
        [Parameter(Mandatory)] [string]$PbfPath,
        [Parameter(Mandatory)] [string]$Url
    )

    $checksum = (Invoke-WebRequest -Uri "$Url.md5").Content.Trim().Split()[0].ToLowerInvariant()
    $actual = (Get-FileHash -LiteralPath $PbfPath -Algorithm MD5).Hash.ToLowerInvariant()
    if ($actual -ne $checksum) {
        throw "Контрольная сумма $PbfPath не совпадает с опубликованной Geofabrik."
    }
}

function Get-JavaExecutable {
    $java = Get-Command java -ErrorAction SilentlyContinue
    if (-not $java) {
        throw "Не найдена Java 21 или новее в PATH."
    }

    # У `java -version` версия идёт в stderr, что при Stop воспринимается
    # PowerShell как ошибка. Вариант с двумя дефисами пишет её в stdout.
    $versionText = (& $java.Source --version | Select-Object -First 1).ToString()
    $version = [regex]::Match($versionText, '(?<major>\d+)\.')
    if (-not $version.Success) {
        throw "Не удалось определить версию Java: $versionText"
    }
    if ([int]$version.Groups["major"].Value -lt 21) {
        throw "Нужна Java 21 или новее, найдена: $versionText"
    }
    return $java.Source
}

$outputRoot = [IO.Path]::GetFullPath($OutputDirectory)
$toolsDirectory = Join-Path $outputRoot "tools"
$pbfPath = Join-Path $outputRoot "russia-latest.osm.pbf"
$outputPath = Join-Path $outputRoot "russia.pmtiles"
$temporaryOutputName = "russia-$([guid]::NewGuid().ToString('N')).pmtiles"
$temporaryOutputPath = Join-Path $outputRoot $temporaryOutputName
$planetilerPath = Join-Path $toolsDirectory "planetiler.jar"

New-Item -ItemType Directory -Path $outputRoot, $toolsDirectory -Force | Out-Null
if ((Test-Path -LiteralPath $outputPath) -and -not $Force) {
    throw "Результат уже существует: $outputPath. Для перезаписи укажите -Force."
}

if ($ForceDownload -or -not (Test-Path -LiteralPath $pbfPath)) {
    Write-Host "Скачиваю OSM PBF России (около 4 ГБ)..."
    Download-Atomically -Url $geofabrikPbfUrl -Destination $pbfPath
}
if ($VerifyDownload) {
    Write-Host "Проверяю контрольную сумму PBF..."
    Test-GeofabrikChecksum -PbfPath $pbfPath -Url $geofabrikPbfUrl
}
if ($RefreshDependencies -or -not (Test-Path -LiteralPath $planetilerPath)) {
    Write-Host "Скачиваю Planetiler..."
    Download-Atomically -Url $planetilerUrl -Destination $planetilerPath
}

if ($TemporaryDirectory) {
    $tmpDirectory = [IO.Path]::GetFullPath($TemporaryDirectory)
}
else {
    $tmpDirectory = Join-Path $outputRoot "planetiler-tmp"
}
New-Item -ItemType Directory -Path $tmpDirectory -Force | Out-Null

$planetilerArguments = @(
    "-Xmx$JavaMemory",
    "-jar", $planetilerPath,
    "--osm-path=$pbfPath",
    "--output=$temporaryOutputPath",
    "--minzoom=$MinZoom",
    "--maxzoom=$MaxZoom",
    "--tmpdir=$tmpDirectory",
    "--nodemap-storage=mmap",
    "--languages=ru",
    "--transliterate=false",
    "--use-wikidata=false",
    "--download"
)
if ($Threads -gt 0) {
    $planetilerArguments += "--threads=$Threads"
}

$javaExecutable = Get-JavaExecutable
Write-Host "Собираю $outputPath..."
& $javaExecutable @planetilerArguments
if ($LASTEXITCODE -ne 0) {
    throw "Planetiler завершился с кодом $LASTEXITCODE. Временные данные сохранены в $tmpDirectory."
}
if (-not (Test-Path -LiteralPath $temporaryOutputPath)) {
    throw "Planetiler завершился без создания $temporaryOutputPath."
}

# Готовый архив подменяется только после успешного завершения Planetiler.
Move-Item -LiteralPath $temporaryOutputPath -Destination $outputPath -Force
$size = [Math]::Round((Get-Item -LiteralPath $outputPath).Length / 1GB, 2)
Write-Host "Готово: $outputPath ($size ГБ)"




