from flask import Flask, render_template, request, jsonify, redirect, session
from pymongo import MongoClient
from bson.objectid import ObjectId
from werkzeug.security import generate_password_hash, check_password_hash
from openai import OpenAI
from textblob import TextBlob
from dotenv import load_dotenv
import os
from datetime import datetime
from flask import request, jsonify, render_template, redirect, session
from google_auth_oauthlib.flow import Flow
import google.oauth2.id_token
from google.auth.transport.requests import Request

load_dotenv()
# --------------------------------------------------
# APP CONFIG
# --------------------------------------------------
app = Flask(__name__)
app.secret_key = "secret123"
#--------------------------------------------------
# Allow HTTP for local development
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
GOOGLE_CLIENT_SECRETS_FILE = "client_secret.json"
# --------------------------------------------------
# DATABASE CONNECTION
# --------------------------------------------------
mongo_client = MongoClient("mongodb://localhost:27017/")
db = mongo_client["mental_chatbot"]
users = db["users"]
feedbacks = db["feedbacks"]
settings_collection = db["system_settings"]

# Create default system settings if not exists
if settings_collection.count_documents({}) == 0:
    settings_collection.insert_one({
        "registration_enabled": True,
        "feedback_enabled": True,
        "maintenance_mode": False
    })
# --------------------------------------------------
# GROQ CLIENT (OpenAI Compatible)
# --------------------------------------------------
ai_client = OpenAI(
    api_key=os.getenv("GROQ_API_KEY"),
    base_url="https://api.groq.com/openai/v1"
)
# --------------------------------------------------
#ADMIN CREDENTIALS
#---------------------------------------------------
ADMIN_EMAIL = "admin@gmail.com"
ADMIN_PASSWORD = "admin123" 
# --------------------------------------------------
# SENTIMENT ANALYSIS
# --------------------------------------------------
def analyze_sentiment(text):
    polarity = TextBlob(text).sentiment.polarity
    if polarity < -0.3:
        return "negative"
    elif polarity > 0.3:
        return "positive"
    else:
        return "neutral"
# SUMMARY FUNCTION
def summarize_chat(chat_history):
    if not chat_history:
        return {
            "summary": "Today felt quiet. I didn’t share much, but I took a moment for myself.",
            "mood": "neutral"
        }
    # Extract only user messages
    user_texts = [
        msg.replace("User:", "").strip()
        for msg in chat_history
        if msg.startswith("User:")
    ]
    combined_text = " ".join(user_texts)
    # Detect overall mood
    overall_mood = analyze_sentiment(combined_text)
    try:
        prompt = f"""
        Convert the following conversation thoughts into a short diary-style reflection.
        Make it emotional, natural, and personal.
        Do NOT mention AI or chatbot.
        Keep it 5-7 lines.
        
        Conversation:
        {combined_text}
        """
        completion = ai_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}]
        )
        diary_text = completion.choices[0].message.content.strip()

    except Exception:
        diary_text = combined_text
    return {
        "summary": diary_text,
        "mood": overall_mood
    }
# --------------------------------------------------
# ROUTES
# --------------------------------------------------
@app.route("/")
def index():
    settings = settings_collection.find_one()
    if settings["maintenance_mode"]:
        return "System is under maintenance. Please try again later."
    return render_template("index.html")

# LOGIN
@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]
        # 🔴 Gmail validation
        if not email.endswith("@gmail.com"):
            return render_template(
                "login.html",
                error="Only Gmail accounts are allowed"
            )
        user = users.find_one({"email": email, "is_active": True})
        # 🔴 Account not found
        if not user:
            inactive_user = users.find_one({"email": email, "is_active": False})
            if inactive_user:
                return render_template(
                    "login.html",
                    error="Your account has been deactivated by admin."
                )
            return render_template("login.html", not_found=True)
        if user.get("google_auth"):
            return "This account uses Google login. Please sign in with Google."
        # 🔴 Wrong password
        if user["password"] != password:
            return render_template(
                "login.html",
                error="Incorrect password"
            )
        session["user"] = email
        if not user.get("profile_completed", False):
            return redirect("/profile")
        return redirect("/dashboard")
    return render_template("login.html")

