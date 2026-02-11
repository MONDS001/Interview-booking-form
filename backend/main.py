"""
InterviewBookingForm API

予約枠取得、予約登録機能を提供する。
予約登録成功時に確認メールを送信する（FastAPI-Mail）。

"""


import os
import re
import logging
import gzip
import shutil
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, BackgroundTasks, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi_mail import FastMail, MessageSchema, ConnectionConfig, MessageType
from logging.handlers import TimedRotatingFileHandler
from pydantic import EmailStr, BaseModel

# --- 1. 設定管理 (AppConfig) ---
class AppConfig:
    """
    アプリケーション全体の設定を管理するクラス。
    """

    def __init__(self):
        load_dotenv()
        # 基礎パス
        self.BASE_DIR: Path = Path(__file__).resolve().parent
        self.JST = timezone(timedelta(hours=9))
    
        # kintone 設定
        self.KINTONE_DOMAIN: str = os.environ["KINTONE_DOMAIN"]
        self.KINTONE_BASE = f"https://{self.KINTONE_DOMAIN}/k/v1"
        self.SLOT_APP_ID: int = int(os.environ["SLOT_APP_ID"])
        self.BOOK_APP_ID: int = int(os.environ["BOOK_APP_ID"])
        self.TOKEN_SLOT: str = os.environ["KINTONE_TOKEN_SLOT"]
        self.TOKEN_BOOK: str = os.environ["KINTONE_TOKEN_BOOK"]

        # 予約ルール
        self.BOOKING_CUTOFF_MIN: int = int(os.environ.get("BOOKING_CUTOFF_MIN", 120))
        self.BOOKING_RANGE_DAYS: int = int(os.environ.get("BOOKING_RANGE_DAYS", 60))

        # フィールドコード管理
        self.F_IS_PUBLIC = "is_public"         
        self.F_SLOT_START = "slot_start_dt"
        self.F_SLOT_ID = "レコード番号"          # 126側のIDとして使う（レコード番号）
        self.F_BOOK_SLOT_ID = "pref1_slot_id"   # 125側に保存される枠ID

        # 証明書設定
        ca_path = self.BASE_DIR / "certs" / "fgcacert202007.cer"
        self.REQUESTS_VERIFY = str(ca_path) if ca_path.exists() else True

        # メールサーバー設定
        self.MAIL_FROM: str = os.environ["MAIL_FROM"]
        self.MAIL_SERVER: str = os.environ["MAIL_SERVER"]
        self.MAIL_PORT: int = int(os.environ.get("MAIL_PORT", 587))
        self.MAIL_FROM_NAME: str = os.environ.get("MAIL_FROM_NAME", "")
        self.MAIL_STARTTLS = self._env_bool("MAIL_STARTTLS", False)
        self.MAIL_SSL_TLS = self._env_bool("MAIL_SSL_TLS", False)
        self.USE_CREDENTIALS = self._env_bool("USE_CREDENTIALS", False)
        self.VALIDATE_CERTS = self._env_bool("VALIDATE_CERTS", True)
        self.MAIL_USERNAME: str = os.environ.get("MAIL_USERNAME", "") 
        self.MAIL_PASSWORD: str = os.environ.get("MAIL_PASSWORD", "")

        # パス関連
        self.LOG_DIR = os.environ.get("APP_LOG_DIR", "./logs")
        self.CONSENT_TEXT_FILE = self._resolve_path("CONSENT_TEXT_FILE")
        self.CONSENT_TEXT = self._read_text_file(self.CONSENT_TEXT_FILE)
        self.MAIL_SUBJECT_TEMPLATE_FILE = self._resolve_path("MAIL_SUBJECT_TEMPLATE_FILE")
        self.MAIL_BODY_TEMPLATE_FILE = self._resolve_path("MAIL_BODY_TEMPLATE_FILE")

        self.CONSENT_VERSION = os.environ.get("CONSENT_VERSION", "")

    # env の真偽値を安定して扱うためのヘルパー
    def _env_bool(self, key: str, default: bool = False) -> bool:
        """
        環境変数の真偽値を文字列として解釈し、bool に変換する。
        "false" などの文字列を bool() に渡すと True になるため、本関数で安定して判定する。
        Args:
            key (str): 参照する環境変数名。
            default (bool): 環境変数が未設定の場合に返す既定値。
        Returns:
            bool: 判定結果。
        """
        v = os.environ.get(key)
        if v is None:
            return default
        s = str(v).strip().lower()
        return s in ("1", "true", "t", "yes", "y", "on")
    
    def _resolve_path(self, env_key: str) -> Path:
        val = os.environ.get(env_key)
        if not val: raise RuntimeError(f"{env_key} is not set")
        return (self.BASE_DIR / val).resolve()
    
    def _read_text_file(self,path: Path) -> str:
        """
        UTF-8 テキストファイルを読み込む。
        Args:
            path (Path): 読み込むファイル。
        Returns:
            str: ファイル内容。
        """
        return path.read_text(encoding="utf-8")

    def _render_template(self,path: Path, vars_dict: dict) -> str:
        """
        テンプレートを読み込み、変数をvars_dictの内容をformat で変数展開する。
        Args:
            path (Path): テンプレートファイル。
            vars_dict (dict): 置換用変数。
        Returns:
            str: 展開後文字列。
        """
        text = self._read_text_file(path)
        return text.strip().format(**vars_dict)

