import streamlit as st
import os
import json
import uuid
from pathlib import Path
from dotenv import load_dotenv
from pydantic import BaseModel
import gspread
from google.oauth2.service_account import Credentials

# Try loading .env file if it exists
load_dotenv()

# --- Pydantic Schemas for Structured Outputs ---
class QuestionModel(BaseModel):
    question_text: str
    code_snippet: str | None = None
    options: list[str]
    correct_answer: str | None = None  # Plain text answer (Teacher Only)
    explanation: str | None = None     # Explanation (Teacher Only)
    answer_hash: str | None = None     # SHA256 Hash of correct answer (Public)

class QuizModel(BaseModel):
    questions: list[QuestionModel]

# --- Google Sheets Configuration ---
# Set these in your online Streamlit Cloud "Secrets" panel!
SPREADSHEET_NAME = st.secrets.get("SPREADSHEET_NAME", os.getenv("SPREADSHEET_NAME", "DSE Results"))
SPREADSHEET_ID_RAW = st.secrets.get("SPREADSHEET_ID", os.getenv("SPREADSHEET_ID", ""))
CREDENTIALS_FILE = "google_credentials.json"

def get_spreadsheet_id():
    """Extracts the Spreadsheet ID from a URL or returns the ID as is."""
    if not SPREADSHEET_ID_RAW:
        return ""
    if "/d/" in SPREADSHEET_ID_RAW:
        # Extract from URL: https://docs.google.com/spreadsheets/d/ID_HERE/edit
        return SPREADSHEET_ID_RAW.split("/d/")[1].split("/")[0]
    return SPREADSHEET_ID_RAW.strip()

SPREADSHEET_ID = get_spreadsheet_id()

def get_gspread_client():
    """Authenticates and returns a gspread client with verbose error reporting."""
    scope = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
    
    try:
        # 1. Fallback to local file first if it exists (for local development)
        local_creds = Path(CREDENTIALS_FILE)
        if local_creds.exists():
            with open(local_creds, 'r') as f:
                creds_info = json.load(f)
                st.session_state.service_account_email = creds_info.get("client_email")
            creds = Credentials.from_service_account_file(str(local_creds.absolute()), scopes=scope)
            return gspread.authorize(creds)
        
        # 2. Then check for service account JSON in Streamlit secrets (for deployment)
        elif "GOOGLE_CREDENTIALS" in st.secrets:
            creds_json = st.secrets["GOOGLE_CREDENTIALS"]
            
            # If it's already a dict (Streamlit parsed it automatically), use it
            if isinstance(creds_json, dict):
                creds_dict = dict(creds_json) # Create a copy to avoid mutating the original
            elif isinstance(creds_json, str):
                try:
                    # Robust cleaning and loading
                    import re
                    clean_json = re.sub(r',\s*([\]}])', r'\1', creds_json) # Trailing commas
                    creds_dict = json.loads(clean_json)
                except json.JSONDecodeError as e:
                    st.error(f"❌ **Secrets JSON Error**: {e}")
                    st.info("Ensure your `GOOGLE_CREDENTIALS` in Streamlit is a valid JSON. Check for trailing commas or missing brackets.")
                    return None
            else:
                st.error(f"❌ **Unexpected type for GOOGLE_CREDENTIALS**: {type(creds_json)}")
                return None
            
            # --- FIX: Handle malformed private key (Super Clean) ---
            if "private_key" in creds_dict:
                pk = str(creds_dict["private_key"]) # Ensure it's a string
                # 1. Fix escaped newlines
                pk = pk.replace("\\n", "\n")
                # 2. Trim whitespace and stray quotes
                pk = pk.strip().strip('"').strip("'")
                # 3. Fix common accidental underscore issues (e.g., BEGIN_PRIVATE_KEY)
                pk = pk.replace("BEGIN_PRIVATE_KEY", "BEGIN PRIVATE KEY")
                pk = pk.replace("END_PRIVATE_KEY", "END PRIVATE KEY")
                # 4. Ensure it starts with the correct header
                if not pk.startswith("-----BEGIN"):
                    pk = "-----BEGIN PRIVATE KEY-----\n" + pk
                if not pk.endswith("-----"):
                    pk = pk + "\n-----END PRIVATE KEY-----"
                
                creds_dict["private_key"] = pk
                
            st.session_state.service_account_email = creds_dict.get("client_email")
            creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
            return gspread.authorize(creds)
            
        else:
            st.warning("⚠️ **Google Sheets Credentials Missing!**")
            st.info("Please add `GOOGLE_CREDENTIALS` to your Streamlit Cloud Secrets (for online use) or provide `google_credentials.json` (for local use).")
            return None
            
    except Exception as e:
        st.error(f"❌ **Google Auth Error**: {e}")
        return None