#GOOGLE  LOGIN
@app.route("/google_login")
def google_login():
    flow = Flow.from_client_secrets_file(
        "client_secret.json",
        scopes=[
            "https://www.googleapis.com/auth/userinfo.profile",
            "https://www.googleapis.com/auth/userinfo.email",
            "openid"
        ],
        redirect_uri="http://127.0.0.1:5000/google_callback"
    )
    authorization_url, state = flow.authorization_url(
        prompt="select_account"
    )
    session["state"] = state
    return redirect(authorization_url)

@app.route("/google_callback")
def google_callback():
    flow = Flow.from_client_secrets_file(
        "client_secret.json",
        scopes=[
            "https://www.googleapis.com/auth/userinfo.profile",
            "https://www.googleapis.com/auth/userinfo.email",
            "openid"
        ],
        redirect_uri="http://127.0.0.1:5000/google_callback"
    )
    flow.fetch_token(authorization_response=request.url)
    if session.get("state") != request.args.get("state"):
        return "State mismatch error"
    credentials = flow.credentials
    request_session = Request()
    id_info = google.oauth2.id_token.verify_oauth2_token(
        credentials.id_token,
        request_session,
        flow.client_config["client_id"]
    )
    email = id_info["email"]
    name = id_info["name"]
    users.update_one(
        {"email": email},
        {"$setOnInsert": {
            "email": email,
            "name": name,
            "google_auth": True,
            "chat_history": [],
            "mood_journal": [],
            "profile_completed": False
        }},
        upsert=True
    )
    session["user"] = email
    user = users.find_one({"email": email})
    if not user.get("profile_completed", False):
        return redirect("/profile")
    return redirect("/dashboard")

# REGISTER
@app.route("/register", methods=["GET","POST"])
def register():
    settings = settings_collection.find_one()
    if not settings["registration_enabled"]:
        return "Registration is currently disabled by admin."
    if request.method == "POST":
        username = request.form["username"]
        email = request.form["email"]
        # 🔴 GMAIL VALIDATION
        if not email.endswith("@gmail.com"):
             return render_template(
                "register.html",
                error="Only Gmail addresses are allowed",
                success=False
            )
        password = request.form["password"]
        # 🔴 CHECK IF USER ALREADY EXISTS
        existing_user = users.find_one({"email": email})
        if existing_user:
            return render_template(
                "register.html",
                error="⚠ Email already registered!",
                success=False
            )
        # 🟢 INSERT NEW USER
        users.insert_one({
            "username": username,
            "email": email,
            "password": password,
            "is_active": True,
            "profile_completed": False,
            "chat_history": [],
            "mood_journal": []
        })
        return render_template("register.html", success=True)
    return render_template("register.html", success=False)

# DASHBOARD
@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect("/login")
    user = users.find_one({"email": session["user"]})
    if not user.get("profile_completed", False):
        return redirect("/profile")
    return render_template("dashboard.html")

# CONNECT PAGE
@app.route("/ai_chat/connect", methods=["GET", "POST"])
def connect():
    if "user" not in session:
        return redirect("/login")
    if request.method == "POST":
        person = request.form.get("person")
        if person:
            session["chat_person"] = person
            return redirect("/ai_chat/chat")
    return render_template("ai_chat/connect.html")

#ABOUT PAGE
@app.route("/about")
def about():
    if "user" not in session:
        return redirect("/login")
    return render_template("about.html")

#PROFILE PAGE
@app.route("/profile", methods=["GET","POST"])
def profile():
    if "user" not in session:
        return redirect("/login")
    user_data = users.find_one({"email": session["user"]})
    # UPDATE PROFILE
    if request.method == "POST":
        users.update_one(
            {"email": session["user"]},
            {"$set":{
                "username": request.form["username"],
                "dob": request.form["dob"],
                "age": request.form["age"],
                "gender": request.form["gender"],
                "bio": request.form["bio"],
                "preference": request.form["preference"],
                "profile_completed": True
            }}
        )
        return redirect("/profile")
    # SEND SEPARATE FIELDS TO HTML
    return render_template(
        "profile.html",
        user=user_data.get("username",""),
        email=user_data.get("email",""),
        dob=user_data.get("dob",""),
        age=user_data.get("age",""),
        gender=user_data.get("gender",""),
        bio=user_data.get("bio",""),
        preference=user_data.get("preference",""),
        avatar=user_data.get("avatar_type")
    )