# 設定のインスタンス化 (ここで環境変数が足りないと KeyError が発生し、起動に失敗する)
try:
    config = AppConfig()
except KeyError as e:
    import sys
    print(f"致命的なエラー: 環境変数 {e} が設定されていません。")
    sys.exit(1)

# --- 2. ログ設定 ---
def setup_logger():
    log_dir = os.environ.get("APP_LOG_DIR", "./logs")
    os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger("app")
    logger.setLevel(logging.INFO)

    # ハンドラの二重登録防止
    if any(isinstance(h, TimedRotatingFileHandler) for h in logger.handlers):
        logger.propagate = False
        return logger

    # --- 圧縮用の関数定義 ---
    def namer(name):
        return name + ".gz"

    def rotator(source, dest):
        with open(source, 'rb') as f_in:
            with gzip.open(dest, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
        os.remove(source)
    # ----------------------

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    # 共通のハンドラ作成用関数
    def create_handler(filename, level):
        h = TimedRotatingFileHandler(
            filename=os.path.join(log_dir, filename),
            when="midnight",  # 深夜（00:00）にローテーション
            interval=1,       # 1日ごとにローテーション
            backupCount=30,   # 30世代保持
            encoding="utf-8"
        )
        h.setFormatter(formatter)
        h.setLevel(level)
        # 圧縮設定を適用
        h.namer = namer
        h.rotator = rotator
        return h

    # INFO用（app.log）
    logger.addHandler(create_handler("app.log", logging.INFO))
    
    # WARNING以上用（warning.log）
    logger.addHandler(create_handler("warning.log", logging.WARNING))

    logger.propagate = False
    return logger

logger = setup_logger()

# FastAPI-Mail 設定
mail_conf = ConnectionConfig(
    MAIL_FROM=config.MAIL_FROM,              # 差出人アドレス
    MAIL_PORT=config.MAIL_PORT,              # 送信ポート(TLS: 587, SSL: 465 など)
    MAIL_SERVER=config.MAIL_SERVER,          # メールサーバーIP(またはホスト名)
    MAIL_FROM_NAME=config.MAIL_FROM_NAME,    # 差出人表示名
    MAIL_STARTTLS=config.MAIL_STARTTLS,      # TARTTLSを利用して接続を暗号化するか (True/False)
    MAIL_SSL_TLS=config.MAIL_SSL_TLS,        # SSL/TLSを利用して接続を暗号化するか (True/False)
    USE_CREDENTIALS=config.USE_CREDENTIALS,  # ID・パスワードによる認証を行うか (True/False)
    VALIDATE_CERTS=config.VALIDATE_CERTS,    # サーバー証明書の検証を厳密に行うか (True/False)
    # fastapi-mail 1.5.2 は必須扱いのため常に渡す（認証しないので空で良い）
    MAIL_USERNAME=config.MAIL_USERNAME,
    MAIL_PASSWORD=config.MAIL_PASSWORD
)

logger.info("起動時処理")
# 起動時に読み込んだ設定値をログ出力
logger.info(
    "config loaded \n"
    f"BOOKING_CUTOFF_MIN={config.BOOKING_CUTOFF_MIN} \n"
    f"BOOKING_RANGE_DAYS={config.BOOKING_RANGE_DAYS} \n"
    f"CONSENT_VERSION={config.CONSENT_VERSION} \n"
    f"CONSENT_TEXT_FILE={config.CONSENT_TEXT_FILE or '(none)'} \n"
    f"CONSENT_TEXT={config.CONSENT_TEXT} \n"
    f"MAIL_SUBJECT_TEMPLATE_FILE ={config.MAIL_SUBJECT_TEMPLATE_FILE} \n"
    f"MAIL_BODY_TEMPLATE_FILE ={config.MAIL_BODY_TEMPLATE_FILE} \n"
    f"MAIL_FROM={config.MAIL_FROM} \n"
    f"MAIL_PORT={config.MAIL_PORT} \n"
    f"MAIL_SERVER={config.MAIL_SERVER} \n"
    f"MAIL_FROM_NAME={config.MAIL_FROM_NAME} \n"
    f"MAIL_STARTTLS={config.MAIL_STARTTLS} \n"
    f"MAIL_SSL_TLS={config.MAIL_SSL_TLS} \n"
    f"USE_CREDENTIALS={config.USE_CREDENTIALS} \n"
    f"VALIDATE_CERTS={config.VALIDATE_CERTS} \n"
    "以下MAIL_USERNAME、MAIL_PASSWORDはAPI上設定が必要なため、設定する。USE_CREDENTIALS=falseの場合は使用しない \n"
    f"MAIL_USERNAME={config.MAIL_USERNAME} \n"
    f"MAIL_PASSWORD={config.MAIL_PASSWORD} \n"
)

logger.info("起動時処理完了")

# --- 3. ユーティリティ・バリデーション関数 ---
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
APP_TZ = config.JST  # アプリ内の判定タイムゾーン（JST固定）

def mask_email(addr: str) -> str:
    if not addr or "@" not in addr:
        return "(empty)"
    local, domain = addr.split("@", 1)
    if len(local) <= 2:
        local_masked = local[0:1] + "*"
    else:
        local_masked = local[:2] + "***"
    return f"{local_masked}@{domain}"

def mask_phone(p: str) -> str:
    if not p:
        return "(empty)"
    digits = re.sub(r"\D", "", p)
    if len(digits) <= 4:
        return "*" * len(digits)
    return ("*" * (len(digits) - 4)) + digits[-4:]

def parse_kintone_datetime(dt_str: str, default_tz=APP_TZ) -> datetime:
    """
    kintone DATETIME（例: 2026-01-10T10:00:00.000Z / +09:00 / tzなし）を datetime に変換する。
    ・末尾Z: UTCとして解釈して default_tz へ変換
    ・オフセットあり: そのまま解釈して default_tz へ変換
    ・tzなし: default_tz とみなす
    """
    if not dt_str:
        raise ValueError("empty datetime string")

    s = dt_str
    if s.endswith("Z"):
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.astimezone(default_tz)

    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz)
    return dt.astimezone(default_tz)

