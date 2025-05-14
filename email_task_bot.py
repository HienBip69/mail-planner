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
from bs4 import BeautifulSoup

app = Flask(__name__, template_folder='templates')
app.secret_key = os.environ.get('SECRET_KEY', 'mysecretkey123')
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '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')

email_credentials = {"email": "", "password": ""}
planned_tasks = []
message_queue = Queue()
next_check_time = None
ignored_senders = set()


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

            if any(ignored.lower() in sender.lower() for ignored in ignored_senders):
                print(f"[{datetime.now()}] Bỏ qua email từ {sender} do nằm trong danh sách loại trừ.")
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
            print(f"[{datetime.now()}] Đã đọc email: Chủ đề: {subject}, Từ: {sender}")
            task = analyze_email(subject, body)
            if task:
                task["sender"] = sender
                task["done"] = False
                tasks.append(task)
                mail.store(email_id, '+FLAGS', '\\Seen')
            else:
                print(f"[{datetime.now()}] Email không có hạn chót hợp lệ: {subject}")
        mail.logout()
        print(f"[{datetime.now()}] Tìm thấy {len(tasks)} email hợp lệ.")
        return tasks
    except imaplib.IMAP4.error as e:
        error_msg = f"Đăng nhập IMAP thất bại: {str(e)}"
        print(f"[{datetime.now()}] {error_msg}")
        message_queue.put(error_msg)
        return tasks
    except Exception as e:
        error_msg = f"Lỗi khi đọc email: {str(e)}"
        print(f"[{datetime.now()}] {error_msg}")
        message_queue.put(error_msg)
        return tasks

def analyze_email(subject, body):
    task = {"title": subject, "deadline": None, "description": body}

    # Bước 1: Dò deadline bằng regex (dấu / hoặc -)
    deadline_match = re.search(r'(due|hạn chót|deadline)[^\d]*(\d{2}[/-]\d{2}[/-]\d{4})', body, re.IGNORECASE)
    if deadline_match:
        deadline = deadline_match.group(2).replace('/', '-')
        try:
            datetime.strptime(deadline, "%d-%m-%Y")
            task["deadline"] = deadline
            return task
        except ValueError:
            pass  # Nếu không hợp lệ thì tiếp tục bước 2

    # Bước 2: Gửi đến Groq AI để phân tích hạn chót
    try:
        print(f"[{datetime.now()}] Gửi nội dung tới Groq AI để nhận diện hạn chót...")
        headers = {
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        }
        url = "https://api.groq.com/openai/v1/chat/completions"
        prompt = (
            f"Nội dung email như sau:\n"
            f"Tiêu đề: {subject}\n"
            f"Nội dung: {body}\n\n"
            f"Hỏi: Trong nội dung trên, hạn chót của công việc là ngày nào? "
            f"Nếu có, trả lời duy nhất bằng định dạng DD-MM-YYYY. Nếu không có thì trả lời KHÔNG CÓ."
        )
        data = {
            "model": "llama3-70b-8192",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 20,
            "temperature": 0.2
        }

        response = requests.post(url, headers=headers, json=data, timeout=20)
        response.raise_for_status()
        reply = response.json()["choices"][0]["message"]["content"].strip()
        reply = reply.replace('/', '-')

        print(f"[{datetime.now()}] AI trả lời hạn chót: {reply}")

        if "KHÔNG CÓ" in reply.upper():
            return None

        try:
            datetime.strptime(reply, "%d-%m-%Y")
            task["deadline"] = reply
            return task
        except ValueError:
            print(f"[{datetime.now()}] AI trả về ngày không hợp lệ: {reply}")
            return None

    except Exception as e:
        print(f"[{datetime.now()}] Lỗi khi gửi AI để dò hạn chót: {str(e)}")
        return None

@app.route('/toggle_done/<int:index>', methods=['POST'])
def toggle_done(index):
    if 0 <= index < len(planned_tasks):
        planned_tasks[index]['done'] = not planned_tasks[index].get('done', False)
    return redirect(url_for('dashboard'))

@app.route('/ignore_sender', methods=['POST'])
def ignore_sender():
    sender = request.form.get('sender')
    if sender:
        ignored_senders.add(sender)
        message_queue.put(f"Đã thêm {sender} vào danh sách loại trừ.")
    return redirect(url_for('dashboard'))

@app.route('/clear_ignore', methods=['POST'])
def clear_ignore():
    ignored_senders.clear()
    message_queue.put("Đã xóa tất cả email khỏi danh sách loại trừ.")
    return redirect(url_for('dashboard'))

