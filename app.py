import os
import json
import re
from datetime import datetime, date, timedelta, time
from functools import wraps
from collections import defaultdict

from dotenv import load_dotenv
from flask import Flask, render_template, redirect, url_for, request, flash, jsonify, abort
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import func, text
import requests

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-only-change-this-secret")
db_url = os.getenv("DATABASE_URL", "sqlite:///home_team_pj.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql+psycopg://", 1)
elif db_url.startswith("postgresql://") and "+psycopg" not in db_url:
    db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)
app.config.update(
    SQLALCHEMY_DATABASE_URI=db_url,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "false").lower() == "true",
    REMEMBER_COOKIE_HTTPONLY=True,
    REMEMBER_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "false").lower() == "true",
)
db = SQLAlchemy(app)
csrf = CSRFProtect(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "ログインしてください。"
login_manager.login_message_category = "warning"

ACTIVITIES = ["勉強", "運動", "睡眠", "その他"]
SUBJECTS = ["国語", "社会", "理科", "英語", "数学", "情報", "その他"]
EXERCISES = ["ウォーキング", "ランニング", "スイミング", "サイクリング", "その他"]
WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(50), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    display_name = db.Column(db.String(80), nullable=False)
    # 学校AI APIで利用するメールアドレス。既存DB移行のため nullable=True。
    email = db.Column(db.String(254), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def get_id(self):
        return str(self.id)


class Schedule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    title = db.Column(db.String(120), nullable=False)
    activity = db.Column(db.String(30), nullable=False)
    repeat_type = db.Column(db.String(20), nullable=False, default="1回のみ")
    weekday = db.Column(db.Integer, nullable=True)
    start = db.Column(db.DateTime, nullable=False)
    end = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Result(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    schedule_id = db.Column(db.Integer, db.ForeignKey("schedule.id"), nullable=True)
    result_date = db.Column(db.Date, nullable=False, index=True)
    activity = db.Column(db.String(30), nullable=False)
    detail = db.Column(db.String(160), nullable=False, default="")
    duration_minutes = db.Column(db.Integer, nullable=False)
    start = db.Column(db.DateTime, nullable=False)
    end = db.Column(db.DateTime, nullable=False)
    source = db.Column(db.String(20), nullable=False, default="manual")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class LocationRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    timestamp = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    address = db.Column(db.String(300), nullable=False, default="")
    stay_time = db.Column(db.Integer, nullable=False, default=0)
    tag = db.Column(db.String(80), nullable=False, default="手動記録")


class FacilityRule(db.Model):
    """利用者が登録した施設と、その施設で自動記録する活動。"""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    name = db.Column(db.String(160), nullable=False)
    activity = db.Column(db.String(30), nullable=False)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    radius_m = db.Column(db.Integer, nullable=False, default=100)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class ActiveStay(db.Model):
    """現在滞在中と判定されている施設。利用者ごとに最大1件。"""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, unique=True, index=True)
    facility_id = db.Column(db.Integer, db.ForeignKey("facility_rule.id"), nullable=False)
    started_at = db.Column(db.DateTime, nullable=False)
    last_seen_at = db.Column(db.DateTime, nullable=False)


@login_manager.user_loader
def load_user(user_id):
    try:
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None


@app.context_processor
def inject_globals():
    return {"activities": ACTIVITIES, "weekdays": WEEKDAYS, "today": date.today(),
            "timedelta": timedelta}


def parse_date(value, default=None):
    if not value:
        return default or date.today()
    return date.fromisoformat(value)


def parse_clock(value):
    return datetime.strptime(value, "%H:%M").time()


def to_iso(dt):
    return dt.isoformat(timespec="seconds")


def own_or_404(model, item_id):
    item = db.session.get(model, item_id)
    if item is None or item.user_id != current_user.id:
        abort(404)
    return item


def week_monday(d):
    return d - timedelta(days=d.weekday())


def duration_minutes(start, end):
    if end <= start:
        end += timedelta(days=1)
    return int((end - start).total_seconds() // 60), end


def build_weekly_analysis(user_id, week_start):
    week_end = week_start + timedelta(days=6)
    rows = Result.query.filter(
        Result.user_id == user_id,
        Result.result_date >= week_start,
        Result.result_date <= week_end
    ).all()
    category_totals = defaultdict(int)
    daily_totals = defaultdict(int)
    for row in rows:
        category_totals[row.activity] += row.duration_minutes
        daily_totals[row.result_date.isoformat()] += row.duration_minutes
    categories = [
        {"category": k, "minutes": v, "hours": round(v / 60, 2),
         "share": round(v / max(sum(category_totals.values()), 1) * 100, 1)}
        for k, v in sorted(category_totals.items(), key=lambda x: x[1], reverse=True)
    ]
    daily = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        mins = daily_totals[d.isoformat()]
        daily.append({"date": d.isoformat(), "weekday": WEEKDAYS[i],
                      "total_minutes": mins, "hours": round(mins / 60, 2)})
    previous_start = week_start - timedelta(days=7)
    previous_end = week_start - timedelta(days=1)
    previous_rows = Result.query.filter(
        Result.user_id == user_id,
        Result.result_date >= previous_start,
        Result.result_date <= previous_end
    ).all()
    previous = defaultdict(int)
    for row in previous_rows:
        previous[row.activity] += row.duration_minutes
    changes = {
        key: {"current_minutes": value, "previous_minutes": previous.get(key, 0),
              "change_minutes": value - previous.get(key, 0)}
        for key, value in category_totals.items()
    }
    observations = []
    if categories:
        observations.append(f"最も時間が多かった活動は「{categories[0]['category']}」で、{categories[0]['hours']:.1f}時間でした。")
    if daily:
        busiest = max(daily, key=lambda x: x["total_minutes"])
        observations.append(f"最も記録時間が長かった曜日は{busiest['weekday']}曜日（{busiest['hours']:.1f}時間）でした。")
    total = sum(category_totals.values())
    observations.append(f"今週の記録合計は{total / 60:.1f}時間です。")
    return {
        "available": bool(rows), "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(), "total_minutes": total,
        "categories": categories, "daily": daily,
        "category_changes": changes, "observations": observations,
        "previous_week": {"available": bool(previous_rows),
                          "category_totals": dict(previous)}
    }


def compare_weekly(user_id, week_start, activity=None):
    week_end = week_start + timedelta(days=6)
    totals = db.session.query(
        Result.user_id, func.sum(Result.duration_minutes)
    ).filter(Result.result_date >= week_start, Result.result_date <= week_end)
    if activity:
        totals = totals.filter(Result.activity == activity)
    totals = totals.group_by(Result.user_id).all()
    values = [{"user_id": uid, "minutes": int(total or 0)} for uid, total in totals]
    values.sort(key=lambda x: x["minutes"], reverse=True)
    mine = next((x for x in values if x["user_id"] == user_id), None)
    avg = sum(x["minutes"] for x in values) / len(values) if values else 0
    rank = next((i + 1 for i, x in enumerate(values) if x["user_id"] == user_id), None)
    return {
        "available": mine is not None, "mine_minutes": mine["minutes"] if mine else 0,
        "average_minutes": avg, "rank": rank, "total_users": len(values),
        "difference_minutes": (mine["minutes"] - avg) if mine else 0,
        "values": values
    }


@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        uid = request.form.get("user_id", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        display_name = request.form.get("display_name", "").strip() or uid
        if not uid or len(uid) > 50:
            flash("ユーザーIDを入力してください（50文字以内）。", "danger")
        elif not re.fullmatch(r"[0-9]{6}@c\.kudan\.ed\.jp", email) or len(email) > 254:
            flash("学校のメールアドレスは6桁の数字@c.kudan.ed.jpの形式で入力してください。", "danger")
        elif len(password) < 8:
            flash("パスワードは8文字以上にしてください。", "danger")
        elif User.query.filter_by(user_id=uid).first():
            flash("このユーザーIDはすでに登録されています。", "danger")
        else:
            user = User(user_id=uid, email=email, display_name=display_name[:80],
                        password_hash=generate_password_hash(password))
            db.session.add(user)
            db.session.commit()
            login_user(user)
            flash("アカウントを作成しました。", "success")
            return redirect(url_for("dashboard"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        uid = request.form.get("user_id", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(user_id=uid).first()
        if user and check_password_hash(user.password_hash, password):
            if not re.fullmatch(r"[0-9]{6}@c\.kudan\.ed\.jp", email) or len(email) > 254:
                flash("学校のメールアドレスは6桁の数字@c.kudan.ed.jpの形式で入力してください。", "danger")
            elif user.email and user.email.lower() != email:
                flash("登録済みのメールアドレスと一致しません。", "danger")
            else:
                # 旧バージョンで作成したアカウントは、初回ログイン時にメールを登録する。
                if not user.email:
                    user.email = email
                    db.session.commit()
                login_user(user, remember=True)
                return redirect(url_for("dashboard"))
        else:
            flash("ユーザーIDまたはパスワードが正しくありません。", "danger")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    today = date.today()
    schedules = Schedule.query.filter_by(user_id=current_user.id).all()
    today_items = []
    for s in schedules:
        if ((s.repeat_type == "毎週" and s.weekday == today.weekday()) or
            (s.repeat_type == "1回のみ" and s.start.date() == today)):
            today_items.append(s)
    today_items.sort(key=lambda x: x.start.time())
    recent = Result.query.filter_by(user_id=current_user.id).order_by(Result.result_date.desc(), Result.start.desc()).limit(5).all()
    total = db.session.query(func.sum(Result.duration_minutes)).filter_by(
        user_id=current_user.id, result_date=today).scalar() or 0
    return render_template("dashboard.html", today_items=today_items, recent=recent, today_minutes=total)


@app.route("/schedules", methods=["GET", "POST"])
@login_required
def schedules():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        activity = request.form.get("activity", "勉強")
        repeat = request.form.get("repeat_type", "1回のみ")
        if not title or activity not in ACTIVITIES or repeat not in ("1回のみ", "毎週"):
            flash("入力内容を確認してください。", "danger")
            return redirect(url_for("schedules"))
        try:
            day = parse_date(request.form.get("schedule_date"))
            start_t, end_t = parse_clock(request.form.get("start")), parse_clock(request.form.get("end"))
            weekday = int(request.form.get("weekday", day.weekday())) if repeat == "毎週" else None
            if weekday is not None and weekday not in range(7):
                raise ValueError()
            anchor = day if repeat == "1回のみ" else date.today() + timedelta(days=(weekday - date.today().weekday()) % 7)
            start_dt = datetime.combine(anchor, start_t)
            end_dt = datetime.combine(anchor, end_t)
            if end_dt <= start_dt:
                end_dt += timedelta(days=1)
            db.session.add(Schedule(user_id=current_user.id, title=title, activity=activity,
                                    repeat_type=repeat, weekday=weekday, start=start_dt, end=end_dt))
            db.session.commit()
            flash("予定を追加しました。", "success")
        except (ValueError, TypeError):
            flash("日付・時刻・曜日の入力を確認してください。", "danger")
        return redirect(url_for("schedules"))
    items = Schedule.query.filter_by(user_id=current_user.id).order_by(Schedule.start).all()
    return render_template("schedules.html", items=items)


@app.post("/schedules/<int:sid>/delete")
@login_required
def delete_schedule(sid):
    item = own_or_404(Schedule, sid)
    Result.query.filter_by(user_id=current_user.id, schedule_id=item.id).delete()
    db.session.delete(item)
    db.session.commit()
    flash("予定と関連する実績を削除しました。", "success")
    return redirect(url_for("schedules"))


@app.route("/records", methods=["GET", "POST"])
@login_required
def records():
    if request.method == "POST":
        try:
            activity = request.form.get("activity", "勉強")
            if activity not in ACTIVITIES:
                raise ValueError()
            day = parse_date(request.form.get("result_date"))
            start_dt = datetime.combine(day, parse_clock(request.form.get("start")))
            end_dt = datetime.combine(day, parse_clock(request.form.get("end")))
            mins, end_dt = duration_minutes(start_dt, end_dt)
            if mins <= 0 or mins > 24 * 60:
                raise ValueError()
            sid_raw = request.form.get("schedule_id", "")
            sid = int(sid_raw) if sid_raw else None
            if sid:
                own_or_404(Schedule, sid)
            detail = request.form.get("detail", "").strip()[:160]
            db.session.add(Result(user_id=current_user.id, schedule_id=sid, result_date=day,
                                  activity=activity, detail=detail, duration_minutes=mins,
                                  start=start_dt, end=end_dt, source="manual"))
            db.session.commit()
            flash("実績を保存しました。", "success")
        except (ValueError, TypeError):
            flash("入力内容を確認してください。", "danger")
        return redirect(url_for("records"))
    items = Result.query.filter_by(user_id=current_user.id).order_by(Result.result_date.desc(), Result.start.desc()).limit(100).all()
    my_schedules = Schedule.query.filter_by(user_id=current_user.id).order_by(Schedule.title).all()
    return render_template("records.html", items=items, my_schedules=my_schedules)


@app.post("/records/<int:rid>/delete")
@login_required
def delete_record(rid):
    item = own_or_404(Result, rid)
    db.session.delete(item)
    db.session.commit()
    flash("実績を削除しました。", "success")
    return redirect(url_for("records"))


@app.route("/analysis")
@login_required
def analysis():
    try:
        selected = parse_date(request.args.get("week"), week_monday(date.today()))
    except (TypeError, ValueError):
        selected = week_monday(date.today())
        flash("週の指定が正しくなかったため、今週を表示します。", "warning")
    selected = week_monday(selected)
    data = build_weekly_analysis(current_user.id, selected)
    comparison = compare_weekly(current_user.id, selected)
    public_users = {u.id: u.display_name for u in User.query.all()}
    for item in comparison["values"]:
        item["display_name"] = "あなた" if item["user_id"] == current_user.id else public_users.get(item["user_id"], "ユーザー")
    return render_template("analysis.html", data=data, comparison=comparison,
                           week=selected, previous=selected-timedelta(days=7),
                           next_week=selected+timedelta(days=7), names=public_users)


@app.post("/analysis/ai")
@login_required
def analysis_ai():
    try:
        selected = week_monday(parse_date(request.form.get("week")))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "text": "週の指定が正しくありません。ページを再読み込みしてください。"}), 400

    if not current_user.email:
        return jsonify({"ok": False, "text": "アカウントにメールアドレスが登録されていません。いったんログアウトし、学校のメールアドレスを入力してログインしてください。"}), 400

    api_key = os.getenv("SCHOOL_AI_API_KEY", "").strip()
    api_url = os.getenv("SCHOOL_AI_API_URL", "https://api.stg.ai.kudan.ed.jp/v1/external/chat").strip()
    if not api_key:
        return jsonify({"ok": False, "text": "学校AI APIキーがRenderの環境変数 SCHOOL_AI_API_KEY に設定されていません。"}), 503

    data = build_weekly_analysis(current_user.id, selected)
    analysis_payload = {
        "week": data["week_start"],
        "week_end": data["week_end"],
        "total_minutes": data["total_minutes"],
        "categories": data["categories"],
        "daily": data["daily"],
        "observations": data["observations"],
        "previous_week": data["previous_week"],
        "category_changes": data["category_changes"],
    }
    user_content = (
        "次の週間記録を分析し、本人向けの振り返りを日本語で作成してください。"
        "記録にない事実は推測せず、勉強・運動・睡眠のバランスを中立的に扱い、"
        "睡眠を削ることや過度な運動・勉強を勧めないでください。"
        "良かった点、気づける傾向、無理のない次週の工夫を具体的に示してください。\n"
        + json.dumps(analysis_payload, ensure_ascii=False)
    )
    system_prompt = (
        "あなたは中高生向けの学習・生活記録の振り返りを支援するアシスタントです。"
        "日本語で、親しみやすく具体的に回答してください。記録にない事実を推測せず、"
        "健康状態の診断をせず、睡眠を削ることや過度な勉強・運動を勧めないでください。"
        "改善案は小さく実行可能なものにしてください。"
    )
    if len(user_content) > 4000 or len(system_prompt) > 2000:
        return jsonify({"ok": False, "text": "分析データがAPIの文字数上限を超えました。記録を減らして再度お試しください。"}), 400

    request_body = {
        "email": current_user.email,
        "model": 0,  # 仕様書: 0 = OpenAI GPT-5.2
        "messages": [{"role": "user", "content": user_content}],
        "system_prompt": system_prompt,
    }
    try:
        with requests.post(
            api_url,
            headers={"X-API-Key": api_key, "Content-Type": "application/json",
                     "Accept": "text/event-stream"},
            json=request_body,
            stream=True,
            timeout=(10, 180),
        ) as response:
            if response.status_code != 200:
                detail = ""
                try:
                    detail = response.json().get("detail", "")
                except (ValueError, AttributeError):
                    detail = response.text[:300]
                messages = {
                    400: "入力内容が長すぎるか、利用できない内容が含まれています。記録を確認してください。",
                    401: "学校AI APIの認証に失敗しました。RenderのAPIキーと有効期限を確認してください。",
                    403: "このメールアドレスでは学校AI APIを利用できません。学校指定のメールアドレスか確認してください。",
                    422: "学校AI APIへの送信形式が正しくありません。",
                    429: "学校AI APIの利用回数上限に達しました。しばらく待ってから再度お試しください。",
                    503: "学校AI APIが一時的に利用できません。しばらく待ってから再度お試しください。",
                }
                app.logger.warning("School AI API returned HTTP %s: %s", response.status_code, detail)
                return jsonify({"ok": False, "text": messages.get(response.status_code, "学校AI APIとの通信に失敗しました。")}), 502

            response.encoding = "utf-8"
            answer_parts = []
            stream_error = None
            for line in response.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    app.logger.warning("School AI API sent an invalid SSE data line")
                    continue
                event_type = event.get("type")
                if event_type == "answer":
                    answer_parts.append(str(event.get("content", "")))
                elif event_type == "error":
                    stream_error = str(event.get("content", "回答の生成中に問題が発生しました。"))
                    break

            if stream_error:
                return jsonify({"ok": False, "text": stream_error}), 502
            answer = "".join(answer_parts).strip()
            if not answer:
                return jsonify({"ok": False, "text": "学校AIから回答を受信できませんでした。しばらくして再度お試しください。"}), 502
            return jsonify({"ok": True, "text": answer})
    except requests.Timeout:
        app.logger.warning("School AI API request timed out")
        return jsonify({"ok": False, "text": "学校AIからの応答が時間内に届きませんでした。再度お試しください。"}), 504
    except requests.RequestException:
        app.logger.exception("School AI API connection failed")
        return jsonify({"ok": False, "text": "学校AI APIに接続できませんでした。通信環境や接続先を確認してください。"}), 502


@app.route("/locations")
@login_required
def locations():
    items = LocationRecord.query.filter_by(user_id=current_user.id).order_by(LocationRecord.timestamp.desc()).limit(200).all()
    facilities = FacilityRule.query.filter_by(user_id=current_user.id).order_by(FacilityRule.name).all()
    active_stay = ActiveStay.query.filter_by(user_id=current_user.id).first()
    facility_data = [{"id": f.id, "name": f.name, "activity": f.activity,
                      "latitude": f.latitude, "longitude": f.longitude,
                      "radius_m": f.radius_m} for f in facilities]
    active_data = None
    if active_stay:
        active_data = {"facility_id": active_stay.facility_id,
                       "started_at": active_stay.started_at.isoformat(timespec="seconds")}
    return render_template("locations.html", items=items, facilities=facilities,
                           facility_data=facility_data, active_data=active_data)


@app.get("/api/facilities/search")
@login_required
def search_facilities():
    """Nominatim検索。検索ボタン操作時のみ呼び出す。"""
    query = (request.args.get("q") or "").strip()
    if len(query) < 2 or len(query) > 100:
        return jsonify({"ok": False, "error": "施設名や住所を2～100文字で入力してください。"}), 400
    try:
        response = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "jsonv2", "limit": 6,
                    "countrycodes": "jp", "addressdetails": 1},
            headers={"User-Agent": os.getenv("NOMINATIM_USER_AGENT", "HomeTeamPJ/1.0 (facility search)")},
            timeout=10,
        )
        response.raise_for_status()
        results = []
        for item in response.json():
            try:
                lat, lon = float(item["lat"]), float(item["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            results.append({"name": item.get("name") or item.get("display_name", "施設"),
                            "display_name": item.get("display_name", ""),
                            "latitude": lat, "longitude": lon,
                            "type": item.get("type", "")})
        return jsonify({"ok": True, "results": results})
    except requests.RequestException:
        app.logger.exception("OpenStreetMap Nominatim search failed")
        return jsonify({"ok": False, "error": "施設検索サービスに接続できませんでした。時間をおいて再度お試しください。"}), 502


@app.post("/facilities")
@login_required
def add_facility():
    try:
        name = request.form.get("name", "").strip()[:160]
        activity = request.form.get("activity", "")
        lat, lon = float(request.form.get("latitude", "")), float(request.form.get("longitude", ""))
        radius = int(request.form.get("radius_m", "100"))
        if not name or activity not in ACTIVITIES or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError()
        if not 30 <= radius <= 500:
            raise ValueError()
        db.session.add(FacilityRule(user_id=current_user.id, name=name, activity=activity,
                                    latitude=lat, longitude=lon, radius_m=radius))
        db.session.commit()
        flash(f"「{name}」を自動実績の対象施設に登録しました。", "success")
    except (ValueError, TypeError):
        flash("施設名・活動・位置・判定範囲を確認してください。", "danger")
    return redirect(url_for("locations"))


@app.post("/facilities/<int:facility_id>/delete")
@login_required
def delete_facility(facility_id):
    facility = own_or_404(FacilityRule, facility_id)
    active = ActiveStay.query.filter_by(user_id=current_user.id, facility_id=facility.id).first()
    if active:
        flash("滞在中の施設は削除できません。位置情報の取得を停止してから削除してください。", "warning")
    else:
        db.session.delete(facility)
        db.session.commit()
        flash("登録施設を削除しました。", "success")
    return redirect(url_for("locations"))


def parse_client_local_datetime(value):
    """ブラウザのローカル時刻（タイムゾーン情報なし）を受け取る。"""
    if not value:
        return datetime.now()
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def finish_active_stay(user_id, ended_at):
    active = ActiveStay.query.filter_by(user_id=user_id).first()
    if not active:
        return None
    facility = db.session.get(FacilityRule, active.facility_id)
    started_at = active.started_at
    if facility and ended_at > started_at:
        minutes = int((ended_at - started_at).total_seconds() // 60)
        # 位置の誤検知・端末放置による極端な記録を防ぐ上限
        minutes = min(minutes, 7 * 24 * 60)
        if minutes > 0:
            db.session.add(Result(user_id=user_id, result_date=started_at.date(),
                                  activity=facility.activity, detail=facility.name,
                                  duration_minutes=minutes, start=started_at,
                                  end=ended_at,
                                  source="location"))
    db.session.delete(active)
    db.session.commit()
    return {"facility": facility.name if facility else "登録施設", "minutes": minutes if facility and ended_at > started_at else 0}


@app.post("/api/facilities/visit/start")
@login_required
def start_facility_visit():
    payload = request.get_json(silent=True) or {}
    try:
        facility = own_or_404(FacilityRule, int(payload["facility_id"]))
        now = parse_client_local_datetime(payload.get("timestamp"))
        active = ActiveStay.query.filter_by(user_id=current_user.id).first()
        if active and active.facility_id == facility.id:
            active.last_seen_at = now
            db.session.commit()
            return jsonify({"ok": True, "status": "already_active", "facility": facility.name})
        if active:
            finish_active_stay(current_user.id, now)
        db.session.add(ActiveStay(user_id=current_user.id, facility_id=facility.id,
                                  started_at=now, last_seen_at=now))
        db.session.commit()
        return jsonify({"ok": True, "status": "started", "facility": facility.name})
    except (KeyError, ValueError, TypeError):
        db.session.rollback()
        return jsonify({"ok": False, "error": "施設または時刻の情報が不正です。"}), 400


@app.post("/api/facilities/visit/heartbeat")
@login_required
def heartbeat_facility_visit():
    payload = request.get_json(silent=True) or {}
    try:
        now = parse_client_local_datetime(payload.get("timestamp"))
        active = ActiveStay.query.filter_by(user_id=current_user.id).first()
        if active:
            active.last_seen_at = now
            db.session.commit()
        return jsonify({"ok": True, "active": bool(active)})
    except (ValueError, TypeError):
        db.session.rollback()
        return jsonify({"ok": False}), 400


@app.post("/api/facilities/visit/end")
@login_required
def end_facility_visit():
    payload = request.get_json(silent=True) or {}
    try:
        now = parse_client_local_datetime(payload.get("timestamp"))
        result = finish_active_stay(current_user.id, now)
        return jsonify({"ok": True, "result": result})
    except (ValueError, TypeError):
        db.session.rollback()
        return jsonify({"ok": False, "error": "終了時刻が不正です。"}), 400


@app.post("/api/locations")
@login_required
def save_location():
    payload = request.get_json(silent=True) or {}
    try:
        lat, lon = float(payload["latitude"]), float(payload["longitude"])
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError()
        tag = str(payload.get("tag", "位置記録"))[:80]
        address = str(payload.get("address", ""))[:300]
        timestamp = datetime.utcnow()
        record = LocationRecord(user_id=current_user.id, timestamp=timestamp,
                                latitude=lat, longitude=lon, address=address,
                                stay_time=max(0, min(int(payload.get("stay_time", 0)), 604800)),
                                tag=tag)
        db.session.add(record)
        db.session.commit()
        return jsonify({"ok": True, "id": record.id, "timestamp": to_iso(timestamp)})
    except (KeyError, ValueError, TypeError):
        return jsonify({"ok": False, "error": "座標または記録内容が不正です。"}), 400


@app.post("/locations/<int:rid>/delete")
@login_required
def delete_location(rid):
    item = own_or_404(LocationRecord, rid)
    db.session.delete(item)
    db.session.commit()
    flash("位置情報記録を削除しました。", "success")
    return redirect(url_for("locations"))


@app.post("/api/locations/clear")
@login_required
def clear_locations():
    LocationRecord.query.filter_by(user_id=current_user.id).delete()
    db.session.commit()
    return jsonify({"ok": True})


@app.get("/health")
def health():
    try:
        db.session.execute(text("SELECT 1"))
        return jsonify({"status": "ok"})
    except Exception:
        return jsonify({"status": "database_error"}), 503


with app.app_context():
    db.create_all()
    # 既存DBにも email 列を追加する（新規DBでは create_all が作成済み）。
    user_columns = {column["name"] for column in db.inspect(db.engine).get_columns("user")}
    if "email" not in user_columns:
        db.session.execute(text('ALTER TABLE "user" ADD COLUMN email VARCHAR(254)'))
        db.session.commit()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=True)
