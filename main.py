# ==========================================================
# 【yoyakuLong】144時間(6日間) 精密 Sniper エンジン（最終修正版）
# 改修内容: 後半(TMA2)日付のSelectクラスによる正確なプルダウン選択対応
# ★スクレイピング/書き込み 2段ジョブ分割対応:
#   STAGE=scrape でスクレイピングのみ実行しcollected_data.jsonへ保存
#   STAGE=write  でJSON読込→シート書き込みのみ実行（リトライ対象はこちらのみ）
#   STAGE未指定(all)は従来通り一括実行（ローカル動作確認用）
# ★シート書き込みにwith_retry（5回・指数バックオフ）を新規追加
#   （従来はリトライなしで即エラー終了していた）
# ★書き込みリトライ発生時のDiscord通知を追加（label付き）。
#   5回とも失敗した場合は専用の「上限到達」通知のみ送信し、
#   従来の「重大なエラー」通知とは重複させない
# ==========================================================
import sys
import os
import json
import pandas as pd
import gspread
import unicodedata
import urllib.request
from time import sleep
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup

# STAGE: 'scrape'=収集のみ, 'write'=書き込みのみ, 'all'=一括（未指定時のデフォルト、ローカル確認用）
STAGE = os.environ.get('STAGE', 'all').lower()
NEEDS_SCRAPE = STAGE in ('scrape', 'all')
NEEDS_WRITE = STAGE in ('write', 'all')
COLLECTED_DATA_FILE = "collected_data.json"

# Seleniumはscrape段階でのみ必要
if NEEDS_SCRAPE:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import Select

# --- Discord通知用設定 ---
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/REDACTED/REDACTED"

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
USER_ID_2 = "REDACTED"
# PW_MODE環境変数（mode1/mode2）に応じてPWを切り替える（ポータルのPWモード切替と連携）
PW_MODE = os.environ.get('PW_MODE', 'mode1').lower()
PASSWORD_MAP = {
    "mode1": "REDACTED",
    "mode2": "REDACTED"
}
PASSWORD = PASSWORD_MAP.get(PW_MODE, "REDACTED")
print(f"[PWモード] {PW_MODE} を使用")

# 2. シート設定
PRODUCTION_SHEET_URL = "https://docs.google.com/spreadsheets/d/1LCyj16nsRYBk5cTpx2Sb75qmtm3YGKNEIdeyUvZzQQI/edit"
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

# ==========================================================
# ★上限到達済みリトライを示す専用例外
# with_retryが5回とも失敗した場合にこれを送出する。
# この時点で「上限到達」通知は送信済みのため、外側の
# 「重大なエラー」通知と重複させないための目印として使う。
# ==========================================================
class RetryExhaustedError(Exception):
    pass

