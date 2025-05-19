import os
import re
import time
import threading
import imaplib
import email
from datetime import datetime, timedelta
from flask import Flask, request, render_template, redirect, url_for, session, Response, send_from_directory
import requests
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
import pickle
from queue import Queue
from bs4 import BeautifulSoup

app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = os.environ.get('SECRET_KEY', 'mysecretkey123')
GROQ_API_KEY = os.environ.get('GROQ_API_KEY', 'gsk_demo_key')

email_credentials = {"email": "", "password": ""}
planned_tasks = []
message_queue = Queue()
next_check_time = None
ignored_senders = set()

# ------------------------------ EMAIL FETCH ------------------------------ #
def get_emails(email_user, email_pass):
    tasks = []
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", timeout=30)
        mail.login(email_user, email_pass)
        mail.select("inbox")
        status, data = mail.search(None, "UNSEEN")
        if status != "OK": return tasks

        email_ids = data[0].split()
        for email_id in email_ids:
            status, msg_data = mail.fetch(email_id, "(BODY.PEEK[])")
            if status != "OK": continue

            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)
            subject = msg["Subject"] or "Không có tiêu đề"
            body = ""
            sender = msg["From"] or "Không rõ người gửi"

            if any(ignored.lower() in sender.lower() for ignored in ignored_senders):
                continue

            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                        break
            else:
                body = msg.get_payload(decode=True).decode('utf-8', errors='ignore')

            task = analyze_email(subject, body)
            if task:
                task["sender"] = sender
                task["done"] = False
                tasks.append(task)
                mail.store(email_id, '+FLAGS', '\\Seen')

        mail.logout()
        return tasks
    except Exception as e:
        message_queue.put(f"Lỗi email: {str(e)}")
        return tasks

# ------------------------------ EMAIL ANALYSIS ------------------------------ #
def analyze_email(subject, body):
    task = {"title": subject, "deadline": None, "description": body}
    deadline_match = re.search(r'(due|hạn chót|deadline)[^\d]*(\d{2}[/-]\d{2}[/-]\d{4})', body, re.IGNORECASE)
    if deadline_match:
        try:
            task["deadline"] = deadline_match.group(2).replace('/', '-')
            datetime.strptime(task["deadline"], "%d-%m-%Y")
            return task
        except: pass

    try:
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
        prompt = (
            f"Tiêu đề: {subject}\n"
            f"Nội dung: {body}\n"
            f"Hạn chót là ngày nào? Trả lời DD-MM-YYYY hoặc KHÔNG CÓ."
        )
        data = {
            "model": "llama3-70b-8192",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 20,
            "temperature": 0.2
        }
        response = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=data, timeout=20)
        reply = response.json()["choices"][0]["message"]["content"].strip().replace('/', '-')
        if "KHÔNG CÓ" in reply.upper(): return None
        datetime.strptime(reply, "%d-%m-%Y")
        task["deadline"] = reply
        return task
    except:
        return None

# ------------------------------ PLAN ------------------------------ #
def ai_plan_and_solve(tasks):
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    plans = []
    for task in tasks:
        prompt = (
            f"Tôi có công việc: {task['title']} với hạn chót {task['deadline']}.\n"
            f"Mô tả: {task['description']}\n"
            f"Tôi chỉ có thể dành 8 tiếng mỗi ngày.\n"
            f"Lập kế hoạch cụ thể theo ngày."
        )
        data = {
            "model": "llama3-70b-8192",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1000
        }
        try:
            response = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=data, timeout=30)
            plan = response.json()["choices"][0]["message"]["content"]
        except:
            plan = "Lỗi khi lập kế hoạch."

        plans.append({
            "title": task["title"],
            "deadline": task["deadline"],
            "description": task["description"],
            "plan": plan,
            "sender": task["sender"],
            "done": False
        })
    return plans

# ------------------------------ THREAD ------------------------------ #
def email_check_thread():
    global planned_tasks, next_check_time
    while True:
        if email_credentials["email"] and email_credentials["password"]:
            new_tasks = get_emails(email_credentials["email"], email_credentials["password"])
            if new_tasks:
                planned_tasks = ai_plan_and_solve(new_tasks)
            next_check_time = datetime.now() + timedelta(seconds=25)
        time.sleep(25)

# ------------------------------ ROUTES ------------------------------ #
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
            return redirect(url_for("dashboard"))
        except:
            return render_template("login.html", error="Sai thông tin đăng nhập.")
    return render_template("login.html")

@app.route("/dashboard")
def dashboard():
    if not session.get("logged_in"): return redirect(url_for("login"))
    visible_tasks = planned_tasks
    logs = []
    while not message_queue.empty():
        logs.append(message_queue.get())
    return render_template("dashboard.html", tasks=visible_tasks, messages=logs, ignored_senders=ignored_senders)

@app.route("/toggle_done/<int:index>", methods=["POST"])
def toggle_done(index):
    if 0 <= index < len(planned_tasks):
        planned_tasks[index]["done"] = not planned_tasks[index].get("done", False)
    return redirect(url_for("dashboard"))

@app.route("/ignore_sender", methods=["POST"])
def ignore_sender():
    sender = request.form.get("sender")
    if sender: ignored_senders.add(sender)
    return redirect(url_for("dashboard"))

@app.route("/remove_ignore_sender", methods=["POST"])
def remove_ignore_sender():
    email = request.form.get("email")
    ignored_senders.discard(email)
    return redirect(url_for("dashboard"))

@app.route("/logout")
def logout():
    session.clear()
    email_credentials.update({"email": "", "password": ""})
    planned_tasks.clear()
    ignored_senders.clear()
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
    return send_from_directory('.', 'firebase-messaging-sw.js')

# ------------------------------ MAIN ------------------------------ #
if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=debug)