def format_slot_label(slot_iso: str) -> str:
    """
    ISO 形式（Z or +HH:MM）の日時文字列を
    'YYYY-MM-DD HH:MM:SS'（JST）に変換する。
    Args:
        slot_iso (str): 例 '2026-01-27T09:00:00Z'
    Returns:
        str: 例 '2026-01-27 18:00:00'
    """
    if not slot_iso:
        return ""
    dt_jst = parse_kintone_datetime(slot_iso, default_tz=APP_TZ)
    return dt_jst.strftime("%Y-%m-%d %H:%M:%S")

# --- 4.本処理 ---
app = FastAPI()

#ローカル用のため、本番リリース時はコメントアウト
# FastAPI に「簡易 Web サーバーの役割」を持たせて
# 静的ファイルは別パスに置く（APIと衝突させない）
app.mount("/InterviewBookingForm/static", StaticFiles(directory="../frontend"), name="static")

@app.get("/InterviewBookingForm/")
def index():
    return FileResponse("../frontend/index.html")

@app.get("/")
def root():
    return RedirectResponse(url="/InterviewBookingForm/")
#ローカル用ここまで

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
    url = f"{config.KINTONE_BASE}/record.json"
    headers = {"X-Cybozu-API-Token": token}
    params = {"app": app_id, "id": record_id}

    logger.info(f"kintone_get_record url={url}  params={params}")

    try:
        r = requests.get(url, headers=headers, params=params, timeout=20,verify=config.REQUESTS_VERIFY)
    except requests.RequestException as e:
        logger.exception(f"kintone_get_record request failed url={url} app_id={app_id} record_id={record_id} error={e}")
        raise HTTPException(status_code=502, detail="kintone record request failed")
    
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
    url = f"{config.KINTONE_BASE}/records.json"
    headers = {"X-Cybozu-API-Token": token}
    params = {
        "app": app_id,
        "query": query,
        "fields[]": fields,
        "totalCount": "false",
    }
    logger.info(f"kintone_get_records url={url}  params={params}")
    try:
        r = requests.get(url, headers=headers, params=params, timeout=20,verify=config.REQUESTS_VERIFY)
    except requests.RequestException as e:
        logger.exception(f"kintone_get_records request failed url={url} app_id={app_id} error={e}")
        raise HTTPException(status_code=502, detail="kintone records request failed")

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
    url = f"{config.KINTONE_BASE}/record.json"
    headers = {
        "X-Cybozu-API-Token": token,
        "Content-Type": "application/json",
    }
    payload = {"app": app_id, "record": record}

    # payloadには個人情報が含まれるため値はログに出さない
    logger.info(f"kintone_add_record url={url} app_id={app_id} record_keys={list(record.keys())}")
    #logger.debug(f"kintone_add_record url={url}  payload={payload}")
    
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=20,verify=config.REQUESTS_VERIFY)
    except requests.RequestException as e:
        logger.exception(f"kintone_add_record request failed url={url} app_id={app_id} error={e}")
        raise HTTPException(status_code=502, detail="kintone add record request failed")

    if r.status_code != 200:
        logger.error(f"sent_body={r.request.body}")
        logger.error(f"sent_headers={r.request.headers}")
        raise HTTPException(status_code=502, detail=f"kintone add error: {r.status_code} {r.text}")
    
    return str(r.json().get("id"))

