## BoardingPass

AI-powered onboarding agent for HR, managers, and new hires.

### Setup

```bash
pip install -r requirements.txt
```

Create a `.env` file:

```
ANTHROPIC_API_KEY=your_key_here
ADMIN_PASSWORD=admin123
```

### Run

```bash
uvicorn main:app --reload --port 8000
```

Open http://localhost:8000

### Demo flow

1. HR Admin tab → password: `admin123`
2. Upload the sample docs from `sample_docs/` (set roles/departments as needed)
3. Create a new hire: "Alex Chen", Software Engineer, Engineering, Junior
4. Wait for pathway generation → click Approve Pathway
5. Copy the hire_id
6. Switch to New Hire Portal → enter the hire_id
7. Read Day 1 content → click Mark Complete
8. Ask: "How do I get my laptop set up?" → see answer with source
9. Ask: "What is the executive compensation structure?" → see FLAGGED response
10. Switch to HR Admin → Dashboard → see Alex's progress and flagged question
11. Switch to Manager View → enter the manager_id used → see Slack nudge preview
