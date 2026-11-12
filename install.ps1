# 구글 포토 폴더 앨범 올리기 설치 스크립트
# 사용: pwsh -File install.ps1 [-Scope AllUsers|CurrentUser] [-Target 폴더] [-NoShortcuts]
#   AllUsers   : C:\Program Files\GPhotoAlbums + 공용 바탕화면·모든 사용자 시작 메뉴 (관리자 권한 필요)
#   CurrentUser: %LOCALAPPDATA%\Programs\GPhotoAlbums + 이 계정 바탕화면·시작 메뉴
# 사용자 자료(열쇠 파일·로그인·올린 기록)는 계정마다 %LOCALAPPDATA%\GPhotoAlbums 에 따로 생긴다.
param(
    [ValidateSet('AllUsers', 'CurrentUser')][string]$Scope = 'AllUsers',
    [string]$Target = '',
    [switch]$NoShortcuts,
    [string]$Python = 'C:\Python312\python.exe'
)
$ErrorActionPreference = 'Stop'
$src = $PSScriptRoot
$appName = '구글 포토 폴더 앨범 올리기'
if (-not $Target) {
    $Target = if ($Scope -eq 'AllUsers') { Join-Path $env:ProgramFiles 'GPhotoAlbums' }
              else { Join-Path $env:LOCALAPPDATA 'Programs\GPhotoAlbums' }
}
if (-not (Test-Path $Python)) { throw "파이썬을 찾을 수 없습니다: $Python" }
Write-Host "설치 위치: $Target"
New-Item -ItemType Directory -Force $Target | Out-Null

# 앱 파일 복사(시험·개발 파일은 빼고). 예전 판의 gphoto·ui는 지우고 새로 넣는다.
foreach ($d in 'gphoto', 'ui') {
    $dst = Join-Path $Target $d
    if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
    Copy-Item (Join-Path $src $d) $dst -Recurse -Force
    Get-ChildItem $dst -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force
}
foreach ($f in 'app.pyw', 'app.ico', 'requirements.txt', 'selfcheck.py', 'uninstall.ps1') {
    Copy-Item (Join-Path $src $f) $Target -Force
}

# 가상환경과 라이브러리(개발 때 시험한 버전 그대로)
$venv = Join-Path $Target '.venv'
$venvPy = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    & $Python -m venv $venv
    if ($LASTEXITCODE) { throw '가상환경을 만들지 못했습니다' }
}
& $venvPy -m pip install --disable-pip-version-check -q -r (Join-Path $Target 'requirements.txt')
if ($LASTEXITCODE) { throw '라이브러리를 설치하지 못했습니다' }
# 다른 계정(쓰기 권한 없음)도 빨리 뜨게 미리 컴파일해 둔다
& $venvPy -m compileall -q (Join-Path $Target 'gphoto') | Out-Null

# 설치 확인(구글 접속 없음)
& $venvPy (Join-Path $Target 'selfcheck.py')
if ($LASTEXITCODE) { throw '설치 확인에 실패했습니다' }

if (-not $NoShortcuts) {
    $desk = if ($Scope -eq 'AllUsers') { [Environment]::GetFolderPath('CommonDesktopDirectory') } else { [Environment]::GetFolderPath('Desktop') }
    $menu = if ($Scope -eq 'AllUsers') { [Environment]::GetFolderPath('CommonPrograms') } else { [Environment]::GetFolderPath('Programs') }
    $ws = New-Object -ComObject WScript.Shell
    foreach ($dir in $desk, $menu) {
        $lnk = $ws.CreateShortcut((Join-Path $dir "$appName.lnk"))
        $lnk.TargetPath = Join-Path $venv 'Scripts\pythonw.exe'
        $lnk.Arguments = '"' + (Join-Path $Target 'app.pyw') + '"'
        $lnk.WorkingDirectory = $Target
        $lnk.IconLocation = (Join-Path $Target 'app.ico') + ',0'
        $lnk.Description = '폴더마다 구글 포토 앨범을 만들어 사진·영상을 올립니다'
        $lnk.Save()
        Write-Host "바로가기: $($lnk.FullName)"
    }
}
Write-Host '설치를 마쳤습니다.'
