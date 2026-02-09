import os
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException

import re
from datetime import date

import logging
from logging.handlers import RotatingFileHandler

from logging.handlers import TimedRotatingFileHandler

#ローカル用
#from fastapi.responses import FileResponse, RedirectResponse
#from fastapi.staticfiles import StaticFiles
#ローカル用ここまで

load_dotenv()

app = FastAPI()

LOG_DIR = os.environ.get("APP_LOG_DIR", "./logs")
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("app")
logger.setLevel(logging.INFO)

# ログ設定
handler = TimedRotatingFileHandler(
    filename=os.path.join(LOG_DIR, "app.log"),
    when="midnight", # 深夜（00:00）にローテーション
    interval=1, # 1日ごとにローテーション
    backupCount=30, # 30世代保持
    encoding="utf-8"
)

formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.propagate = False

#ローカル用のため、本番リリース時はコメントアウト
# FastAPI に「簡易 Web サーバーの役割」を持たせて
# 静的ファイルは別パスに置く（APIと衝突させない）
#app.mount("/InterviewBookingForm/static", StaticFiles(directory="../frontend"), name="static")

#@app.get("/InterviewBookingForm/")
#def index():
#    return FileResponse("../frontend/index.html")

#@app.get("/")
#def root():
#    return RedirectResponse(url="/InterviewBookingForm/")
#ローカル用ここまで

logger.info("起動時処理")
KINTONE_DOMAIN = os.environ["KINTONE_DOMAIN"]
SLOT_APP_ID = int(os.environ["SLOT_APP_ID"])
BOOK_APP_ID = int(os.environ["BOOK_APP_ID"])
TOKEN_SLOT = os.environ["KINTONE_TOKEN_SLOT"]
TOKEN_BOOK = os.environ["KINTONE_TOKEN_BOOK"]
BOOKING_CUTOFF_MIN = int(os.environ.get("BOOKING_CUTOFF_MIN", "120"))
BOOKING_RANGE_DAYS = int(os.environ.get("BOOKING_RANGE_DAYS", "60"))
CONSENT_SOURCE = (os.environ.get("CONSENT_SOURCE") or "auto").strip().lower()
CONSENT_VERSION = (os.environ.get("CONSENT_VERSION") or "").strip()
CONSENT_TEXT_FILE = (os.environ.get("CONSENT_TEXT_FILE") or "").strip()
CONSENT_TEXT = ""

if CONSENT_TEXT_FILE and os.path.exists(CONSENT_TEXT_FILE):
    with open(CONSENT_TEXT_FILE, "r", encoding="utf-8") as f:
        CONSENT_TEXT = f.read().strip()

KINTONE_BASE = f"https://{KINTONE_DOMAIN}/k/v1"

# 起動時に読み込んだ設定値をログ出力
logger.info(
    "config loaded "
    f"BOOKING_CUTOFF_MIN={BOOKING_CUTOFF_MIN} "
    f"BOOKING_RANGE_DAYS={BOOKING_RANGE_DAYS} "
    f"CONSENT_VERSION={CONSENT_VERSION} "
    f"CONSENT_TEXT_FILE={CONSENT_TEXT_FILE or '(none)'}"
    f"CONSENT_TEXT={CONSENT_TEXT} "
)

# フィールドコード
F_IS_PUBLIC = "is_public"
F_SLOT_START = "slot_start_dt"
F_SLOT_ID = "レコード番号"          # 126側のIDとして使う（レコード番号）
F_BOOK_SLOT_ID = "pref1_slot_id"   # 125側に保存される枠ID

logger.info("起動時処理完了")

def kintone_get_record(app_id: int, token: str, record_id: str) -> dict:
    """
    kintoneから指定したレコードを1件取得する。
    Args:
        app_id (int): アプリID
        token (str): APIトークン
        record_id (str): 取得したいレコードのID
    Returns:
        dict: 取得したレコードの内容（recordオブジェクト）
    Raises:
        HTTPException: kintone APIへのリクエストが失敗した場合（重複エラー、権限不足など）、
                      ステータスコード502としてエラーを発生させる。
    """
    url = f"{KINTONE_BASE}/record.json"
    headers = {"X-Cybozu-API-Token": token}
    params = {"app": app_id, "id": record_id}

    logger.info(f"kintone_get_record url={url}  params={params}")

    r = requests.get(url, headers=headers, params=params, timeout=20)
    if r.status_code != 200:
        logger.error(f"kintone_get_record failed status={r.status_code} body={r.text}")
        raise HTTPException(status_code=502, detail=f"kintone record error: {r.status_code} {r.text}")
    
    return r.json().get("record", {})