@app.get("/InterviewBookingForm/api/consent", response_class=Response)
def get_consent_text():
    """
    個人情報の取り扱い（同意文面）をテキストとして返す。
    Returns:
    Response: text/plain; charset=utf-8 の本文に同意文面を返す。
    """
    text = config.CONSENT_TEXT
    return Response(content=text, media_type="text/plain; charset=utf-8")

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
    now = datetime.now(tz=APP_TZ)

    # 直前予約防止：ユーザーが急に予約できないよう、開始N分前を切った枠は非表示にする
    cutoff = now + timedelta(minutes=config.BOOKING_CUTOFF_MIN)

    # 予約済み枠ID一覧（125）
    booked_ids: set[str] = set()
    try:
        booked_recs = kintone_get_records(
            app_id=config.BOOK_APP_ID,
            token=config.TOKEN_BOOK,
            query=f'{config.F_BOOK_SLOT_ID} != ""',
            fields=[config.F_BOOK_SLOT_ID],
        )
        booked_ids = set()
        for rec in booked_recs:
            v = rec.get(config.F_BOOK_SLOT_ID, {}).get("value")
            if v:
                booked_ids.add(str(v))
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"failed to load booked slot ids error={e}")
        raise HTTPException(status_code=502, detail="予約情報の取得に失敗しました")

    # JST基準で今日〜60日(設定値)の枠を取得
    start_date = now.strftime("%Y-%m-%d")
    end_date = (now + timedelta(days=config.BOOKING_RANGE_DAYS)).strftime("%Y-%m-%d")

    query = (
        f'{config.F_IS_PUBLIC} in ("公開する") '
        f'and {config.F_SLOT_START} >= "{start_date}T00:00:00+09:00" '
        f'and {config.F_SLOT_START} <= "{end_date}T23:59:59+09:00" '
        f'order by {config.F_SLOT_START} asc'
    )

    slot_recs = kintone_get_records(
        app_id=config.SLOT_APP_ID,
        token=config.TOKEN_SLOT,
        query=query,
        fields=[config.F_SLOT_ID, config.F_SLOT_START, config.F_IS_PUBLIC],
    )


    counters = {
        "raw": len(slot_recs),
        "missing": 0,
        "invalid_dt": 0,
        "past": 0,
        "within_cutoff": 0,
        "already_booked": 0,
        "available": 0,
    }

    result = []
    for rec in slot_recs:
        slot_id = str(rec.get(config.F_SLOT_ID, {}).get("value") or "").strip()
        start_str = rec.get(config.F_SLOT_START, {}).get("value")

        if not slot_id or not start_str:
            counters["missing"] += 1
            continue

        try:
            start_dt = parse_kintone_datetime(start_str, default_tz=APP_TZ)
        except Exception:
            counters["invalid_dt"] += 1
            continue

		# すでに開始時間を過ぎている枠は除外
        if start_dt <= now:
            counters["past"] += 1
            continue

		# 開始直前（設定時間内）の枠は、予約が間に合わないため除外
        if start_dt <= cutoff:
            counters["within_cutoff"] += 1
            continue
		
		# すでに予約アプリ側に登録がある（予約済み）枠は除外
        if slot_id in booked_ids:
            counters["already_booked"] += 1
            continue

        counters["available"] += 1
        result.append({"id": slot_id, "label": start_dt.strftime("%Y-%m-%d %H:%M")})

    logger.info(
        "slots summary "
        f"raw={counters['raw']} "
        f"available={counters['available']} "
        f"missing={counters['missing']} "
        f"invalid_dt={counters['invalid_dt']} "
        f"past={counters['past']} "
        f"within_cutoff={counters['within_cutoff']} "
        f"already_booked={counters['already_booked']}"
    )
    return result

