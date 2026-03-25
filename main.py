# ==========================================================
# 【yoyakuLong】144時間(6日間)データ取得用エンジン（厳格版）
# 改修内容: 自爆トリガー実装、データ穴埋め全廃、144h整合性強制チェック
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

# エリア指定（144hモード固定）
TARGET_AREA = 'force_all'
print(f"\n[モード] 144時間(6日間)データ取得実行（厳格チェックモード）")

# I. リスト読み込み
if not os.path.exists(CSV_FILE_NAME):
    raise FileNotFoundError(f"必須ファイル {CSV_FILE_NAME} が見つかりません。")

df_map = pd.read_csv(CSV_FILE_NAME)
df_map.columns = df_map.columns.str.strip()
if 'area' in df_map.columns: df_map = df_map.rename(columns={'area': 'city'})
if 'station_name' in df_map.columns: df_map = df_map.rename(columns={'station_name': 'station'})
target_stations_raw = df_map.drop_duplicates(subset=['stationCd']).to_dict('records')

# inspectionlogからステータス取得
print(f"\n[ステータス確認] 車両単位のフィルタリング準備中...")
try:
    inspection_sh_key = INSPECTION_SHEET_URL.split('/d/')[1].split('/edit')[0]
    sh_inspection = gc.open_by_key(inspection_sh_key)
    ws_inspection = sh_inspection.worksheet("inspectionlog")
    inspection_values = ws_inspection.get_all_values()
except Exception as e:
    send_discord_notification(f"❌ Inspectionシート読み込み失敗: {e}")
    raise

inspection_status_map = {} 
if len(inspection_values) > 1:
    for row in inspection_values[1:]:
        if len(row) > 5:
            plate = str(row[3]).strip().replace(" ", "") 
            status = str(row[5]).strip().lower()         
            inspection_status_map[plate] = status

# ドライバ設定
options = Options()
options.add_argument('--headless')
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')
options.add_argument('--window-size=1920,1080')
driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
wait = WebDriverWait(driver, 15) # 待機時間を15秒に強化
collected_data = []