# ==========================================================
# ★新規追加: リトライ付きAPI呼び出しラッパー（書き込み専用）
# Google Sheets APIの一時的なエラー(5xx, 429)のみリトライする
# 5回, 1→2→4→8→16秒の指数バックオフ
# label: どの書き込み処理か判別するための表示名（Discord通知用）
# リトライ試行のたびにDiscord通知。5回とも失敗した場合は
# 「上限到達」専用通知のみ送り、以後は外側で重複通知しない
# ==========================================================
def with_retry(func, *args, label="不明な処理", **kwargs):
    max_retries = 5
    delay = 1
    last_exception = None
    for attempt in range(1, max_retries + 1):
        try:
            return func(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            status_code = None
            try:
                status_code = e.response.status_code
            except Exception:
                pass
            is_retryable = (status_code == 429) or (status_code is not None and 500 <= status_code < 600)
            if not is_retryable:
                raise
            if attempt == max_retries:
                send_discord_notification(f"⛔ {label} への書き込みに{max_retries}回失敗。時間をおいて再実行してください。")
                raise RetryExhaustedError(f"{label}: 上限{max_retries}回リトライしても失敗 ({e})") from e
            print(f"   !! [リトライ {attempt}/{max_retries}] APIエラー(status={status_code})。{delay}秒後に再試行します...")
            send_discord_notification(f"⚠️ [リトライ {attempt}/{max_retries}] {label} への書き込みを再試行中...")
            last_exception = e
            sleep(delay)
            delay *= 2
    if last_exception:
        raise last_exception

print(f"\n[モード] 144時間(6日間) Sniper（日付プルダウン正確選択モード）")

# I. 車両リスト(CSV)読み込み
df_map = pd.read_csv(CSV_FILE_NAME, encoding='utf-8')
df_map.columns = df_map.columns.str.strip()
if 'area' in df_map.columns: df_map = df_map.rename(columns={'area': 'city'})
if 'station_name' in df_map.columns: df_map = df_map.rename(columns={'station_name': 'station'})

collected_data = []
absent_vehicles = []  # TMA上で見つからなかった車両（station, plate, noteの3列でTMA不在シートに書き込む）

if NEEDS_SCRAPE:
    # II. inspectionlogからターゲットを特定
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
        if STAGE == 'scrape':
            with open(COLLECTED_DATA_FILE, "w", encoding="utf-8") as f:
                json.dump({"collected_data": [], "absent_vehicles": []}, f, ensure_ascii=False)
        sys.exit(0)

    print(f"-> ターゲット確定: {len(target_vehicles)} 台")

    # ドライバ設定
    options = Options()
    options.add_argument('--headless')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--window-size=1920,1080')
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    wait = WebDriverWait(driver, 25)

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

        # 実行開始時刻を1回だけ取得して固定（ループ中に00分をまたいでもズレない）
        now_jst = datetime.now(timezone(timedelta(hours=+9), 'JST'))
        start_time_str = f"{now_jst.strftime('%Y-%m-%d')} {now_jst.hour:02d}:00"
        print(f"[基準時刻] {start_time_str}")

        for i, target in enumerate(target_vehicles):
            target_plate = target['plate']
            station_name = target['station']
            station_cd = target['stationCd']
            area = target['city']

            print(f"[{i+1}/{len(target_vehicles)}] {target_plate} ({station_name}) を狙い撃ち中...")

            try:
                base_url = f"https://dailycheck.tc-extsys.jp/tcrappsweb/web/routineStationVehicle.html?stationCd={station_cd}"
                driver.get(base_url)

                # ローディング画面の待機
                try:
                    wait.until(EC.invisibility_of_element_located((By.CLASS_NAME, "loading-view")))
                except:
                    raise Exception(f"ローディング画面が消えません。通信環境を確認してください。")

                # 車両BOXの特定（TMA上に見つからない場合はスキップ＝TMA不在として扱う）
                wait.until(EC.presence_of_element_located((By.CLASS_NAME, "car-list-box")))
                car_boxes = driver.find_elements(By.CLASS_NAME, "car-list-box")
                target_element = None
                for box in car_boxes:
                    title_area = box.find_element(By.CLASS_NAME, "car-list-title-area").text
                    if target_plate in title_area.replace(" ", ""):
                        target_element = box
                        model = title_area.split(" / ")[-1].strip() if " / " in title_area else ""
                        break

                if not target_element:
                    print(f"  !! [スキップ] 車両 {target_plate} をページ内で特定できませんでした（TMA不在として記録）。")
                    absent_vehicles.append([station_name, target_plate, "※TMA未検知"])
                    continue

                # 予約表の描画待機
                try:
                    wait.until(lambda d: target_element.find_elements(By.CLASS_NAME, "timetable"))
                except:
                    raise Exception(f"車両 {target_plate} の予約表が描画されませんでした。")

                # 前半データ解析
                soup = BeautifulSoup(driver.page_source, "lxml")
                target_box = None
                for box in soup.find_all("div", class_="car-list-box"):
                    raw_text = box.find("div", class_="car-list-title-area").get_text(strip=True).replace(" ", "")
                    if target_plate in raw_text:
                        target_box = box
                        break

                first_72h = []
                timetable = target_box.find("table", class_="timetable")
                # 後半と同様にフラット取得し、対象クラスのtdだけ処理する（breakなし）
                for cell in timetable.find_all("td"):
                    cls = cell.get("class", [])
                    if any(x in cls for x in ["vacant", "full", "impossible", "others", "myself"]):
                        symbol = "○" if "vacant" in cls else ("s" if "impossible" in cls else "×")
                        colspan = int(cell.get("colspan", 1))
                        first_72h.extend([symbol] * colspan)

                if len(first_72h) != 288:
                    raise ValueError(f"前半データ不足: {len(first_72h)}/288")

                # --- 【後半: 72h】 (TMA2) ---
                reserve_link = target_box.find("span", class_="link-btn").find("a")['href']
                driver.get(f"https://dailycheck.tc-extsys.jp{reserve_link}")

                wait.until(EC.invisibility_of_element_located((By.CLASS_NAME, "loading-view")))
                wait.until(EC.presence_of_element_located((By.ID, "reserveStartDate")))

                target_date_val = (now_jst + timedelta(days=3)).strftime('%Y-%m-%d')
                date_select_element = driver.find_element(By.ID, "reserveStartDate")

                # ★修正: プルダウン(select)要素として正確に選択する
                try:
                    Select(date_select_element).select_by_value(target_date_val)
                except Exception as e:
                    raise Exception(f"後半日付プルダウンの選択に失敗しました (指定値: {target_date_val}): {e}")

                # 描画待ち (ローディング再出現に備える)
                sleep(2)
                wait.until(EC.invisibility_of_element_located((By.CLASS_NAME, "loading-view")))
                wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".timetable-contents table")))

                soup_detail = BeautifulSoup(driver.page_source, "lxml")
                timetable_detail = soup_detail.find("div", class_="timetable-contents").find("table")
                detail_cells = timetable_detail.find_all("td")
                second_72h = []
                for cell in detail_cells:
                    cls = cell.get("class", [])
                    if any(x in cls for x in ["vacant", "full", "impossible", "others", "myself"]):
                        symbol = "○" if "vacant" in cls else ("s" if "impossible" in cls else "×")
                        colspan = int(cell.get("colspan", 1))
                        second_72h.extend([symbol] * colspan)

                if len(second_72h) != 288:
                    raise ValueError(f"後半データ不足: {len(second_72h)}/288")

                full_rsv = "".join(first_72h) + "".join(second_72h)
                if len(full_rsv) != 576:
                    raise ValueError(f"最終結合データ不備: {len(full_rsv)}/576")

                collected_data.append([area, station_name, target_plate, model, start_time_str, full_rsv])
                print(f"    -> {target_plate} 144h取得完了")

            except Exception as ex:
                # 1台分のエラーは全体を止めず、ログだけ出してスキップして次の車両へ進む（yoyaku側と同じ挙動）
                print(f"  !! [スキップ] 車両解析エラー [{station_name} / {target_plate}]: {ex}")
                continue

        if STAGE == 'scrape':
            # ★scrape専用: 収集結果をJSONに保存。書き込みはwrite jobに任せる
            with open(COLLECTED_DATA_FILE, "w", encoding="utf-8") as f:
                json.dump({"collected_data": collected_data, "absent_vehicles": absent_vehicles}, f, ensure_ascii=False)
            print(f"\n[scrape完了] 車両データ{len(collected_data)}件・TMA不在{len(absent_vehicles)}件を{COLLECTED_DATA_FILE}に保存しました。")

    except Exception as e:
        import traceback
        if isinstance(e, RetryExhaustedError):
            print(f"\nエラー発生のため停止: {e}")
        else:
            error_msg = f"❌ yoyakuLong重大エラー（停止）: {e}"
            print(f"\n{error_msg}")
            print(traceback.format_exc())
            send_discord_notification(error_msg)
        sys.exit(1)

    finally:
        if 'driver' in locals(): driver.quit()

