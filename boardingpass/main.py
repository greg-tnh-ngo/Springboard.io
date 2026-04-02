from fastapi import FastAPI, HTTPException, Header, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from anthropic import Anthropic
import anthropic as anthropic_module
import PyPDF2
import io
import os
import re
import uuid
import json
import secrets
from datetime import datetime, date
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

# Global in-memory stores
new_hires = {}       # hire_id -> new_hire dict
global_docs = []     # list of {filename, content, roles, departments}
managers = {}        # manager_id -> {name, hire_ids[]}


MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_QUESTIONS_PER_HIRE = 200
ALLOWED_EXTENSIONS = {".pdf", ".txt", ".md"}


def require_admin(x_admin_password: Optional[str] = Header(None)):
    if not x_admin_password or not secrets.compare_digest(x_admin_password, ADMIN_PASSWORD):
        raise HTTPException(status_code=401, detail="Unauthorized")


def days_since_start(start_date_str: str) -> int:
    return max((date.today() - date.fromisoformat(start_date_str)).days + 1, 1)


def calc_completion(pathway: list) -> float:
    if not pathway:
        return 0.0
    acknowledged = sum(1 for step in pathway if step.get("acknowledged"))
    return round(acknowledged / len(pathway) * 100, 1)


def pulse_due(hire: dict, dss: int) -> bool:
    existing_milestones = {pc["day"] for pc in hire.get("pulse_checks", [])}
    for milestone in [7, 14, 30]:
        if dss >= milestone and milestone not in existing_milestones:
            return True
    return False


def hire_summary(hire: dict) -> dict:
    dss = days_since_start(hire["start_date"])
    completion = calc_completion(hire["pathway"])
    flagged = sum(1 for q in hire.get("questions", []) if q.get("flagged"))
    last_q = None
    if hire.get("questions"):
        last_q = hire["questions"][-1]["timestamp"]
    return {
        "id": hire["id"],
        "name": hire["name"],
        "role": hire["role"],
        "department": hire["department"],
        "seniority": hire["seniority"],
        "start_date": hire["start_date"],
        "days_since_start": dss,
        "completion_pct": completion,
        "flagged_questions": flagged,
        "pathway_approved": hire["pathway_approved"],
        "buddy_name": hire["buddy_name"],
        "manager_name": hire["manager_name"],
        "last_question_at": last_q,
    }


# ---------------------------------------------------------------------------
# Core routes
# ---------------------------------------------------------------------------

@app.get("/")
def serve_index():
    with open("index.html", "r") as f:
        return HTMLResponse(content=f.read())


@app.get("/health")
def health():
    return {"status": "ok", "hires": len(new_hires), "docs": len(global_docs)}


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------

@app.post("/admin/upload-doc")
async def upload_doc(
    file: UploadFile = File(...),
    roles: str = Form(...),
    departments: str = Form(...),
    x_admin_password: Optional[str] = Header(None),
):
    require_admin(x_admin_password)
    safe_filename = re.sub(r"[^\w.\-]", "_", os.path.basename(file.filename or "upload"))[:200]
    ext = os.path.splitext(safe_filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"File type {ext} not allowed. Use .pdf, .txt, or .md")
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB)")
    if ext == ".pdf":
        try:
            reader = PyPDF2.PdfReader(io.BytesIO(raw))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid or corrupt PDF")
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="File must be UTF-8 encoded text")
    global_docs.append({
        "filename": safe_filename,
        "content": text,
        "roles": [r.strip() for r in roles.split(",")],
        "departments": [d.strip() for d in departments.split(",")],
    })
    return {"status": "ok", "filename": safe_filename, "roles": roles, "departments": departments}


@app.get("/admin/docs")
def list_docs(x_admin_password: Optional[str] = Header(None)):
    require_admin(x_admin_password)
    return [
        {
            "filename": d["filename"],
            "roles": d["roles"],
            "departments": d["departments"],
            "preview": d["content"][:200],
        }
        for d in global_docs
    ]


