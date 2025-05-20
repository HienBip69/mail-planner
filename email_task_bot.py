import re
import time
import threading
import imaplib
import email
from email.header import decode_header
from datetime import datetime, timedelta
from flask import Flask, request, render_template, redirect, url_for, session, Response, send_from_directory, jsonify
import requests
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
import pickle
from queue import Queue
from bs4 import BeautifulSoup
import firebase_admin
from firebase_admin import credentials, messaging
import os
import logging

# Cấu hình logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = os.environ.get('SECRET_KEY', 'mysecretkey123')
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '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')
SCOPES = ['https://www.googleapis.com/auth/calendar']

email_credentials = {"email": "", "password": ""}
planned_tasks = []
message_queue = Queue()
next_check_time = None
ignored_senders = set()
creds = None
fcm_tokens = set()
user_fcm_token = None
firebase_initialized = False

# ---------------- FIREBASE ADMIN INIT ------------------ #
cred_path = os.environ.get('FIREBASE_CRED', 'firebase-adminsdk.json')
try:
    if not os.path.exists(cred_path):
        raise FileNotFoundError(
            f"Tệp thông tin Firebase ({cred_path}) không tồn tại. "
            f"Vui lòng đảm bảo tệp firebase-adminsdk.json được đặt trong thư mục dự án "
            f"({os.getcwd()}) hoặc cập nhật biến môi trường FIREBASE_CRED với đường dẫn đúng."
        )
    cred = credentials.Certificate(cred_path)
    firebase_admin.initialize_app(cred)
    firebase_initialized = True
    logger.info(f"Firebase khởi tạo thành công với {cred_path}")
except Exception as e:
    logger.error(f"Lỗi khởi tạo Firebase: {str(e)}")
    firebase_initialized = False

# ---------------- SEND NOTIFICATION ------------------ #
def send_notification(title, body, token):
    if not firebase_initialized:
        logger.warning(f"Không thể gửi thông báo '{title}': Firebase chưa được khởi tạo")
        return False
    if not token:
        logger.warning(f"Không thể gửi thông báo '{title}': Thiếu token FCM")
        return False
    try:
        message = messaging.Message(
            notification=messaging.Notification(
                title=title,
                body=body
            ),
            token=token
        )
        response = messaging.send(message)
        logger.info(f"Thông báo đã gửi: {response}")
        return True
    except Exception as e:
        logger.error(f"Lỗi gửi thông báo '{title}': {str(e)}")
        return False

@app.route("/register_token", methods=["POST"])
def register_token():
    token = request.json.get("token")
    if token:
        with open("fcm_token.txt", "w") as f:
            f.write(token)
        logger.info("Token FCM đã được lưu: fcm_token.txt")
        return jsonify({"status": "ok"})
    logger.warning("Yêu cầu đăng ký token thất bại: Thiếu token")
    return jsonify({"status": "missing token"}), 400

# ---------------- GOOGLE CALENDAR SETUP ------------------ #
def get_calendar_service():
    global creds
    if os.path.exists('token.pickle'):
        with open('token.pickle', 'rb') as token:
            creds = pickle.load(token)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                'credentials.json', SCOPES
            )
            creds = flow.run_local_server(port=0, open_browser=False)  # Sửa ở đây
        with open('token.pickle', 'wb') as token:
            pickle.dump(creds, token)
    return build('calendar', 'v3', credentials=creds)
    
def add_to_calendar(task):
    try:
        service = get_calendar_service()
        deadline = datetime.strptime(task['deadline'], "%d-%m-%Y")
        event = {
            'summary': task['title'],
            'description': task['plan'],
            'start': {'date': deadline.strftime("%Y-%m-%d")},
            'end': {'date': (deadline + timedelta(days=1)).strftime("%Y-%m-%d")},
        }
        service.events().insert(calendarId='primary', body=event).execute()
        logger.info(f"Đã thêm sự kiện vào Google Calendar: {task['title']}")
    except Exception as e:
        logger.error(f"Lỗi thêm sự kiện vào Google Calendar: {str(e)}")

# ---------------- EMAIL PROCESSING ------------------ #
def decode_mime_words(s):
    if not s:
        return ""
    decoded = decode_header(s)
    return ''.join([
        (part.decode(encoding or 'utf-8') if isinstance(part, bytes) else part)
        for part, encoding in decoded
    ])