#CHANGE PASSWORD
@app.route("/change_password", methods=["POST"])
def change_password():
    if "user" not in session:
        return redirect("/login")
    current_password = request.form["current_password"]
    new_password = request.form["new_password"]
    confirm_password = request.form["confirm_password"]
    user = users.find_one({"email": session["user"]})
    # 🔴 Check current password
    if user["password"] != current_password:
        return render_template(
            "profile.html",
            error="Current password incorrect"
        )
    # 🔴 Confirm password match
    if new_password != confirm_password:
        return render_template(
            "profile.html",
            error="Passwords do not match"
        )
    # 🟢 Update password
    users.update_one(
        {"email": session["user"]},
        {"$set":{"password": new_password}}
    )
    return render_template(
        "profile.html",
        success="Password updated successfully"
    )

# FEEDBACK PAGE
@app.route("/feedback")
def feedback():
    if "user" not in session:
        return redirect("/login")
    settings = settings_collection.find_one()
    if not settings["feedback_enabled"]:
        return "Feedback is currently disabled by admin."
    return render_template("feedback.html")

# SUBMIT FEEDBACK
@app.route("/submit_feedback", methods=["POST"])
def submit_feedback():
    if "user" not in session:
        return redirect("/login")
    website_feedback = request.form["website_feedback"]
    ai_feedback = request.form["ai_feedback"]
    rating = int(request.form["rating"])
    feedbacks.insert_one({
        "email": session["user"],
        "website_feedback": website_feedback,
        "ai_feedback": ai_feedback,
        "rating": rating,
        "date": datetime.now()
    })
    return redirect("/dashboard")

# CHAT PAGE
@app.route("/ai_chat/chat")
def chat():

    if "user" not in session:
        return redirect("/login")

    # Start a new chat session tracker
    session["current_chat"] = []

    user_data = users.find_one({"email": session["user"]})
    selected_person = session.get("chat_person")
    avatar_map = {
        "Mother": "avatars/mother.glb",
        "Father": "avatars/father.glb",
        "Brother": "avatars/brother.glb",
        "Sister": "avatars/sister.glb",
        "Friend": "avatars/friend.glb",
        "Teacher": "avatars/teacher.glb",
        "Boyfriend": "avatars/boyfriend.glb",
        "Stranger": "avatars/stranger.glb"
    }
    avatar_model = avatar_map.get(selected_person)
    background_map = {
    "Mother": "background/living_room.jpg",
    "Father": "background/living_room.jpg",
    "Brother": "background/living_room.jpg",
    "Sister": "background/living_room.jpg",
    "Friend": "background/park.jpg",
    "Teacher": "background/classroom.jpg",
    "Boyfriend": "background/park.jpg",
    "Stranger": "background/park.jpg"
    }
    background_image = background_map.get(selected_person)
    history = user_data.get("chat_history", [])

    # 🟢 FIRST TIME → Warm Welcome
    if not history:
        if selected_person:
            welcome_message = (
                f"You need a person to listen you and {selected_person} is here for you💬\n\n"
                "Speak freely.\n"
                "Say what you feel.\n\n"
                "What would you like to tell them?"
            )
        else:
            welcome_message = (
                "Hey… I’m really glad you’re here 💚\n\n"
                "This is your safe space.\n"
                "You can talk to me about anything.\n\n"
                "How are you feeling today?"
            )

        users.update_one(
            {"email": session["user"]},
            {"$push":{
                "chat_history":{
                    "role":"bot",
                    "text": welcome_message,
                    "timestamp": datetime.now()
                }
            }}
        )

        history = [{
            "role": "bot",
            "text": welcome_message
        }]

    # 🟢 NOT FIRST TIME → AI STARTS CONVERSATION
    else:

        # Get last 3 messages for context
        last_messages = history[-3:]
        context_text = "\n".join([msg["text"] for msg in last_messages])

        try:
            continuation_prompt = f"""
            You are a warm AI companion.
            Based on this previous conversation:
            {context_text}

            Start the conversation naturally again.
            Keep it short (2-4 lines).
            Be friendly and caring.
            Do not repeat old sentences.
            """
            completion = ai_client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {"role": "system", "content": continuation_prompt}
                ]
            )

            starter_message = completion.choices[0].message.content.strip()

            # Save continuation message
            users.update_one(
                {"email": session["user"]},
                {"$push":{
                    "chat_history":{
                        "role":"bot",
                        "text": starter_message
                    }
                }}
            )

            history.append({
                "role": "bot",
                "text": starter_message
            })

        except Exception as e:
            print("Starter message error:", e)

    return render_template(
        "ai_chat/chat.html",
        avatar_model=avatar_model,
        background_image=background_image,
        history=history,
        datetime=datetime
    )