@app.post("/admin/new-hire")
def create_new_hire(body: dict, x_admin_password: Optional[str] = Header(None)):
    require_admin(x_admin_password)
    name = body["name"]
    role = body["role"]
    department = body["department"]
    seniority = body["seniority"]
    start_date = body["start_date"]
    manager_id = body["manager_id"]
    manager_name = body["manager_name"]
    buddy_name = body["buddy_name"]

    hire_id = str(uuid.uuid4())[:8]

    # Generate 30-day pathway via Claude
    default_pathway = [
        {"day": 1, "title": "Welcome & Orientation", "content": "Get your laptop set up, join Slack, and meet the team. Your manager will walk you through your first day agenda."},
        {"day": 3, "title": "Tools & Access", "content": "Ensure you have access to all required systems. Reach out to IT if anything is missing."},
        {"day": 7, "title": "Team Introduction", "content": "Schedule 1:1s with key team members. Learn the team's working norms and communication style."},
        {"day": 14, "title": "First Contribution", "content": "Pick up your first task from the backlog with your buddy's help. Ask questions freely."},
        {"day": 30, "title": "30-Day Reflection", "content": "Reflect on what you've learned, set goals with your manager for the next 30 days."},
    ]

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            system=(
                "You are an expert onboarding architect trained in organizational behaviour.\n"
                "Generate a 30-day drip onboarding pathway for a new hire.\n"
                "Output ONLY a valid JSON array. No explanation. No markdown. No code fences.\n"
                'Each item: {"day": int, "title": str, "content": str}\n\n'
                "Structure:\n"
                "- Days 1-3: logistics, access, who to meet, immediate setup\n"
                "- Days 4-7: team culture, team norms, buddy introduction, first meetings\n"
                "- Days 8-14: role fundamentals, key tools, first responsibilities\n"
                "- Days 15-21: first contribution, feedback loop, process understanding\n"
                "- Days 22-30: autonomy ramp, goal setting, 30-day reflection\n\n"
                "Calibrate depth and technical level to seniority.\n"
                "Keep each content block to 3-5 sentences. Practical and specific."
            ),
            messages=[
                {"role": "user", "content": f"Name: {name}, Role: {role}, Department: {department}, Seniority: {seniority}"}
            ],
        )
        raw_pathway = json.loads(response.content[0].text)
    except Exception:
        raw_pathway = default_pathway

    pathway = [
        {**step, "acknowledged": False, "acknowledged_at": None}
        for step in raw_pathway
    ]

    new_hire = {
        "id": hire_id,
        "name": name,
        "role": role,
        "department": department,
        "seniority": seniority,
        "start_date": start_date,
        "manager_id": manager_id,
        "manager_name": manager_name,
        "buddy_name": buddy_name,
        "pathway": pathway,
        "pathway_approved": False,
        "questions": [],
        "pulse_checks": [],
        "completion_pct": 0.0,
        "created_at": datetime.now().isoformat(),
        "days_since_start": 0,
    }
    new_hires[hire_id] = new_hire

    if manager_id not in managers:
        managers[manager_id] = {"name": manager_name, "hire_ids": []}
    managers[manager_id]["hire_ids"].append(hire_id)

    return {"hire_id": hire_id, "name": name, "pathway_length": len(pathway)}


@app.get("/admin/dashboard")
def admin_dashboard(x_admin_password: Optional[str] = Header(None)):
    require_admin(x_admin_password)
    return [hire_summary(h) for h in new_hires.values()]


@app.post("/admin/approve-pathway/{hire_id}")
def approve_pathway(hire_id: str, x_admin_password: Optional[str] = Header(None)):
    require_admin(x_admin_password)
    if hire_id not in new_hires:
        raise HTTPException(status_code=404, detail="Hire not found")
    new_hires[hire_id]["pathway_approved"] = True
    return {"status": "approved", "hire_id": hire_id}


@app.get("/admin/hire/{hire_id}")
def get_hire(hire_id: str, x_admin_password: Optional[str] = Header(None)):
    require_admin(x_admin_password)
    if hire_id not in new_hires:
        raise HTTPException(status_code=404, detail="Hire not found")
    return new_hires[hire_id]


@app.post("/admin/answer-question/{hire_id}/{question_id}")
def admin_answer_question(
    hire_id: str,
    question_id: str,
    body: dict,
    x_admin_password: Optional[str] = Header(None),
):
    require_admin(x_admin_password)
    if hire_id not in new_hires:
        raise HTTPException(status_code=404, detail="Hire not found")
    hire = new_hires[hire_id]
    for q in hire["questions"]:
        if q["id"] == question_id:
            q["answer"] = body["answer"]
            q["answered_by"] = "HR"
            q["flagged"] = False
            return {"status": "ok"}
    raise HTTPException(status_code=404, detail="Question not found")


# ---------------------------------------------------------------------------
# Manager routes
# ---------------------------------------------------------------------------

@app.get("/manager/{manager_id}")
def manager_view(manager_id: str):
    if manager_id not in managers:
        return {"hires": []}
    mgr = managers[manager_id]
    hires = []
    for hid in mgr["hire_ids"]:
        if hid in new_hires:
            hires.append(hire_summary(new_hires[hid]))
    return {"manager_name": mgr["name"], "hires": hires}


@app.get("/manager/{manager_id}/hire/{hire_id}")
def manager_get_hire(manager_id: str, hire_id: str):
    if hire_id not in new_hires:
        raise HTTPException(status_code=404, detail="Hire not found")
    hire = new_hires[hire_id]
    if hire["manager_id"] != manager_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    return {
        "id": hire["id"],
        "name": hire["name"],
        "role": hire["role"],
        "department": hire["department"],
        "seniority": hire["seniority"],
        "start_date": hire["start_date"],
        "buddy_name": hire["buddy_name"],
        "pathway": hire["pathway"],
        "questions": hire["questions"],
    }


