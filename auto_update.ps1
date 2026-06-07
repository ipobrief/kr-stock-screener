# 국내주식 정배열 스크리너 - 자동 스캔 & 배포
# 작업 스케줄러에서: powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\Users\USER\kr-stock-screener\auto_update.ps1"
# G: (구글드라이브)는 스케줄 실행 시 접근 불가하므로 모든 작업을 C: 저장소에서 수행한다.

$ErrorActionPreference = 'Continue'
$REPO = 'C:\Users\USER\kr-stock-screener'
$PY   = 'C:\Users\USER\AppData\Local\Python\pythoncore-3.14-64\python.exe'
$LOG  = Join-Path $REPO 'auto_update.log'

function Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Add-Content -Path $LOG -Value $line -Encoding utf8
}

Set-Content -Path $LOG -Value "=== 시작 ===" -Encoding utf8

if (-not (Test-Path $PY)) { Log "ERROR: 파이썬 없음: $PY"; exit 1 }
if (-not (Test-Path (Join-Path $REPO 'scanner.py'))) { Log "ERROR: scanner.py 없음"; exit 1 }

# 1. 스캐너 실행 (C: 저장소 내에서 직접 실행 -> scan_result.json 이 같은 폴더에 생성됨)
#    asyncio 정리 단계에서 비정상 종료코드가 나올 수 있으나 결과 파일 생성 여부로 성공 판정한다.
Log "scanner.py 실행..."
Set-Location $REPO
& $PY scanner.py *>> $LOG
Log "scanner.py 종료"

$json = Join-Path $REPO 'scan_result.json'
if (-not (Test-Path $json)) { Log "ERROR: scan_result.json 생성 안됨"; exit 1 }

# 방금(10분 이내) 갱신됐는지 확인 -> 스캔 실패 시 옛 파일로 잘못 배포 방지
$age = (Get-Date) - (Get-Item $json).LastWriteTime
if ($age.TotalMinutes -gt 10) {
    Log ("WARN: scan_result.json이 {0:N1}분 전 파일. 스캔 실패 추정 - 배포 중단" -f $age.TotalMinutes)
    exit 1
}

# 2. git add / commit / push
git add scan_result.json 2>> $LOG
git diff --cached --quiet
if ($LASTEXITCODE -eq 0) {
    Log "변경사항 없음. 푸시 생략."
    Log "=== 완료 ==="
    exit 0
}

$msg = "데이터 업데이트 {0}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm')
git commit -m $msg *>> $LOG
git push *>> $LOG
if ($LASTEXITCODE -ne 0) { Log "ERROR: git push 실패"; exit 1 }

Log "푸시 완료: $msg"
Log "=== 완료 ==="
exit 0