try:
    print("\n[ログイン] TMAシステムへアクセス...")
    driver.get(LOGIN_URL)
    wait.until(EC.presence_of_element_located((By.ID, "cardNo1")))
    driver.find_element(By.ID, "cardNo1").send_keys(USER_ID_1)
    driver.find_element(By.ID, "cardNo2").send_keys(USER_ID_2)
    driver.find_element(By.ID, "password").send_keys(PASSWORD)
    driver.find_element(By.ID, "password").send_keys(Keys.RETURN)
    
    # ログイン成否のチェック
    sleep(5)
    if "login" in driver.current_url.lower():
        raise Exception("ログインに失敗しました。IDまたはパスワードを確認してください。")

    for i, item in enumerate(target_stations_raw):
        station_name = item.get('station', '不明')
        station_cd = str(item.get('stationCd', '')).replace('.0', '')
        area = str(item.get('city', 'other')).strip()

        print(f"[{i+1}/{len(target_stations_raw)}] {station_name} 巡回中...")
        base_url = f"https://dailycheck.tc-extsys.jp/tcrappsweb/web/routineStationVehicle.html?stationCd={station_cd}"
        driver.get(base_url)
        wait.until(EC.presence_of_element_located((By.CLASS_NAME, "car-list-box")))

        soup = BeautifulSoup(driver.page_source, "lxml")
        car_boxes = soup.find_all("div", class_="car-list-box")
        
        if not car_boxes:
            raise Exception(f"ステーション {station_name} で車両リストが取得できませんでした。")

        # 基準時刻の取得
        now_jst = datetime.now(timezone(timedelta(hours=+9), 'JST'))
        start_time_str = f"{now_jst.strftime('%Y-%m-%d')} {now_jst.hour:02d}:00"

        for box_idx, box in enumerate(car_boxes):
            # 車両特定
            raw_car_text = box.find("div", class_="car-list-title-area").get_text(strip=True)
            plate = raw_car_text.split(" / ")[0].strip().replace(" ", "")
            model = raw_car_text.split(" / ")[1].strip() if " / " in raw_car_text else ""
            
            # 車両単位フィルタリング
            current_status = inspection_status_map.get(plate, 'standby')
            if current_status in ['checked', 'unnecessary', '7days_rule']:
                print(f"    -> {plate} は巡回済みのためスキップ")
                continue

            # --- 【前半: 72h】取得 ---
            first_72h = []
            timetable = box.find("table", class_="timetable")
            if not timetable:
                raise Exception(f"車両 {plate} のタイムライン(前半)が見つかりません。")
                
            rows = timetable.find_all("tr")
            data_cells = []
            for r in rows:
                cells = r.find_all("td")
                if cells and any(x in c.get("class", []) for c in cells for x in ["vacant", "full", "impossible", "others"]):
                    data_cells = cells
                    break
            
            if not data_cells:
                raise Exception(f"車両 {plate} の予約データセル(前半)が特定できません。")

            for cell in data_cells:
                cls = cell.get("class", [])
                symbol = "○" if "vacant" in cls else ("s" if "impossible" in cls else "×")
                colspan = int(cell.get("colspan", 1))
                first_72h.extend([symbol] * colspan)
            
            # 整合性チェック（穴埋めせず、個数が違えば即座にエラー）
            if len(first_72h) != 288:
                raise ValueError(f"【致命的】{plate} の前半データが 288 個ではありません (現在: {len(first_72h)}個)。取得に失敗しています。")

            # --- 【後半: 72h】取得 (TMA2へ遷移) ---
            second_72h = []
            link_element = box.find("span", class_="link-btn")
            if not link_element or not link_element.find("a"):
                raise Exception(f"車両 {plate} の詳細リンクが見つかりません。")
                
            reserve_link = link_element.find("a")['href']
            driver.get(f"https://dailycheck.tc-extsys.jp{reserve_link}")
            
            # 画面遷移の確認
            wait.until(EC.presence_of_element_located((By.ID, "reserveStartDate")))
            
            # 今日+3日の日付を選択
            target_date_val = (now_jst + timedelta(days=3)).strftime('%Y-%m-%d')
            date_select = driver.find_element(By.ID, "reserveStartDate")
            date_select.clear()
            date_select.send_keys(target_date_val)
            date_select.send_keys(Keys.RETURN)
            
            # 通信と描画を待機（timetable-contents が更新されるまで）
            sleep(4) 
            wait.until(EC.presence_of_element_located((By.CLASS_NAME, "timetable-contents")))

            # TMA2のタイムライン解析
            soup_detail = BeautifulSoup(driver.page_source, "lxml")
            timetable_div = soup_detail.find("div", class_="timetable-contents")
            if not timetable_div:
                raise Exception(f"車両 {plate} の後半画面(TMA2)でタイムラインが見つかりません。")
                
            detail_cells = timetable_div.find_all("td")
            if not detail_cells:
                raise Exception(f"車両 {plate} の予約データセル(後半)が取得できません。")

            for cell in detail_cells:
                cls = cell.get("class", [])
                # スロットに関係ないtdを除外するためのフィルタ
                if any(x in cls for x in ["vacant", "full", "impossible", "others"]):
                    symbol = "○" if "vacant" in cls else ("s" if "impossible" in cls else "×")
                    colspan = int(cell.get("colspan", 1))
                    second_72h.extend([symbol] * colspan)

            # 整合性チェック（後半も穴埋め禁止）
            if len(second_72h) != 288:
                raise ValueError(f"【致命的】{plate} の後半データが 288 個ではありません (現在: {len(second_72h)}個)。ページ遷移後の取得に失敗しています。")

            # 最終結合チェック
            full_rsv = "".join(first_72h) + "".join(second_72h)
            if len(full_rsv) != 576:
                raise ValueError(f"【致命的】{plate} の最終データ数が 576 個に達していません ({len(full_rsv)}個)。")

            collected_data.append([area, station_name, plate, model, start_time_str, full_rsv])
            print(f"    -> {plate} 144hデータ確保完了 (576/576)")

            # TMA1（一覧）に戻る
            driver.get(base_url)
            wait.until(EC.presence_of_element_located((By.CLASS_NAME, "car-list-box")))

    # シート保存
    if collected_data:
        try:
            sh_prod = gc.open_by_url(PRODUCTION_SHEET_URL)
            df_output = pd.DataFrame(collected_data, columns=['city', 'station', 'plate', 'model', 'getTime', 'rsvData'])
            for area_name in df_output['city'].unique():
                df_area = df_output[df_output['city'] == area_name].copy()
                ws_name = f"{str(area_name).replace('市','').strip()}_更新用"
                try: 
                    ws = sh_prod.worksheet(ws_name)
                except: 
                    ws = sh_prod.add_worksheet(title=ws_name, rows=1000, cols=10)
                
                ws.clear()
                # ヘッダー＋データの一括更新
                data_to_update = [df_area.drop(columns=['city']).columns.values.tolist()] + df_area.drop(columns=['city']).values.tolist()
                ws.update(data_to_update)
            
            send_discord_notification("✅ yoyakuLong: 144時間データ更新が完了しました。")
            print("\n[完了] すべての処理が正常に終了しました。")
        except Exception as e:
            send_discord_notification(f"❌ スプレッドシート保存エラー: {e}")
            raise
    else:
        print("\n[通知] 更新対象の車両がありませんでした。")

except Exception as e:
    error_msg = f"❌ yoyakuLong重大エラー（停止）: {e}"
    print(f"\n{error_msg}")
    send_discord_notification(error_msg)
    sys.exit(1) # 異常終了を明示してGitHub Actionsを赤く光らせる

finally:
    driver.quit()
