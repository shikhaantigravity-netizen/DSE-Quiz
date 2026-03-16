import streamlit as st
import os
import json
import uuid
from pathlib import Path
from dotenv import load_dotenv
from groq import Groq
from pydantic import BaseModel
import ast
import gspread
from google.oauth2.service_account import Credentials

# Try loading .env file if it exists
load_dotenv()

# --- Pydantic Schemas for Structured Outputs ---
class QuestionModel(BaseModel):
    question_text: str
    code_snippet: str | None = None
    options: list[str]
    correct_answer: str
    explanation: str

class QuizModel(BaseModel):
    questions: list[QuestionModel]

# --- Google Sheets Configuration ---
# You can use the name OR the ID (from the URL)
SPREADSHEET_NAME = "DSE Results"
SPREADSHEET_ID = "19jrchXDdR-6RUevaAt1Vvwy8_3bT6WIb42UT5RkywrU" # Paste your sheet ID here (the long string in the browser URL)
CREDENTIALS_FILE = "google_credentials.json"

def get_gspread_client():
    """Authenticates and returns a gspread client."""
    scope = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive']
    
    try:
        # 1. Fallback to local file first if it exists (for local development)
        local_creds = Path(CREDENTIALS_FILE)
        if local_creds.exists():
            with open(local_creds, 'r') as f:
                creds_info = json.load(f)
                st.session_state.service_account_email = creds_info.get("client_email")
            creds = Credentials.from_service_account_file(str(local_creds.absolute()), scopes=scope)
        
        # 2. Then check for service account JSON in Streamlit secrets (for deployment)
        elif "GOOGLE_CREDENTIALS" in st.secrets:
            creds_dict = json.loads(st.secrets["GOOGLE_CREDENTIALS"])
            st.session_state.service_account_email = creds_dict.get("client_email")
            creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
            
        else:
            return None
            
        return gspread.authorize(creds)
    except Exception as e:
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
    except Exception as e:
        st.error(f"Failed to submit results to Google Sheet: {e}")
        return False


def sync_to_github(quiz_title):
    """Automatically adds, commits, and pushes the new quiz to GitHub."""
    import subprocess
    try:
        # Check if we are in a git repo
        if not Path(".git").exists():
            return False, "Not a git repository. Please initialize git first."
            
        # 1. Add changes
        subprocess.run(["git", "add", "."], check=True)
        
        # 2. Commit
        commit_msg = f"Auto-sync: Added {quiz_title}"
        subprocess.run(["git", "commit", "-m", commit_msg], check=True)
        
        # 3. Push
        subprocess.run(["git", "push"], check=True)
        
        return True, "Successfully synced with GitHub!"
    except subprocess.CalledProcessError as e:
        return False, f"Git command failed. Ensure you are logged in and have push permissions. Error: {e}"
    except Exception as e:
        return False, f"Sync error: {e}"

def generate_quiz(topic: str, api_key: str):
    """Generates a 10-question quiz using Groq (Llama 3.1)."""
    try:
        # Initialize the Groq client
        client = Groq(api_key=api_key)
    except Exception as e:
        st.error(f"Failed to initialize Groq Client. Check API Key. Error: {e}")
        return None

    prompt = f"""
    Create a 10-question multiple-choice quiz about: Python Data Analysis and Visualization, specifically focusing on the topic: '{topic}'.
    
    Requirements:
    - Exactly 10 questions.
    - Questions should be a mix of: Code-based, Output-based, and Error-based.
    - Each question must have exactly 4 options.
    - One correct answer clearly identified.
    - A brief explanation of why the correct answer is right and others are wrong.
    - If a question is code or output-based, put the code in the 'code_snippet' field (otherwise leave it null).
    - IMPORTANT: If a question or an option contains a Pandas DataFrame output or tabular data, you MUST wrap it in Markdown code blocks (e.g., ```text\\nDataFrame content\\n```) to preserve the two-dimensional row-by-row structure. Do NOT flatten tabular data into one line.
    
    You MUST return the output in a JSON format that strictly matches this schema:
    {{
      "questions": [
        {{
          "question_text": "string",
          "code_snippet": "string or null",
          "options": ["string", "string", "string", "string"],
          "correct_answer": "string (must match one of the options exactly)",
          "explanation": "string"
        }}
      ]
    }}
    """

    try:
        completion = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "You are a specialized quiz generator for Python Data Analysis. You always respond with valid JSON."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"}
        )
        
        content = completion.choices[0].message.content
        if content:
             quiz_data = QuizModel.model_validate_json(content)
             return quiz_data.questions
        else:
             st.error("Empty response received from Groq.")
             return None

    except Exception as e:
        st.error(f"Error generating quiz from Groq: {e}")
        return None

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

if quiz_id_param and not st.session_state.get("quiz_loaded"):
    quiz_file = QUIZZES_DIR / f"{quiz_id_param}.json"
    if quiz_file.exists():
        with open(quiz_file, "r", encoding="utf-8") as f:
            quiz_data = json.load(f)
            st.session_state.quiz_questions = [QuestionModel(**q) for q in quiz_data.get("questions", [])]
            st.session_state.quiz_access_code = quiz_data.get("access_code")
            st.session_state.quiz_title = quiz_data.get("title", "Python Quiz")
            st.session_state.quiz_id = quiz_id_param
            st.session_state.quiz_loaded = True
            st.session_state.authentication_passed = False
            st.session_state.submitted = False
            st.session_state.result_submitted = False
    else:
        st.error("Quiz not found. Please check the link provided by your teacher.")

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
                answer = st.radio("Select answer:", options=q.options, key=f"q_{idx}", label_visibility="collapsed")
                user_answers.append(answer)
                st.write("---")
            
            submit_button = st.form_submit_button("Submit Quiz")
            if submit_button:
                st.session_state.submitted = True

# Grading & Result Submission
if st.session_state.submitted and st.session_state.quiz_questions:
    st.header("Results")
    score = 0
    for idx, q in enumerate(st.session_state.quiz_questions):
        user_ans = st.session_state[f"q_{idx}"]
        if user_ans == q.correct_answer:
            score += 1
    
    st.metric("Your Score", f"{score} / {len(st.session_state.quiz_questions)}")
    
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