class EmailSchema(BaseModel):
    """
    メール送信 API の入力スキーマ。

    Attributes:
        recipients (list[EmailStr]): 送信先メールアドレス一覧。
        subject (str | None): 件名（未指定の場合は既定値を使用）。
        body (str | None): 本文（未指定の場合は既定値を使用）。
        subtype (MessageType): 本文形式（plain または html）。
    """
    # 送信先（複数対応）
    recipients: List[EmailStr]

    # 任意項目（未指定の場合は既定値を使用）
    subject: Optional[str] = None
    body: Optional[str] = None
    subtype: MessageType = MessageType.plain


async def send_booking_mail(to_addr: str, name: str, slot_label: str, alt_request: str,note: str,booking_id: str) -> None:
    """
    予約登録完了メールを送信する。
    本関数は BackgroundTasks から呼び出す想定であり、送信失敗時に予約処理自体を失敗させない運用を前提とする。
    メールの件名、本文は設定ファイルに指定されたパスの内容を使用する。

    Args:
        to_addr (str): 宛先メールアドレス。
        name (str): 宛名（氏名）。
        slot_label (str): 予約日時表示用ラベル。
        alt_request(str):予約可能な日時以外でご希望がある場合の入力値。空白でなければ表示する。
        note(str):備考の入力値。空白でなければ表示する。
        booking_id (str): 予約番号。
    Returns:
        None
    """
    try:
        display_slot = (
            "その他希望"
            if slot_label == "その他希望"
            else format_slot_label(slot_label)
        )

        display_alt_request = ""
        if alt_request:
            display_alt_request = f"\n{alt_request}"

        display_note = ""
        if note:
            display_note = f"\n備考：{note}"

        vars_dict = {
            "name": name,
            "slot_label": display_slot,
            "booking_id": booking_id,
            "request_label": display_alt_request,
            "note_label": display_note,
        }

        try:
            subject = config._render_template(config.MAIL_SUBJECT_TEMPLATE_FILE, vars_dict)
        except Exception as e:
            logger.exception(f"Mail subject template render failed error={e}")
            subject = f"面談予約を受け付けました（{name} 様）"

        try:
            body = config._render_template(config.MAIL_BODY_TEMPLATE_FILE, vars_dict)
        except Exception as e:
            logger.exception(f"Mail body template render failed error={e}")
            body = (
                f"{name} 様\n\n"
                f"面談予約を受け付けました。\n"
                f"予約内容：{slot_label}\n"
                f"{display_alt_request}"
                f"{display_note}"
                f"受付番号：{booking_id}\n"
            )

        message = MessageSchema(
            subject=subject,
            recipients=[to_addr],
            body=body,
            subtype=MessageType.plain,
        )

        logger.info(f"mail send start: to_addr={to_addr},subject={subject},body={body},booking_id={booking_id},slot_label={slot_label} ")
        fm = FastMail(mail_conf)
        await fm.send_message(message)
        logger.info(f"mail send success: to_addr={to_addr}, booking_id={booking_id}")
    except Exception as e:
        logger.exception(f"mail send failed: to_addr={to_addr}, booking_id={booking_id}, error={e}")

