# 구글 포토 폴더 앨범 올리기 제거 스크립트(설치 폴더 안에서 실행)
# 사용: pwsh -File "<설치 폴더>\uninstall.ps1" [-RemoveData]
#   -RemoveData: 이 계정의 사용자 자료(%LOCALAPPDATA%\GPhotoAlbums: 열쇠 파일·로그인·올린 기록)도 지운다.
# 구글 포토에 올린 사진·앨범은 지우지 않는다(이 프로그램으로는 지울 수 없다).
param([switch]$RemoveData)
$ErrorActionPreference = 'Stop'
$target = $PSScriptRoot
$appName = '구글 포토 폴더 앨범 올리기'

# 엉뚱한 폴더(개발 폴더 등)를 지우지 않게 설치 폴더인지 확인한다
$isInstall = (Split-Path $target -Leaf) -eq 'GPhotoAlbums' -and
             (Test-Path (Join-Path $target 'app.pyw')) -and
             (Test-Path (Join-Path $target 'gphoto\__init__.py')) -and
             -not (Test-Path (Join-Path $target 'tests'))
if (-not $isInstall) { throw "설치 폴더가 아니라서 멈춥니다: $target" }

$dirs = @([Environment]::GetFolderPath('CommonDesktopDirectory'), [Environment]::GetFolderPath('CommonPrograms'),
          [Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))
foreach ($dir in $dirs) {
    $p = Join-Path $dir "$appName.lnk"
    if (Test-Path $p) { Remove-Item $p -Force; Write-Host "지움: $p" }
}
if ($RemoveData) {
    $d = Join-Path $env:LOCALAPPDATA 'GPhotoAlbums'
    if (Test-Path $d) { Remove-Item $d -Recurse -Force; Write-Host "지움: $d" }
}
Set-Location $env:TEMP
Remove-Item $target -Recurse -Force
Write-Host "지움: $target"
Write-Host '제거했습니다.'