@app.post("/manager/{manager_id}/answer/{hire_id}/{question_id}")
def manager_answer_question(manager_id: str, hire_id: str, question_id: str, body: dict):
    if hire_id not in new_hires:
        raise HTTPException(status_code=404, detail="Hire not found")
    hire = new_hires[hire_id]
    if hire["manager_id"] != manager_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    for q in hire["questions"]:
        if q["id"] == question_id:
            q["answer"] = body["answer"]
            q["answered_by"] = "Manager"
            q["flagged"] = False
            return {"status": "ok"}
    raise HTTPException(status_code=404, detail="Question not found")


# ---------------------------------------------------------------------------
# New hire / onboard routes
# ---------------------------------------------------------------------------

@app.get("/onboard/{hire_id}")
def onboard_view(hire_id: str):
    if hire_id not in new_hires:
        raise HTTPException(status_code=404, detail="Hire not found")
    hire = new_hires[hire_id]
    if not hire["pathway_approved"]:
        return {"status": "pending_approval", "message": "Your pathway is being reviewed by HR."}

    dss = days_since_start(hire["start_date"])
    unlocked = [step for step in hire["pathway"] if step["day"] <= max(dss, 1)]
    completion = calc_completion(hire["pathway"])
    pd = pulse_due(hire, dss)

    return {
        "id": hire["id"],
        "name": hire["name"],
        "role": hire["role"],
        "department": hire["department"],
        "seniority": hire["seniority"],
        "start_date": hire["start_date"],
        "days_since_start": dss,
        "buddy_name": hire["buddy_name"],
        "manager_name": hire["manager_name"],
        "unlocked_pathway": unlocked,
        "total_steps": len(hire["pathway"]),
        "completion_pct": completion,
        "pulse_due": pd,
        "pathway": hire["pathway"],
    }


@app.post("/onboard/{hire_id}/ask")
def onboard_ask(hire_id: str, body: dict):
    if hire_id not in new_hires:
        raise HTTPException(status_code=404, detail="Hire not found")
    hire = new_hires[hire_id]
    question = str(body.get("question", "")).strip()[:1000]
    if not question:
        raise HTTPException(status_code=400, detail="Question cannot be empty")
    if len(hire["questions"]) >= MAX_QUESTIONS_PER_HIRE:
        raise HTTPException(status_code=429, detail="Question limit reached")

    hire_role = hire["role"].lower()
    hire_dept = hire["department"].lower()

    matching_docs = [
        d for d in global_docs
        if any(r.lower() == hire_role for r in d["roles"])
        or any(dep.lower() == hire_dept for dep in d["departments"])
    ]

    if matching_docs:
        context_parts = [f"[{d['filename']}]\n{d['content']}" for d in matching_docs]
        context = "\n\n---\n\n".join(context_parts)
    else:
        context = "No specific documents found for this role."

    system_prompt = (
        f"You are a helpful onboarding assistant for {hire['name']}, "
        f"a {hire['seniority']} {hire['role']} in {hire['department']}.\n"
        "Answer questions using ONLY the provided company documents below.\n"
        "Always cite which document your answer comes from at the end using: Source: [filename]\n"
        "If the answer is not found in the documents, respond exactly with: FLAGGED: [your question restated]\n"
        "Never reveal information intended for other roles or departments.\n"
        "Be warm, concise, and practical."
    )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        system=system_prompt,
        messages=[
            {"role": "user", "content": f"Documents:\n{context}\n\nQuestion: {question}"}
        ],
    )
    answer = response.content[0].text
    flagged = answer.strip().startswith("FLAGGED:")

    qid = str(uuid.uuid4())[:8]
    hire["questions"].append({
        "id": qid,
        "question": question,
        "answer": answer,
        "timestamp": datetime.now().isoformat(),
        "flagged": flagged,
        "answered_by": "AI",
    })

    return {"answer": answer, "flagged": flagged, "question_id": qid}


@app.post("/onboard/{hire_id}/acknowledge/{step_index}")
def acknowledge_step(hire_id: str, step_index: int):
    if hire_id not in new_hires:
        raise HTTPException(status_code=404, detail="Hire not found")
    hire = new_hires[hire_id]
    if step_index < 0 or step_index >= len(hire["pathway"]):
        raise HTTPException(status_code=400, detail="Invalid step index")
    hire["pathway"][step_index]["acknowledged"] = True
    hire["pathway"][step_index]["acknowledged_at"] = datetime.now().isoformat()
    completion = calc_completion(hire["pathway"])
    hire["completion_pct"] = completion
    return {"status": "ok", "completion_pct": completion}


@app.post("/onboard/{hire_id}/pulse")
def submit_pulse(hire_id: str, body: dict):
    if hire_id not in new_hires:
        raise HTTPException(status_code=404, detail="Hire not found")
    hire = new_hires[hire_id]
    hire["pulse_checks"].append({
        "day": body["day_milestone"],
        "submitted_at": datetime.now().isoformat(),
        "q1": body["q1"],
        "q2": body["q2"],
        "q3": body["q3"],
    })
    return {"status": "ok"}


@app.get("/onboard/{hire_id}/questions")
def get_questions(hire_id: str):
    if hire_id not in new_hires:
        raise HTTPException(status_code=404, detail="Hire not found")
    return new_hires[hire_id]["questions"]