#CHATBOT PAGE
@app.route("/chatbot", methods=["POST"])
def chatbot():

    user_message = request.json["message"]

    # Save to session chat tracker
    if "current_chat" not in session:
        session["current_chat"] = []

    session["current_chat"].append(f"User: {user_message}")
    session.modified = True

    # 🟢 SAVE USER MESSAGE
    users.update_one(
        {"email": session["user"]},
        {"$push":{
            "chat_history":{
                "role":"user",
                "text": user_message,
                "timestamp": datetime.now()
            }
        }}
    )

    try:
        # Detect mood
        mood = analyze_sentiment(user_message)
        system_prompt = f"""
            You are a warm, emotionally intelligent AI companion similar to Replika.

            User mood: {mood}

            Rules:
            - Keep replies short (maximum 4-5 lines).
            - Be friendly and natural.
            - Match the user’s mood.
            - If negative: be comforting and supportive.
            - If positive: be excited and encouraging.
            - If neutral: be calm and conversational.
            - Do not give long explanations.
            - Sound human, caring, and soft.
            """

        completion = ai_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ]
        )

        reply = completion.choices[0].message.content.strip()
        session["current_chat"].append(f"Bot: {reply}")
        session.modified = True

        # 🟢 SAVE BOT MESSAGE
        users.update_one(
            {"email": session["user"]},
            {"$push":{
                "chat_history":{
                    "role":"bot",
                    "text": reply,
                    "timestamp": datetime.now()
                }
            }}
        )

    except Exception as e:
        print("AI Error:", e)
        reply = "Sorry, I'm having trouble responding."

    return jsonify({
        "reply": reply,
        "voice_text": reply
    })

#STOP CHAT
@app.route("/stop_chat", methods=["POST"])
def stop_chat():

    if "user" not in session:
        return jsonify({"status": "error"})

    chat_session = session.get("current_chat", [])

    if not chat_session:
        return jsonify({"status": "empty"})

    # Generate summary
    result = summarize_chat(chat_session)

    # Save diary entry
    users.update_one(
        {"email": session["user"]},
        {"$push":{
            "mood_journal":{
                "summary": result["summary"],
                "mood": result["mood"],
                "date": datetime.now().strftime("%d-%m-%Y %H:%M")
            }
        }}
    )

    # Clear session chat
    session["current_chat"] = []

    return jsonify({"status": "saved"})


#ACTIVITIES PAGE
@app.route("/activities")
def activities():
    if "user" not in session:
        return redirect("/login")
    return render_template("activities.html")

#EXERCISE PAGE
@app.route("/activity/exercise")
def exercise():

    if "user" not in session:
        return redirect("/login")

    return render_template("activity/exercise.html")

#EXERCISE COMPLETION TRACK
from datetime import datetime

@app.route("/exercise_complete", methods=["POST"])
def exercise_complete():

    if "user" not in session:
        return jsonify({"status":"error"})

    users.update_one(
        {"email": session["user"]},
        {
            "$push":{
                "exercise_history":{
                    "date": datetime.now(),
                    "type":"breathing"
                }
            }
        }
    )

    return jsonify({"status":"saved"})

#GUIDED RELAXATION
@app.route("/activity/guided_relaxation")
def guided_relaxation():

    if "user" not in session:
        return redirect("/login")

    return render_template("activity/guided_relaxation.html")

