import os
import re
import time
import threading
import imaplib
import email
from datetime import datetime, timedelta
from flask import Flask, request, render_template, redirect, url_for, session, Response
import requests
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
import pickle
from queue import Queue

app = Flask(__name__, template_folder='templates')
app.secret_key = os.environ.get('SECRET_KEY', 'mysecretkey123')
OPENROUTER_API_KEY = os.environ.get('OPENROUTER_API_KEY', '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')

# Biến toàn cục
email_credentials = {"email": "", "password": ""}
planned_tasks = []
message_queue = Queue()
next_check_time = None

# Hàm đọc email qua IMAP
def get_emails(email_user, email_pass):
    tasks = []
    try:
        print(f"[{datetime.now()}] Đang kết nối IMAP với email: {email_user}")
        mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=30)
        print(f"[{datetime.now()}] Đang đăng nhập với mật khẩu: {'*' * len(email_pass)}")
        mail.login(email_user, email_pass)
        print(f"[{datetime.now()}] Đăng nhập thành công, chọn hộp thư đến")
        mail.select("inbox")
        status, data = mail.search(None, "UNSEEN")
        print(f"[{datetime.now()}] Trạng thái tìm kiếm email: {status}, Số email chưa đọc: {len(data[0].split())}")
        if status != "OK":
            print(f"[{datetime.now()}] Không tìm thấy email nào.")
            return tasks

        email_ids = data[0].split()
        for email_id in email_ids:
            status, msg_data = mail.fetch(email_id, "(BODY.PEEK[])")
            if status != "OK":
                print(f"[{datetime.now()}] Không thể lấy email ID {email_id}")
                continue
            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)
            subject = msg["Subject"] or "Không có tiêu đề"
            body = ""
            sender = msg["From"] or "Không rõ người gửi"
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                        break
            else:
                body = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
            print(f"[{datetime.now()}] Đã đọc email: Chủ đề: {subject}, Từ: {sender}")
            task = analyze_email(subject, body)
            if task:
                task["sender"] = sender
                tasks.append(task)
                mail.store(email_id, '+FLAGS', '\\Seen')
            else:
                print(f"[{datetime.now()}] Email không có hạn chót hợp lệ: {subject}")
        mail.logout()
        print(f"[{datetime.now()}] Tìm thấy {len(tasks)} email hợp lệ.")
        return tasks
    except Exception as e:
        error_msg = f"Lỗi khi đọc email: {str(e)}"
        print(f"[{datetime.now()}] {error_msg}")
        message_queue.put(error_msg)
        return tasks

# Phân tích email
def analyze_email(subject, body):
    task = {"title": subject, "deadline": None, "description": body}
    deadline_match = re.search(r'due (\d{2}-\d{2}-\d{4})|due (\d{2}/\d{2}/\d{4})', body, re.IGNORECASE)
    if deadline_match:
        deadline = deadline_match.group(1) or deadline_match.group(2)
        deadline = deadline.replace('/', '-')
        try:
            datetime.strptime(deadline, "%d-%m-%Y")
            task["deadline"] = deadline
        except ValueError:
            print(f"[{datetime.now()}] Ngày {deadline} không hợp lệ.")
            return None
    return task if task["deadline"] else None