if NEEDS_WRITE:
    if STAGE == 'write':
        # ★write専用: scrape jobがartifactとして保存したJSONを読み込む
        if not os.path.exists(COLLECTED_DATA_FILE):
            print(f"!! エラー: {COLLECTED_DATA_FILE} が見つかりません（scrape jobのartifactを確認してください）。")
            sys.exit(1)
        with open(COLLECTED_DATA_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        collected_data = payload.get("collected_data", [])
        absent_vehicles = payload.get("absent_vehicles", [])

    try:
        # シート保存
        prod_sheet_key = PRODUCTION_SHEET_URL.split('/d/')[1].split('/edit')[0]

        if collected_data:
            print(f"[シート保存] 書き込み先キー: {prod_sheet_key}")
            try:
                sh_prod = gc.open_by_key(prod_sheet_key)
            except Exception as e:
                raise Exception(f"【自爆】本番シートのオープンに失敗 (key={prod_sheet_key}): {e}")
            df_output = pd.DataFrame(collected_data, columns=['city', 'station', 'plate', 'model', 'getTime', 'rsvData'])
            for area_name in df_output['city'].unique():
                df_area = df_output[df_output['city'] == area_name].copy()
                ws_name = f"{str(area_name).replace('市','').strip()}_更新用"
                try:
                    ws = with_retry(sh_prod.worksheet, ws_name, label=f"予約管理メイン/{ws_name}")
                except gspread.exceptions.WorksheetNotFound:
                    try:
                        ws = with_retry(sh_prod.add_worksheet, title=ws_name, rows=1000, cols=10, label=f"予約管理メイン/{ws_name}")
                    except Exception as e:
                        if isinstance(e, RetryExhaustedError): raise
                        raise Exception(f"【自爆】シート '{ws_name}' の新規作成に失敗 (編集者権限でもオーナー操作が必要な場合があります): {e}")
                with_retry(ws.clear, label=f"予約管理メイン/{ws_name}")
                with_retry(ws.update, [df_area.drop(columns=['city']).columns.values.tolist()] + df_area.drop(columns=['city']).values.tolist(), value_input_option='RAW', label=f"予約管理メイン/{ws_name}")
                print(f"    -> '{ws_name}' 書き込み完了 ({len(df_area)}台)")

            send_discord_notification(f"✅ yoyakuLong Sniper: {len(collected_data)}台の更新が完了。")

        # TMA不在シートへの書き込み（yoyakuと同じ予約管理メインSS内の「TMA不在」シートに上書き）
        if absent_vehicles:
            try:
                sh_prod_absence = gc.open_by_key(prod_sheet_key)
                absence_sheet = with_retry(sh_prod_absence.worksheet, "TMA不在", label="TMA不在シート")
                with_retry(absence_sheet.clear, label="TMA不在シート")
                with_retry(absence_sheet.update, [['station', 'plate', 'note']] + absent_vehicles, value_input_option='RAW', label="TMA不在シート")
                print(f"[TMA不在] {len(absent_vehicles)}台を記録しました。")
            except Exception as e:
                print(f"  !! [警告] TMA不在シートへの書き込みに失敗しました: {e}")
                if not isinstance(e, RetryExhaustedError):
                    send_discord_notification(f"⚠️ yoyakuLong: TMA不在シートへの書き込みに失敗しました:\n```{e}```")

    except Exception as e:
        import traceback
        if isinstance(e, RetryExhaustedError):
            print(f"\nエラー発生のため停止: {e}")
        else:
            error_msg = f"❌ yoyakuLong重大エラー（停止）: {e}"
            print(f"\n{error_msg}")
            print(traceback.format_exc())
            send_discord_notification(error_msg)
        sys.exit(1)