def kintone_get_records(app_id: int, token: str, query: str, fields: list[str]) -> list[dict]:
    """
    kintoneから条件に合致するレコードを複数件取得する。
    Args:
        app_id (int): 対象のアプリID。
        token (str): APIトークン（レコード閲覧権限が必要）。
        query (str): 取得条件のクエリ文字列（例: "status = '完了' order by $id asc"）。
        fields (list[str]): 取得したいフィールドコードのリスト。
    Returns:
        list[dict]: 取得したレコードのリスト。1件も該当しない場合は空のリストを返す。
    Raises:
        HTTPException: kintone APIへのリクエストが失敗した場合（重複エラー、権限不足など）、
                      ステータスコード502としてエラーを発生させる。
    """
    url = f"{KINTONE_BASE}/records.json"
    headers = {"X-Cybozu-API-Token": token}
    params = {
        "app": app_id,
        "query": query,
        "fields[]": fields,
        "totalCount": "false",
    }
    logger.info(f"kintone_get_records url={url}  params={params}")
    r = requests.get(url, headers=headers, params=params, timeout=20)
    if r.status_code != 200:
        logger.error(f"kintone_get_records failed status={r.status_code} body={r.text}")
        raise HTTPException(status_code=502, detail=f"kintone error: {r.status_code} {r.text}")
    return r.json().get("records", [])

def kintone_add_record(app_id: int, token: str, record: dict) -> str:
    """
    kintoneに新しいレコードを1件登録する。

    Args:
        app_id (int): 登録先のアプリID。
        token (str): APIトークン（レコード追加権限が必要）。
        record (dict): 登録するレコードの内容。
            例: {"文字列フィールド": {"value": "値"}, "数値フィールド": {"value": 100}}
    Returns:
        str: 新しく作成されたレコードのID。
    Raises:
        HTTPException: kintone APIへのリクエストが失敗した場合（重複エラー、権限不足など）、
                      ステータスコード502としてエラーを発生させる。
    """
    url = f"{KINTONE_BASE}/record.json"
    headers = {
        "X-Cybozu-API-Token": token,
        "Content-Type": "application/json",
    }
    payload = {"app": app_id, "record": record}
    logger.info(f"kintone_add_record url={url}  payload={payload}")
    r = requests.post(url, headers=headers, json=payload, timeout=20)
    if r.status_code != 200:
        logger.error(f"sent_body={r.request.body}")
        logger.error(f"sent_headers={r.request.headers}")
        raise HTTPException(status_code=502, detail=f"kintone add error: {r.status_code} {r.text}")
    return str(r.json().get("id"))