# Gọi OpenRouter API và lập kế hoạch chi tiết
def ai_plan_and_solve(tasks):
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }
    url = "https://openrouter.ai/api/v1/chat/completions"
    planned_tasks = []

    if not OPENROUTER_API_KEY or "sk-or-v1-" not in OPENROUTER_API_KEY:
        print(f"[{datetime.now()}] Lỗi: Khóa API OpenRouter không hợp lệ hoặc chưa được cấu hình.")
        message_queue.put("Lỗi: Khóa API OpenRouter không hợp lệ.")
        return planned_tasks

    for task in tasks:
        deadline_date = datetime.strptime(task["deadline"], "%d-%m-%Y")
        days_until_deadline = max((deadline_date - datetime.now()).days, 1)

        prompt = (
            f"Tạo kế hoạch chi tiết cho nhiệm vụ này:\n"
            f"Tiêu đề: {task['title']}\n"
            f"Mô tả: {task['description']}\n"
            f"Hạn chót: {task['deadline']} (định dạng DD-MM-YYYY)\n"
            f"Ước lượng tổng thời gian hoàn thành (giờ) và lập kế hoạch chi tiết phân bổ công việc cụ thể cho từng ngày trong {days_until_deadline} ngày. "
            f"Trả về định dạng:\n"
            f"- Tổng thời gian: X giờ\n"
            f"- Ngày 1: Y giờ - Công việc cụ thể\n"
            f"- Ngày 2: Z giờ - Công việc cụ thể\n"
            f"(và tiếp tục cho đến hết số ngày)"
        )
        data = {
            "model": "mistralai/mixtral-8x7b-instruct:free",
            "messages": [{"role": "user", "content": prompt}]
        }
        try:
            print(f"[{datetime.now()}] Gửi yêu cầu tới OpenRouter: {url}")
            print(f"[{datetime.now()}] Đầu đề: {headers}")
            print(f"[{datetime.now()}] Dữ liệu gửi: {data}")
            response = requests.post(url, headers=headers, json=data, timeout=30)
            response.raise_for_status()
            plan = response.json()["choices"][0]["message"]["content"]
            print(f"[{datetime.now()}] Nhận phản hồi: {plan[:50]}...")

            total_hours = extract_total_hours(plan) or 8
            hours_per_day = total_hours / days_until_deadline

            planned_task = {
                "title": task["title"],
                "deadline": task["deadline"],
                "description": task["description"],
                "total_hours": total_hours,
                "hours_per_day": round(hours_per_day, 2),
                "days": days_until_deadline,
                "plan": plan,
                "sender": task["sender"]
            }
            planned_tasks.append(planned_task)
            add_task_to_calendar(planned_task)
        except requests.exceptions.HTTPError as e:
            print(f"[{datetime.now()}] Lỗi HTTP khi gọi OpenRouter: {str(e)}")
            message_queue.put(f"Lỗi: {str(e)}")
        except Exception as e:
            print(f"[{datetime.now()}] Lỗi khác: {str(e)}")
            message_queue.put(f"Lỗi: {str(e)}")
    return planned_tasks

# Trích xuất tổng giờ
def extract_total_hours(plan):
    match = re.search(r'tổng thời gian.*?(\d+\.?\d*) giờ', plan, re.IGNORECASE)
    return float(match.group(1)) if match else None

# Kiểm tra email định kỳ
def check_emails_periodically():
    global planned_tasks, next_check_time
    while True:
        if not email_credentials["email"] or not email_credentials["password"]:
            print(f"[{datetime.now()}] Chưa đăng nhập. Đang chờ...")
            time.sleep(60)
            continue
        
        try:
            message_queue.put("Bot đang đọc email...")
            next_check_time = time.time() + 60
            tasks = get_emails(email_credentials["email"], email_credentials["password"])
            if tasks:
                print(f"[{datetime.now()}] Đã tìm thấy {len(tasks)} email mới.")
                new_planned_tasks = ai_plan_and_solve(tasks)
                if new_planned_tasks:
                    planned_tasks = new_planned_tasks
                message_queue.put(f"Đã xử lý xong {len(tasks)} email.")
            else:
                message_queue.put("Không có email mới hoặc nhiệm vụ hợp lệ.")
        except Exception as e:
            message_queue.put(f"Lỗi: {str(e)}")
            print(f"[{datetime.now()}] Lỗi trong quá trình kiểm tra email: {str(e)}")
        
        time.sleep(60)

