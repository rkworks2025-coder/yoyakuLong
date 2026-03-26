# ==========================================================
# 【yoyakuLong】144時間(6日間) 精密狙い撃ちエンジン（待機ロジック強化版）
# 改修内容: JS描画待ちの実装、車両ベースループ、144h整合性強制チェック
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

print(f"\n[モード] 144時間(6日間) Sniper（JS描画待機モード）")

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
wait = WebDriverWait(driver, 20) # 待機時間を20秒に延長（電波不良対策）
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
        plate = target['plate']
        station_name = target['station']
        station_cd = target['stationCd']
        area = target['city']

        print(f"[{i+1}/{len(target_vehicles)}] {plate} ({station_name}) を解析中...")
        
        base_url = f"https://dailycheck.tc-extsys.jp/tcrappsweb/web/routineStationVehicle.html?stationCd={station_cd}"
        driver.get(base_url)
        
        # 1. まず車両BOXが表示されるのを待つ
        wait.until(EC.presence_of_element_located((By.CLASS_NAME, "car-list-box")))

        # 2. ★【最重要】特定の車両の「タイムライン表」がJSで描画されるのを精密に待つ
        # XPATH: プレート名を含むBOX内の 'table.timetable' が出現するまで待機
        xpath_timetable = f"//div[contains(text(), '{plate}')]/ancestor::div[@class='car-list-box']//table[@class='timetable']"
        try:
            wait.until(EC.presence_of_element_located((By.XPATH, xpath_timetable)))
        except:
            raise Exception(f"【自爆】車両 {plate} のタイムライン描画がタイムアウトしました。電波不良かJSエラーの可能性があります。")

        # 描画完了後のソースを解析
        soup = BeautifulSoup(driver.page_source, "lxml")
        target_box = None
        for box in soup.find_all("div", class_="car-list-box"):
            raw_text = box.find("div", class_="car-list-title-area").get_text(strip=True)
            if plate in raw_text.replace(" ", ""):
                target_box = box
                model = raw_text.split(" / ")[1].strip() if " / " in raw_text else ""
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
            raise ValueError(f"【整合性エラー】{plate} 前半データ不備: {len(first_72h)}/288")

        # --- 【後半: 72h】 (TMA2) ---
        reserve_link = target_box.find("span", class_="link-btn").find("a")['href']
        driver.get(f"https://dailycheck.tc-extsys.jp{reserve_link}")
        wait.until(EC.presence_of_element_located((By.ID, "reserveStartDate")))
        
        target_date_val = (now_jst + timedelta(days=3)).strftime('%Y-%m-%d')
        date_input = driver.find_element(By.ID, "reserveStartDate")
        date_input.clear()
        date_input.send_keys(target_date_val)
        date_input.send_keys(Keys.RETURN)
        
        # 後半画面もJS描画を確実に待つ
        sleep(2) # 通信のきっかけ
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".timetable-contents table")))

        soup_detail = BeautifulSoup(driver.page_source, "lxml")
        detail_cells = soup_detail.find("div", class_="timetable-contents").find("table").find_all("td")
        
        second_72h = []
        for cell in detail_cells:
            cls = cell.get("class", [])
            if any(x in cls for x in ["vacant", "full", "impossible", "others"]):
                symbol = "○" if "vacant" in cls else ("s" if "impossible" in cls else "×")
                colspan = int(cell.get("colspan", 1))
                second_72h.extend([symbol] * colspan)

        if len(second_72h) != 288:
            raise ValueError(f"【整合性エラー】{plate} 後半データ不備: {len(second_72h)}/288")

        full_rsv = "".join(first_72h) + "".join(second_72h)
        if len(full_rsv) != 576:
            raise ValueError(f"【整合性エラー】{plate} 最終データ不備: {len(full_rsv)}/576")

        collected_data.append([area, station_name, plate, model, start_time_str, full_rsv])
        print(f"    -> {plate} 144h取得成功")

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
        
        send_discord_notification(f"✅ yoyakuLong: {len(collected_data)}台の Sniper 更新完了。")

except Exception as e:
    error_msg = f"❌ yoyakuLong重大エラー（停止）: {e}"
    print(f"\n{error_msg}")
    send_discord_notification(error_msg)
    sys.exit(1)

finally:
    driver.quit()
