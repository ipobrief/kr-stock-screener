"""
국내주식 전종목 스크리너 백엔드
- 네이버 금융에서 전종목(KOSPI+KOSDAQ) 코드 수집
- 비동기로 OHLCV 데이터 일괄 수집
- 정배열, 거래량급등, 점수 계산
- 상위 종목에 투자자(기관/외국인) 데이터 추가
- scan_result.json 출력 → 프론트엔드에서 로드
"""
import asyncio, aiohttp, json, time, re, os, sys, io
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

# Windows CP949 인코딩 문제 해결
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# ─── 설정 ───
OHLCV_CONCURRENCY = 30      # OHLCV 동시 요청 수
INVESTOR_CONCURRENCY = 10   # 투자자 데이터 동시 요청 수
INVESTOR_TOP_N = 150         # 상위 N개만 투자자 데이터 수집
OHLCV_DAYS = 90              # OHLCV 조회 일수
MIN_DATA_DAYS = 60           # 최소 데이터 일수 (60MA용)
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'scan_result.json')

# ─── 1단계: 전종목 코드 수집 ───
async def fetch_all_stock_codes(session):
    """네이버 전종목 시세 페이지에서 모든 종목 코드/이름 수집"""
    stocks = []
    for sosok in [0, 1]:  # 0=KOSPI, 1=KOSDAQ
        market = 'KOSPI' if sosok == 0 else 'KOSDAQ'
        page = 1
        while True:
            url = f'https://finance.naver.com/sise/sise_market_sum.naver?sosok={sosok}&page={page}'
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    buf = await resp.read()
                    html = buf.decode('euc-kr', errors='replace')
                    soup = BeautifulSoup(html, 'lxml')

                    table = soup.find('table', class_='type_2')
                    if not table:
                        break

                    rows = table.find_all('tr')
                    found = 0
                    for tr in rows:
                        tds = tr.find_all('td')
                        if len(tds) < 2:
                            continue
                        a = tds[1].find('a')
                        if not a or not a.get('href'):
                            continue
                        href = a['href']
                        m = re.search(r'code=(\d{6})', href)
                        if not m:
                            continue
                        code = m.group(1)
                        name = a.get_text(strip=True)
                        if name and code:
                            stocks.append({'c': code, 'n': name, 'm': market})
                            found += 1

                    if found == 0:
                        break

                    # 마지막 페이지 체크
                    paging = soup.find('td', class_='pgRR')
                    if not paging:
                        # pgRR이 없으면 현재 페이지 근처 확인
                        pass

                    page += 1
                    if page > 50:  # 안전장치
                        break

            except Exception as e:
                print(f'  ⚠ 종목 목록 수집 실패 (sosok={sosok}, page={page}): {e}')
                break

    # 중복 제거
    seen = set()
    unique = []
    for s in stocks:
        if s['c'] not in seen:
            seen.add(s['c'])
            unique.append(s)

    return unique

# ─── 2단계: OHLCV 데이터 수집 ───
async def fetch_ohlcv(session, sem, code, days=OHLCV_DAYS):
    """네이버 siseJson API에서 OHLCV 조회"""
    async with sem:
        end = datetime.now()
        start = end - timedelta(days=days * 2 + 90)
        start_str = start.strftime('%Y%m%d')
        end_str = end.strftime('%Y%m%d')
        url = f'https://fchart.stock.naver.com/siseJson.naver?symbol={code}&requestType=1&startTime={start_str}&endTime={end_str}&timeframe=day'

        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                text = await resp.text()
                text = text.strip()
                if text and text[0] == '﻿':
                    text = text[1:]
                text = text.replace("'", '"')
                data = json.loads(text)
                result = []
                for r in data[1:]:
                    result.append({
                        'date': str(r[0]).strip().replace('"', ''),
                        'open': float(r[1]),
                        'high': float(r[2]),
                        'low': float(r[3]),
                        'close': float(r[4]),
                        'volume': float(r[5])
                    })
                return result
        except Exception:
            return None