def analyze_email(subject, body):
    task = {"title": subject, "deadline": None, "description": body}
    if not isinstance(body, str) or not body:
        logger.warning(f"Nội dung email không hợp lệ hoặc rỗng: {subject}")
        return None

    # Tìm hạn chót bằng biểu thức chính quy
    pattern = r'(?:due|hạn chót|deadline)[^\d]*(\d{2}[/-]\d{2}[/-]\d{4})'
    deadline_match = re.search(pattern, body, re.IGNORECASE)
    if deadline_match:
        deadline = deadline_match.group(1).replace('/', '-')
        try:
            datetime.strptime(deadline, "%d-%m-%Y")
            task["deadline"] = deadline
            logger.info(f"Tìm thấy hạn chót: {deadline} trong email: {subject}")
            return task
        except ValueError:
            logger.warning(f"Ngày không hợp lệ: {deadline} trong email: {subject}")

    # Nếu không tìm thấy bằng regex, dùng Groq AI
    try:
        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        }
        url = "https://api.groq.com/openai/v1/chat/completions"
        prompt = (
            f"Bạn là một trợ lý học tập thông minh và chỉ sử dụng tiếng Việt.\n"
            f"Tiêu đề email: {subject}\n"
            f"Nội dung email: {body}\n"
            f"Trong nội dung trên, hạn chót của công việc là ngày nào?\n"
            f"Trả lời duy nhất bằng định dạng DD-MM-YYYY. Nếu không có thì trả lời KHÔNG CÓ."
        )
        data = {
            "model": "llama3-70b-8192",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 20,
            "temperature": 0.2
        }
        response = requests.post(url, headers=headers, json=data, timeout=20)
        response.raise_for_status()
        reply = response.json()["choices"][0]["message"]["content"].strip().replace('/', '-')
        if "KHÔNG CÓ" in reply.upper():
            logger.info(f"Không tìm thấy hạn chót trong email: {subject}")
            return None
        datetime.strptime(reply, "%d-%m-%Y")
        task["deadline"] = reply
        logger.info(f"Tìm thấy hạn chót qua Groq AI: {reply} trong email: {subject}")
        return task
    except Exception as e:
        logger.error(f"Lỗi phân tích email qua Groq AI: {str(e)}")
        return None

def ai_plan_and_solve(tasks):
    planned = []
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    url = "https://api.groq.com/openai/v1/chat/completions"
    for task in tasks:
        try:
            deadline_date = datetime.strptime(task["deadline"], "%d-%m-%Y")
            days = max((deadline_date - datetime.now()).days, 1)
        except:
            days = 1
        prompt = (
            f"Bạn là trợ lý học thông minh. Trả lời bằng tiếng Việt.\n"
            f"Tiêu đề: {task['title']}\n"
            f"Mô tả: {task['description']}\n"
            f"Hạn chót: {task['deadline']}\n"
            f"Trong {days} ngày, chia công việc mỗi ngày để hoàn thành đúng hạn.\n"
            f"Trả về:\n- Tổng thời gian: X giờ\n- Ngày 1: Y giờ - Việc cụ thể\n- Ngày 2: ..."
        )
        data = {
            "model": "llama3-70b-8192",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 512,
            "temperature": 0.4
        }
        try:
            res = requests.post(url, headers=headers, json=data, timeout=30)
            res.raise_for_status()
            plan = res.json()["choices"][0]["message"]["content"]
            task.update({
                "plan": plan,
                "total_hours": 8,
                "hours_per_day": round(8 / days, 2),
                "days": days
            })
            planned.append(task)
            add_to_calendar(task)
            send_notification(f"Kế hoạch mới: {task['title']}", f"Học {task['hours_per_day']}h/ngày đến {task['deadline']}", user_fcm_token)
        except Exception as e:
            logger.error(f"Lỗi lập kế hoạch: {str(e)}")
            task.update({"plan": "Không thể tạo kế hoạch.", "total_hours": 8, "hours_per_day": 8, "days": 1})
            planned.append(task)
    return planned

@app.route("/save_token", methods=["POST"])
def save_token():
    global user_fcm_token
    data = request.get_json()
    token = data.get("token")
    if token:
        user_fcm_token = token
        fcm_tokens.add(token)
        logger.info(f"Token FCM nhận được: {token}")
        return jsonify({"status": "ok"}), 200
    logger.warning("Yêu cầu lưu token thất bại: Thiếu token")
    return jsonify({"status": "missing token"}), 400

def get_emails(email_user, email_pass):
    tasks = []
    try:
        logger.info(f"Đang kết nối IMAP với email: {email_user}")
        mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=30)
        logger.info(f"Đang đăng nhập với mật khẩu: {'*' * len(email_pass)}")
        mail.login(email_user, email_pass)
        logger.info("Đăng nhập thành công, chọn hộp thư đến")
        mail.select("inbox")
        status, data = mail.search(None, "UNSEEN")
        if status != "OK":
            logger.info("Không tìm thấy email nào.")
            return tasks

        email_ids = data[0].split()
        logger.info(f"Số email chưa đọc: {len(email_ids)}")

        if not ignored_senders:
            message_queue.put("Danh sách loại trừ email hiện tại đang trống.")
        else:
            message_queue.put(f"Đang loại trừ email từ: {', '.join(ignored_senders)}")

        for email_id in email_ids:
            status, msg_data = mail.fetch(email_id, "(BODY.PEEK[])")
            if status != "OK":
                logger.warning(f"Không thể lấy email ID {email_id}")
                continue
            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)
            subject = decode_mime_words(msg["Subject"] or "Không có tiêu đề")
            body = ""
            sender = decode_mime_words(msg["From"] or "Không rõ người gửi")

            if any(ignored.lower() in sender.lower() for ignored in ignored_senders):
                logger.info(f"Bỏ qua email từ {sender} do nằm trong danh sách loại trừ.")
                continue

            if msg.is_multipart():
                for part in msg.walk():
                    content_type = part.get_content_type()
                    if content_type == "text/plain":
                        body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                        break
                    elif content_type == "text/html" and not body:
                        html = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                        soup = BeautifulSoup(html, 'html.parser')
                        body = soup.get_text()
            else:
                body = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
            logger.info(f"Đã đọc email: Chủ đề: {subject}, Từ: {sender}")
            task = analyze_email(subject, body)
            if task:
                task["sender"] = sender
                task["done"] = False
                tasks.append(task)
                mail.store(email_id, '+FLAGS', '\\Seen')
            else:
                logger.info(f"Email không có hạn chót hợp lệ: {subject}")
        mail.logout()
        logger.info(f"Tìm thấy {len(tasks)} email hợp lệ.")
        if tasks:
            message_queue.put(f"Đã tìm thấy và xử lý {len(tasks)} email mới hợp lệ.")
        else:
            message_queue.put("Không có email mới hợp lệ được tìm thấy.")
        return tasks
    except imaplib.IMAP4.error as e:
        error_msg = f"Đăng nhập IMAP thất bại: {str(e)}"
        logger.error(error_msg)
        message_queue.put(error_msg)
        return tasks
    except Exception as e:
        error_msg = f"Lỗi khi đọc email: {str(e)}"
        logger.error(error_msg)
        message_queue.put(error_msg)
        return tasks