#RELAXATION COMPLETION
@app.route("/relaxation_complete", methods=["POST"])
def relaxation_complete():

    if "user" not in session:
        return jsonify({"status":"error"})

    users.update_one(
        {"email": session["user"]},
        {"$push":{"relaxation_history":{"type":"guided"}}}
    )

    return jsonify({"status":"saved"})

#MOOD JOURNAL
@app.route("/activity/mood_journal")
def mood_journal():

    if "user" not in session:
        return redirect("/login")

    user_data = users.find_one({"email": session["user"]})

    entries = user_data.get("mood_journal", [])

    return render_template(
        "activity/mood_journal.html",
        entries=entries[::-1]
    )

@app.route("/save_mood", methods=["POST"])
def save_mood():

    if "user" not in session:
        return jsonify({"status":"error"})

    data = request.json

    users.update_one(
        {"email": session["user"]},
        {
            "$push":{
                "mood_journal":{
                    "mood": data["mood"],
                    "note": data["note"],
                    "date": datetime.now().strftime("%d-%m-%Y %H:%M")
                }
            }
        }
    )

    return jsonify({"status":"saved"})

@app.route("/delete_journal", methods=["POST"])
def delete_journal():

    if "user" not in session:
        return jsonify({"status": "error"})

    index = int(request.json.get("index"))

    user = users.find_one({"email": session["user"]})
    journal = user.get("mood_journal", [])

    if 0 <= index < len(journal):
        journal.pop(len(journal) - 1 - index)  # reverse display fix

        users.update_one(
            {"email": session["user"]},
            {"$set": {"mood_journal": journal}}
        )

    return jsonify({"status": "deleted"})

# AVATAR PAGE
@app.route("/activity/avatar")
def avatar():
    if "user" not in session:
        return redirect("/login")
    return render_template("activity/avatar.html")

#AVATAR SAVE
@app.route("/save_avatar", methods=["POST"])
def save_avatar():

    if "user" not in session:
        return redirect("/login")

    users.update_one(
        {"email": session["user"]},
        {"$set":{
            "avatar_type": request.form["avatar_type"],
            "avatar_name": request.form["avatar_name"],
            "avatar_personality": request.form["avatar_personality"]
        }}
    )

    return redirect("/profile")

#MEMORY GAME
@app.route("/activity/memory_game")
def memory_game():
    return render_template("activity/memory_game.html")

#WORD SCRAMBLE
@app.route("/activity/word_scramble")
def word_scramble():
    return render_template("activity/word_scramble.html")

# LOGOUT
@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect("/")

#ADMIN LOGIN
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():

    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]

        if email == ADMIN_EMAIL and password == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect("/admin/dashboard")

        return render_template(
            "admin/admin_login.html",
            error="Invalid admin credentials"
        )

    return render_template("admin/admin_login.html")

#ADMIN DASHBOARD
@app.route("/admin/dashboard")
def admin_dashboard():

    if not session.get("admin"):
        return redirect("/admin/login")

    user_count = users.count_documents({})
    journal_count = users.count_documents({"mood_journal": {"$exists": True}})

    return render_template(
        "admin/admin_dashboard.html",
        user_count=user_count,
        journal_count=journal_count
    )

#ADMIN VIEW ALL USERS
@app.route("/admin/users")
def admin_users():

    if not session.get("admin"):
        return redirect("/admin/login")
    
    search = request.args.get("search", "").strip()
    status = request.args.get("status", "all")

    query = {}

    # 🔍 Search by username or email
    if search:
        query["$or"] = [
            {"email": {"$regex": search, "$options": "i"}},
            {"username": {"$regex": search, "$options": "i"}}
        ]

    # 🟢 Active / 🔴 Deactivated filter
    if status == "active":
        query["is_active"] = True
    elif status == "inactive":
        query["is_active"] = False

    all_users = users.find({}, {"password": 0})

    return render_template(
        "admin/admin_users.html",
        users=all_users,
        search=search,
        status=status
    )