# ─── 3단계: 투자자 데이터 수집 ───
async def fetch_investor(session, sem, code):
    """네이버 frgn.naver에서 기관/외국인 순매매 파싱"""
    async with sem:
        url = f'https://finance.naver.com/item/frgn.naver?code={code}'
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                buf = await resp.read()
                html = buf.decode('euc-kr', errors='replace')

                # type2 테이블들 찾기
                tables = re.findall(r'<table[^>]*class="type2"[^>]*>([\s\S]*?)</table>', html)
                if len(tables) < 2:
                    return None

                table_html = tables[1]
                # mouseOver 행에서 데이터 추출
                row_re = re.compile(r'<tr\s+onMouseOver[^>]*>([\s\S]*?)</tr>')
                rows = []
                for m in row_re.finditer(table_html):
                    cells = []
                    for td_m in re.finditer(r'<td[^>]*>([\s\S]*?)</td>', m.group(1)):
                        val = re.sub(r'<[^>]*>', '', td_m.group(1)).replace(',', '').replace('\n', '').replace('\t', '').strip()
                        cells.append(val)
                    if len(cells) >= 7:
                        rows.append(cells)
                    if len(rows) >= 5:
                        break

                if not rows:
                    return None

                inst_net = 0
                frgn_net = 0
                for row in rows:
                    try:
                        inst_net += int(row[5]) if row[5] else 0
                    except: pass
                    try:
                        frgn_net += int(row[6]) if row[6] else 0
                    except: pass

                indiv_net = -(inst_net + frgn_net)
                abs_f = abs(frgn_net)
                abs_i = abs(inst_net)
                abs_p = abs(indiv_net)
                abs_total = abs_f + abs_i + abs_p or 1

                return {
                    'foreignNet': frgn_net,
                    'instNet': inst_net,
                    'indivNet': indiv_net,
                    'foreignVol': abs_f,
                    'instVol': abs_i,
                    'indivVol': abs_p,
                    'totalVol': abs_total
                }
        except Exception:
            return None

# ─── 분석 함수들 ───
def calc_ma(closes, period):
    result = []
    for i in range(len(closes)):
        if i < period - 1:
            result.append(None)
        else:
            result.append(sum(closes[i - period + 1:i + 1]) / period)
    return result

def check_ma_alignment(closes):
    ma5 = calc_ma(closes, 5)
    ma20 = calc_ma(closes, 20)
    ma60 = calc_ma(closes, 60)
    n = len(closes)
    if not ma5[n-1] or not ma20[n-1] or not ma60[n-1]:
        return {'aligned': False, 'just': False}
    today_ok = ma5[n-1] > ma20[n-1] > ma60[n-1]
    yesterday_ok = (n >= 2 and ma5[n-2] and ma20[n-2] and ma60[n-2] and
                    ma5[n-2] > ma20[n-2] > ma60[n-2])
    return {'aligned': today_ok, 'just': today_ok and not yesterday_ok}

def analyze_volume(volumes):
    n = len(volumes)
    if n < 2:
        return {'todayVol': 0, 'avgVol': 0, 'change': 0}
    today_vol = volumes[-1]
    period = min(20, n - 1)
    avg_vol = sum(volumes[n - 1 - period:n - 1]) / period if period > 0 else 0
    change = ((today_vol - avg_vol) / avg_vol * 100) if avg_vol > 0 else 0
    return {'todayVol': today_vol, 'avgVol': avg_vol, 'change': change}

def check_new_high(data):
    n = len(data)
    if n < 10:
        return {'allTime': False, 'near': False, 'prevHigh': 0}
    max_h = max(d['high'] for d in data[:-1])
    today_close = data[-1]['close']
    all_time = today_close >= max_h
    near = not all_time and max_h > 0 and today_close >= max_h * 0.97
    return {'allTime': all_time, 'near': near, 'prevHigh': max_h}

def count_consecutive_up(data):
    c = 0
    for i in range(len(data) - 1, -1, -1):
        if data[i]['close'] > data[i]['open']:
            c += 1
        else:
            break
    return c

def calc_today_return(closes):
    if len(closes) < 2: return 0
    return (closes[-1] - closes[-2]) / closes[-2] * 100

def calc_3day_return(closes):
    if len(closes) < 4: return 0
    return (closes[-1] - closes[-4]) / closes[-4] * 100