# ------------------------------ ROUTES ------------------------------ #
def email_check_thread():
    global planned_tasks, next_check_time
    while True:
        if email_credentials["email"] and email_credentials["password"]:
            logger.info("Bắt đầu kiểm tra email...")
            new_tasks = get_emails(email_credentials["email"], email_credentials["password"])
            if new_tasks:
                planned_tasks = ai_plan_and_solve(new_tasks)
            next_check_time = datetime.now() + timedelta(seconds=25)
        time.sleep(25)

@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email_input = request.form.get("email")
        password_input = request.form.get("password")
        try:
            mail = imaplib.IMAP4_SSL("imap.gmail.com")
            mail.login(email_input, password_input)
            mail.logout()
            email_credentials.update({"email": email_input, "password": password_input})
            session['logged_in'] = True
            if not any(t.name == "email_thread" for t in threading.enumerate()):
                t = threading.Thread(target=email_check_thread, name="email_thread", daemon=True)
                t.start()
            logger.info(f"Đăng nhập thành công cho email: {email_input}")
            return redirect(url_for("dashboard"))
        except Exception as e:
            logger.error(f"Đăng nhập thất bại: {str(e)}")
            return render_template("login.html", error="Sai thông tin đăng nhập.")
    return render_template("login.html")

@app.route("/dashboard")
def dashboard():
    if not session.get("logged_in"):
        logger.warning("Truy cập dashboard bị từ chối: Chưa đăng nhập")
        return redirect(url_for("login"))
    visible_tasks = planned_tasks
    logs = []
    while not message_queue.empty():
        logs.append(message_queue.get())
    return render_template("dashboard.html", tasks=visible_tasks, messages=logs, ignored_senders=ignored_senders)

@app.route("/toggle_done/<int:index>", methods=["POST"])
def toggle_done(index):
    if 0 <= index < len(planned_tasks):
        planned_tasks[index]["done"] = not planned_tasks[index].get("done", False)
        logger.info(f"Đã chuyển trạng thái công việc tại chỉ số {index}")
    return redirect(url_for("dashboard"))

@app.route("/ignore_sender", methods=["POST"])
def ignore_sender():
    sender = request.form.get("sender")
    if sender:
        ignored_senders.add(sender)
        logger.info(f"Đã thêm {sender} vào danh sách loại trừ")
    return redirect(url_for("dashboard"))

@app.route("/remove_ignore_sender", methods=["POST"])
def remove_ignore_sender():
    email = request.form.get("email")
    ignored_senders.discard(email)
    logger.info(f"Đã xóa {email} khỏi danh sách loại trừ")
    return redirect(url_for("dashboard"))

@app.route("/logout")
def logout():
    session.clear()
    email_credentials.update({"email": "", "password": ""})
    planned_tasks.clear()
    ignored_senders.clear()
    logger.info("Đã đăng xuất người dùng")
    return redirect(url_for("login"))

@app.route("/stream")
def stream():
    def event_stream():
        last_sent = -1
        while True:
            if next_check_time:
                remaining = int((next_check_time - datetime.now()).total_seconds())
                if remaining != last_sent:
                    last_sent = remaining
                    if remaining >= 0:
                        yield f"data: {{\"countdown\": {remaining}}}\n\n"
            time.sleep(1)
    return Response(event_stream(), mimetype='text/event-stream')

@app.route('/firebase-messaging-sw.js')
def service_worker():
    try:
        return send_from_directory('.', 'firebase-messaging-sw.js', mimetype='application/javascript')
    except Exception as e:
        logger.error(f"Lỗi phục vụ firebase-messaging-sw.js: {str(e)}")
        return jsonify({"error": "Không tìm thấy firebase-messaging-sw.js"}), 404

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