@app.post("/InterviewBookingForm/api/book")
def book(data: dict, background_tasks: BackgroundTasks):
    """
    面談の予約申し込みを受け付け、kintoneにレコードを登録する。

    処理フロー:
    1. 入力値のバリデーション（名前、メール、スロットID、同意）
    2. 予約枠の存在確認と公開ステータスのチェック
    3. 予約期限（カットオフ）の判定
    4. 二重予約のチェック（同一スロットIDの重複確認）
    5. kintoneへの予約レコード追加
    6. 予約完了メール送信（BackgroundTasks）

    Args:
        data (dict): リクエストボディ。以下のキーを含むことを期待:
            - name (str): 氏名
            - email (str): メールアドレス
            - slotId (str): 予約枠のレコードID
            - consent (bool): 個人情報同意フラグ
            - altRequest (str): 代替希望日などの自由記述（任意）
    background_tasks (BackgroundTasks): 予約完了メール送信を委譲するためのタスクコンテナ。

    Returns:
        dict: 成功レスポンス。 {"ok": True, "bookingId": "123"}

    Raises:
        HTTPException: 予約枠不正、期限切れ、二重予約、kintone API 失敗などのエラー時。
    """
    # 受信データのログ（セキュリティを考慮して一部のみ）
    logger.info(f"Booking request received: email={mask_email(data.get('email') or '')}, slotId={data.get('slotId')}")

    # --- 1. 入力値の取得とバリデーション ---
    name = (data.get("name") or "").strip()              #お名前
    email = (data.get("email") or "").strip()            #メールアドレス
    phone = (data.get("phone") or "").strip()            #電話番号
    slot_id = (data.get("slotId") or "").strip()         #予約可能な日時
    alt_request = (data.get("altRequest") or "").strip() #予約可能な日時以外でご希望がある場合
    consent = bool(data.get("consent"))                  #上記の個人情報の取り扱いについて同意しますのチェック
    note = (data.get("note") or "").strip()              #備考

    if not name:
        logger.warning("Validation failed: name is empty")
        return {"ok": False,"message": "お名前が未入力です"}
    
    if not email or not EMAIL_RE.match(email):
        logger.warning(f"Validation failed: invalid email format ({email})")
        return {"ok": False,"message": "メールアドレス形式が不正です"}

    if not slot_id:
        logger.warning("Validation failed: slotId is empty")
        return {"ok": False, "message": "予約枠が未選択です"}

    if not consent:
        logger.warning(f"Validation failed: no consent({consent})")
        return {"ok": False,"message": "個人情報の同意が必要です"}

    is_other = (slot_id == "OTHER") #その他が選択されているかのフラグ

    if (not is_other) and (not bool(re.fullmatch(r"\d+", slot_id))):
        logger.warning(f"Validation failed: invalid slotId format (slotId={slot_id})")
        return {"ok": False, "message": "予約枠の指定が不正です"}

    if is_other and (not alt_request):
        logger.warning("Validation failed: altRequest is required when slotId=OTHER")
        return {"ok": False, "message": "候補以外の日時を希望する場合は、希望内容を入力してください",
    }

    start_str = "" # 開始日時。OTHER の場合に備えて初期化

    if not is_other:
        # 枠の存在チェック／公開チェック／開始日時取得（start_str 作成）／期限チェック

        # 公開設定の確認
        rec = kintone_get_record(
            app_id=config.SLOT_APP_ID,
            token=config.TOKEN_SLOT,
            record_id=slot_id
        )

        pub_vals = rec.get(config.F_IS_PUBLIC, {}).get("value") or []
        if "公開する" not in pub_vals:
            logger.warning(f"not public: pub_vals({pub_vals})")
            return {"ok": False,"message": "指定枠は公開されていません"}

        # --- 3. 時間制限のチェック ---
        start_str = rec.get(config.F_SLOT_START, {}).get("value")
        if not start_str:
            logger.warning(f"not get F_SLOT_START: start_str({start_str})")
            return {"ok": False,"message": "開始日時が取得できません"}

        # タイムゾーンの考慮（kintone UTC/Z・tzなし対策）
        now = datetime.now(tz=APP_TZ)
        cutoff = now + timedelta(minutes=config.BOOKING_CUTOFF_MIN)

        try:
            start_dt = parse_kintone_datetime(start_str, default_tz=APP_TZ)
        except Exception as e:
            logger.error(f"Book failed: invalid start datetime. slot_id={slot_id} start_str={start_str} error={e}")
            return {"ok": False, "message": "開始日時の形式が不正です"}

        if start_dt <= now:
            logger.warning(f"Book failed: slot {slot_id} is in the past. start_dt={start_dt.isoformat()}")
            return {"ok": False, "message": "過去の枠は予約できません"}
        if start_dt <= cutoff:
            logger.warning(f"Book failed: slot {slot_id} within cutoff. start_dt={start_dt.isoformat()}")
            return {"ok": False, "message": f"開始{config.BOOKING_CUTOFF_MIN}分以内の枠は予約できません"}

        # 予約済みチェック（BOOK_APP_ID 側を参照）
        booked = kintone_get_records(
            app_id=config.BOOK_APP_ID,
            token=config.TOKEN_BOOK,
            query=f'{config.F_BOOK_SLOT_ID} = "{slot_id}"',
            fields=[config.F_BOOK_SLOT_ID],
        )
        if booked:
            logger.warning(f"duplicate booking: slot {slot_id} is duplicate. {start_dt}")
            return {"ok": False, "message": "すでに予約済みです"}

    # 同意日時（UTCのISO、末尾Z）
    consent_dt = datetime.now(APP_TZ).replace(microsecond=0).isoformat()

    # --- 5. 登録用データの作成とkintoneへの送信 ---

    record = {
        "cand_name": {"value": name},
        "cand_email": {"value": email},
        "consent_given": {"value": ["同意する"]}, # kintone では CHECK_BOX は「選択肢ラベルの配列」で渡す必要があるため、この形で送ること。
        "consent_date": {"value": consent_dt},
    }
    if phone:
        record["cand_phone"] = {"value": phone}
    if note:
        record["cand_note"] = {"value": note}
    if alt_request:
        record["alt_request"] = {"value": alt_request}

    BOOKING_TYPE_SLOT = "枠から選択"
    BOOKING_TYPE_OTHER = "その他（候補以外）"

    if not is_other:
        record["pref1_slot_id"] = {"value": slot_id}
        record["pref1_start_dt"] = {"value": start_str}
        record["booking_type"] = {"value": BOOKING_TYPE_SLOT}
    else:
        record["booking_type"] = {"value": BOOKING_TYPE_OTHER}


    # 環境設定に応じて同意バージョン、同意文を追加
    if config.CONSENT_VERSION:
        record["consent_version"] = {"value": config.CONSENT_VERSION}

    if config.CONSENT_TEXT:
        record["consent_text"] = {"value": config.CONSENT_TEXT}

    # 登録データの出力
    logger.info(f"Creating new booking record for slot_id={slot_id}")

    booking_id = kintone_add_record(
        app_id=config.BOOK_APP_ID,
        token=config.TOKEN_BOOK,
        record=record
    )

    logger.info(f"Booking successfully created: booking_id={booking_id}")

    # 予約完了メール送信（非同期）
    # 送信失敗しても予約処理を失敗させないため BackgroundTasks に委譲する
    try:
        slot_label = start_str if start_str else "その他希望"
        background_tasks.add_task(send_booking_mail, email, name, slot_label, alt_request, note ,booking_id)
    except Exception as e:
        logger.exception(f"Failed to enqueue booking mail task error={e}")

    return {"ok": True, "bookingId": booking_id}

@app.post("/InterviewBookingForm/api/send-email")
async def send_mail(payload: EmailSchema):
    """
    任意宛先へのメール送信（テスト用途）recipients:宛先メールアドレス subject:件名 body:本文 subtype (MessageType): 本文形式（plain または html）。
    Args:
        payload (EmailSchema): 入力スキーマ。
    Returns:
        dict: {"message": "email has been sent"}
    """
    subject = payload.subject or "FastAPI からのテストメール"
    body = payload.body or "これは FastAPI-Mail によるテスト送信です。"

    message = MessageSchema(
        subject=subject,
        recipients=payload.recipients,
        body=body,
        subtype=payload.subtype,
    )

    fm = FastMail(mail_conf)
    await fm.send_message(message)

    return {"message": "email has been sent"}