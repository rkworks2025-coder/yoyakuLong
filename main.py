# ==========================================================
# 【yoyakuLong】144時間(6日間) 精密 Sniper エンジン（完全同期版）
# 改修内容: ローディング画面(loading-view)の消失待機、プレートスペース対応、自爆強化
# ==========================================================
import sys
import os
import pandas as pd
import gspread
import unicodedata
import urllib.request
import json
from time import sleep
from datetime import datetime, timezone, timedelta
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from bs4 import BeautifulSoup

# --- Discord通知用設定 ---
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1474006170057441300/Emo5Ooe48jBUzMhzLrCBn85_3Td-ck3jYtXtVa2vdXWWyT2HxSuKghWchrG7gCsZhEqY"

def send_discord_notification(message):
    if not DISCORD_WEBHOOK_URL: return
    data = {"content": message}
    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    req = urllib.request.Request(DISCORD_WEBHOOK_URL, data=json.dumps(data).encode(), headers=headers)
    try:
        urllib.request.urlopen(req)
    except Exception as e:
        print(f"Discord通知エラー: {e}")

# 1. ログイン情報
LOGIN_URL = "https://dailycheck.tc-extsys.jp/tcrappsweb/web/login/tawLogin.html"
USER_ID_1 = "0030"
USER_ID_2 = "927583"
PASSWORD = "Ccj-322222"

# 2. シート設定
PRODUCTION_SHEET_URL = "https://docs.google.com/spreadsheets/d/13cQngK_Xx38VU67yLS-iTHyOZgsACZdxM34l-Jq_U9A/edit"
CSV_FILE_NAME = "station_code_map.csv"
INSPECTION_SHEET_URL = "https://docs.google.com/spreadsheets/d/11XglLANtnG7bCxYjLRMGoZY25wspjHsGR3IG2ZyRITs/edit"

# 3. Google認証
SERVICE_ACCOUNT_KEY_FILE = "service_account.json"
if not os.path.exists(SERVICE_ACCOUNT_KEY_FILE):
    msg = "!! 認証キー(service_account.json)が見つかりません。停止します。"
    print(msg)
    send_discord_notification(f"❌ {msg}")
    sys.exit(1)

try:
    gc = gspread.service_account(filename=SERVICE_ACCOUNT_KEY_FILE)
except Exception as e:
    send_discord_notification(f"❌ Google認証失敗: {e}")
    raise

print(f"\n[モード] 144時間(6日間) Sniper（完全同期・遮断回避モード）")

# I. 車両リスト(CSV)読み込み
df_map = pd.read_csv(CSV_FILE_NAME)
df_map.columns = df_map.columns.str.strip()
if 'area' in df_map.columns: df_map = df_map.rename(columns={'area': 'city'})
if 'station_name' in df_map.columns: df_map = df_map.rename(columns={'station_name': 'station'})

# II. inspectionlogから「今取るべき車両(93台想定)」を特定
print(f"\n[ターゲット特定] inspectionlogを解析中...")
try:
    inspection_sh_key = INSPECTION_SHEET_URL.split('/d/')[1].split('/edit')[0]
    sh_inspection = gc.open_by_key(inspection_sh_key)
    ws_inspection = sh_inspection.worksheet("inspectionlog")
    inspection_values = ws_inspection.get_all_values()
except Exception as e:
    send_discord_notification(f"❌ Inspectionシート読み取り失敗: {e}")
    raise

target_vehicles = []
if len(inspection_values) > 1:
    for row in inspection_values[1:]:
        if len(row) > 5:
            st_name = str(row[1]).strip()
            plate = str(row[3]).strip().replace(" ", "")
            status = str(row[5]).strip().lower()
            if status in ['standby', 'stopped']:
                match = df_map[df_map['station'] == st_name]
                if not match.empty:
                    target_vehicles.append({
                        'plate': plate,
                        'station': st_name,
                        'stationCd': str(match.iloc[0]['stationCd']).replace('.0', ''),
                        'city': match.iloc[0]['city']
                    })

if not target_vehicles:
    print("\n[通知] 巡回対象の車両がいませんでした。終了します。")
    sys.exit(0)

print(f"-> ターゲット確定: {len(target_vehicles)} 台")

# ドライバ設定
options = Options()
options.add_argument('--headless')
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')
options.add_argument('--window-size=1920,1080')
driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
wait = WebDriverWait(driver, 25) # 待機時間を25秒に強化
collected_data = []