#本番用 @app.get("/slots")
@app.get("/InterviewBookingForm/api/slots")
def slots():
    """
    予約可能な面談スロットの一覧を取得する。

    以下の条件に合致するスロットのみを返す:
    1. ステータスが「公開する」である。
    2. 取得範囲が今日から指定日数（BOOKING_RANGE_DAYS）以内である。
    3. 現在時刻より未来であり、かつ直前予約制限（BOOKING_CUTOFF_MIN）を過ぎていない。
    4. 既に予約（予約アプリ側に登録）されていない。

    Returns:
        list[dict]: 予約可能なスロット情報のリスト。
            例: [{"id": "101", "label": "2026-01-15 14:00"}, ...]
    """
    # JST基準で判定
    jst = timezone(timedelta(hours=9))
    now = datetime.now(tz=jst)

    # 直前予約防止：ユーザーが急に予約できないよう、開始N分前を切った枠は非表示にする
    cutoff = now + timedelta(minutes=BOOKING_CUTOFF_MIN)

    # 予約済み枠ID一覧（125）
    booked_recs = kintone_get_records(
        app_id=BOOK_APP_ID,
        token=TOKEN_BOOK,
        query=f'{F_BOOK_SLOT_ID} != ""',
        fields=[F_BOOK_SLOT_ID],
    )
    booked_ids = set()
    for rec in booked_recs:
        v = rec.get(F_BOOK_SLOT_ID, {}).get("value")
        if v:
            booked_ids.add(str(v))

    # JST基準で今日〜60日(設定値)の枠を取得
    start_date = now.strftime("%Y-%m-%d")
    end_date = (now + timedelta(days=BOOKING_RANGE_DAYS)).strftime("%Y-%m-%d")

    query = (
        f'{F_IS_PUBLIC} in ("公開する") '
        f'and {F_SLOT_START} >= "{start_date}T00:00:00+09:00" '
        f'and {F_SLOT_START} <= "{end_date}T23:59:59+09:00" '
        f'order by {F_SLOT_START} asc'
    )

    slot_recs = kintone_get_records(
        app_id=SLOT_APP_ID,
        token=TOKEN_SLOT,
        query=query,
        fields=[F_SLOT_ID, F_SLOT_START, F_IS_PUBLIC],
    )

    result = []
    for rec in slot_recs:
        slot_id = str(rec.get(F_SLOT_ID, {}).get("value"))
        start_str = rec.get(F_SLOT_START, {}).get("value")

        if not slot_id or not start_str:
            logger.warning(f"Skip slot: Missing data. id={slot_id}")
            continue

        # kintone DATETIME は通常 ISO形式（例: 2026-01-10T10:00:00.000Z）
        # ZならUTCとして解釈しJSTへ変換
        try:
            if start_str.endswith("Z"):
                start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00")).astimezone(jst)
            else:
                start_dt = datetime.fromisoformat(start_str)
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=jst)
        except Exception as e:
            logger.error(f"Skip slot: Invalid date format {start_str}. error={e}")
            continue

        # すでに開始時間を過ぎている枠は除外
        if start_dt <= now:
            logger.debug(f"Skip slot {slot_id}: Past time. {start_dt}")
            continue

        # 開始直前（設定時間内）の枠は、予約が間に合わないため除外
        if start_dt <= cutoff:
            logger.debug(f"Skip slot {slot_id}: Within cutoff. {start_dt}")
            continue

        # すでに予約アプリ側に登録がある（予約済み）枠は除外
        if slot_id in booked_ids:
            logger.debug(f"Skip slot {slot_id}: Already booked.")
            continue

        label = start_dt.strftime("%Y-%m-%d %H:%M")

        result.append({"id": slot_id, "label": label})

    logger.info(f"slots found: {len(result)} available slots (Total raw records: {len(slot_recs)})")
    return result

#メールアドレスの正規表現チェック
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