# Gọi Groq AI API với yêu cầu trả về tiếng Việt
# Gọi Groq AI API với yêu cầu trả về tiếng Việt
def ai_plan_and_solve(tasks):
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    url = "https://api.groq.com/openai/v1/chat/completions"
    planned_tasks = []

    if not GROQ_API_KEY or not GROQ_API_KEY.startswith("gsk_"):
        print(f"[{datetime.now()}] Lỗi: Khóa API Groq không hợp lệ hoặc chưa được cấu hình.")
        message_queue.put("Lỗi: Khóa API Groq không hợp lệ. Bot vẫn chạy nhưng không lập kế hoạch.")
        for task in tasks:
            planned_tasks.append({
                "title": task["title"],
                "deadline": task["deadline"],
                "description": task["description"],
                "total_hours": 8,
                "hours_per_day": 8,
                "days": 1,
                "plan": "Không thể lập kế hoạch do lỗi API.",
                "sender": task["sender"]
            })
        return planned_tasks

    for task in tasks:
        try:
            deadline_date = datetime.strptime(task['deadline'], "%d-%m-%Y")
            today = datetime.today()
            days_until_deadline = max((deadline_date - today).days, 1)

            prompt = (
                f"Tạo kế hoạch chi tiết cho nhiệm vụ này bằng tiếng Việt:\n"
                f"Tiêu đề: {task['title']}\n"
                f"Mô tả: {task['description']}\n"
                f"Hạn chót: {task['deadline']} (định dạng DD-MM-YYYY)\n"
                f"Ước lượng tổng thời gian hoàn thành (giờ) và lập kế hoạch chi tiết phân bổ công việc cụ thể cho từng ngày trong {days_until_deadline} ngày. "
                f"Trả về định dạng bằng tiếng Việt:\n"
                f"- Tổng thời gian: X giờ\n"
                f"- Ngày 1: Y giờ - Công việc cụ thể\n"
                f"- Ngày 2: Z giờ - Công việc cụ thể\n"
                f"(và tiếp tục cho đến hết số ngày)"
            )

            data = {
                "model": "llama3-70b-8192",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 512,
                "temperature": 0.7
            }

            print(f"[{datetime.now()}] Gửi yêu cầu tới Groq AI: {task['title']}")
            response = requests.post(url, headers=headers, json=data, timeout=30)
            response.raise_for_status()

            plan = response.json()["choices"][0]["message"]["content"]
            print(f"[{datetime.now()}] Phản hồi từ Groq AI: {plan[:60]}...")

            total_hours = extract_total_hours(plan) or 8
            hours_per_day = round(total_hours / days_until_deadline, 2)

            planned_task = {
                "title": task["title"],
                "deadline": task["deadline"],
                "description": task["description"],
                "total_hours": total_hours,
                "hours_per_day": hours_per_day,
                "days": days_until_deadline,
                "plan": plan,
                "sender": task["sender"]
            }

            planned_tasks.append(planned_task)
            add_task_to_calendar(planned_task)

        except Exception as e:
            print(f"[{datetime.now()}] Lỗi khi xử lý task '{task['title']}': {str(e)}")
            message_queue.put(f"Lỗi khi lập kế hoạch: {str(e)}")
            planned_tasks.append({
                "title": task["title"],
                "deadline": task["deadline"],
                "description": task["description"],
                "total_hours": 8,
                "hours_per_day": 8,
                "days": 1,
                "plan": "Không thể lập kế hoạch do lỗi xử lý.",
                "sender": task["sender"]
            })

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
            time.sleep(25)
            continue
        
        try:
            message_queue.put("Bot đang đọc email...")
            next_check_time = time.time() + 25  # Cập nhật chu kỳ mới
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
        
        time.sleep(25)
# Google Calendar
def get_calendar_service():
    SCOPES = ['https://www.googleapis.com/auth/calendar']
    try:
        creds = None
        token_path = 'token.pickle'
        credentials_path = 'credentials.json'

        # ⚠️ Nếu không có credentials.json thì không thể xác thực OAuth
        if not os.path.exists(credentials_path):
            print("❌ Không tìm thấy file 'credentials.json'. Bạn cần tải nó từ Google Cloud Console.")
            return None

        # 🧾 Nếu đã có token
        if os.path.exists(token_path):
            with open(token_path, 'rb') as token_file:
                creds = pickle.load(token_file)

        # 🔄 Nếu token hết hạn hoặc chưa có
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                print("🔁 Token hết hạn, đang làm mới...")
                creds.refresh(Request())
            else:
                print("🔐 Cần xác thực OAuth mới. Trình duyệt sẽ mở.")
                flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
                creds = flow.run_local_server(port=0)

            # 💾 Lưu lại token
            with open(token_path, 'wb') as token_file:
                pickle.dump(creds, token_file)
                print("✅ Token mới đã được lưu vào token.pickle")

        # Trả về dịch vụ calendar
        return build('calendar', 'v3', credentials=creds)

    except Exception as e:
        print(f"❗ Lỗi khi khởi tạo Google Calendar API: {e}")
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
                    yield f'data: {{"countdown": {remaining_seconds}}}\n\n'
            time.sleep(1)  # tránh vòng lặp chạy quá nhanh
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