try:
    print("\n[ログイン] TMAシステムへアクセス...")
    driver.get(LOGIN_URL)
    wait.until(EC.presence_of_element_located((By.ID, "cardNo1")))
    driver.find_element(By.ID, "cardNo1").send_keys(USER_ID_1)
    driver.find_element(By.ID, "cardNo2").send_keys(USER_ID_2)
    driver.find_element(By.ID, "password").send_keys(PASSWORD)
    driver.find_element(By.ID, "password").send_keys(Keys.RETURN)
    
    sleep(5)
    if "login" in driver.current_url.lower():
        raise Exception("ログイン失敗。認証情報を確認してください。")

    for i, target in enumerate(target_vehicles):
        target_plate = target['plate']
        station_name = target['station']
        station_cd = target['stationCd']
        area = target['city']

        print(f"[{i+1}/{len(target_vehicles)}] {target_plate} ({station_name}) を狙い撃ち中...")
        
        base_url = f"https://dailycheck.tc-extsys.jp/tcrappsweb/web/routineStationVehicle.html?stationCd={station_cd}"
        driver.get(base_url)

        # --- ★【最重要】ローディング画面が消えるまで待機 ---
        # 画面を覆っている Loading... が消えない限り、下の要素は触れない
        try:
            wait.until(EC.invisibility_of_element_located((By.CLASS_NAME, "loading-view")))
        except:
            raise Exception(f"【自爆】ローディング画面(loading-view)が消えません。通信環境を確認してください。")

        # 車両BOXの特定
        wait.until(EC.presence_of_element_located((By.CLASS_NAME, "car-list-box")))
        car_boxes = driver.find_elements(By.CLASS_NAME, "car-list-box")
        target_element = None
        for box in car_boxes:
            title_area = box.find_element(By.CLASS_NAME, "car-list-title-area").text
            if target_plate in title_area.replace(" ", ""):
                target_element = box
                model = title_area.split(" / ")[1].strip() if " / " in title_area else ""
                break
        
        if not target_element:
            raise Exception(f"【自爆】車両 {target_plate} をページ内で特定できませんでした。")

        # 予約表(table.timetable)が描画されるのを精密に待つ
        try:
            wait.until(lambda d: target_element.find_elements(By.CLASS_NAME, "timetable"))
        except:
            raise Exception(f"【自爆】車両 {target_plate} の予約表が描画されませんでした。")

        # 描画完了後のソースを解析
        soup = BeautifulSoup(driver.page_source, "lxml")
        target_box = None
        for box in soup.find_all("div", class_="car-list-box"):
            raw_text = box.find("div", class_="car-list-title-area").get_text(strip=True).replace(" ", "")
            if target_plate in raw_text:
                target_box = box
                break
        
        now_jst = datetime.now(timezone(timedelta(hours=+9), 'JST'))
        start_time_str = f"{now_jst.strftime('%Y-%m-%d')} {now_jst.hour:02d}:00"

        # --- 【前半: 72h】 ---
        first_72h = []
        timetable = target_box.find("table", class_="timetable")
        data_cells = []
        for r in timetable.find_all("tr"):
            cells = r.find_all("td")
            if cells and any(x in c.get("class", []) for c in cells for x in ["vacant", "full", "impossible", "others"]):
                data_cells = cells
                break
        
        for cell in data_cells:
            cls = cell.get("class", [])
            symbol = "○" if "vacant" in cls else ("s" if "impossible" in cls else "×")
            colspan = int(cell.get("colspan", 1))
            first_72h.extend([symbol] * colspan)
        
        if len(first_72h) != 288:
            raise ValueError(f"【不整合】{target_plate} 前半データ不足: {len(first_72h)}/288")

        # --- 【後半: 72h】 (TMA2) ---
        reserve_link = target_box.find("span", class_="link-btn").find("a")['href']
        driver.get(f"https://dailycheck.tc-extsys.jp{reserve_link}")
        
        # 後半画面でもローディングを待つ
        wait.until(EC.invisibility_of_element_located((By.CLASS_NAME, "loading-view")))
        wait.until(EC.presence_of_element_located((By.ID, "reserveStartDate")))
        
        target_date_val = (now_jst + timedelta(days=3)).strftime('%Y-%m-%d')
        date_input = driver.find_element(By.ID, "reserveStartDate")
        date_input.clear()
        date_input.send_keys(target_date_val)
        date_input.send_keys(Keys.RETURN)
        
        # 描画待ち
        sleep(2)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".timetable-contents table")))

        soup_detail = BeautifulSoup(driver.page_source, "lxml")
        timetable_detail = soup_detail.find("div", class_="timetable-contents").find("table")
        detail_cells = timetable_detail.find_all("td")
        second_72h = []
        for cell in detail_cells:
            cls = cell.get("class", [])
            if any(x in cls for x in ["vacant", "full", "impossible", "others"]):
                symbol = "○" if "vacant" in cls else ("s" if "impossible" in cls else "×")
                colspan = int(cell.get("colspan", 1))
                second_72h.extend([symbol] * colspan)

        if len(second_72h) != 288:
            raise ValueError(f"【不整合】{target_plate} 後半データ不足: {len(second_72h)}/288")

        full_rsv = "".join(first_72h) + "".join(second_72h)
        if len(full_rsv) != 576:
            raise ValueError(f"【不整合】最終結合データ不備: {len(full_rsv)}/576")

        collected_data.append([area, station_name, target_plate, model, start_time_str, full_rsv])
        print(f"    -> {target_plate} 144h取得完了")

    # シート保存
    if collected_data:
        sh_prod = gc.open_by_url(PRODUCTION_SHEET_URL)
        df_output = pd.DataFrame(collected_data, columns=['city', 'station', 'plate', 'model', 'getTime', 'rsvData'])
        for area_name in df_output['city'].unique():
            df_area = df_output[df_output['city'] == area_name].copy()
            ws_name = f"{str(area_name).replace('市','').strip()}_更新用"
            try: ws = sh_prod.worksheet(ws_name)
            except: ws = sh_prod.add_worksheet(title=ws_name, rows=1000, cols=10)
            ws.clear()
            ws.update([df_area.drop(columns=['city']).columns.values.tolist()] + df_area.drop(columns=['city']).values.tolist())
        
        send_discord_notification(f"✅ yoyakuLong Sniper: {len(collected_data)}台の更新が完了。")

except Exception as e:
    error_msg = f"❌ yoyakuLong重大エラー（停止）: {e}"
    print(f"\n{error_msg}")
    send_discord_notification(error_msg)
    sys.exit(1)

finally:
    driver.quit()
