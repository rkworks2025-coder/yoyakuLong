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
# ★TMAサーバー遅延対策（2026-10-26）:
#   - ページ読込タイムアウトを設定（1ページの無限待ちで全体が止まらないように）
#   - ログインを関数化し、失敗時は1回だけ再ログイン
#   - ステーションページ・後半予約ページそれぞれ最大3回試行（描画遅れ・データ不足・セッション切れ・タイムアウト）
#   - 「TMA不在」判定は描画遅れの誤判定を避けるため2回確認してから確定
#   - それでも失敗した車両は最後にまとめてもう1周
#   - 取得できなかった車両はJSON経由でwrite jobへ渡し、完了通知に添える
#   - 再試行で順序が崩れないよう、結果は元の車両順で組み立てる
# ★高速化（2026-10-26）:
#   A. 対象車両をステーション単位にまとめ、ステーションページは1回だけ開いて
#      前半72hを全台分まとめて読み取る（従来は1台ごとに同じページを開き直していた）
#   B. 画像を読み込まない・ページ読込完了を待たない(eager)設定
#   C. 後半72hの日付切替後の固定sleep(2)を廃止し、表の切替を検知した時点で進む
#      （切替を検知できない場合も最大2秒で進むので従来より遅くはならない）
#   D. 再試行の待機時間を短縮（3→6秒、再巡回前15秒）
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
    # ★遅延対策: 例外判定用
    from selenium.common.exceptions import TimeoutException, WebDriverException

# --- Discord通知用設定 ---
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

def send_discord_notification(message):
    if not DISCORD_WEBHOOK_URL: return
    data = {"content": message}
    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    req = urllib.request.Request(DISCORD_WEBHOOK_URL, data=json.dumps(data).encode(), headers=headers)
    try:
        urllib.request.urlopen(req)
    except Exception as e:
        print(f"Discord通知エラー: {e}")

# ==========================================================
# ★取得失敗一覧をDiscord通知用の文字列にまとめる
# 長くなりすぎないよう先頭20件まで表示し、残りは件数のみ
# ==========================================================
def format_failures(failed_vehicles):
    if not failed_vehicles:
        return ""
    shown = "、".join(failed_vehicles[:20])
    more = f" 他{len(failed_vehicles) - 20}台" if len(failed_vehicles) > 20 else ""
    return f"取得失敗 {len(failed_vehicles)}台: {shown}{more}"

# 1. ログイン情報
LOGIN_URL = "https://dailycheck.tc-extsys.jp/tcrappsweb/web/login/tawLogin.html"
USER_ID_1 = os.environ.get("TMA_ID_1", "0030")
USER_ID_2 = os.environ.get("TMA_ID_2", "")
# PW_MODE環境変数（mode1/mode2）に応じてPWを切り替える（ポータルのPWモード切替と連携）
PW_MODE = os.environ.get('PW_MODE', 'mode1').lower()
PASSWORD_MAP = {
    "mode1": os.environ.get("TMA_PW_MODE1", ""),
    "mode2": os.environ.get("TMA_PW_MODE2", "")
}
PASSWORD = PASSWORD_MAP.get(PW_MODE, os.environ.get("TMA_PW_MODE1", ""))
print(f"[PWモード] {PW_MODE} を使用")

# 2. シート設定
PRODUCTION_SHEET_URL = "https://docs.google.com/spreadsheets/d/1LCyj16nsRYBk5cTpx2Sb75qmtm3YGKNEIdeyUvZzQQI/edit"
CSV_FILE_NAME = "station_code_map.csv"
INSPECTION_SHEET_URL = "https://docs.google.com/spreadsheets/d/11XglLANtnG7bCxYjLRMGoZY25wspjHsGR3IG2ZyRITs/edit"

# ★遅延対策: 待機・再試行の設定
PAGE_LOAD_TIMEOUT = 60       # 1ページの読込上限(秒)
ELEMENT_WAIT_TIMEOUT = 25    # 要素待ちの上限(秒)（従来値）
VEHICLE_MAX_ATTEMPTS = 3     # ステーションページ・後半予約ページそれぞれの試行回数
ABSENT_CONFIRM_ATTEMPTS = 2  # 「TMA不在」を確定するまでの確認回数
RETRY_BASE_WAIT = 3          # 再試行前の待機(秒)。試行ごとに倍（★高速化D: 5→3）
SECOND_PASS_WAIT = 15        # 失敗分をもう1周する前の待機(秒)（★高速化D: 30→15）
DATE_SWITCH_WAIT = 2         # ★高速化C: 日付切替の検知を待つ上限(秒)（従来の固定sleep(2)と同じ）

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