# Google Calendar
def get_calendar_service():
    try:
        creds = None
        if os.path.exists('token.pickle'):
            with open('token.pickle', 'rb') as token:
                creds = pickle.load(token)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists('credentials.json'):
                    print(f"[{datetime.now()}] Thông báo: Không tìm thấy file credentials.json. Bỏ qua Google Calendar.")
                    return None
                flow = InstalledAppFlow.from_client_secrets_file('credentials.json', ['https://www.googleapis.com/auth/calendar'])
                creds = flow.run_local_server(port=0)
                with open('token.pickle', 'wb') as token:
                    pickle.dump(creds, token)
        return build('calendar', 'v3', credentials=creds)
    except Exception as e:
        print(f"[{datetime.now()}] Lỗi khi khởi tạo Google Calendar: {str(e)}")
        return None

def add_task_to_calendar(task):
    service = get_calendar_service()
    if not service:
        print(f"[{datetime.now()}] Bỏ qua việc thêm vào Google Calendar vì không có dịch vụ.")
        return
    
    try:
        start_date = datetime.now().date()
        for day in range(task["days"]):
            event_date = start_date + timedelta(days=day)
            event = {
                'summary': f"{task['title']} - Ngày {day + 1}/{task['days']}",
                'description': (
                    f"Mô tả: {task.get('description', '')}\n"
                    f"Kế hoạch: {task['plan']}\n"
                    f"Thời gian hôm nay: {task['hours_per_day']} giờ\n"
                    f"Tổng thời gian: {task['total_hours']} giờ\n"
                    f"Số ngày làm: {task['days']} ngày"
                ),
                'start': {'date': event_date.strftime("%Y-%m-%d")},
                'end': {'date': event_date.strftime("%Y-%m-%d")}
            }
            service.events().insert(calendarId='primary', body=event).execute()
        print(f"[{datetime.now()}] Đã thêm nhiệm vụ '{task['title']}' vào Google Calendar.")
        message_queue.put(f"Đã thêm '{task['title']}' vào Google Calendar.")
    except Exception as e:
        print(f"[{datetime.now()}] Lỗi khi thêm vào Google Calendar: {str(e)}")
        message_queue.put(f"Lỗi khi thêm lịch: {str(e)}")

# Endpoint SSE
@app.route('/stream')
def stream():
    def event_stream():
        global next_check_time
        while True:
            if not message_queue.empty():
                message = message_queue.get()
                yield f"data: {{ \"message\": \"{message}\" }}\n\n"
            if next_check_time:
                remaining_seconds = int(next_check_time - time.time())
                if remaining_seconds >= 0:
                    yield f"data: {{ \"countdown\": {remaining_seconds} }}\n\n"
            time.sleep(1)
    return Response(event_stream(), mimetype="text/event-stream")

@app.route('/')
def index():
    return render_template('index.html', error=None)

@app.route('/login', methods=['POST'])
def login():
    email_user = request.form['email']
    email_pass = request.form['password']
    
    try:
        get_emails(email_user, email_pass)  # Kiểm tra tài khoản hợp lệ
        email_credentials["email"] = email_user
        email_credentials["password"] = email_pass
        session['logged_in'] = True
        
        print(f"[{datetime.now()}] Đăng nhập thành công với {email_user}")
        if not any(t.name == 'email_thread' for t in threading.enumerate()):
            email_thread = threading.Thread(target=check_emails_periodically, name='email_thread', daemon=True)
            email_thread.start()
            print(f"[{datetime.now()}] Luồng kiểm tra email đã khởi động")
        
        return redirect(url_for('dashboard'))
    except Exception as e:
        error_msg = f"Đăng nhập thất bại: {str(e)}. Vui lòng kiểm tra email/mật khẩu ứng dụng."
        print(f"[{datetime.now()}] {error_msg}")
        return render_template('index.html', error=error_msg)

@app.route('/dashboard')
def dashboard():
    if not session.get('logged_in'):
        return redirect(url_for('index'))
    return render_template('dashboard.html', plans=planned_tasks)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