#USER PROFILE VIEW
@app.route("/admin/user/<user_id>")
def admin_user_profile(user_id):

    if not session.get("admin"):
        return redirect("/admin/login")

    user = users.find_one({"_id": ObjectId(user_id)})

    return render_template(
        "admin/admin_user_profile.html",
        user=user
    )

#ADMIN ACTIVATE/DEACTIVATE USER
@app.route("/admin/user/<user_id>/toggle", methods=["POST"])
def toggle_user(user_id):

    if not session.get("admin"):
        return redirect("/admin/login")

    user = users.find_one({"_id": ObjectId(user_id)})
    new_status = not user.get("is_active", True)

    users.update_one(
        {"_id": ObjectId(user_id)},
        {"$set": {"is_active": new_status}}
    )

    return redirect(f"/admin/user/{user_id}")

#ADMIN VIEW MOOD JOURNALS
@app.route("/admin/journals")
def admin_journals():

    if not session.get("admin"):
        return redirect("/admin/login")

    search = request.args.get("search", "").strip()
    date = request.args.get("date", "")

    user_query = {"mood_journal": {"$exists": True, "$ne": []}}

    # 🔍 Search by user email
    if search:
        user_query["email"] = {"$regex": search, "$options": "i"}

    all_users = users.find(
        {"mood_journal": {"$exists": True}},
        {"email": 1, "mood_journal": 1}
    )

    return render_template(
        "admin/admin_journals.html",
        users=all_users,
        search=search,
        date=date
    )

#ANALYTICS ROUTE
from collections import Counter
from datetime import datetime

@app.route("/admin/analytics")
def admin_analytics():

    if not session.get("admin"):
        return redirect("/admin/login")

    all_users = list(users.find({}))

    total_users = len(all_users)
    active_users = users.count_documents({"is_active": True})
    inactive_users = users.count_documents({"is_active": False})

    journal_dates = []

    for user in all_users:
        mood_journal = user.get("mood_journal")

        if not mood_journal or not isinstance(mood_journal, list):
            continue

        for entry in mood_journal:
            if not isinstance(entry, dict):
                continue

            date_str = entry.get("date")
            if not date_str or not isinstance(date_str, str):
                continue

            # Expected: DD-MM-YYYY HH:MM
            try:
                date_only = date_str.split(" ")[0]
                date_obj = datetime.strptime(date_only, "%d-%m-%Y")
                journal_dates.append(date_obj)
            except Exception:
                continue

    journal_count = len(journal_dates)

    if journal_count == 0:
        journal_labels = []
        journal_values = []
    else:
        stats = Counter(journal_dates)
        sorted_stats = sorted(stats.items(), key=lambda x: x[0])

        journal_labels = [d.strftime("%d-%m-%Y") for d, _ in sorted_stats]
        journal_values = [count for _, count in sorted_stats]

    return render_template(
        "admin/admin_analytics.html",
        total_users=total_users,
        active_users=active_users,
        inactive_users=inactive_users,
        journal_count=journal_count,
        journal_labels=journal_labels,
        journal_values=journal_values
    )

# ADMIN VIEW FEEDBACK
@app.route("/admin/feedbacks")
def admin_feedbacks():

    if not session.get("admin"):
        return redirect("/admin/login")

    all_feedbacks = feedbacks.find().sort("date", -1)

    return render_template(
        "admin/admin_feedbacks.html",
        feedbacks=all_feedbacks
    )

# SIMPLE SYSTEM SETTINGS
@app.route("/admin/settings", methods=["GET", "POST"])
def admin_settings():

    if not session.get("admin"):
        return redirect("/admin/login")

    settings = settings_collection.find_one()

    if request.method == "POST":

        updated = {
            "registration_enabled": request.form.get("registration_enabled") == "on",
            "feedback_enabled": request.form.get("feedback_enabled") == "on",
            "maintenance_mode": request.form.get("maintenance_mode") == "on"
        }

        settings_collection.update_one({}, {"$set": updated})

        return redirect("/admin/settings")

    return render_template("admin/admin_settings.html", settings=settings)

#ADMIN LOGOUT
@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect("/admin/login")

# RUN
if __name__ == "__main__":
    app.run(debug=True)