def init_quiz_worksheet(quiz_id, title):
    """Creates a new worksheet for a specific quiz if it doesn't exist."""
    client = get_gspread_client()
    if not client:
        st.error("Could not obtain Google Sheets client. Check your credentials file.")
        return False
        
    try:
        spreadsheet = client.open_by_key(SPREADSHEET_ID) if SPREADSHEET_ID else client.open(SPREADSHEET_NAME)
        
        # Clean title for Google Sheets (max 31 chars, no special chars)
        import re
        sheet_name = re.sub(r'[^\w\s-]', '', title)[:31].strip()
        if not sheet_name:
            sheet_name = f"Quiz_{quiz_id[:8]}"
            
        try:
            worksheet = spreadsheet.worksheet(sheet_name)
        except gspread.exceptions.WorksheetNotFound:
            # Create new worksheet with headers
            worksheet = spreadsheet.add_worksheet(title=sheet_name, rows="100", cols="5")
            worksheet.append_row(["Timestamp", "Student Name", "Roll No", "Score", "Total Questions"])
            try:
                worksheet.format('A1:E1', {'textFormat': {'bold': True}})
            except:
                pass 
        return True
    except gspread.exceptions.SpreadsheetNotFound:
        email = st.session_state.get("service_account_email", "your service account email")
        st.error(f"❌ **Spreadsheet Not Found!**")
        st.info(f"Please ensure you have a Google Sheet named exactly **'{SPREADSHEET_NAME}'** and that you have shared it with this email: \n\n `{email}`")
        return False
    except Exception as e:
        st.error(f"Detailed Error: {e}")
        return False

def submit_result_to_gsheet(quiz_id, name, roll, score, total, sheet_title):
    """Appends a student result to the corresponding quiz worksheet."""
    client = get_gspread_client()
    if not client:
        return False
        
    try:
        if not SPREADSHEET_ID and not SPREADSHEET_NAME:
            st.error("❌ **Configuration Error**: Both SPREADSHEET_ID and SPREADSHEET_NAME are empty. Please check your Secrets.")
            return False
            
        spreadsheet = client.open_by_key(SPREADSHEET_ID) if SPREADSHEET_ID else client.open(SPREADSHEET_NAME)
        
        # Use simple cleaned name
        import re
        sheet_name = re.sub(r'[^\w\s-]', '', sheet_title)[:31].strip() if sheet_title else f"Quiz_{quiz_id[:8]}"
        
        try:
            worksheet = spreadsheet.worksheet(sheet_name)
        except gspread.exceptions.WorksheetNotFound:
            # Fallback to ID if title-based sheet missing
            sheet_name = f"Quiz_{quiz_id[:8]}"
            worksheet = spreadsheet.worksheet(sheet_name)
        
        import datetime
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        worksheet.append_row([timestamp, name, roll, f"{score} / {total}", total])
        return True
    except gspread.exceptions.SpreadsheetNotFound:
        email = st.session_state.get("service_account_email", "your service account email")
        st.error(f"❌ **Spreadsheet Not Found!**")
        st.info(f"The app could not find a sheet named '{SPREADSHEET_NAME}' or with ID '{SPREADSHEET_ID}'.\n\n**Action Required:** Ensure the Google Sheet is shared with this email:\n\n`{email}`")
        return False
    except Exception as e:
        st.error(f"❌ **Failed to submit results**: {e}")
        return False



# --- Student UI ---
st.set_page_config(page_title="Student Quiz Portal", page_icon="📝", layout="wide")

st.title("📝 Student Quiz Portal")
st.write("Enter your quiz link and access code to begin.")

# Check if a quiz_id is in the URL
query_params = st.query_params
quiz_id_param = query_params.get("quiz_id")

# Create quizzes directory 
QUIZZES_DIR = Path("quizzes")
QUIZZES_DIR.mkdir(exist_ok=True)

# State management
if "quiz_questions" not in st.session_state:
    st.session_state.quiz_questions = None
if "submitted" not in st.session_state:
    st.session_state.submitted = False
if "result_submitted" not in st.session_state:
    st.session_state.result_submitted = False

# --- GitHub Configuration ---
GITHUB_USER_REPO = os.getenv("GITHUB_REPO", "") # Set this in Streamlit Secrets

def load_quiz_file(quiz_id):
    """Loads quiz from local file or falls back to GitHub URL."""
    quiz_file = QUIZZES_DIR / f"{quiz_id}.json"
    
    # 1. Try local file (works if pushed to repo)
    if quiz_file.exists():
        with open(quiz_file, "r", encoding="utf-8") as f:
            return json.load(f)
            
    # 2. Try GitHub Raw URL (works immediately after upload)
    try:
        import httpx
        # Use the raw link to fetch the JSON
        github_url = f"https://raw.githubusercontent.com/{GITHUB_USER_REPO}/main/quizzes/{quiz_id}.json"
        response = httpx.get(github_url)
        if response.status_code == 200:
            return response.json()
    except Exception as e:
        st.error(f"Failed to fetch quiz from GitHub: {e}")
        
    return None