def calc_bull_score(d):
    sd = {}
    total = 0

    # 1. 오늘 수익률 (10점)
    tr = d['todayReturn']
    s1 = 10 if tr >= 10 else 8 if tr >= 5 else 6 if tr >= 2 else 3 if tr >= 0 else -3 if tr >= -3 else -8
    sd['today'] = {'s': s1, 'max': 10, 'label': '오늘 수익률'}
    total += s1

    # 2. 3일 평균 수익률 (10점)
    ar = d['avg3Return']
    s2 = 10 if ar >= 10 else 8 if ar >= 5 else 6 if ar >= 2 else 3 if ar >= 0 else -3 if ar >= -3 else -8
    sd['avg3'] = {'s': s2, 'max': 10, 'label': '3일 평균 수익률'}
    total += s2

    # 3. 이평선 정배열 (20점)
    s3 = 20 if d['maJust'] else 15 if d['maAligned'] else 0
    sd['ma'] = {'s': s3, 'max': 20, 'label': '이평선 정배열'}
    total += s3

    # 4. 거래량 급등 (20점)
    vc = d['volChange']
    s4 = 20 if vc >= 300 else 16 if vc >= 200 else 12 if vc >= 100 else 6 if vc >= 50 else 0 if vc >= 0 else -3 if vc >= -30 else -8
    sd['vol'] = {'s': s4, 'max': 20, 'label': '거래량 급등'}
    total += s4

    # 5. 연속 양봉 (5점)
    cc = d['consecutiveUp']
    s5 = 5 if cc >= 5 else 3 if cc >= 3 else 2 if cc >= 2 else 1 if cc >= 1 else 0
    sd['candle'] = {'s': s5, 'max': 5, 'label': '연속 양봉'}
    total += s5

    # 6. 직전 고점 돌파 (15점)
    s6 = 15 if d['newHighAll'] else 8 if d['newHighNear'] else 0
    sd['high'] = {'s': s6, 'max': 15, 'label': '직전 고점 돌파'}
    total += s6

    # 7. 콤보 보너스 (20점)
    is_aligned = d['maAligned'] or d['maJust']
    is_vol_spike = d['volChange'] >= 50
    is_today_up = d['todayReturn'] > 0
    s7 = 20 if (is_aligned and is_vol_spike and is_today_up) else 10 if (is_aligned and is_vol_spike) else 5 if (is_aligned and is_today_up) else 0
    sd['combo'] = {'s': s7, 'max': 20, 'label': '콤보(정배열+거래량+양봉)'}
    total += s7

    return {'bullScore': max(0, min(100, total)), 'scoreDetail': sd}