# ★遅延対策用の例外
class SessionExpired(Exception):
    """ログイン画面へ戻された（再ログイン後に再試行）"""
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
failed_vehicles = []  # ★最終的に取得できなかった車両（station/plate）

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
                json.dump({"collected_data": [], "absent_vehicles": [], "failed_vehicles": []}, f, ensure_ascii=False)
        sys.exit(0)

    print(f"-> ターゲット確定: {len(target_vehicles)} 台")

    # ドライバ設定
    # ★遅延対策: ページ読込タイムアウトを設定し、落ちた時に作り直せるよう関数化
    # ★高速化B: 画像を読み込まない・ページ読込完了(load)を待たずHTML到着(eager)で進む
    #   （以降はすべて必要な要素の出現を明示的に待っているため取得内容は変わらない）
    def create_driver():
        options = Options()
        options.add_argument('--headless')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--window-size=1920,1080')
        options.add_argument('--blink-settings=imagesEnabled=false')
        options.add_experimental_option("prefs", {"profile.managed_default_content_settings.images": 2})
        options.page_load_strategy = 'eager'
        d = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
        d.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
        return d

    # ==========================================================
    # ★ログイン（成否確認つき・失敗時は1回だけ再試行）
    # ==========================================================
    def login(d):
        last_err = None
        for attempt in range(1, 3):
            try:
                print("\n[ログイン] TMAシステムへアクセス...")
                d.get(LOGIN_URL)
                WebDriverWait(d, ELEMENT_WAIT_TIMEOUT).until(EC.presence_of_element_located((By.ID, "cardNo1")))
                d.find_element(By.ID, "cardNo1").clear()
                d.find_element(By.ID, "cardNo1").send_keys(USER_ID_1)
                d.find_element(By.ID, "cardNo2").clear()
                d.find_element(By.ID, "cardNo2").send_keys(USER_ID_2)
                d.find_element(By.ID, "password").clear()
                d.find_element(By.ID, "password").send_keys(PASSWORD)
                d.find_element(By.ID, "password").send_keys(Keys.RETURN)
                try:
                    WebDriverWait(d, ELEMENT_WAIT_TIMEOUT).until(lambda x: "login" not in x.current_url.lower())
                except TimeoutException:
                    raise Exception("ログイン失敗。認証情報を確認してください。")
                print("[ログイン] 成功")
                return
            except Exception as e:
                last_err = e
                print(f"   !! [ログイン失敗 {attempt}/2] {type(e).__name__}: {e}")
                if attempt < 2:
                    sleep(10)
        raise Exception(f"TMAへのログインに失敗しました（PWモード={PW_MODE}・2回試行）: {type(last_err).__name__}: {last_err}")

    def driver_alive(d):
        try:
            _ = d.current_url
            return True
        except Exception:
            return False

    # ==========================================================
    # ★試行中に起きた例外の共通処理
    # - セッション切れ → 再ログイン
    # - ブラウザが落ちた → ドライバを作り直して再ログイン
    # 戻り値: ログ表示用の短いエラー文字列
    # ==========================================================
    def handle_attempt_error(state, e):
        if isinstance(e, SessionExpired):
            try:
                login(state["driver"])
            except Exception as le:
                return f"{e} → 再ログイン失敗: {le}"
            return f"{e} → 再ログインしました"
        if isinstance(e, WebDriverException):
            first_line = str(e).splitlines()[0] if str(e) else ""
            msg = f"ブラウザエラー({type(e).__name__}): {first_line}"
            if not driver_alive(state["driver"]):
                print("   !! ブラウザが応答しないため作り直します")
                try: state["driver"].quit()
                except Exception: pass
                state["driver"] = create_driver()
                login(state["driver"])
            return msg
        return f"{type(e).__name__}: {e}"

    # ==========================================================
    # ★前半72hの読み取り（ステーションページのHTMLから1台分）
    # 戻り値: None=ページ内に車両なし / dict(first=記号リスト, link=後半予約ページ, model=車種)
    # 予約表の描画が不足している場合はValueErrorを送出（再試行対象）
    # ==========================================================
    def parse_first_half(soup, target_plate):
        target_box = None
        for box in soup.find_all("div", class_="car-list-box"):
            title_el = box.find("div", class_="car-list-title-area")
            if title_el is None:
                continue
            raw_text = title_el.get_text(strip=True).replace(" ", "")
            if target_plate in raw_text:
                target_box = box
                break

        if target_box is None:
            return None

        title_text = target_box.find("div", class_="car-list-title-area").get_text(strip=True)
        model = title_text.split(" / ")[-1].strip() if " / " in title_text else ""

        first_72h = []
        timetable = target_box.find("table", class_="timetable")
        if timetable is None:
            raise ValueError(f"車両 {target_plate} の予約表が描画されませんでした。")
        # 後半と同様にフラット取得し、対象クラスのtdだけ処理する（breakなし）
        for cell in timetable.find_all("td"):
            cls = cell.get("class", [])
            if any(x in cls for x in ["vacant", "full", "impossible", "others", "myself"]):
                symbol = "○" if "vacant" in cls else ("s" if "impossible" in cls else "×")
                colspan = int(cell.get("colspan", 1))
                first_72h.extend([symbol] * colspan)

        if len(first_72h) != 288:
            raise ValueError(f"前半データ不足: {len(first_72h)}/288")

        link_btn = target_box.find("span", class_="link-btn")
        link_a = link_btn.find("a") if link_btn else None
        if link_a is None or not link_a.get('href'):
            raise ValueError(f"車両 {target_plate} の予約ページリンクが見つかりません。")

        return {"first": first_72h, "link": link_a['href'], "model": model}

    # ==========================================================
    # ★高速化A: ステーションページを1回開き、対象車両の前半72hをまとめて読む
    # 戻り値: (ok={index: info}, absent=[index], failed={index: エラー})
    # - 描画不足の車両だけを対象に再試行（最大VEHICLE_MAX_ATTEMPTS回）
    # - ページ内に見つからない車両はABSENT_CONFIRM_ATTEMPTS回確認してからTMA不在として確定
    # ==========================================================
    def load_station(state, station_cd, idx_list):
        pending = list(idx_list)
        ok = {}
        absent_seen = {}
        vehicle_err = {}
        last_err = None

        for attempt in range(1, VEHICLE_MAX_ATTEMPTS + 1):
            is_last = (attempt == VEHICLE_MAX_ATTEMPTS)
            d = state["driver"]
            try:
                base_url = f"https://dailycheck.tc-extsys.jp/tcrappsweb/web/routineStationVehicle.html?stationCd={station_cd}"
                d.get(base_url)
                if "login" in d.current_url.lower():
                    raise SessionExpired("ログイン画面へ戻されました")

                wait = WebDriverWait(d, ELEMENT_WAIT_TIMEOUT)
                # ローディング画面の待機
                try:
                    wait.until(EC.invisibility_of_element_located((By.CLASS_NAME, "loading-view")))
                except:
                    raise Exception(f"ローディング画面が消えません。通信環境を確認してください。")

                # 車両BOXの出現待ち
                wait.until(EC.presence_of_element_located((By.CLASS_NAME, "car-list-box")))

                # ★予約表の描画待ち（車両BOXの出現後に予約表が遅れて描画されるため）
                # 未確定の対象車両すべてについて前半72hが読み取れる状態になるまで待つ
                # （ページ内に無い車両はNoneが返るので待ち対象外）
                # 時間内に揃わなければ揃った分だけで判定し、残りは再試行に回す
                def targets_ready(x):
                    s = BeautifulSoup(x.page_source, "lxml")
                    for i in pending:
                        try:
                            parse_first_half(s, target_vehicles[i]['plate'])
                        except Exception:
                            return False
                    return ("ready", s)
                try:
                    _, soup = WebDriverWait(d, ELEMENT_WAIT_TIMEOUT, poll_frequency=0.5).until(targets_ready)
                except TimeoutException:
                    soup = BeautifulSoup(d.page_source, "lxml")

                # 前半データ解析（対象車両をまとめて）
                next_pending = []
                for i in pending:
                    target_plate = target_vehicles[i]['plate']
                    try:
                        info = parse_first_half(soup, target_plate)
                    except Exception as e:
                        vehicle_err[i] = e
                        next_pending.append(i)
                        continue
                    if info is None:
                        # 車両BOXの特定（TMA上に見つからない場合は確認回数に達したらTMA不在として扱う）
                        absent_seen[i] = absent_seen.get(i, 0) + 1
                        if absent_seen[i] < ABSENT_CONFIRM_ATTEMPTS:
                            vehicle_err[i] = Exception("ページ内で車両を特定できませんでした（念のため再確認）")
                            next_pending.append(i)
                        continue
                    ok[i] = info
                    vehicle_err.pop(i, None)

                pending = next_pending
                if not pending:
                    break
                last_err = Exception(f"未確定 {len(pending)}台: " + " / ".join(f"{target_vehicles[i]['plate']}={vehicle_err.get(i)}" for i in pending))
                print(f"   !! [試行 {attempt}/{VEHICLE_MAX_ATTEMPTS}] {last_err}")

            except Exception as e:
                last_err = e
                print(f"   !! [試行 {attempt}/{VEHICLE_MAX_ATTEMPTS}] {handle_attempt_error(state, e)}")

            if not is_last:
                sleep(RETRY_BASE_WAIT * (2 ** (attempt - 1)))

        absent = [i for i in idx_list if i not in ok and absent_seen.get(i, 0) >= ABSENT_CONFIRM_ATTEMPTS]
        failed = {i: vehicle_err.get(i, last_err) for i in pending if i not in absent}
        return ok, absent, failed

    # ==========================================================
    # ★後半72h（TMA2）を再試行つきで取得する（1台分）
    # ==========================================================
    def fetch_second_half(state, reserve_link, now_jst):
        last_err = None
        for attempt in range(1, VEHICLE_MAX_ATTEMPTS + 1):
            is_last = (attempt == VEHICLE_MAX_ATTEMPTS)
            d = state["driver"]
            try:
                wait = WebDriverWait(d, ELEMENT_WAIT_TIMEOUT)
                # --- 【後半: 72h】 (TMA2) ---
                d.get(f"https://dailycheck.tc-extsys.jp{reserve_link}")
                if "login" in d.current_url.lower():
                    raise SessionExpired("ログイン画面へ戻されました")

                wait.until(EC.invisibility_of_element_located((By.CLASS_NAME, "loading-view")))
                wait.until(EC.presence_of_element_located((By.ID, "reserveStartDate")))

                # ★高速化C: 日付切替前の表を覚えておき、差し替わったことを検知する
                old_tables = d.find_elements(By.CSS_SELECTOR, ".timetable-contents table")
                old_table = old_tables[0] if old_tables else None

                target_date_val = (now_jst + timedelta(days=3)).strftime('%Y-%m-%d')
                date_select_element = d.find_element(By.ID, "reserveStartDate")

                # ★修正: プルダウン(select)要素として正確に選択する
                try:
                    Select(date_select_element).select_by_value(target_date_val)
                except Exception as e:
                    raise Exception(f"後半日付プルダウンの選択に失敗しました (指定値: {target_date_val}): {e}")

                # 描画待ち (ローディング再出現に備える)
                # ★高速化C: 固定sleep(2)の代わりに「旧表の消滅」か「ローディング表示」を検知したら即進む
                #   どちらも検知できなければ従来と同じ最大2秒で進む
                def date_switched(x):
                    try:
                        if old_table is not None and EC.staleness_of(old_table)(x):
                            return True
                    except Exception:
                        pass
                    try:
                        if EC.visibility_of_element_located((By.CLASS_NAME, "loading-view"))(x):
                            return True
                    except Exception:
                        pass
                    return False
                try:
                    WebDriverWait(d, DATE_SWITCH_WAIT, poll_frequency=0.1).until(date_switched)
                except TimeoutException:
                    pass
                wait.until(EC.invisibility_of_element_located((By.CLASS_NAME, "loading-view")))
                wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".timetable-contents table")))

                soup_detail = BeautifulSoup(d.page_source, "lxml")
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

                return second_72h

            except Exception as e:
                last_err = e
                print(f"   !! [後半 試行 {attempt}/{VEHICLE_MAX_ATTEMPTS}] {handle_attempt_error(state, e)}")

            if not is_last:
                sleep(RETRY_BASE_WAIT * (2 ** (attempt - 1)))

        raise Exception(f"後半72hを{VEHICLE_MAX_ATTEMPTS}回試行しても取得できませんでした（最後のエラー: {type(last_err).__name__}: {last_err}）")

    # ==========================================================
    # ★高速化A: 対象車両をステーション単位にまとめる（元の並び順を保持）
    # ==========================================================
    def group_by_station(indexes):
        groups = {}
        order = []
        for i in indexes:
            cd = target_vehicles[i]['stationCd']
            if cd not in groups:
                groups[cd] = []
                order.append(cd)
            groups[cd].append(i)
        return [(cd, groups[cd]) for cd in order]

    state = {"driver": create_driver()}

    try:
        login(state["driver"])

        # 実行開始時刻を1回だけ取得して固定（ループ中に00分をまたいでもズレない）
        now_jst = datetime.now(timezone(timedelta(hours=+9), 'JST'))
        start_time_str = f"{now_jst.strftime('%Y-%m-%d')} {now_jst.hour:02d}:00"
        print(f"[基準時刻] {start_time_str}")

        # ★再試行で順序が崩れないよう、結果は車両の元の順番(index)で保持する
        rows_by_index = {}
        absent_set = set()

        # ==========================================================
        # 指定した車両群をステーション単位で処理し、取得できなかった車両のindexを返す
        # 1台分のエラーは全体を止めず、ログだけ出して次へ進む
        # ==========================================================
        def process(indexes, label):
            failed_here = []
            groups = group_by_station(indexes)
            for g_no, (station_cd, idx_list) in enumerate(groups):
                station_name = target_vehicles[idx_list[0]]['station']
                prefix = f"{label} " if label else ""
                print(f"[{prefix}{g_no+1}/{len(groups)}] {station_name} ({len(idx_list)}台) を狙い撃ち中...")

                ok, absent, failed = load_station(state, station_cd, idx_list)

                for i in absent:
                    print(f"  !! [スキップ] 車両 {target_vehicles[i]['plate']} をページ内で特定できませんでした（TMA不在として記録）。")
                    absent_set.add(i)
                for i, err in failed.items():
                    print(f"  !! [後回し] 車両解析エラー [{station_name} / {target_vehicles[i]['plate']}]: {err}")
                    failed_here.append(i)

                for i in idx_list:
                    if i not in ok:
                        continue
                    t = target_vehicles[i]
                    info = ok[i]
                    try:
                        second_72h = fetch_second_half(state, info["link"], now_jst)
                        full_rsv = "".join(info["first"]) + "".join(second_72h)
                        if len(full_rsv) != 576:
                            raise ValueError(f"最終結合データ不備: {len(full_rsv)}/576")
                        rows_by_index[i] = [t['city'], t['station'], t['plate'], info["model"], start_time_str, full_rsv]
                        print(f"    -> {t['plate']} 144h取得完了")
                    except Exception as ex:
                        print(f"  !! [後回し] 車両解析エラー [{station_name} / {t['plate']}]: {ex}")
                        failed_here.append(i)
            return failed_here

        failed_indexes = process(list(range(len(target_vehicles))), "")

        # ★失敗分をまとめてもう1周（サーバーが一時的に重かった分の回収）
        if failed_indexes:
            print(f"\n[再巡回] 取得失敗 {len(failed_indexes)}台を{SECOND_PASS_WAIT}秒後に再取得します...")
            sleep(SECOND_PASS_WAIT)
            failed_indexes = process(sorted(failed_indexes), "再巡回")

        # 元の順番で組み立て
        for i in sorted(rows_by_index.keys()):
            collected_data.append(rows_by_index[i])
        for i in sorted(absent_set):
            absent_vehicles.append([target_vehicles[i]['station'], target_vehicles[i]['plate'], "※TMA未検知"])
        failed_vehicles = [f"{target_vehicles[i]['station']}/{target_vehicles[i]['plate']}" for i in sorted(failed_indexes)]

        if failed_vehicles:
            print(f"\n[取得失敗まとめ] {format_failures(failed_vehicles)}")

        if STAGE == 'scrape':
            # ★scrape専用: 収集結果をJSONに保存。書き込みはwrite jobに任せる
            with open(COLLECTED_DATA_FILE, "w", encoding="utf-8") as f:
                json.dump({"collected_data": collected_data, "absent_vehicles": absent_vehicles, "failed_vehicles": failed_vehicles}, f, ensure_ascii=False)
            print(f"\n[scrape完了] 車両データ{len(collected_data)}件・TMA不在{len(absent_vehicles)}件・取得失敗{len(failed_vehicles)}件を{COLLECTED_DATA_FILE}に保存しました。")

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
        try: state["driver"].quit()
        except Exception: pass

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
        failed_vehicles = payload.get("failed_vehicles", [])

    failure_text = format_failures(failed_vehicles)

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

            # ★取得失敗があれば完了通知に添える
            if failure_text:
                send_discord_notification(f"✅ yoyakuLong Sniper: {len(collected_data)}台の更新が完了（一部取得失敗あり）\n{failure_text}")
            else:
                send_discord_notification(f"✅ yoyakuLong Sniper: {len(collected_data)}台の更新が完了。")
        elif failure_text:
            # ★1台も取れなかった場合も黙って終わらないよう警告する
            send_discord_notification(f"⚠️ yoyakuLong Sniper: 取得できた車両が0台でした（シートは更新していません）\n{failure_text}")

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