if quiz_id_param and not st.session_state.get("quiz_loaded"):
    quiz_data = load_quiz_file(quiz_id_param)
    if quiz_data:
        st.session_state.quiz_questions = [QuestionModel(**q) for q in quiz_data.get("questions", [])]
        st.session_state.quiz_access_code = quiz_data.get("access_code")
        st.session_state.quiz_title = quiz_data.get("title", "Python Quiz")
        st.session_state.quiz_id = quiz_id_param
        st.session_state.quiz_loaded = True
        st.session_state.authentication_passed = False
        st.session_state.submitted = False
        st.session_state.result_submitted = False
    else:
        st.error("🔍 **Quiz Not Found!**")
        st.info("The quiz might still be uploading to GitHub. Please wait 10 seconds and refresh this page.")

# Main Portal Content
if not st.session_state.get("quiz_loaded", False):
    st.info("👋 Welcome! Please use the specialized link provided by your teacher to start your quiz.")
    st.image("https://images.unsplash.com/photo-1434030216411-0b793f4b4173?ixlib=rb-1.2.1&auto=format&fit=crop&w=1350&q=80", caption="Ready to test your Python skills?")

# Display the quiz if loaded
if st.session_state.quiz_questions:
    
    # Access Code Authentication
    if not st.session_state.get("authentication_passed"):
        st.subheader(f"Quiz: {st.session_state.quiz_title}")
        with st.form("access_form"):
            student_name = st.text_input("Full Name")
            student_roll = st.text_input("Roll No")
            entered_code = st.text_input("Access Code", type="password")
            auth_submit = st.form_submit_button("Start Quiz")
            
            if auth_submit:
                if not student_name or not student_roll:
                    st.error("Please enter your Name and Roll No.")
                elif entered_code.strip().upper() == st.session_state.quiz_access_code:
                    st.session_state.authentication_passed = True
                    st.session_state.student_name = student_name
                    st.session_state.student_roll = student_roll
                    st.rerun()
                else:
                    st.error("Invalid access code.")
                    
    else:
        # Quiz Form
        st.write(f"📝 **Attempting:** {st.session_state.quiz_title}")
        st.write(f"👤 **Student:** {st.session_state.student_name} ({st.session_state.student_roll})")
        
        with st.form("quiz_form"):
            user_answers = []
            for idx, q in enumerate(st.session_state.quiz_questions):
                st.markdown(f"**Q{idx + 1}: {q.question_text}**")
                if q.code_snippet:
                    st.code(q.code_snippet, language="python")
                # Removed default selection by adding index=None
                # Removed default selection
                answer = st.radio("Select answer:", options=q.options, key=f"q_{idx}", index=None, label_visibility="collapsed")
                user_answers.append(answer)
                st.write("---")
            
            submit_button = st.form_submit_button("Submit Quiz")
            if submit_button:
                # Check if all questions are answered
                unanswered = [i + 1 for i, ans in enumerate(user_answers) if ans is None]
                if unanswered:
                    st.error(f"⚠️ **Please answer all questions!** Missing: {', '.join(map(str, unanswered))}")
                else:
                    st.session_state.submitted = True
                    st.rerun() # Refresh to show results

# Grading & Result Submission
if st.session_state.submitted and st.session_state.quiz_questions:
    st.header("Results")
    score = 0
    import hashlib
    def get_hash(text):
        return hashlib.sha256(text.encode()).hexdigest()

    for idx, q in enumerate(st.session_state.quiz_questions):
        user_ans = st.session_state[f"q_{idx}"]
        
        # Discover correct answer text from hash (Privacy Mode)
        correct_ans_text = q.correct_answer
        if not correct_ans_text and q.answer_hash:
            for opt in q.options:
                if get_hash(opt) == q.answer_hash:
                    correct_ans_text = opt
                    break

        is_correct = (get_hash(user_ans) == q.answer_hash) if q.answer_hash else (user_ans == q.correct_answer)
            
        if is_correct:
            score += 1
            st.success(f"✅ **Question {idx + 1}: Correct!**")
        else:
            st.error(f"❌ **Question {idx + 1}: Incorrect.**")
            if correct_ans_text:
                st.write(f"The correct answer was: **{correct_ans_text}**")
            
        if q.explanation:
            st.info(f"**Explanation:** {q.explanation}")
        st.write("---")
    
    if not st.session_state.get("result_submitted"):
        with st.spinner("Recording score..."):
             success = submit_result_to_gsheet(
                 st.session_state.quiz_id,
                 st.session_state.student_name,
                 st.session_state.student_roll,
                 score,
                 len(st.session_state.quiz_questions),
                 st.session_state.quiz_title
             )
             if success:
                 st.session_state.result_submitted = True
                 st.success("✅ Results submitted to teacher!")
             else:
                 st.error("❌ Submission failed. Please try again or contact your teacher.")
    
    if score == len(st.session_state.quiz_questions):
        st.balloons()