# ─── 메인 스캔 ───
async def main():
    start_time = time.time()

    connector = aiohttp.TCPConnector(limit=50, ssl=False)
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'}
    async with aiohttp.ClientSession(connector=connector, headers=headers) as session:

        # 1단계: 전종목 코드 수집
        print('📋 1단계: 전종목 코드 수집 중...')
        stocks = await fetch_all_stock_codes(session)
        print(f'   → {len(stocks)}개 종목 수집 완료 (KOSPI: {sum(1 for s in stocks if s["m"]=="KOSPI")}, KOSDAQ: {sum(1 for s in stocks if s["m"]=="KOSDAQ")})')

        # 2단계: OHLCV 일괄 수집
        print(f'📊 2단계: OHLCV 데이터 수집 중... ({len(stocks)}개, 동시 {OHLCV_CONCURRENCY}건)')
        sem = asyncio.Semaphore(OHLCV_CONCURRENCY)

        progress = {'done': 0}
        total = len(stocks)
        async def fetch_with_progress(code):
            result = await fetch_ohlcv(session, sem, code)
            progress['done'] += 1
            if progress['done'] % 200 == 0 or progress['done'] == total:
                print(f'   → {progress["done"]}/{total} ({progress["done"]*100//total}%)')
            return result

        ohlcv_results = await asyncio.gather(*[fetch_with_progress(s['c']) for s in stocks])

        # 3단계: 분석
        print('🔍 3단계: 분석 중...')
        analyzed = []
        for i, s in enumerate(stocks):
            data = ohlcv_results[i]
            if not data or len(data) < MIN_DATA_DAYS:
                continue

            closes = [d['close'] for d in data]
            volumes = [d['volume'] for d in data]

            ma_result = check_ma_alignment(closes)
            vol_result = analyze_volume(volumes)
            today_return = calc_today_return(closes)
            avg3_return = calc_3day_return(closes)
            high_result = check_new_high(data)
            consecutive_up = count_consecutive_up(data)

            # 이상 데이터 필터 (액면분할, 합병 등 노이즈 제거)
            if abs(today_return) > 50 or abs(avg3_return) > 100:
                continue

            # 거래대금 계산 (종가 × 거래량)
            trade_value = closes[-1] * vol_result['todayVol']

            # 거래대금 1000억 미만 제외
            if trade_value < 10_000_000_000:
                continue

            score_input = {
                'todayReturn': today_return,
                'avg3Return': avg3_return,
                'maAligned': ma_result['aligned'],
                'maJust': ma_result['just'],
                'volChange': vol_result['change'],
                'newHighAll': high_result['allTime'],
                'newHighNear': high_result['near'],
                'consecutiveUp': consecutive_up
            }
            score_result = calc_bull_score(score_input)

            # 차트용 최근 데이터 (상위 종목만 포함, 나머지는 빈 배열)
            chart_data = data[-30:]  # 최근 30일만 (JSON 크기 축소)

            analyzed.append({
                'code': s['c'],
                'name': s['n'],
                'market': s['m'],
                'close': closes[-1],
                'todayReturn': round(today_return, 2),
                'avg3Return': round(avg3_return, 2),
                'maAligned': ma_result['aligned'],
                'maJust': ma_result['just'],
                'volChange': round(vol_result['change'], 1),
                'todayVol': vol_result['todayVol'],
                'avgVol': round(vol_result['avgVol']),
                'tradeValue': trade_value,
                'combo20': score_result['scoreDetail']['combo']['s'] == 20,
                'newHighAll': high_result['allTime'],
                'newHighNear': high_result['near'],
                'prevHigh': high_result['prevHigh'],
                'consecutiveUp': consecutive_up,
                'bullScore': score_result['bullScore'],
                'scoreDetail': score_result['scoreDetail'],
                'investor': None,
                'rawData': chart_data
            })

        # 점수순 정렬
        analyzed.sort(key=lambda x: x['bullScore'], reverse=True)
        print(f'   → {len(analyzed)}개 종목 분석 완료')

        # 차트 데이터: 상위 500개 + 주요 신호 종목만 유지, 나머지 제거
        for i, a in enumerate(analyzed):
            keep = (i < 500 or a['maJust'] or a['bullScore'] >= 40 or
                   (a['maAligned'] and a['volChange'] >= 100))
            if not keep:
                a['rawData'] = []

        # 4단계: 상위 종목 투자자 데이터
        top_n = min(INVESTOR_TOP_N, len(analyzed))
        print(f'👥 4단계: 상위 {top_n}개 투자자 데이터 수집 중...')
        inv_sem = asyncio.Semaphore(INVESTOR_CONCURRENCY)
        inv_tasks = [fetch_investor(session, inv_sem, analyzed[i]['code']) for i in range(top_n)]
        inv_results = await asyncio.gather(*inv_tasks)

        for i in range(top_n):
            analyzed[i]['investor'] = inv_results[i]

        inv_count = sum(1 for r in inv_results if r is not None)
        print(f'   → {inv_count}/{top_n}개 투자자 데이터 수집 성공')

    # 5단계: JSON 저장
    output = {
        'scanTime': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'totalStocks': len(stocks),
        'analyzedStocks': len(analyzed),
        'results': analyzed
    }

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False)

    elapsed = time.time() - start_time
    file_size = os.path.getsize(OUTPUT_FILE) / 1024 / 1024

    print(f'\n✅ 스캔 완료!')
    print(f'   종목 수: {len(stocks)}개 → 분석: {len(analyzed)}개')
    print(f'   정배열: {sum(1 for a in analyzed if a["maAligned"])}개')
    print(f'   정배열 전환: {sum(1 for a in analyzed if a["maJust"])}개')
    print(f'   거래량 급등(100%↑): {sum(1 for a in analyzed if a["volChange"] >= 100)}개')
    print(f'   고점 돌파: {sum(1 for a in analyzed if a["newHighAll"])}개')
    print(f'   소요 시간: {elapsed:.1f}초')
    print(f'   결과 파일: {OUTPUT_FILE} ({file_size:.1f}MB)')

if __name__ == '__main__':
    asyncio.run(main())