@app.post("/InterviewBookingForm/api/book")
def book(data: dict):
    """
    面談の予約申し込みを受け付け、kintoneにレコードを登録する。

    処理フロー:
    1. 入力値のバリデーション（名前、メール、スロットID、同意）
    2. 予約枠の存在確認と公開ステータスのチェック
    3. 予約期限（カットオフ）の判定
    4. 二重予約のチェック（同一スロットIDの重複確認）
    5. kintoneへの予約レコード追加

    Args:
        data (dict): リクエストボディ。以下のキーを含むことを期待:
            - name (str): 氏名
            - email (str): メールアドレス
            - slotId (str): 予約枠のレコードID
            - consent (bool): 個人情報同意フラグ
            - altRequest (str): 代替希望日などの自由記述（任意）

    Returns:
        dict: 成功レスポンス。 {"ok": True, "bookingId": "123"}

    Raises:
        HTTPException (400): 入力不備、過去枠、非公開枠などの場合
        HTTPException (409): すでに予約が埋まっている場合
    """
    # 受信データのログ（セキュリティを考慮して一部のみ）
    logger.info(f"Booking request received: email={data.get('email')}, slotId={data.get('slotId')}")

    # --- 1. 入力値の取得とバリデーション ---
    name = (data.get("name") or "").strip()              #お名前
    email = (data.get("email") or "").strip()            #メールアドレス
    slot_id = (data.get("slotId") or "").strip()         #予約可能な日時
    consent = bool(data.get("consent"))                  #上記の個人情報の取り扱いについて同意しますのチェック
    alt_request = (data.get("altRequest") or "").strip() #予約可能な日時以外でご希望がある場合

    if not name:
        logger.warning("Validation failed: name is empty")
        return {"ok": False,"message": "お名前が未入力です"}
    
    if not email or not EMAIL_RE.match(email):
        logger.warning(f"Validation failed: invalid email format ({email})")
        return {"ok": False,"message": "メールアドレス形式が不正です"}
    if not slot_id.isdigit():
        logger.warning(f"Validation failed: slot_id is not digit ({slot_id})")
        return {"ok": False,"message": "予約可能日時が不正です"}
    if not consent:
        logger.warning(f"Validation failed: no consent({consent})")
        return {"ok": False,"message": "個人情報の同意が必要です"}

    # --- 2. 予約枠（126）の状態チェック ---
    rec = kintone_get_record(
        app_id=SLOT_APP_ID,
        token=TOKEN_SLOT,
        record_id=slot_id
    )

    # 公開設定の確認
    pub_vals = rec.get(F_IS_PUBLIC, {}).get("value") or []
    if "公開する" not in pub_vals:
        logger.warning(f"not public: pub_vals({pub_vals})")
        return {"ok": False,"message": "指定枠は公開されていません"}

    # --- 3. 時間制限のチェック ---
    start_str = rec.get(F_SLOT_START, {}).get("value")
    if not start_str:
        logger.warning(f"not get F_SLOT_START: start_str({start_str})")
        return {"ok": False,"message": "開始日時が取得できません"}

    # タイムゾーンの考慮（kintone UTC 対策）
    jst = timezone(timedelta(hours=9))
    now = datetime.now(tz=jst)
    cutoff = now + timedelta(minutes=BOOKING_CUTOFF_MIN)

    if start_str.endswith("Z"):
        start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00")).astimezone(jst)
    else:
        start_dt = datetime.fromisoformat(start_str)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=jst)

    if start_dt <= now:
        logger.warning(f"Book failed: slot {slot_id} is in the past. {start_dt}")
        return {"ok": False,"message": "過去の枠は予約できません"}
    if start_dt <= cutoff:
        logger.warning(f"Book failed: slot {slot_id} within cutoff. {start_dt}")
        return {"ok": False,"message": f"開始{BOOKING_CUTOFF_MIN}分以内の枠は予約できません"}
    # --- 4. 重複予約の最終チェック（排他制御の代わり） ---
    booked = kintone_get_records(
        app_id=BOOK_APP_ID,
        token=TOKEN_BOOK,
        query=f'{F_BOOK_SLOT_ID} = "{slot_id}"',
        fields=[F_BOOK_SLOT_ID],
    )
    if booked:
        logger.warning(f"Book failed: slot {slot_id} is already taken.")
        return {"ok": False,"message": "すでに予約済みです"}

    # --- 5. 登録用データの作成とkintoneへの送信 ---
    today_jst = date.today().isoformat()

    record = {
        "cand_name": {"value": name},
        "cand_email": {"value": email},
        "pref1_slot_id": {"value": slot_id},
        "pref1_start_dt": {"value": start_str},
        "consent_given": {"value": ["同意する"]}, # kintone では CHECK_BOX は「選択肢ラベルの配列」で渡す必要があるため、この形で送ること。
        "consent_date": {"value": today_jst},
        "alt_request": {"value": alt_request},
    }

    # 環境設定に応じて同意バージョン、同意文を追加
    if CONSENT_VERSION:
        record["consent_version"] = {"value": CONSENT_VERSION}

    if CONSENT_TEXT:
        record["consent_text"] = {"value": CONSENT_TEXT}

    # 登録データの出力
    logger.info(f"Creating new booking record for slot_id={slot_id}")

    booking_id = kintone_add_record(
        app_id=BOOK_APP_ID,
        token=TOKEN_BOOK,
        record=record
    )

    logger.info(f"Booking successfully created: booking_id={booking_id}")
    return {"ok": True, "bookingId": booking_id}

